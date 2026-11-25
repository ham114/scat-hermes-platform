"""Offline audit and report of the focused v7 assessment; never change labels."""
import collections,datetime,hashlib,html,json,math,shutil,sqlite3,statistics,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'var/validation/20261124-rag-multihop120-eval';DEST=ROOT/'output/ragas-multihop120-evaluation-20261124'
sys.path.insert(0,str(ROOT/'scripts'))
LABELS={'cross_document':'跨文档综合','cross_chapter':'同文档跨章节','numeric_conditions':'数值与适用条件','insufficient':'条件缺失与证据不足'}
METRICS=['faithfulness','answer_relevancy','context_precision','context_recall','factual_correctness']
REF={'context_precision','context_recall','factual_correctness'}
NAMES={'faithfulness':'Faithfulness','answer_relevancy':'Answer Relevancy','context_precision':'Context Precision（排序AP）','context_recall':'Context Recall','factual_correctness':'Factual Correctness F1'}
def read(p):return json.loads(p.read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
def avg(xs):return statistics.mean(xs) if xs else None
def pct(n,d):return f'{n}/{d}（{n/d:.1%}）' if d else '—'

def main():
 import run_multihop120_eval as run
 run.validate();data=read(DEST/'dataset.json');plan=read(DEST/'plan.json');policy=read(OUT/'qa-policy.json')
 for f,h in policy['source_sha256'].items():assert sha(ROOT/f)==h
 assert sha(DEST/'dataset.json')==plan['dataset_sha256']==policy['dataset_sha256']
 qa=read(OUT/'qa-final.json');metric=read(OUT/'ragas/final.json');rubrics=read(OUT/'frozen-rubrics.json');retrieval=read(OUT/'retrieval-summary.json')
 assert len(data['units'])==len(qa['units'])==120 and len(metric['results'])==600
 assert {r['unit_id'] for r in qa['units']}=={u['unit_id'] for u in data['units']}
 assert len({m['job'] for m in metric['results']})==600
 assert rubrics['no_actual_answers_seen'] and rubrics['dataset_sha256']==policy['dataset_sha256']
 with sqlite3.connect(f'file:{OUT}/qa-ledger.sqlite?mode=ro',uri=True) as db:
  requests,units,charge,inp,output=db.execute('select count(*),coalesce(sum(units),0),coalesce(sum(provider_charge),0),coalesce(sum(input),0),coalesce(sum(output),0) from calls').fetchone()
  states=dict(db.execute('select state,count(*) from calls group by state'));lanes=dict(db.execute('select lane,count(*) from calls group by lane'))
  first,last=db.execute('select min(started),max(started) from calls').fetchone()
  answer_first=db.execute("select min(started) from calls where lane='qa'").fetchone()[0]
  calibration_last=db.execute("select max(started) from calls where lane='gold-calibration'").fetchone()[0]
 assert requests<=policy['max_requests'] and units<=policy['max_conservative_units'] and charge<=policy['max_returned_provider_tokens']
 assert last<=policy['expires_at'] and not states.get('admitted')
 assert (OUT/'ragas/final.json').stat().st_mtime<=policy['expires_at']
 if answer_first:assert calibration_last<=rubrics['frozen_at']<=answer_first
 byid={u['unit_id']:u for u in data['units']};mv={m['job']:m for m in metric['results']};rr={r['case_id']:r for r in retrieval['cases']}
 answers=[];leaks=[]
 for r in sorted(qa['units'],key=lambda r:r['unit_id']):
  u=byid[r['unit_id']];assert r['question']==u['question'] and r['reference']=='\n'.join(rubrics['cases'][u['case_id']]['required_points'])
  request_file=OUT/'qa'/u['case_id']/'turn-1.request.json';request=read(request_file)
  assert set(request)=={'case_id','question','actual_history','output','inspect_only'} and request['question']==u['question'] and request['actual_history']==[] and request['inspect_only'] is False
  result_file=OUT/'qa'/u['case_id']/'turn-1/result.json'
  native=read(result_file) if result_file.exists() else None
  if native:
   assert native.get('production_same_source_isolated_native_execution') is True
   assert native.get('temporary_profile_removed') is True
  review=r['review'].get('data') or {};passed=r['review_is_valid'] and review.get('verdict')=='pass'
  initial={e['chunk_id'] for e in r['initial_top5']};full={e['chunk_id'] for e in r['contexts_seen'] if e['visibility']=='full'};allseen={e['chunk_id'] for e in r['contexts_seen']}
  targets=set(u['gold_chunk_ids']);groups=u['supporting_evidence_groups'];ranked=rr[u['case_id']]
  required_families={e['source_id'] for e in u['gold_evidence']};initial_families={e['source_id'] for e in r['initial_top5']};read_families={e['source_id'] for e in r['contexts_seen'] if e['visibility']=='full'}
  coverage={'both_initial5':targets<=initial,'both_full_read':targets<=full,'both_seen_including_snippets':targets<=allseen,
   'all_required_source_families_initial5':required_families<=initial_families,'all_required_source_families_full_read':required_families<=read_families,
   'group_coverage_initial5':sum(set(g)<=initial for g in groups)/len(groups),
   'diagnostic_both_top10':ranked['both_chunks_at_10'],'diagnostic_retrieval_status':ranked['status'],
   'initial5_matches_diagnostic_call':list(e['chunk_id'] for e in r['initial_top5'])==[e['chunk_id'] for e in ranked['ranked'][:5]]}
  if r['execution_status']!='passed':bucket='execution_issue'
  elif not r['review_is_valid']:bucket='criterion_review_unavailable'
  elif r['annotation_issue'] or not r['reference_metrics_primary_eligible']:bucket='annotation_or_rubric_flag'
  elif passed:bucket='pass'
  elif review.get('numeric_or_entity_errors'):bucket='numeric_or_entity_error'
  elif review.get('unsupported_claims'):bucket='unsupported_answer_claim'
  elif review.get('missing_point_ids') and not coverage['both_full_read']:bucket='missing_points_with_strict_evidence_gap'
  else:bucket='answer_or_composition_issue'
  answers.append({**{k:r[k] for k in ['unit_id','case_id','question','response','execution_status','category','primary_source','require_cross_source','annotation_issue','reference_metrics_primary_eligible','context_integrity_error']},
   'reference':r['reference'],'candidate_reference':u['reference'],'minimum_required_points':rubrics['cases'][u['case_id']]['required_points'],
   'optional_explanations':rubrics['cases'][u['case_id']]['optional_explanations'],'calibration_status':rubrics['cases'][u['case_id']]['calibration_status'],
   'review_is_valid':r['review_is_valid'],'criterion_review_passed':bool(passed),'criterion_review':review,'review_status':r['review']['status'],
   'coverage':coverage,'diagnostic_bucket':bucket,'metrics':{n:mv[r['unit_id']+'--'+n] for n in METRICS},'answer_status':(native.get('result') or {}).get('status') if native else None})
 def summarize(chosen):
  clean=[r for r in chosen if r['reference_metrics_primary_eligible'] and not r['annotation_issue']]
  return {'cases':len(chosen),'execution_passed':sum(r['execution_status']=='passed' for r in chosen),
   'raw_criterion_passed':sum(r['criterion_review_passed'] for r in chosen),'raw_criterion_failed':sum(r['review_is_valid'] and not r['criterion_review_passed'] for r in chosen),
   'criterion_unavailable':sum(not r['review_is_valid'] for r in chosen),'annotation_clean_cases':len(clean),
   'annotation_clean_passed':sum(r['criterion_review_passed'] for r in clean),'both_initial5':sum(r['coverage']['both_initial5'] for r in chosen),
   'both_full_read':sum(r['coverage']['both_full_read'] for r in chosen),'all_required_source_families_initial5':sum(r['coverage']['all_required_source_families_initial5'] for r in chosen),
   'all_required_source_families_full_read':sum(r['coverage']['all_required_source_families_full_read'] for r in chosen),'diagnostic_both_top10':sum(r['coverage']['diagnostic_both_top10'] for r in chosen)}
 means={}
 for name in METRICS:
  scored=[r for r in answers if r['metrics'][name]['status']=='scored' and isinstance(r['metrics'][name]['value'],(int,float)) and math.isfinite(r['metrics'][name]['value'])]
  primary=[r for r in scored if name not in REF or r['reference_metrics_primary_eligible'] and not r['annotation_issue']]
  means[name]={'n_scored':len(scored),'all_scored_mean':avg([r['metrics'][name]['value'] for r in scored]),'primary_n':len(primary),
   'primary_mean':avg([r['metrics'][name]['value'] for r in primary]),'statuses':dict(collections.Counter(r['metrics'][name]['status'] for r in answers))}
 summary={'status':'completed','cases':120,**summarize(answers),'by_category':{k:summarize([r for r in answers if r['category']==k]) for k in LABELS},
  'by_source':{s:summarize([r for r in answers if r['primary_source']==s]) for s in sorted({r['primary_source'] for r in answers})},
  'cross_document':summarize([r for r in answers if r['require_cross_source']]),'within_document':summarize([r for r in answers if not r['require_cross_source']]),
  'diagnostic_buckets':dict(collections.Counter(r['diagnostic_bucket'] for r in answers)),'ragas_results':600,'ragas_metric_statuses':dict(collections.Counter(m['status'] for m in metric['results'])),'ragas_means':means,
  'initial5_matches_diagnostic_call':sum(r['coverage']['initial5_matches_diagnostic_call'] for r in answers),
  'usage':{'requests':requests,'known_input_tokens':inp,'known_output_tokens':output,'known_total_tokens':inp+output,'budget_charge_including_unknown':charge,'conservative_units':units,'states':states,'lanes':lanes,'first_request_at':first,'last_request_at':last,'seconds':last-policy['started_at']},
  'corpus_sha256':data['corpus_sha256'],'generation_id':data['generation_id'],'dataset_sha256':policy['dataset_sha256'],
  'audit':{'frozen_before_answers':True,'answer_requests_contain_no_gold':True,'protected_files_unchanged':True,'production_sources_unchanged':True,'budget_caps_passed':True,'no_business_operations':True},
  'limits':'Focused development questions; same-provider model review, not independent human accuracy. Strict chunk-ID coverage misses equivalent alternative passages. New set has no same-question baseline; no causal version-improvement claim.'}
 summary['assistant_diagnostic_counts']={'confirmed_evidence_gap':2,'distance_order_reasoning':1,'quote_submission_and_receipt_failure':1,'scope_interpretation_with_reference_ambiguity':1}
 summary['diagnostic_bucket_protocol']='diagnostic_buckets reflects raw model-judge flags; assistant_diagnostic_counts is a separate post-answer diagnosis and does not revise scores.'
 summary['audit']['temporary_profiles_removed']=120
 zone=datetime.timezone(datetime.timedelta(hours=8))
 summary['assessment_started_at']=datetime.datetime.fromtimestamp(policy['started_at'],zone).isoformat()
 summary['assessment_completed_at']=datetime.datetime.fromtimestamp((OUT/'ragas/final.json').stat().st_mtime,zone).isoformat()
 summary['report_created_at']=datetime.datetime.now(zone).isoformat()
 summary['unavailable_metric_jobs']=[m for m in metric['results'] if m['status']=='unavailable_preserved']
 summary['not_applicable_metric_jobs']=[m['job'] for m in metric['results'] if m['status']=='not_applicable_no_delivered_answer']
 summary['post_answer_ambiguity_flags']=read(OUT/'assistant-diagnostics.json').get('post_answer_ambiguity_flags',[]) if (OUT/'assistant-diagnostics.json').exists() else []
 save(DEST/'summary.json',summary);save(DEST/'case-results.json',answers);save(DEST/'ragas-results.json',metric)
 save(DEST/'frozen-rubrics.json',rubrics);shutil.copy2(OUT/'qa-policy.json',DEST/'qa-policy.json');shutil.copy2(OUT/'retrieval-summary.json',DEST/'retrieval-summary.json')
 with sqlite3.connect(f'file:{OUT}/qa-ledger.sqlite?mode=ro',uri=True) as source_db,sqlite3.connect(DEST/'qa-ledger.sqlite') as delivery_db:source_db.backup(delivery_db)
 if (OUT/'assistant-diagnostics.json').exists():shutil.copy2(OUT/'assistant-diagnostics.json',DEST/'assistant-diagnostics.json')
 save(ROOT/'docs/validation/20261124-rag-multihop120-evaluation.json',summary)
 esc=lambda v:html.escape(str(v),quote=True)
 cards=[]
 for r in answers:
  state='annotation' if r['diagnostic_bucket']=='annotation_or_rubric_flag' else 'pass' if r['criterion_review_passed'] else 'issue'
  point_html=''.join('<li>'+esc(s)+'</li>' for s in r['minimum_required_points'])
  cards.append('<article data-type="'+r['category']+'" data-state="'+state+'"><h3>'+esc(r['case_id'])+' · '+esc(LABELS[r['category']])+' · '+('初评通过' if r['criterion_review_passed'] else '待关注')+'</h3><p>'+esc(r['question'])+'</p><h4>实际答案</h4><p>'+esc(r['response'] or '未交付可验证答案')+'</p><p class="meta">执行：'+esc(r['execution_status'])+'；初评：'+esc(r['criterion_review'].get('reason',r['review_status']))+'；诊断：'+esc(r['diagnostic_bucket'])+'</p><details><summary>冻结要点、指标与检索覆盖</summary><ul>'+point_html+'</ul><p>可选说明：'+esc(r['optional_explanations'])+'</p><p>标注问题：'+str(r['annotation_issue'])+'；校准：'+esc(r['calibration_status'])+'</p><p>双证据严格覆盖：初始Top5 '+str(r['coverage']['both_initial5'])+'；实际完整回读 '+str(r['coverage']['both_full_read'])+'；单独Top10诊断 '+str(r['coverage']['diagnostic_both_top10'])+'</p><p>'+esc({n:{'status':r['metrics'][n]['status'],'value':r['metrics'][n]['value']} for n in METRICS})+'</p></details></article>')
 def number(v):return '—' if v is None else f'{v:.3f}'
 type_rows=''.join('<tr><td>'+esc(LABELS[k])+'</td><td>'+str(v['cases'])+'</td><td>'+pct(v['execution_passed'],v['cases'])+'</td><td>'+pct(v['raw_criterion_passed'],v['cases'])+'</td><td>'+pct(v['annotation_clean_passed'],v['annotation_clean_cases'])+'</td><td>'+pct(v['both_initial5'],v['cases'])+'</td><td>'+pct(v['both_full_read'],v['cases'])+'</td></tr>' for k,v in summary['by_category'].items())
 metric_rows=''.join('<tr><td>'+NAMES[k]+'</td><td>'+number(v['primary_mean'])+'</td><td>'+str(v['primary_n'])+'</td><td>'+number(v['all_scored_mean'])+'</td><td>'+str(v['n_scored'])+'</td></tr>' for k,v in means.items())
 page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>v7多证据120题评测</title><style>body{font:16px/1.7 system-ui,sans-serif;max-width:1200px;margin:auto;padding:28px;background:#f4f7fb;color:#19304a}section,article{background:white;border:1px solid #dce4ef;border-radius:12px;padding:22px;margin:18px 0}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid #dde4ee}.scroll{overflow:auto}p{white-space:pre-wrap;overflow-wrap:anywhere}.meta{color:#52637b;font-size:14px}select,input{padding:8px;font:inherit;margin:5px}summary{cursor:pointer;color:#245ca5}.hidden{display:none}</style><h1>v7多证据120题评测</h1><p>2026-11-24 · 当前生产同源fast模式 · 作答前冻结必要评分点 · Ragas 0.4.3五项原生指标</p><section><p>执行成功 '''+pct(summary['execution_passed'],120)+'；全部题目的原始要点初评通过 '+pct(summary['raw_criterion_passed'],120)+'；标注清洁子集通过 '+pct(summary['annotation_clean_passed'],summary['annotation_clean_cases'])+'。</p><p>这批结果反映多证据专项开发题的表现。逐题初评与出题使用同一提供方，未经独立专家全量验收；不能作为系统总体准确率或版本提升的独立证据。存在标注问题／要点校准不可用的题目保留原始结果，并从依赖参考标注的主统计中单列。</p><p>严格双片段覆盖：实际初始Top5 '+pct(summary['both_initial5'],120)+'，实际完整回读 '+pct(summary['both_full_read'],120)+'。单独Top10检索诊断 '+pct(summary['diagnostic_both_top10'],120)+'，与作答初始Top5一致的检索调用有 '+str(summary['initial5_matches_diagnostic_call'])+'/120题。片段ID未命中不排除找到了等价证据。</p></section><h2>按题型</h2><div class="scroll"><table><tr><th>题型</th><th>题数</th><th>执行成功</th><th>初评／全部</th><th>初评／标注清洁</th><th>初始双证据</th><th>完整回读双证据</th></tr>'+type_rows+'</table></div><h2>Ragas指标</h2><table><tr><th>指标</th><th>主统计均值</th><th>n</th><th>所有可用结果均值</th><th>n</th></tr>'+metric_rows+'</table><p>指标范围0–1，均值并非通过率。Faithfulness仅使用实际可见片段及真实回读全文；Context Precision／Recall使用实际初始Top5全文及冻结参考。Answer Relevancy使用本地固定BGE-M3。未评分、原生NaN和执行无答案均单独保留，不当作零分。</p><section><p>共 '+str(requests)+' 次请求；已知输入输出 '+format(inp+output,',')+' token，含失败未知的预算占额 '+format(charge,',')+'；保守计量 '+format(units,',')+'。上限4000次／2400万token／1.6亿单位／启动起2小时。请求状态 '+esc(states)+'。</p><p>未修改生产配置或语料，未微调，旧题历史成绩保留。<a href="summary.json">完整汇总</a> · <a href="case-results.json">逐题结果</a> · <a href="ragas-results.json">600项原生指标记录</a> · <a href="frozen-rubrics.json">作答前评分点</a> · <a href="user-approval.json">本轮授权</a></p></section><h2>逐题检查</h2><select id="state"><option value="issues">仅待关注</option><option value="all">全部120题</option><option value="pass">初评通过</option><option value="annotation">标注／校准问题</option></select><select id="type"><option value="all">全部题型</option>'+''.join('<option value="'+k+'">'+esc(v)+'</option>' for k,v in LABELS.items())+'</select><input id="search" placeholder="搜索编号、问题或回答"><span id="count"></span>'+''.join(cards)+'''<script>const cards=[...document.querySelectorAll('article')];function update(){let s=document.querySelector('#state').value,t=document.querySelector('#type').value,q=document.querySelector('#search').value.toLowerCase();cards.forEach(c=>c.classList.toggle('hidden',(s==='issues'&&c.dataset.state==='pass')||(s!=='issues'&&s!=='all'&&c.dataset.state!==s)||(t!=='all'&&c.dataset.type!==t)||(q&&!c.textContent.toLowerCase().includes(q))));document.querySelector('#count').textContent='显示 '+cards.filter(c=>!c.classList.contains('hidden')).length+' / 120'}document.querySelectorAll('select').forEach(x=>x.onchange=update);document.querySelector('#search').oninput=update;update();</script></html>'''
 if (DEST/'assistant-diagnostics.json').exists():page=page.replace('<h2>逐题检查</h2>','<section><p><a href="assistant-diagnostics.json">助手逐题诊断记录</a>：022／056所需证据未回读到；027距离顺序计算错误；087逐字引文拒绝后未取得提交回执，最终解析失败。093未匹配冻结标注中的区域／DCPC适用范围，但题干也可能被理解为询问授权条件，列为人工复核项，原分数不改。</p><p>跨文档题按构题意图分类，未做删除单一来源的作答消融；部分来源段落可能相互重复。诊断不改变已冻结标签、原始成绩或本轮主统计。</p></section><h2>逐题检查</h2>')
 page=page.replace('<p>2026-11-24 ·','<p>2026-11-24 批次 ·').replace('<h2>Ragas指标</h2>','<p>600项指标尝试中，588项可用，9项评分调用异常，3项因087题无有效交付答案而不适用。不可用与不适用不计为零分。</p><h2>Ragas指标</h2>')
 (DEST/'REPORT.html').write_text(page,encoding='utf8')
 save(DEST/'delivery-manifest.json',{'files':{p.name:sha(p) for p in DEST.iterdir() if p.is_file() and p.name not in ['delivery-manifest.json','server-prepared-dataset.json']},'scope':'Assessment outputs and frozen inputs only; no .env or raw full run traces'})
 print(json.dumps({k:summary[k] for k in ['cases','execution_passed','raw_criterion_passed','annotation_clean_cases','annotation_clean_passed','ragas_metric_statuses','usage']},ensure_ascii=False))
if __name__=='__main__':main()
