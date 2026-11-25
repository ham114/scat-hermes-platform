"""Close out this targeted retest without changing any historical score."""
import json,hashlib,html,sqlite3,statistics,shutil,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261124-rag-problem-retest';DEST=ROOT/'output/rag-problem-retest-20261124'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
def main():
 import sys
 sys.path.insert(0,str(ROOT/'scripts'))
 import run_problem_retest as run
 config=run.validate();policy=read(OUT/'qa-policy.json');data=read(DEST/'dataset.json');qa=read(OUT/'qa-final.json');metrics=read(OUT/'ragas/final.json')
 assert len(qa['units'])==15 and len(metrics['results'])==77 and all(x['status']=='scored' for x in metrics['results'])
 assert sha(DEST/'dataset.json')==policy['dataset_sha256']
 for p,h in policy['source_sha256'].items():assert sha(ROOT/p)==h
 with sqlite3.connect(f'file:{OUT/"qa-ledger.sqlite"}?mode=ro',uri=True) as db:
  n,units,charge,inputs,outputs,unknown=db.execute("select count(*),sum(units),sum(provider_charge),sum(input),sum(output),sum(case when state!='returned' then 1 else 0 end) from calls").fetchone()
  lanes=dict(db.execute('select lane,count(*) from calls group by lane'))
 assert n<=650 and units<=40000000 and charge<=6000000 and unknown==0
 assert (OUT/'ragas/final.json').stat().st_mtime<=policy['expires_at']
 byid={u['unit_id']:u for u in data['units']};values={m['job']:m['value'] for m in metrics['results']}
 comparison=[]
 for row in sorted(qa['units'],key=lambda r:r['unit_id']):
  unit=byid[row['unit_id']];d=row['review'].get('data') or {};passed=row['review_is_valid'] and d.get('verdict')=='pass'
  comparison.append({'unit_id':row['unit_id'],'question_changed':unit['question_changed'],'reference_revised':unit['reference_revised'],
    'inclusion_reason':unit['inclusion_reason'],'baseline_verdict':unit['baseline_review'].get('verdict',unit['baseline_review']['status']),
    'execution_status':row['execution_status'],'criterion_review_passed':passed,'criterion_review':d,'answer':row['response'],
    'baseline_answer':unit['baseline_answer'],'question':unit['question'],'baseline_question':unit['baseline_question'],'reference':unit['reference'],
    'metrics':{m:values[row['unit_id']+'--'+m] for m in ['faithfulness','answer_relevancy','context_precision','context_recall','factual_correctness']}})
 means={m:{'n':15,'mean':statistics.mean(c['metrics'][m] for c in comparison)} for m in comparison[0]['metrics']}
 groups={}
 for label,chosen in [('prior_problem',[c for c in comparison if c['inclusion_reason']=='prior failure or unavailable review']),
   ('added_controls',[c for c in comparison if c['inclusion_reason']!='prior failure or unavailable review']),
   ('revised_question_acceptance',[c for c in comparison if c['question_changed']]),
   ('unchanged_question',[c for c in comparison if not c['question_changed']]),
   ('unchanged_question_and_reference_prior_failures',[c for c in comparison if not c['question_changed'] and not c['reference_revised'] and c['baseline_verdict']=='fail'])]:
  groups[label]={'turns':len(chosen),'review_passed':sum(c['criterion_review_passed'] for c in chosen),'units':[c['unit_id'] for c in chosen]}
 baseline={p.stem:read(p) for p in (OUT/'baseline-rubric-checks').glob('*.json')}
 diagnostic={'remaining_failure':'AV300-20261124-233-T1','gold_chunk_present':'kco_192e4dfabb888ac74c68026949968f95',
  'finding':'Correct AIM 4-2-1 General paragraph 3 is in v7. The initial retrieval and two explicit follow-up searches missed it. Answer honestly reported insufficiency; no fabrication, but missing two required points.',
  'next_fix':'Resolve explicit document and section identifiers before semantic ranking; retrieve and verify coverage for each required source. Decomposition alone is insufficient: two targeted queries were already attempted.'}
 trace=read(OUT/'qa/AV300-20261124-233/turn-1/result.json')
 diagnostic['searches']=[{'query':e['query'],'chunk_ids':e['chunk_ids']} for e in trace['evidence_operations'] if e['tool']=='rag_search']
 assert all(diagnostic['gold_chunk_present'] not in e['chunk_ids'] for e in diagnostic['searches'])
 summary={'status':'completed','cases':14,'turns':15,'execution_passed':sum(c['execution_status']=='passed' for c in comparison),
  'criterion_review_passed':sum(c['criterion_review_passed'] for c in comparison),'criterion_review_failed':sum(not c['criterion_review_passed'] for c in comparison),
  'groups':groups,'ragas_native_metric_results':77,'ragas_means_new_answers_only':means,'saved_answer_corrected_rubric_reviews':baseline,
  'saved_answer_corrected_reference_f1':{cid:values[cid+'-old-rubric--factual_correctness'] for cid in ['P0-20261123-024','AV300-20261124-261']},
  'diagnostic':diagnostic,'usage':metrics['usage'],'lanes':lanes,'seconds':(OUT/'ragas/final.json').stat().st_mtime-policy['started_at'],
  'corpus_sha256':data['corpus_sha256'],'generation_id':data['generation_id'],
  'limits':'Targeted development regression, not overall QA accuracy; five questions revised; two same-question rubrics corrected; same-provider initial reviews, not independent human validation.',
  'audit':{'protected_baselines_unchanged':True,'dataset_frozen_before_answers':True,'qa_inputs_do_not_contain_reference':True,'actual_current_synthetic_history_only':True,
           'source_hashes_match_preflight':True,'shared_budget_caps_passed':True,'unknown_or_error_calls':unknown,'production_config_unchanged':True}}
 save(DEST/'summary.json',summary);save(DEST/'case-comparison.json',comparison)
 for name in ['qa-final.json','qa-policy.json']:shutil.copyfile(OUT/name,DEST/name)
 save(DEST/'ragas-results.json',metrics)
 save(DEST/'remaining-failure.json',diagnostic)
 (DEST/'calls').mkdir(exist_ok=True)
 for p in (OUT/'ragas/calls').glob('*.json'):shutil.copyfile(p,DEST/'calls'/p.name)
 save(ROOT/'docs/validation/20261124-rag-problem-retest.json',summary)
 esc=lambda x:html.escape(str(x));cards=[]
 for c in comparison:
  tags=('题干修订后验收' if c['question_changed'] else '同题，标答口径修订' if c['reference_revised'] else '同题同口径')
  cards.append(f'<details><summary>{esc(c["unit_id"])} · {"通过" if c["criterion_review_passed"] else "未通过"} · {tags}</summary><h3>本轮题目</h3><p>{esc(c["question"])}</p><h3>本轮答案</h3><p>{esc(c["answer"])}</p><h3>最小参考</h3><p>{esc(c["reference"])}</p><h3>评审</h3><p>{esc(c["criterion_review"])}</p><h3>原题与旧答案</h3><p>{esc(c["baseline_question"])}</p><p>{esc(c["baseline_answer"])}</p><p>Ragas: {esc(c["metrics"])}</p></details>')
 labels={'faithfulness':'Faithfulness','answer_relevancy':'Answer Relevancy','context_precision':'Context Precision（排序AP）','context_recall':'Context Recall','factual_correctness':'Factual Correctness F1'}
 text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>问题题目v7重评</title><style>body{{font:16px/1.75 system-ui,sans-serif;color:#172b40;background:#f4f7fb;max-width:1100px;margin:auto;padding:28px}}section,details{{background:white;border:1px solid #d8e1ec;border-radius:10px;margin:16px 0;padding:18px}}summary{{font-weight:650;cursor:pointer}}p{{white-space:pre-wrap;overflow-wrap:anywhere}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #d8e1ec}}a{{color:#1458a5}}</style><h1>之前问题题目 · v7重评</h1><p>2026-11-24 · 14题15轮 · 原生RAG重新作答＋要点初评＋Ragas标准指标</p><section><p>15/15轮完成执行；14/15轮通过要点初评；233题仍缺少第二来源。该结果只针对挑选的问题题目，不是整个系统的准确率。</p><p>原有问题13轮中12轮通过，另2轮补充回归均通过。5道改过题干的题目单列为验收，不能将新旧分数差当成系统提升。024、261的旧答案按正确标答重新评审也通过，因此不能把它们改判通过归功于新模型回答。</p><p>同题同口径的原失败样本：090本轮已正确区分起始阈值；172本轮成功交付答案；233仍未完整回答。</p></section><h2>Ragas新答案均值</h2><table><tr><th>指标</th><th>均值</th><th>n</th></tr>{''.join(f'<tr><td>{labels[m]}</td><td>{v["mean"]:.3f}</td><td>{v["n"]}</td></tr>' for m,v in means.items())}</table><p>上述是0至1的指标均值，不是通过率。上下文来自实际读取或可见片段；Context Precision/Recall使用初始Top5。双轮题按每轮实际前文评分，非Ragas原生MultiTurn指标。旧300/426题整体均值与本次精选难题不能直接比较。</p><section><h2>剩余问题：233</h2><p>仍只回答P TIME指向PROPOSED DEPARTURE TIME，未取得AIM 4-2-1 General第3段，因此未覆盖学习词汇表的理由与两处关系。答案如实说明证据不足，没有编造。该段确实存在于v7语料。</p><p>本轮已补查两次明确的章节相关查询，仍未命中。下一步应优先修复文档／章节号精确定位和逐来源覆盖检查，而非仅增加拆题提示。</p></section><h2>逐题对照</h2>{''.join(cards)}<section><h2>用量与审计</h2><p>总请求{n}次；已知输入{inputs:,}、输出{outputs:,}，合计{inputs+outputs:,} token；缓存命中已包含在输入内。保守计量{units:,}，无未知或失败请求，未超授权650次／600万输入输出token／4000万保守单位／1小时。</p><p>77项Ragas结果全部可用，其中75项属于15轮新答案，2项为旧答案的新参考F1。原始题集、语料和历史成绩未覆盖；生产配置未修改。</p><p><a href="summary.json">完整汇总</a> · <a href="case-comparison.json">逐题数据</a> · <a href="ragas-results.json">Ragas结果</a> · <a href="user-approval.json">本轮授权</a></p></section></html>'''
 (DEST/'REPORT.html').write_text(text,encoding='utf8');print(json.dumps({'status':'completed','turns':15,'criterion_passed':summary['criterion_review_passed'],'ragas_results':77,'requests':n,'provider_tokens':inputs+outputs}),flush=True)
if __name__=='__main__':main()
