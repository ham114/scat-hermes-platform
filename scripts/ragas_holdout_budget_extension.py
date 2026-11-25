"""User-authorized request-only extension, preserving policy and all counters."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from ragas_aviation_budget import CampaignBudget


class ExtendedBudget(CampaignBudget):
    def __init__(self,folder):
        super().__init__(folder)
        folder=Path(folder);approval_path=folder/'request-extension-approval.json'
        approval=json.loads(approval_path.read_text(encoding='utf8'))
        assert approval['authorized'] is True and approval['max_requests']==3000
        assert self.policy['max_requests']==1200
        assert approval['original_policy_sha256']==hashlib.sha256((folder/'policy.json').read_bytes()).hexdigest()
        for key in ['max_provider_tokens','max_conservative_units','expires_at']:assert approval[key]==self.policy[key]
        digest=hashlib.sha256(approval_path.read_bytes()).hexdigest()
        with sqlite3.connect(self.path,timeout=20)as db:
            db.execute('begin immediate')
            old=db.execute("select value from meta where key='request_extension_approval_sha256'").fetchone()
            if old:assert old[0]==digest
            else:
                count,units,charge=db.execute('select count(*),coalesce(sum(reserved),0),coalesce(sum(charge),0)from calls').fetchone()
                assert count<=1200 and units<self.policy['max_conservative_units'] and charge<self.policy['max_provider_tokens']
                assert time.time()<self.policy['expires_at']
                assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
                db.execute("insert into meta values('request_extension_approval_sha256',?)",(digest,))
                exhausted=db.execute("select value from meta where key='exhausted'").fetchone()
                if exhausted and exhausted[0]=='campaign_limit' and count==1200:
                    db.execute("delete from meta where key='exhausted'")
        self.policy={**self.policy,'max_requests':3000}

    def snapshot(self):
        return {**super().snapshot(),'effective_max_requests':self.policy['max_requests']}
