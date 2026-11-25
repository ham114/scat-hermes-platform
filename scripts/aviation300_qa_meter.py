"""Process-safe request/usage ledger for the frozen aviation 300 QA campaign."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import time
from uuid import uuid4


class AggregateBudget:
    def __init__(self,folder,case_id,lane):
        self.folder=Path(folder)
        self.policy=json.loads((self.folder/'qa-policy.json').read_text())
        self.path=self.folder/'qa-ledger.sqlite'
        self.case_id,self.lane=case_id,lane
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,case_id TEXT,lane TEXT,started REAL,state TEXT,units INTEGER,provider_charge INTEGER,input INTEGER,output INTEGER,cached INTEGER,digest TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT)')
            identity=hashlib.sha256(json.dumps(self.policy,sort_keys=True).encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('policy_sha256',identity))
            assert db.execute('SELECT value FROM metadata WHERE key=?',('policy_sha256',)).fetchone()[0]==identity
        self.path.chmod(0o600)

    def wrap_create(self,create):
        def invoke(**kwargs):
            body=json.dumps({k:kwargs[k] for k in ('messages','tools','response_format') if k in kwargs},ensure_ascii=False,default=str)
            assert 0<int(kwargs.get('max_tokens',4096))<=self.policy['max_output_tokens']
            reserve=len(body.encode())+int(kwargs.get('max_tokens',4096))+2048
            identity=uuid4().hex
            with closing(sqlite3.connect(self.path,timeout=15)) as db,db:
                db.execute('BEGIN IMMEDIATE')
                calls,units,provider=db.execute('SELECT count(*),coalesce(sum(units),0),coalesce(sum(provider_charge),0) FROM calls').fetchone()
                reason=db.execute('SELECT value FROM metadata WHERE key=?',('exhausted',)).fetchone()
                if reason or time.time()>self.policy['expires_at'] or calls>=self.policy['max_requests'] or units+reserve>self.policy['max_conservative_units'] or provider+reserve>self.policy['max_returned_provider_tokens']:
                    db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('exhausted','campaign_admission_limit'))
                    db.commit()
                    raise RuntimeError('aviation300_qa_campaign_budget_exhausted')
                db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identity,self.case_id,self.lane,time.time(),'admitted',reserve,reserve,0,0,0,hashlib.sha256(body.encode()).hexdigest()))
            try:
                result=create(**kwargs)
                if kwargs.get('stream'):
                    return MeteredStream(result,self,identity)
                self.settle(identity,getattr(result,'usage',None))
                return result
            except Exception:
                self.settle(identity,None,state='error_or_unknown')
                raise
        return invoke

    def settle(self,identity,usage,state='returned'):
        if hasattr(usage,'model_dump'):usage=usage.model_dump()
        with closing(sqlite3.connect(self.path,timeout=15)) as db,db:
            if isinstance(usage,dict) and usage.get('prompt_tokens'):
                inp=int(usage['prompt_tokens']);out=int(usage.get('completion_tokens') or 0)
                cached=int(usage.get('prompt_cache_hit_tokens') or (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0)
                db.execute('UPDATE calls SET state=?,provider_charge=?,input=?,output=?,cached=? WHERE id=?',('returned',inp+out,inp,out,cached,identity))
            else:
                db.execute('UPDATE calls SET state=? WHERE id=? AND state=?',(state if state!='returned' else 'usage_unknown',identity,'admitted'))

    def admit_tool(self):
        if time.time()>self.policy['expires_at']:raise RuntimeError('aviation300_qa_deadline')

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            row=db.execute('SELECT count(*),coalesce(sum(units),0),coalesce(sum(input),0),coalesce(sum(output),0),coalesce(sum(cached),0),sum(case when state!=? then 1 else 0 end) FROM calls',('returned',)).fetchone()
            reason=db.execute('SELECT value FROM metadata WHERE key=?',('exhausted',)).fetchone()
        return {'llm_calls':row[0],'charged_tokens':row[1],'provider_input_tokens':row[2],'provider_output_tokens':row[3],'provider_cached_tokens':row[4],'unknown_or_error_calls':row[5] or 0,
                'provider_budget_charge':self.charge(),
                'exhausted':reason[0] if reason else None if time.time()<=self.policy['expires_at'] else 'wall_seconds'}


    def charge(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('select coalesce(sum(provider_charge),0) from calls').fetchone()[0]


class MeteredStream:
    def __init__(self,stream,meter,identity):
        self.stream,self.meter,self.identity=stream,meter,identity
        self.usage=None;self.closed=False
    def __getattr__(self,name):return getattr(self.stream,name)
    def __iter__(self):
        try:
            for chunk in self.stream:
                self.usage=getattr(chunk,'usage',None) or self.usage
                yield chunk
        finally:self.close()
    def close(self):
        if self.closed:return
        self.closed=True
        self.meter.settle(self.identity,self.usage)
        if hasattr(self.stream,'close'):self.stream.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def process_dns(policy):
    original=socket.getaddrinfo
    def resolve(host,port,*args,**kwargs):
        if host in ('api.deepseek.com',b'api.deepseek.com'):
            if time.time()>policy['expires_at']:raise RuntimeError('aviation300_dns_mapping_expired')
            return original(policy['verified_dns_address'],port,*args,**kwargs)
        return original(host,port,*args,**kwargs)
    socket.getaddrinfo=resolve
