"""Archive assistant-identified construction errors, refill under original limits."""
import time
import ragas_multihop120_revision4 as revision
core=revision.core
original=core.run
async def run(limit):
 request=core.read(core.OUT/'refinement-request.json');state=core.read(core.OUT/'state.json')
 mapping={r['qid']:r['reason'] for r in request};assert mapping and all(len(r)>12 for r in mapping.values())
 removed=[c for c in state['accepted'] if c['qid'] in mapping];assert len(removed)==len(mapping)
 archive=core.OUT/'refinement-archive.json';history=core.read(archive)
 for c in removed:
  history.append({'candidate':c,'reason':mapping[c['qid']],'removed_at':time.time()})
  state['excluded'].append({'job':c['generation']['job'],'slot':c['slot'],'category':c['category'],'source':c['primary_source'],'error_type':'AssistantConstructionAudit','reason':mapping[c['qid']]})
 core.save(archive,history);state['accepted']=[c for c in state['accepted'] if c['qid'] not in mapping];core.save(core.OUT/'state.json',state)
 await original(limit)
if __name__=='__main__':core.run=run;revision.main()
