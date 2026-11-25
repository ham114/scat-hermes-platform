"""Persistent request reservations for the 300-question public-document campaign."""
from contextlib import closing
import hashlib,json,sqlite3,time
from pathlib import Path
from uuid import uuid4

class CampaignBudget:
    def __init__(self,folder):
        self.folder=Path(folder);self.path=self.folder/'calls.sqlite';self.policy=json.loads((self.folder/'policy.json').read_text())
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('create table if not exists calls(id text primary key,job text,lane text,started real,state text,reserved integer,charge integer,input integer,output integer,cached integer,prompt_sha256 text)')
            db.execute('create table if not exists meta(key text primary key,value text)')
            digest=hashlib.sha256(json.dumps(self.policy,sort_keys=True).encode()).hexdigest()
            db.execute('insert or ignore into meta values(?,?)',('policy_sha256',digest))
            assert db.execute('select value from meta where key=?',('policy_sha256',)).fetchone()[0]==digest
    def admit(self,job,lane,messages,max_tokens):
        body=json.dumps(messages,ensure_ascii=False);reserve=len(body.encode())+max_tokens+2048;identity=uuid4().hex
        with closing(sqlite3.connect(self.path,timeout=20)) as db,db:
            db.execute('begin immediate')
            count,units,charge=db.execute('select count(*),coalesce(sum(reserved),0),coalesce(sum(charge),0) from calls').fetchone()
            exhausted=db.execute("select value from meta where key='exhausted'").fetchone()
            if exhausted or time.time()>self.policy['expires_at'] or count>=self.policy['max_requests'] or units+reserve>self.policy['max_conservative_units'] or charge+reserve>self.policy['max_provider_tokens']:
                db.execute('insert or ignore into meta values(?,?)',('exhausted','campaign_limit'));db.commit();raise RuntimeError('campaign_budget_exhausted')
            db.execute('insert into calls values(?,?,?,?,?,?,?,?,?,?,?)',(identity,job,lane,time.time(),'admitted',reserve,reserve,0,0,0,hashlib.sha256(body.encode()).hexdigest()))
        return identity
    def settle(self,identity,response=None,error=None):
        usage=response.usage.model_dump() if response is not None and response.usage is not None else None
        with closing(sqlite3.connect(self.path,timeout=20)) as db,db:
            if usage and usage.get('prompt_tokens'):
                inp=int(usage['prompt_tokens']);out=int(usage.get('completion_tokens') or 0)
                cache=int(usage.get('prompt_cache_hit_tokens') or (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0)
                db.execute('update calls set state=?,charge=?,input=?,output=?,cached=? where id=?',('returned',inp+out,inp,out,cache,identity))
            else:db.execute('update calls set state=? where id=?',('error_or_unknown' if error else 'usage_unknown',identity))
    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            count,units,charge,inp,out,cache=db.execute('select count(*),coalesce(sum(reserved),0),coalesce(sum(charge),0),coalesce(sum(input),0),coalesce(sum(output),0),coalesce(sum(cached),0) from calls').fetchone()
            states=dict(db.execute('select state,count(*) from calls group by state'))
            reason=db.execute("select value from meta where key='exhausted'").fetchone()
        return {'requests':count,'conservative_units':units,'provider_budget_charge':charge,'provider_input_tokens':inp,'provider_output_tokens':out,'provider_cached_tokens_included_in_input':cache,'states':states,'exhausted':reason[0] if reason else None,'deadline_passed':time.time()>self.policy['expires_at']}
