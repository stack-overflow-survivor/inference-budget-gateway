from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from budget_gateway.app import create_app
from budget_gateway.engine import Ledger

class FakeProvider:
    def __init__(self): self.calls=0
    async def generate(self,model,body):
        self.calls+=1
        return 'Test response',{'prompt_tokens':10,'completion_tokens':5},'mock'

@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('API_KEYS','alice,bob')
    monkeypatch.setenv('DAILY_BUDGET_USD','1')
    provider=FakeProvider()
    app=create_app(str(tmp_path/'gateway.db'),provider)
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer alice'
        yield client,provider

def payload(**kwargs):
    return {'messages':[{'role':'user','content':'Hello'}],**kwargs}

def test_auth(setup):
    c,_=setup
    assert c.get('/metrics',headers={'Authorization':''}).status_code==401

def test_simple_and_complex_routing(setup):
    c,_=setup
    assert c.post('/v1/chat/completions',json=payload()).json()['model']=='small'
    assert c.post('/v1/chat/completions',json=payload(priority='quality')).json()['model']=='large'
    assert c.post('/v1/chat/completions',json=payload(messages=[{'role':'user','content':'debug this architecture'}])).json()['model']=='large'

def test_exact_cache_and_zero_charge(setup):
    c,p=setup
    c.post('/v1/chat/completions',json=payload())
    result=c.post('/v1/chat/completions',json=payload()).json()
    assert result['gateway']['cache_hit']
    assert result['gateway']['cost_usd']==0
    assert p.calls==1

def test_cache_isolated_by_tenant(setup):
    c,p=setup
    c.post('/v1/chat/completions',json=payload())
    c.post('/v1/chat/completions',json=payload(),headers={'Authorization':'Bearer bob'})
    assert p.calls==2

def test_cache_keys_include_system_prompt_and_parameters(setup):
    c,p=setup
    c.post('/v1/chat/completions',json=payload())
    c.post('/v1/chat/completions',json=payload(max_tokens=16))
    c.post('/v1/chat/completions',json=payload(messages=[{'role':'system','content':'Speak French'},{'role':'user','content':'Hello'}]))
    assert p.calls==3

def test_sampling_not_cached(setup):
    c,p=setup
    for _ in range(2): c.post('/v1/chat/completions',json=payload(temperature=.7))
    assert p.calls==2

def test_cache_expiry(setup):
    from budget_gateway.common import connect
    c,p=setup
    c.post('/v1/chat/completions',json=payload())
    with connect(c.app.state.ledger.path) as db: db.execute('UPDATE cache SET expires=0')
    c.post('/v1/chat/completions',json=payload())
    assert p.calls==2

def test_idempotency_replay(setup):
    c,p=setup
    headers={'Idempotency-Key':'same'}
    first=c.post('/v1/chat/completions',json=payload(cache=False),headers=headers).json()
    second=c.post('/v1/chat/completions',json=payload(cache=False),headers=headers).json()
    assert first==second
    assert p.calls==1
    assert c.get('/metrics').json()['requests']==1

def test_idempotency_conflict(setup):
    c,_=setup
    c.post('/v1/chat/completions',json=payload(),headers={'Idempotency-Key':'same'})
    assert c.post('/v1/chat/completions',json=payload(model='large'),headers={'Idempotency-Key':'same'}).status_code==409

def test_request_cost_rejected_before_provider(setup):
    c,p=setup
    assert c.post('/v1/chat/completions',json=payload(max_cost_usd=.00000001)).status_code==402
    assert p.calls==0

def test_daily_limit(setup,monkeypatch):
    c,p=setup
    monkeypatch.setenv('DAILY_BUDGET_USD','0')
    assert c.post('/v1/chat/completions',json=payload()).status_code==429
    assert p.calls==0

def test_atomic_concurrent_reservations(tmp_path):
    ledger=Ledger(str(tmp_path/'race.db'))
    def reserve(i):
        try: ledger.reserve('tenant',str(i),str(i),'small',60,100); return True
        except Exception: return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve,range(8)))==1

def test_provider_failure_keeps_reservation(tmp_path,monkeypatch):
    monkeypatch.setenv('API_KEYS','alice')
    class Failed:
        async def generate(self,*args): raise TimeoutError()
    with TestClient(create_app(str(tmp_path/'fail.db'),Failed())) as c:
        c.headers['Authorization']='Bearer alice'
        assert c.post('/v1/chat/completions',json=payload(),headers={'Idempotency-Key':'same'}).status_code==502
        assert c.post('/v1/chat/completions',json=payload(),headers={'Idempotency-Key':'same'}).status_code==409
        assert c.get('/metrics').json()['held_reservations_usd']>0

def test_usage_accounting(setup):
    c,_=setup
    result=c.post('/v1/chat/completions',json=payload()).json()
    assert result['usage']['total_tokens']==15
    assert result['gateway']['cost_usd']==.000003
    assert c.get('/metrics').json()['charged_usd']==.000003

def test_purge_preserves_accounting(setup):
    c,_=setup
    c.post('/v1/chat/completions',json=payload())
    before=c.get('/metrics').json()['charged_usd']
    assert c.delete('/responses').status_code==204
    assert c.get('/metrics').json()['charged_usd']==before

def test_invalid_model_and_streaming(setup):
    c,_=setup
    assert c.post('/v1/chat/completions',json=payload(model='unknown')).status_code==422
    assert c.post('/v1/chat/completions',json=payload(stream=True)).status_code==422

def test_metrics_are_tenant_scoped(setup):
    c,_=setup
    c.post('/v1/chat/completions',json=payload())
    assert c.get('/metrics',headers={'Authorization':'Bearer bob'}).json()['requests']==0
