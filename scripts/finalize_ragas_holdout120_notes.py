"""Freeze local construction notes after all 120 candidates pass source audit."""
import hashlib,json,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261125-ragas-holdout120'
read=lambda p:json.loads(p.read_text(encoding='utf8'))

def main():
 cases=[json.loads(l) for l in (OUT/'candidates.jsonl').read_text(encoding='utf8').splitlines()]
 audit=read(OUT/'audit.json');sim=read(OUT/'similarity-audit.json')
 digest=hashlib.sha256((OUT/'candidates.jsonl').read_bytes()).hexdigest()
 assert len(cases)==120 and audit['status']=='passed'
 assert audit['candidates_sha256']==sim['candidates_sha256']==digest
 assert all(c['assistant_source_review']['decision']in ['retain','revise_reference'] for c in cases)
 # Similarity hits require an explicit, case-specific decision, never an automatic waiver.
 adjudications=read(OUT/'similarity-adjudications.json') if (OUT/'similarity-adjudications.json').exists() else []
 pending={x['qid']for x in sim['previous_topic_flags']}|{x[k]for x in sim['within_batch_pairs']for k in ['left','right']}
 assert pending<={x['qid']for x in adjudications} and all(x['decision']=='retain' and x['reason']for x in adjudications), 'Adjudicate similarity hits before freezing notes'
 focus=['HO120-20261125-035','HO120-20261125-045','HO120-20261125-112']
 assert set(focus)<={c['qid'] for c in cases}
 result={'status':'assistant-reviewed synthetic candidates; independent expert review not performed',
  'created_at':time.time(),'candidates_sha256':digest,'native_questions_unchanged':120,
  'assistant_reviewed':120,'assistant_reference_revisions':audit['assistant_revised_reference_count'],
  'explicit_assistant_admissions':audit['explicit_assistant_admissions'],
  'source_guided_native_questions':sum(bool(c.get('assistant_source_plan'))for c in cases),
  'recommended_spot_checks':focus,
  'spot_check_reasons':{
   focus[0]:'区分MOR一般测量范围和RVR显示范围；250ft基线乘1–25得到约250–6250ft，但8000ft是否有效还需设备实际校准范围。',
   focus[1]:'区分报告分辨率与测量不确定度；仅在题设理想恒定风、最近步长舍入模型下，0.5m/s分辨率结合3m响应长度得到约0.92–1.09s，时间不唯一。',
   focus[2]:'区分关车压力测量与随后发动机运行两个阶段：按所引近似由21.92inHg推算约8000ft，再与制造商假定的6000ft临界压力高度比较；不是精确ISA反算。'},
  'similarity_adjudications':adjudications,
  'semantic_quality_flag_reviews':[{'qid':f['qid'],'kind':f['kind'],'decision':'retain','reason':'已逐题检查：数值题要求基于已给实例对照数值边界、计算门槛或判断范围；长题保留必要运行条件，未以长度或关键词自动判定质量。'}for f in audit['semantic_quality_flags']],
  'construction_standards':['按冻结来源、题型、语言配额构题','每题绑定两段不同章节正文，历史gold片段与批内gold片段不重复','检验编码、单位、垂直基准、条件及may/should等限定','删除无支撑计算、不同实例迁移、答案规则泄露和独立子问拼接','标答最小修订单独记录，原生问题不改写','模型误判由助手原文裁定时保留原拒绝和独立证据移除证明，不伪造模型通过'],
  'limitations':['合成题集及同一提供商审核，未经独立航空领域专家逐题验证','两段必要性包含数量/定义对照、同一情景条件分析及串行推导，不是全部都属于严格的链式两跳','BGE-M3用于原文配对和本地查重，未用本批检索/重排成绩选题；不宣称完全独立的人工基准','按冻结文档版本评价，不把旧版示例直接升级为所有现行运行的普遍规则','未运行问答、证据检索或重排评测，不能据此报告当前RAG准确率'],
  'records':{'curation':'assistant-curation-history.json','references':'assistant-label-revisions.json','provider_overrides':'assistant-admission-adjudications.json','source_risks':'source-risk-register.json'},
  'qa_evaluation_calls':0,'production_changes':0,'training_runs':0}
 (OUT/'assistant-quality-notes.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
 print(json.dumps({'notes_frozen':True,'candidates':120,'recommended_spot_checks':focus,'candidates_sha256':digest}))

if __name__=='__main__':main()
