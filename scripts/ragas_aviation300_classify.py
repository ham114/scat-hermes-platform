"""Record assistant type adjudications without changing native questions or answers."""
import collections,fcntl,json,time
import ragas_aviation300 as core

changes={
 'AV300-20261124-063':('numeric_units','fact','Asks authorization documents and a regulation identifier, not a quantitative quantity.'),
 'AV300-20261124-259':('numeric_units','fact','Asks acronym expansions, not numerical values or units.'),
 'AV300-20261124-035':('fact','numeric_units','Explicitly asks the altitude unit and FL hundred-foot representation.'),
 'AV300-20261124-099':('fact','numeric_units','Explicitly asks the percentage definition and unit of relative humidity.'),
}
def main():
    lock=(core.OUT/'campaign.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=core.read(core.OUT/'state.json')
    if (core.OUT/'assistant-type-adjudications.json').exists():raise ValueError('Adjudications already recorded')
    before=collections.Counter(c['category'] for c in state['accepted']);records=[]
    for c in state['accepted']:
        if c['qid'] not in changes:continue
        old,new,reason=changes[c['qid']];assert c['category']==old
        c['category']=new;c['original_requested_category']=old
        c['assistant_type_adjudication']={'category':new,'reason':reason,'native_question_and_reference_unchanged':True}
        records.append({'qid':c['qid'],'before':old,'after':new,'reason':reason})
    assert len(records)==4 and before==collections.Counter(c['category'] for c in state['accepted'])
    core.save(core.OUT/'assistant-type-adjudications.json',{'records':records,'not_a_qa_score_change':True,'external_model_calls':0})
    core.save(core.OUT/'state.json',state)
    (core.OUT/'candidates.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in sorted(state['accepted'],key=lambda c:c['slot'])),encoding='utf8')
    print(json.dumps({'type_adjudications':4,'category_counts_unchanged':True}))

if __name__=='__main__':main()
