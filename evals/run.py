"""Local deterministic replay workload. No live GPU or LLM benchmark."""
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.pop('MODEL_BASE_URL',None)
os.environ['API_KEYS']='eval-owner'
os.environ['DAILY_BUDGET_USD']='1'
from fastapi.testclient import TestClient
from budget_gateway.app import create_app

fixtures=[('Say hello','small'),('Summarize this note','small'),('List three colors','small'),
          ('Debug this architecture','large'),('Compare retrieval tradeoffs','large'),('Prove the following theorem','large')]
with tempfile.TemporaryDirectory() as tmp:
    client=TestClient(create_app(str(Path(tmp)/'eval.db')))
    client.headers['Authorization']='Bearer eval-owner'
    rows=[]
    for repeat in range(2):
        for prompt,expected in fixtures:
            response=client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':prompt}]}).json()
            rows.append({'prompt':prompt,'expected_tier':expected,'actual_tier':response['model'],'repeat':bool(repeat),
                         'cache_hit':response['gateway']['cache_hit'],'cost_usd':response['gateway']['cost_usd'],
                         'latency_ms':response['gateway']['latency_ms']})
    report={'mode':'deterministic-demo','scope':'Synthetic heuristic-routing and exact-cache workload; no model quality or GPU speed claims',
            'python':platform.python_version(),'routing_fixture_matches':sum(r['actual_tier']==r['expected_tier'] for r in rows),
            'total_requests':len(rows),'metrics':client.get('/metrics').json(),'requests':rows}
    Path(__file__).with_name('results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    if report['routing_fixture_matches']!=12 or report['metrics']['cache_hits']!=6:raise SystemExit(1)
