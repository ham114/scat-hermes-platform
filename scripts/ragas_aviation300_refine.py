"""Replace construction-time quality defects under the original campaign ledger."""
import argparse,asyncio,copy,fcntl,hashlib,json,re,time
from pathlib import Path
import ragas_aviation300 as core

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--phase',default='quality-v2');parser.add_argument('--qids',nargs='*');parser.add_argument('--wait',action='store_true');a=parser.parse_args()
    lock=(core.OUT/'campaign.lock').open('a')
    while True:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
        except BlockingIOError:
            if not a.wait:raise
            if time.time()>core.read(core.OUT/'policy.json')['expires_at']:raise TimeoutError('Original campaign deadline reached')
            time.sleep(5)
    state=core.read(core.OUT/'state.json');policy=core.read(core.OUT/'policy.json')
    assert time.time()<policy['expires_at']
    prior=core.OUT/f'{a.phase}-removed.json'
    if prior.exists():raise ValueError('Refinement phase already recorded; no automatic replay')
    requested=set(a.qids or [])
    if not requested:
        requested={c['qid'] for c in state['accepted'] if c['category']=='insufficient'}
        requested|={'AV300-20261124-001','AV300-20261124-063','AV300-20261124-085','AV300-20261124-159','AV300-20261124-203'}
    removed=[c for c in state['accepted'] if c['qid'] in requested]
    if not removed:raise ValueError('No requested candidates found')
    core.save(prior,{'phase':a.phase,'removed':removed,'candidates_sha256_before':core.sha(core.OUT/'candidates.jsonl'),
        'reason':'Assistant construction review: replace templated/weak boundary questions, non-numerical questions in numerical slots, or over-specified gold. Not a QA-score-based selection.',
        'original_deadline':policy['expires_at'],'limits_and_ledger_unchanged':True,'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    state['accepted']=[c for c in state['accepted'] if c['qid'] not in requested]
    state['excluded']+=[{'job':c['generation']['job'],'slot':c['slot'],'category':c['category'],'source':c['primary_source'],'error_type':'assistant_construction_review','reason':'Replaced before evaluation: '+a.phase} for c in removed]
    state['status']='refining-candidates';core.save(core.OUT/'state.json',state)
    # Scope and budget do not change. New prompts remain recorded in each
    # native Ragas scenario and all replaced native outputs remain on disk.
    core.TYPE_HINTS['numeric_units']='Ask for an actual numerical quantity, unit, encoding bit value/resolution, quantitative bound, formula, or a grounded conversion/calculation. Do not ask for standard radio wording, procedure priority, a final symbol already spelled out in the question, or a list of non-numerical maintenance actions. Do not put the requested answer in the question.'
    core.TYPE_HINTS['insufficient']='Create ONE natural, specific aviation research question about one missing measurement or input directly related to this excerpt. Prefer a calculation or interpretation that requires an explicitly unprovided input. Explain the applicable known rule and name the one missing input. Do not first ask to list the whole excerpt. Do not repeat a laundry list of aircraft/instruments/weather/operating states. Do not copy the answer into the question. Avoid organizational descriptions and generic can-this-book-tell-me-a-real-world-value questions. Clearly state the particular guide or actual-data boundary. Never infer real-world nonexistence. Use a concise reference with only the minimum necessary known rule, missing input and resulting uncertainty.'
    original_select=core.select_rows
    def select(slot,attempt,pools,byid,vectors,used):
        if slot['category']=='insufficient':
            candidates=[r for r in pools[slot['primary_source']] if r['chunk_id'] not in used and re.search(r'depends|requir|measur|observ|actual|calibrat|manufacturer|temperature|pressure|accuracy|uncertainty|altitude|visibility|LSB|data age|wind speed|wind direction|provided',byid[r['chunk_id']]['text'],re.I)]
            if len(candidates)>=4:
                pools=copy.copy(pools);pools[slot['primary_source']]=candidates
        return original_select(slot,attempt,pools,byid,vectors,used)
    core.select_rows=select
    asyncio.run(core.run(0))

if __name__=='__main__':main()
