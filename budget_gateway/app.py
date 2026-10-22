import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from .common import owner
from .engine import Ledger, PRICES, NANO, route, estimate_input
from .provider import Provider

class Message(BaseModel):
    role: Literal['system','user','assistant']
    content: str = Field(min_length=1,max_length=20000)
class Completion(BaseModel):
    model: Literal['auto','small','large'] = 'auto'
    messages: list[Message] = Field(min_length=1,max_length=20)
    max_tokens: int = Field(default=256,ge=1,le=4096)
    temperature: float = Field(default=0,ge=0,le=2)
    priority: Literal['cost','balanced','quality'] = 'balanced'
    cache: bool = True
    max_cost_usd: float = Field(default=.10,gt=0,le=100,allow_inf_nan=False)
    stream: Literal[False] = False


def create_app(path=None,provider=None):
    app=FastAPI(title='Inference Budget Gateway',version='0.1.0')
    ledger=Ledger(path or os.getenv('DB_PATH','gateway.db'))
    upstream=provider or Provider()
    app.state.ledger=ledger

    @app.get('/')
    def home():
        return FileResponse(Path(__file__).parent.parent/'web/index.html')

    @app.get('/health')
    def health():
        return {'status':'ok','mode':'live' if os.getenv('MODEL_BASE_URL') else 'demo','pricing':'illustrative; configure before real billing use'}

    @app.get('/metrics')
    def metrics(user=Depends(owner)):
        return ledger.metrics(user)

    @app.delete('/responses',status_code=204)
    def purge(user=Depends(owner)):
        ledger.purge(user)

    @app.post('/v1/chat/completions')
    async def completion(body:Completion,user=Depends(owner),idempotency_key:str|None=Header(default=None)):
        if idempotency_key and len(idempotency_key)>128:
            raise HTTPException(422,'Idempotency key must be at most 128 characters')
        start=time.perf_counter()
        model,reason=route(body)
        in_rate,out_rate=PRICES[model]
        fingerprint=ledger.fingerprint(body)
        cache_key=fingerprint+':'+model+':'+os.getenv('MODEL_BASE_URL','demo')+':'+os.getenv(model.upper()+'_MODEL',model)
        cache_hit=ledger.cached(user,cache_key) if body.cache and body.temperature==0 else None
        reserved=0 if cache_hit else estimate_input(body.messages)*in_rate+body.max_tokens*out_rate
        if reserved/NANO>body.max_cost_usd:
            raise HTTPException(402,'Estimated request reservation exceeds max_cost_usd')
        ident,replay=ledger.reserve(user,idempotency_key or str(uuid.uuid4()),fingerprint,model,reserved,
                                   int(float(os.getenv('DAILY_BUDGET_USD','1'))*NANO))
        if replay:
            return replay
        if cache_hit:
            text=cache_hit['choices'][0]['message']['content']
            usage={'prompt_tokens':0,'completion_tokens':0}
            mode=cache_hit['gateway']['mode']
        else:
            try:
                text,usage,mode=await upstream.generate(model,body)
            except asyncio.CancelledError:
                ledger.uncertain(ident)
                raise
            except Exception:
                # The upstream may have processed a request even when the response was lost.
                ledger.uncertain(ident)
                raise HTTPException(502,'Provider outcome uncertain; reservation retained and automatic retries disabled')
        charge=usage['prompt_tokens']*in_rate+usage['completion_tokens']*out_rate
        latency=round((time.perf_counter()-start)*1000,3)
        response={'id':ident,'object':'chat.completion','created':int(time.time()),'model':model,
                  'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}],
                  'usage':{**usage,'total_tokens':usage['prompt_tokens']+usage['completion_tokens']},
                  'gateway':{'mode':mode,'route_reason':reason,'cache_hit':bool(cache_hit),'cost_usd':charge/NANO,
                             'reserved_usd':reserved/NANO,'reservation_exceeded':charge>reserved,
                             'latency_ms':latency,'pricing':'illustrative'}}
        ledger.finish(ident,user,response,charge,latency,bool(cache_hit),cache_key if body.cache and body.temperature==0 and not cache_hit else None)
        return response

    return app

app=create_app()
