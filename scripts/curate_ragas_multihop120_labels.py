"""Explicit assistant gold edits; native Ragas questions and answers remain on disk."""
import copy,json
from pathlib import Path
import ragas_multihop120_core as core

def main():
 out=core.OUT;state=core.read(out/'state.json');assert len(state['accepted'])==120
 revision_path=out/'assistant-label-revisions.json'
 revisions=core.read(revision_path)['revisions'] if revision_path.exists() else []
 already={r['qid'] for r in revisions}
 for c in state['accepted']:
  if c['qid'] in already:continue
  before=copy.deepcopy(c);slot=c['slot'];reason=None
  if slot==86:
   reason='ft and FL use different unit scales for altitude; calling them different physical dimensions is inaccurate. Remove optional unasked encoding-width/datum commentary from required points.'
   c['reference']='I062/130 的 LSB 为 6.25 ft，原始值 1000 对应 6,250 ft；I062/136 的 LSB 为 1/4 FL，原始值 1000 对应 FL 250。两者使用的单位尺度不同，不能直接比较原始数值；应分别按各自 LSB 解码。'
   c['claims']=c['claims'][:2]
  elif slot==107:
   reason='Drop unasked range-check point that compares a raw integer to FL physical bounds. Requested core answer is FL340 and raw1360.'
   c['reference']='标准气压基准 29.92 inHg 下，34,000 ft 表示为 FL 340。I062/136 的 LSB 为 1/4 FL，因此编码值为 340 / 0.25 = 1360（十进制）。'
   c['claims']=c['claims'][:3]
  elif slot==91:
   reason='Clarify the requested outer left/right runway labels; do not incorrectly describe the three-runway scene as a two-runway set.'
   c['reference']='183.4° 的磁方位角除以 10 后取最接近整数，编号为 18；所问两条外侧平行跑道按进近方向分别标为 18L（左侧）和 18R（右侧）。测量基准为从磁北顺时针量得的跑道中心线磁方位角，编号和字母均按进近方向确定。'
   c['claims'][1]['text']='所问两条外侧平行跑道按进近方向分别标为 18L（左侧）和 18R（右侧）。'
  elif slot==44:
   reason='A source if-condition is not an only-if medical rule. Preserve textual conditional advice without claiming other circumstances exclude treatment.'
   c['reference']='两份摘录均说明检测到排气气味或相关症状时，应立即关闭加热器并打开通风口／新鲜空气；FAA-H-8083-25C 还提及开窗及在可用时使用补充氧气。AIM 8-1-4 在症状严重或落地后仍持续时明确建议寻求医疗治疗。题中没有这两项情况的信息，不能确认是否触发该段明确列出的就医条件；这不表示其他情况下无需就医。'
   for cl in c['claims']:
    if '只有在' in cl['text']:cl['text']='AIM 8-1-4 第3点在症状严重或落地后仍持续时明确建议寻求医疗治疗，并未声称其他情形都不应就医。'
    if '无法确定是否必须寻求医疗治疗' in cl['text']:cl['text']='题中未提供症状严重程度或落地后是否持续，不能确认是否触发该段明确列出的就医条件。'
  elif slot==74:
   reason='Remove unasked A-RNP examples from required scoring points; preserve the if-required qualification on extra PBN-box items.'
   c['reference']='不能仅凭通用 GPS 设备确定同时符合 SID 和 STAR。需分别核对两个具体程序 PBN box 内实际列明的 NavSpec 以及适用时要求的传感器／基础设施、附加功能、最低 RNP 值和备注，并核对机载系统是否满足相应 AC 90-100 性能要求。RNAV 1／RNP 1 须在至少 95% 的总飞行时间内保持总系统误差不超过 1 NM。'
   for cl in c['claims']:
    if 'PBN box中' in cl['text']:cl['text']='需分别核对 SID 和 STAR 的 PBN box 内实际列出的 NavSpec 及适用时要求的传感器／基础设施、附加功能、最低 RNP 值和备注；实际列出的要求适用于相应程序的 PBN 元素。'
    if 'A-RNP' in cl['text']:cl['text']='RNAV 1 和 RNP 1 程序须在至少 95% 的总飞行时间内保持总系统误差不超过 1 NM。'
  elif slot==58:
   reason='Do not compare AGL with indicated MSL altitude; source true altitude is mean-sea-level referenced.'
   c['reference']=c['reference'].replace('实际离地高度','真实高度（以海平面为基准）')
   for cl in c['claims']:cl['text']=cl['text'].replace('实际离地高度','真实高度（以海平面为基准）')
  if reason:
   c['required_claims']=[cl['text'] for cl in c['claims']]
   c['supporting_evidence_groups']=list({tuple(sorted(e['chunk_id'] for e in cl['evidence'])) for cl in c['claims']})
   c['assistant_label_revision']={'reason':reason,'native_question_unchanged':True,'original_native_answer_preserved':True,'not_independent_human_review':True}
   revisions.append({'qid':c['qid'],'reason':reason,'original_reference':before['reference'],'revised_reference':c['reference'],'original_claims':before['claims'],'revised_claims':c['claims'],'native_job':c['generation']['job']})
 core.save(out/'assistant-label-revisions.json',{'scope':'Assistant source-and-arithmetic label curation; not new model output or independent expert review','native_questions_unchanged':True,'revisions':revisions,'external_model_calls':0})
 core.save(out/'state.json',state)
 cases=sorted(state['accepted'],key=lambda c:c['slot']);(out/'candidates.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in cases),encoding='utf8')
 from ragas import SingleTurnSample
 from ragas.testset.synthesizers.testset_schema import Testset,TestsetSample
 _,rows=core.load_rows();byid={r['chunk_id']:r for r in rows}
 native=Testset(samples=[TestsetSample(eval_sample=SingleTurnSample(user_input=c['question'],reference=c['reference'],reference_contexts=[byid[cid]['text'] for cid in dict.fromkeys(e['chunk_id'] for cl in c['claims'] for e in cl['evidence'])]),synthesizer_name=c['generation']['synthesizer']) for c in cases])
 native.to_pandas().to_json(out/'ragas-testset.json',orient='records',force_ascii=False,indent=2)
 core.save(out/'final-native-export.json',{'samples':120,'ragas_version':'0.4.3','export':'ragas-testset.json','native_questions':120,'assistant_revised_references':[r['qid'] for r in revisions],'external_qa_calls':0})
 print(json.dumps({'candidates':120,'label_revisions':len(revisions),'external_calls':0}))
if __name__=='__main__':main()
