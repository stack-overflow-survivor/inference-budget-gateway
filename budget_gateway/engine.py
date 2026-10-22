import hashlib
import json
import math
import os
import time
import uuid
from fastapi import HTTPException
from .common import connect

# Illustrative configuration, nanodollars per token. Not vendor quotes.
PRICES = {'small': (100, 400), 'large': (1000, 4000)}
NANO = 1_000_000_000


def route(body):
    if body.model != 'auto':
        return body.model, 'Explicit model tier requested'
    text = ' '.join(m.content for m in body.messages).lower()
    if body.priority == 'quality' or len(text) > 2000 or any(word in text for word in ('prove', 'tradeoff', 'debug', 'architecture', 'compare')):
        return 'large', 'Quality priority or complexity heuristic'
    return 'small', 'Short request with no complexity trigger'


def estimate_input(messages):
    # Conservative byte-count allowance, not a provider tokenizer or billing guarantee.
    return sum(len(m.content.encode('utf-8')) + 128 for m in messages) + 128


class Ledger:
    def __init__(self, path):
        self.path = path
        with connect(path) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS requests(
              id TEXT PRIMARY KEY, owner TEXT, idem TEXT, fingerprint TEXT, day TEXT,
              state TEXT, model TEXT, reserved INTEGER, charged INTEGER DEFAULT 0,
              latency_ms REAL DEFAULT 0, cached INTEGER DEFAULT 0,
              response TEXT, created REAL, UNIQUE(owner,idem));
            CREATE INDEX IF NOT EXISTS owner_day ON requests(owner,day);
            CREATE TABLE IF NOT EXISTS cache(owner TEXT, key TEXT, response TEXT, expires REAL,
              PRIMARY KEY(owner,key));
            ''')

    def fingerprint(self, body):
        return hashlib.sha256(json.dumps(body.model_dump(),sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def reserve(self, owner, idem, fingerprint, model, amount, limit):
        day = time.strftime('%Y-%m-%d',time.gmtime())
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM requests WHERE owner=? AND idem=?',(owner,idem)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise HTTPException(409,'Idempotency key reused with a different request')
                if old['state']=='complete':
                    return old['id'], json.loads(old['response'])
                raise HTTPException(409,'Request already '+old['state']+'; do not automatically retry uncertain provider work')
            # Include unresolved reservations from earlier days, to avoid a midnight bypass.
            spent = db.execute("SELECT COALESCE(SUM(CASE WHEN state IN ('reserved','uncertain') THEN reserved WHEN day=? THEN charged ELSE 0 END),0) FROM requests WHERE owner=?",(day,owner)).fetchone()[0]
            if spent + amount > limit:
                raise HTTPException(429,'Daily spending allowance exhausted, including in-flight reservations')
            ident = str(uuid.uuid4())
            db.execute('INSERT INTO requests(id,owner,idem,fingerprint,day,state,model,reserved,created) VALUES (?,?,?,?,?,?,?,?,?)',
                       (ident,owner,idem,fingerprint,day,'reserved',model,amount,time.time()))
        return ident, None

    def cached(self, owner, key):
        with connect(self.path) as db:
            db.execute('DELETE FROM cache WHERE expires<=?',(time.time(),))
            row = db.execute('SELECT response FROM cache WHERE owner=? AND key=?',(owner,key)).fetchone()
        return json.loads(row[0]) if row else None

    def finish(self, ident, owner, response, charge, latency, cached, cache_key=None, ttl=300):
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE requests SET state='complete',charged=?,latency_ms=?,cached=?,response=? WHERE id=? AND owner=? AND state='reserved'",
                       (charge,latency,int(cached),json.dumps(response),ident,owner))
            if cache_key:
                db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?,?)',(owner,cache_key,json.dumps(response),time.time()+ttl))

    def uncertain(self, ident):
        with connect(self.path) as db:
            db.execute("UPDATE requests SET state='uncertain' WHERE id=? AND state='reserved'",(ident,))

    def metrics(self, owner):
        with connect(self.path) as db:
            rows = db.execute('SELECT state,charged,reserved,latency_ms,cached FROM requests WHERE owner=?',(owner,)).fetchall()
        done = [r for r in rows if r['state']=='complete']
        latencies = sorted(r['latency_ms'] for r in done)
        return {'requests':len(rows),'completed':len(done),'cache_hits':sum(r['cached'] for r in done),
                'charged_usd':sum(r['charged'] for r in done)/NANO,
                'held_reservations_usd':sum(r['reserved'] for r in rows if r['state'] in ('reserved','uncertain'))/NANO,
                'p50_ms':latencies[math.ceil(len(latencies)*.50)-1] if latencies else None,
                'p95_ms':latencies[math.ceil(len(latencies)*.95)-1] if latencies else None}

    def purge(self, owner):
        with connect(self.path) as db:
            db.execute('DELETE FROM cache WHERE owner=?',(owner,))
            # Preserve financial metadata and idempotency tombstones; remove response content.
            db.execute("UPDATE requests SET response=NULL,state='purged' WHERE owner=? AND state='complete'",(owner,))
