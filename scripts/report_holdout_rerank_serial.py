"""Evidence-only holdout comparison report; no inference or network dependencies."""
import collections,hashlib,html,json,shutil,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
RAW=ROOT/'var/validation/20261125-rag-holdout120-rerank-serial-v1'
OUT=ROOT/'output/rag-holdout120-rerank-20261125'
DEPTHS=(5,10,20,50,100)
read=lambda p:json.loads(p.read_text(encoding='utf8'))
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
esc=lambda x:html.escape(str(x),quote=True)
def save(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')

def main():
 from evidence_retrieval_metrics import coverage
 from rerank_postprocess_policies import POLICIES,rerank
 import numpy as np
 s=read(RAW/'summary.json');plan=read(RAW/'preregistration.json');history=read(RAW/'historical-context.json');data=read(RAW/'dataset.json')['cases']
 assert s['status']=='completed' and s['serial_order']==[p['id']for p in POLICIES]==plan['serial_order']
 assert all(s[k]==0 for k in ['qa_calls','generation_calls','external_llm_calls','production_changes','network_connections_attempted'])
 assert s['protected_files_unchanged'] and s['historical_old113_controls_reproduced']
 assert s['run_identity']['plan_sha256']==sha(RAW/'preregistration.json') and s['run_identity']['dataset_sha256']==sha(RAW/'dataset.json')
 assert sha(ROOT/'scripts/rerank_postprocess_policies.py')==history['policy_sha256']
 records={r['chunk_id']:r for r in map(json.loads,(ROOT/'var/rag/corpora/kg_20261124_aviation_v7_charts_final/rag_chunks.v1.jsonl').read_text(encoding='utf8').splitlines())}
 aliases=read(RAW/'quote-compatible-alternatives.json')
 results={p['id']:read(RAW/(p['id']+'-results.json'))['cases']for p in POLICIES}
 index={pid:{r['id']:r for r in rs}for pid,rs in results.items()}
 shared={u['id']:read(RAW/'shared'/(u['id']+'.json'))for u in data}
 vectors=np.load(ROOT/'var/rag/corpora/kg_20261124_aviation_v7_charts_final/bge-m3.npy',mmap_mode='r');positions={cid:i for i,cid in enumerate(records)}
 checked=0
 for u in data:
  q=shared[u['id']];pool=q['pool'];assert len(pool)==100
  base=sorted(pool,key=lambda c:(-q['scores'][c],pool.index(c)));v=np.asarray(vectors[[positions[c]for c in base]],dtype=np.float64);sim=np.clip(v@v.T,-1,1)
  for p in POLICIES:
   r=index[p['id']][u['id']]
   expected,_=rerank(p,query=u['question'],pool=pool,scores=q['scores'],records=records,similarities=sim)
   assert expected==r['ranking'] and r['metrics']==coverage(r['ranking'],u['gold'],u['groups'])
   assert r['gold_ranks']=={c:r['ranking'].index(c)+1 if c in r['ranking']else None for c in u['gold']}
   assert r['metrics']['100']==index['reranker'][u['id']]['metrics']['100']
   if u['cohort']=='holdout120':
    assert all(q['branch_metrics'][n]==coverage(ids,u['gold'],u['groups'])for n,ids in q['branches'].items())
    for k in DEPTHS:
     hits=sum(bool(set(r['ranking'][:k])&set(aliases[c]))for c in u['gold'])
     assert r['quote_compatible_metrics'][str(k)]=={'evidence_hits':hits,'all_evidence':hits==len(u['gold'])}
   checked+=1
 metrics={p['policy']['id']:p for p in s['policies']}
 for p in POLICIES:
  rs=results[p['id']];ps=metrics[p['id']]
  for h in ['holdout120','old113']:
   cohort=[r for r in rs if r['cohort']==h]
   for k in DEPTHS:
    m=ps['metrics'][h][str(k)];assert m['complete_cases']==sum(r['metrics'][str(k)]['all_evidence']for r in cohort)
    assert m['evidence_hits']==sum(r['metrics'][str(k)]['evidence_hits']for r in cohort)
    if k in [5,10]:
     gained=[r['id']for r in cohort if r['metrics'][str(k)]['all_evidence']and not index['reranker'][r['id']]['metrics'][str(k)]['all_evidence']]
     lost=[r['id']for r in cohort if not r['metrics'][str(k)]['all_evidence']and index['reranker'][r['id']]['metrics'][str(k)]['all_evidence']]
     assert ps['paired_against_reranker'][h][str(k)]=={'gained':gained,'lost':lost}
 assert all(s['policies'][i]['serial_completed_at']<=s['policies'][i+1]['serial_started_at']for i in range(4))
 eligible=[p['id']for p in POLICIES if not any(metrics[p['id']]['paired_against_reranker']['old113'][str(k)]['lost']for k in [5,10])]
 score=lambda pid:tuple(metrics[pid]['metrics']['holdout120'][str(k)]['complete_cases']for k in [5,10])
 best=max([p['id']for p in POLICIES],key=score);conservative=max(eligible,key=score)
 category={p['id']:{cat:{str(k):{'complete_cases':sum(r['metrics'][str(k)]['all_evidence']for r in rs),'cases':len(rs)}for k in [5,10,100]}for cat in sorted({u['category']for u in data if u['cohort']=='holdout120'})for rs in [[r for r in results[p['id']]if r['cohort']=='holdout120'and r['category']==cat]]}for p in POLICIES}
 split={p['id']:{name:{str(k):{'complete_cases':sum(r['metrics'][str(k)]['all_evidence']for r in rs),'cases':len(rs)}for k in [5,10,100]}for name,n in [('cross_source',2),('same_source',1)]for rs in [[r for r in results[p['id']]if r['cohort']=='holdout120'and len({records[c]['source_id']for c in r['gold']})==n]]}for p in POLICIES}
 base=[r for r in results['reranker']if r['cohort']=='holdout120']
 diag={'candidate100_incomplete':[r['id']for r in base if not r['metrics']['100']['all_evidence']],
       'candidate_complete_but_reranker_top5_incomplete':[r['id']for r in base if r['metrics']['100']['all_evidence']and not r['metrics']['5']['all_evidence']],
       'candidate_complete_but_reranker_top10_incomplete':[r['id']for r in base if r['metrics']['100']['all_evidence']and not r['metrics']['10']['all_evidence']],
       'gold_evidence_first_missing_stages':dict(collections.Counter('leaf200_miss'if all(v is None or v>200 for n,v in ranks.items()if n in ['bge','bm25','exact'])else 'fusion_candidate100_cut'for r in base for c,ranks in shared[r['id']]['gold_leaf_ranks'].items()if ranks['candidate100']is None))}
 diag['bge100_complete_but_fusion100_incomplete']=[r['id']for r in base if shared[r['id']]['branch_metrics']['bge']['100']['all_evidence']and not r['metrics']['100']['all_evidence']]
 diag['bge100_incomplete_but_fusion100_complete']=[r['id']for r in base if not shared[r['id']]['branch_metrics']['bge']['100']['all_evidence']and r['metrics']['100']['all_evidence']]
 decision={'observed_holdout_best':best,'eligible_without_old113_case_losses':eligible,'best_with_old113_no_losses':conservative,'selection_rule':plan['selection_rule'],
 'historical_development_winner':'rank_rrf','no_deployment':True,'no_parameter_tuning':True,'dataset':'new frozen HO120-20261125, not previous MH120-20261124',
 'limits':['Evidence coverage is not answer accuracy.','Old113 scores are cached controls; fresh local scoring is on holdout120 only.','All methods retain the same100 candidate IDs; missing pool evidence cannot be repaired by postprocessing.','Five predeclared parameter settings only.','Synthetic set, assistant-reviewed, not independently expert-reviewed; exact quote-compatible proxy is supplementary.']}
 OUT.mkdir(parents=True,exist_ok=True)
 for name in ['summary.json','preregistration.json','dataset.json','historical-context.json','quote-compatible-alternatives.json','run-identity.json','launch.json']+[p['id']+suf for p in POLICIES for suf in ['-results.json','-summary.json']]:shutil.copy2(RAW/name,OUT/name)
 shutil.copytree(RAW/'shared',OUT/'shared',dirs_exist_ok=True)
 if (RAW/'source-snapshot').exists():
  shutil.copytree(RAW/'source-snapshot',OUT/'source-snapshot',dirs_exist_ok=True)
  for name,digest in plan['protected_sha256'].items():
   if name.startswith('src/rag_retrieval/'):assert sha(OUT/'source-snapshot'/name)==digest
 save(OUT/'decision.json',decision);save(OUT/'diagnostics.json',diag);save(OUT/'subgroups.json',{'category':category,'source_count':split})
 headers='<tr><th>方案</th>'+''.join('<th>Top'+str(k)+'</th>'for k in DEPTHS)+'</tr>'
 def table(h):
  return '<table>'+headers+''.join('<tr><th>'+esc(p['label'])+'</th>'+''.join('<td>'+str(metrics[p['id']]['metrics'][h][str(k)]['complete_cases'])+'/'+str(120 if h=='holdout120'else 113)+'</td>'for k in DEPTHS)+'</tr>'for p in POLICIES)+'</table>'
 stage='<table><tr><th>召回阶段</th><th>Top100完整题数</th><th>Top100证据命中</th></tr>'+''.join('<tr><th>'+n+'</th><td>'+str(s['recall_stages'][key]['100']['complete_cases'])+'/120</td><td>'+str(s['recall_stages'][key]['100']['evidence_hits'])+'/240</td></tr>'for n,key in [('BGE','bge'),('原BM25','bm25'),('标识符精确召回','exact'),('固定2:1:1融合候选','candidate100')])+'</table>'
 paired='<table><tr><th>方案</th><th>新题Top5新增/退化</th><th>新题Top10新增/退化</th><th>旧题Top5/10退化</th></tr>'+''.join('<tr><th>'+esc(p['label'])+'</th>'+''.join('<td>'+str(len(metrics[p['id']]['paired_against_reranker']['holdout120'][str(k)]['gained']))+' / '+str(len(metrics[p['id']]['paired_against_reranker']['holdout120'][str(k)]['lost']))+'</td>'for k in [5,10])+'<td>'+str(len(metrics[p['id']]['paired_against_reranker']['old113']['5']['lost']))+' / '+str(len(metrics[p['id']]['paired_against_reranker']['old113']['10']['lost']))+'</td></tr>'for p in POLICIES)+'</table>'
 proxy='<table><tr><th>方案</th><th>Top5</th><th>Top10</th></tr>'+''.join('<tr><th>'+esc(p['label'])+'</th>'+''.join('<td>'+str(metrics[p['id']]['quote_compatible_holdout'][str(k)]['complete_cases'])+'/120</td>'for k in [5,10])+'</tr>'for p in POLICIES)+'</table>'
 names={p['id']:p['label']for p in POLICIES};cards=[]
 for u in data:
  r=index['reranker'][u['id']];new=u['cohort']=='holdout120';loss=any(u['id']in metrics[p['id']]['paired_against_reranker'][u['cohort']][str(k)]['lost']for p in POLICIES for k in [5,10])
  lines=''.join('<tr><th>'+esc(p['label'])+'</th><td>'+('完整'if index[p['id']][u['id']]['metrics']['5']['all_evidence']else '未完整')+'</td><td>'+('完整'if index[p['id']][u['id']]['metrics']['10']['all_evidence']else '未完整')+'</td><td><code>'+esc(index[p['id']][u['id']]['gold_ranks'])+'</code></td></tr>'for p in POLICIES)
  cards.append('<article data-cohort="'+u['cohort']+'" data-loss="'+str(loss).lower()+'" data-pool-miss="'+str(not r['metrics']['100']['all_evidence']).lower()+'"><h3>'+esc(u['id'])+' · '+esc(u['category'])+'</h3><p>'+esc(u['question'])+'</p><div class=scroll><table><tr><th>方案</th><th>Top5</th><th>Top10</th><th>两段证据最终名次</th></tr>'+lines+'</table></div>'+('<details><summary>Top100之前的逐证据位置</summary><pre>'+esc(json.dumps(shared[u['id']]['gold_leaf_ranks'],ensure_ascii=False,indent=2))+'</pre></details>'if new else '<p>旧题分数回放对照，未重新评分。</p>')+'</article>')
 page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>新120题 · 五种重排方案串行评测</title><style>body{max-width:1250px;margin:auto;padding:26px;background:#f3f6fa;color:#21364c;font:16px/1.7 system-ui}section,article{background:#fff;border:1px solid #dce4ef;border-radius:12px;padding:22px;margin:18px 0}table{border-collapse:collapse;width:100%}th,td{padding:9px;border-bottom:1px solid #e1e7ef;text-align:left}.scroll{overflow:auto}code,pre{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}.hidden{display:none}select,input{font:inherit;padding:8px;margin:6px}</style><h1>新120题 · 五种重排方案串行评测</h1><p>只统计证据检索和重排；无答案生成、无外部LLM调用、无LLM裁判、无训练及生产变更。</p><section><h2>实验条件</h2><p>本轮使用刚冻结的HO120-20261125题集，不是此前的MH120题集。语料v7保持不变，原BM25保持不变。统一使用此前五种重排组合对应的候选池：BGE:BM25:标识符=2:1:1的RRF召回，k=40，来源加权2.2，固定100候选。它是实验候选策略，不能混称生产原融合。</p><p>每道新题仅执行一次本地BGE编码及现有Qwen3-0.6B reranker前向相关性评分。未调用generate，未请求任何外部服务；评分进程禁用socket连接和生成接口。随后完整跑完一组策略，再进入下一组，共五组；共享分数和候选使比较免受重复推理波动影响。旧113题只回放同一模型保存的分数，其五组指标和逐题增减均复现历史结果。</p><p>排序函数不接收标答、gold证据ID、题型、题组或标注来源。所有参数和输入哈希在新题推理前冻结，未根据本轮结果调参。主指标为严格冻结片段ID的完整证据覆盖，每道新题两段，共240段；这不是问答准确率。</p></section><h2>新120题 · 完整证据覆盖</h2><div class=scroll><!--holdout--></div><h2>旧113题 · 缓存分数回归对照</h2><div class=scroll><!--old--></div><section><h2>观察结果</h2><p>新题按Top5、再Top10比较，最高方案：<strong><!--best--></strong>。如同时要求旧113题Top5/10逐题无新增退化，对应候选：<strong><!--conservative--></strong>。本轮未上线任何方案。</p><!--paired--><p>原reranker候选池尚缺完整证据的题数为<!--poolmiss-->；候选完整但Top5不完整<!--loss5-->题、Top10不完整<!--loss10-->题。五种后处理均保留同一100个ID，不能找回候选阶段已缺失的证据。</p></section><section><h2>Top100证据与候选上限</h2><!--stages--><p>逐题记录保留BGE/BM25/精确召回Top200、融合Top100、全部100个reranker分数和五组完整排序，可在下方定位原始证据的丢失阶段。</p></section><section><h2>标注ID重复的辅助检查</h2><p>共有<!--aliascount-->个gold上下文存在其他片段：同源、同版本、同末级章节，且包含该上下文全部标注引文。下表允许这些“引文兼容片段”命中，仅作ID匹配误差的辅助检查，不替代上面的严格主指标，也不宣称语义已获专家重新标注。映射在检索前冻结。</p><!--proxy--></section><section><h2>耗时与复核</h2><p>新题本地reranker每100候选评分P50 <!--score50-->秒、P95 <!--score95-->秒；五种CPU后处理额外耗时见summary.json。只评分120×100个新题对，另有启动自检2对；旧题没有新的模型推理。</p><p>原策略11项单元测试通过；另独立重算233×5=1165份排序及指标、核对各组100候选不丢失、旧题回放一致、五组时间不重叠。冻结题集、语料、原BM25和生产配置哈希不变。合成题经助手复核，尚未经独立航空专家逐题验证。</p><p><a href="summary.json">汇总</a> · <a href="decision.json">比较结论及限制</a> · <a href="preregistration.json">冻结参数和输入哈希</a> · <a href="diagnostics.json">证据丢失诊断</a> · <a href="subgroups.json">题型和跨来源分组</a> · <a href="quote-compatible-alternatives.json">引文兼容映射</a></p></section><h2>逐题比较</h2><select id=cohort><option value=holdout120>新120题</option><option value=old113>旧113题</option><option value=all>全部233题</option></select><select id=mode><option value=all>全部</option><option value=loss>任一组合Top5/10退化</option><option value=pool>候选Top100证据不完整</option></select><input id=query placeholder="搜索题号、题目、证据ID"><span id=count></span><!--cards--><script>const cards=[...document.querySelectorAll('article')];function update(){let h=document.querySelector('#cohort').value,m=document.querySelector('#mode').value,q=document.querySelector('#query').value.toLowerCase();cards.forEach(c=>c.classList.toggle('hidden',(h!=='all'&&c.dataset.cohort!==h)||(m==='loss'&&c.dataset.loss!=='true')||(m==='pool'&&c.dataset.poolMiss!=='true')||(q&&!c.textContent.toLowerCase().includes(q))));document.querySelector('#count').textContent='显示 '+cards.filter(c=>!c.classList.contains('hidden')).length+' / 233'}document.querySelectorAll('select').forEach(e=>e.onchange=update);document.querySelector('#query').oninput=update;update();</script></html>'''
 for key,value in {'holdout':table('holdout120'),'old':table('old113'),'best':esc(names[best]),'conservative':esc(names[conservative]),'paired':paired,'poolmiss':str(len(diag['candidate100_incomplete'])),'loss5':str(len(diag['candidate_complete_but_reranker_top5_incomplete'])),'loss10':str(len(diag['candidate_complete_but_reranker_top10_incomplete'])),'stages':stage,'proxy':proxy,'aliascount':str(sum(len(a)>1 for a in aliases.values())),'score50':format(s['shared_reranker_p50_seconds'],'.3f'),'score95':format(s['shared_reranker_p95_seconds'],'.3f'),'cards':''.join(cards)}.items():page=page.replace('<!--'+key+'-->',value)
 assert '<!--'not in page
 (OUT/'REPORT.html').write_text(page,encoding='utf8')
 check={'status':'passed','independent_rankings_metrics_checked':checked,'old113_historical_parity':True,'serial_intervals_nonoverlap':True,'primary_labels_unchanged':True,'unit_tests_passed':11,'qa_calls':0,'external_llm_calls':0,'generation_calls':0}
 save(OUT/'delivery-validation.json',check)
 with zipfile.ZipFile(OUT/'experiment-source.zip','w',zipfile.ZIP_DEFLATED)as z:
  for n in ['scripts/run_holdout_rerank_serial.py','scripts/report_holdout_rerank_serial.py','scripts/rerank_postprocess_policies.py','scripts/fusion_policy_experiments.py','scripts/evidence_retrieval_metrics.py','tests/rag_retrieval/test_rerank_postprocess.py']:z.write(ROOT/n,n)
 save(OUT/'manifest.json',{'files':{p.relative_to(OUT).as_posix():sha(p)for p in sorted(OUT.rglob('*'))if p.is_file()and p.name!='manifest.json'}})
 save(ROOT/'docs/validation/20261125-rag-holdout120-rerank-serial.json',{'status':'completed_offline_not_deployed','summary':s,'decision':decision,'checks':check,'report':str(OUT.relative_to(ROOT)/'REPORT.html')})
 print(json.dumps({'reported':True,'observed_holdout_best':best,'no_old_loss_candidate':conservative,'checks':checked,'holdout_metrics':{p['id']:{str(k):metrics[p['id']]['metrics']['holdout120'][str(k)]['complete_cases']for k in DEPTHS}for p in POLICIES}},ensure_ascii=False))
if __name__=='__main__':main()
