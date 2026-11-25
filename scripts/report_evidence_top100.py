"""Evidence-only diagnostic report from frozen stage rankings."""
import collections,hashlib,html,json,shutil,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'var/validation/20261125-rag-evidence-top100';DEST=ROOT/'output/rag-evidence-top100-20261125'
STAGES={'dense':'纯BGE-M3向量','bm25':'纯BM25词法','rrf_union':'RRF融合（来源加权前）','boosted':'来源加权后（保留200）','candidate100':'送入重排的100条','reranked':'Qwen本地重排后'}
DEPTHS=[5,10,20,50,100]
TYPES={'cross_document':'跨文档综合','cross_chapter':'同文档跨章节','numeric_conditions':'数值与条件','insufficient':'证据不足'}
LOSS={'leaf_recall_miss_200':'三路Top200均未召回','rrf_cut_200':'融合截取200时丢失','source_boost_pushes_out_100':'来源加权挤出100','candidate_cut_100':'200截取100时丢失','reranker_below_delivery_5':'候选池内、重排后排在第5名之后','delivered_5':'已进入最终Top5'}
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 s=read(OUT/'summary.json');plan=read(OUT/'plan.json');rows=read(OUT/'results.json')['cases'];assert s['status']=='completed' and len(rows)==120 and s['evidence_instances']==240
 for p,h in plan['protected_files'].items():assert sha(ROOT/p)==h
 for p,h in plan['source_sha256'].items():assert sha(ROOT/p)==h
 assert s['external_llm_calls']==s['qa_calls']==s['network_connections_attempted']==0
 assert s['candidate100_equals_rerank100_all_cases'] and s['protected_files_unchanged']
 stage_compare={}
 for a,b in [('dense','candidate100'),('candidate100','reranked'),('rrf_union','boosted')]:
  stage_compare[a+'__'+b]={str(k):{'complete_case_gains':[r['case_id'] for r in rows if not r['metrics'][a][str(k)]['all_evidence'] and r['metrics'][b][str(k)]['all_evidence']],
   'complete_case_losses':[r['case_id'] for r in rows if r['metrics'][a][str(k)]['all_evidence'] and not r['metrics'][b][str(k)]['all_evidence']]} for k in DEPTHS}
 gold=[{'case_id':r['case_id'],**g} for r in rows for g in r['gold']]
 trace={'dense_top100_missing_evidence':sum(g['ranks']['dense']>100 for g in gold),'dense_top200_missing_evidence':sum(g['ranks']['dense']>200 for g in gold),
  'dense_top100_misses_recovered_in_pool100':sum(g['ranks']['dense']>100 and g['ranks']['candidate100'] is not None for g in gold),
  'dense_top200_present_but_pool100_absent':sum(g['ranks']['dense']<=200 and g['ranks']['candidate100'] is None for g in gold),
  'pool100_missing_evidence':sum(g['ranks']['candidate100'] is None for g in gold),
  'pool100_present_but_rerank_below5':sum(g['ranks']['candidate100'] is not None and g['ranks']['reranked']>5 for g in gold),
  'reranker_pushes_candidate_top5_evidence_below5':sum(g['ranks']['candidate100'] is not None and g['ranks']['candidate100']<=5 and g['ranks']['reranked']>5 for g in gold),
  'reranker_lifts_candidate_below5_evidence_into5':sum(g['ranks']['candidate100'] is not None and g['ranks']['candidate100']>5 and g['ranks']['reranked']<=5 for g in gold)}
 groups={category:{stage:{str(k):{'cases':len(sub),'complete_cases':sum(r['metrics'][stage][str(k)]['all_evidence'] for r in sub),'evidence_hits':sum(r['metrics'][stage][str(k)]['evidence_hits'] for r in sub)} for k in DEPTHS} for stage in STAGES} for category in TYPES for sub in [[r for r in rows if r['category']==category]]}
 diagnosis={'counts':trace,'stage_gain_loss':stage_compare,'by_category':groups,
  'pool100_miss_cases':[r['case_id'] for r in rows if not r['metrics']['candidate100']['100']['all_evidence']],
  'dense100_miss_cases':[r['case_id'] for r in rows if not r['metrics']['dense']['100']['all_evidence']],
  'source_boost_target_counts':dict(collections.Counter(sid for r in rows for sid in r['inferred_source_boost_ids'])),
  'limits':['Dense misses describe current BGE/query/chunk representation, not a controlled comparison with another embedding model.',
   'Final Top100 contains exactly the reranker input candidates; reranking can change order but cannot recover absent evidence.',
   'Being ranked below5 is not by itself proof the reranker is wrong; compare its gains/losses versus pre-rerank order.',
   'Primary metrics use frozen chunk IDs. Quote-equivalent alternatives require all gold quotes in one same-source/version chunk after whitespace normalization; paraphrases are not detected.'],
  'llm_answer_calls':0,'external_model_calls':0}
 experiments={}
 for filename in ['dense-pool-ablation.json','bm25-df-counterfactual.json']:
  if (OUT/filename).exists():
   experiment=read(OUT/filename);assert experiment['status']=='completed' and experiment['cases']==120
   assert experiment['qa_calls']==experiment['external_llm_calls']==experiment['network_connections_attempted']==0
   assert experiment['input_results_sha256']==sha(OUT/'results.json')
   experiments[filename]={k:v for k,v in experiment.items() if k!='results'}
 diagnosis['controlled_experiments']=experiments
 DEST.mkdir(parents=True,exist_ok=True)
 for n in ['summary.json','plan.json','results.json','embedding-audit.json']:shutil.copy2(OUT/n,DEST/n)
 for n in ['dense-pool-ablation.json','bm25-df-counterfactual.json','bm25-df-audit.json','lexical-token-audit.json']:
  if (OUT/n).exists():shutil.copy2(OUT/n,DEST/n)
 save(DEST/'diagnosis.json',diagnosis)
 esc=lambda x:html.escape(str(x),quote=True)
 def grid(alt=False,micro=False):
  source=s['quote_equivalent_by_stage'] if alt else s['by_stage']
  return ''.join('<tr><th>'+label+'</th>'+''.join('<td>'+str(source[stage][str(k)]['evidence_hits' if micro else 'complete_cases'])+'/'+str(240 if micro else 120)+'</td>' for k in DEPTHS)+'</tr>' for stage,label in STAGES.items())
 head='<tr><th>检索阶段</th>'+''.join('<th>Top'+str(k)+'</th>' for k in DEPTHS)+'</tr>'
 cards=[]
 for r in rows:
  table=''.join('<tr><td>'+esc(g['source_id'])+'<br><code>'+esc(g['chunk_id'])+'</code></td>'+''.join('<td>'+str(g['ranks'][name] if g['ranks'][name] is not None else '—')+'</td>' for name in ['dense','bm25','rrf_union','boosted','candidate100','reranked'])+'<td>'+esc(LOSS[g['loss_at_top5']])+'</td></tr>' for g in r['gold'])
  quotes=''.join('<h4>'+esc(g['source_id'])+' · '+esc(' / '.join(g['section_path']))+'</h4>'+''.join('<blockquote>'+esc(q)+'</blockquote>' for q in g['quotes'])+'<p class="meta">'+esc(json.dumps(g['locator'],ensure_ascii=False))+'</p>' for g in r['gold'])
  densemiss=not r['metrics']['dense']['100']['all_evidence'];poolmiss=not r['metrics']['candidate100']['100']['all_evidence'];miss5=not r['metrics']['reranked']['5']['all_evidence']
  cards.append('<article data-type="'+r['category']+'" data-dense="'+str(densemiss).lower()+'" data-pool="'+str(poolmiss).lower()+'" data-miss5="'+str(miss5).lower()+'"><h3>'+esc(r['case_id'])+' · '+esc(TYPES[r['category']])+'</h3><p>'+esc(r['question'])+'</p><div class="scroll"><table><tr><th>冻结证据</th><th>BGE全库名次</th><th>BM25全库名次</th><th>融合名次</th><th>来源加权名次</th><th>候选100名次</th><th>重排名次</th><th>Top5阶段位置</th></tr>'+table+'</table></div><details><summary>冻结引文与来源</summary>'+quotes+'</details></article>')
 page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Top100证据检索归因</title><style>body{max-width:1350px;margin:auto;padding:28px;font:16px/1.7 system-ui,sans-serif;background:#f4f7fb;color:#1c3048}section,article{background:white;border:1px solid #dbe4ef;border-radius:12px;padding:22px;margin:18px 0}table{width:100%;border-collapse:collapse}th,td{border-bottom:1px solid #dce3ed;text-align:left;padding:9px}code,.meta{font-size:12px;overflow-wrap:anywhere;color:#53627a}blockquote{margin-left:0;border-left:3px solid #4e78aa;padding-left:14px}.scroll{overflow:auto}select,input{font:inherit;padding:8px;margin:5px}.hidden{display:none}summary{cursor:pointer}</style><h1>120题 · Top100证据检索归因</h1><p>2026-11-25 · v7固定语料 · 仅原始题目检索，不作答、不运行Ragas、不调用外部LLM</p><section><p>实际链路：BGE Top200 + BM25 Top200 + 标识符Top200 → RRF融合截取200 → 来源加权 → 截取100 → 本地Qwen相关性重排 → 返回Top5/10。现有HTTP接口只允许返回最多10条；本次离线调用相同固定实现保留完整阶段排序，未改变生产配置。</p><p>统计对象为120题、每题2条冻结证据，共240个“题目—证据片段”关系。两条全部命中才计该题完整覆盖。重排前后Top100集合必然相同，因此只看最终Top100无法单独判定BGE问题。</p><p>外部模型调用0、问答调用0、测试进程网络连接尝试0。生产配置及此前模型调用账本前后哈希不变。</p></section><h2>严格双证据完整覆盖（题数／120）</h2><div class="scroll"><table>'''+head+grid()+'</table></div><h2>逐证据召回（命中／240）</h2><div class="scroll"><table>'+head+grid(micro=True)+'</table></div><section><h2>阶段定位</h2><p>纯BGE Top100遗漏 '+str(trace['dense_top100_missing_evidence'])+' 条证据，其中 '+str(trace['dense_top100_misses_recovered_in_pool100'])+' 条被其他召回／融合补回候选100。最终候选100仍遗漏 '+str(trace['pool100_missing_evidence'])+' 条。</p><p>有 '+str(trace['dense_top200_present_but_pool100_absent'])+' 条已在BGE Top200，但没有进入混合候选100；不能把这些直接归为BGE没召回。有 '+str(trace['pool100_present_but_rerank_below5'])+' 条已经进入候选100，却在最终Top5之外。</p><p>相对重排前Top5，Qwen将 '+str(trace['reranker_pushes_candidate_top5_evidence_below5'])+' 条原本前5的冻结证据排到了5之后，同时将 '+str(trace['reranker_lifts_candidate_below5_evidence_into5'])+' 条原本5之后的证据提入前5。须同时看收益和退化，不能把所有低于5的结果都称为重排错误。</p><p>丢失阶段计数：'+esc(s['loss_counts_top5'])+'</p></section><h2>同来源、同版本的相同引文命中（辅助排查）</h2><div class="scroll"><table>'+head+grid(alt=True)+'</table></div><p>辅助统计要求同一片段包含该条冻结证据的所有引句，仅规范空白，不使用LLM或语义判断，不修改原标注。相同短引句也可能属于不同字段，必须结合上下文核对；此项不构成语义等价认证，亦不计入主统计。</p><section><h2>向量与复现核验</h2><p>全库向量 '+str(s['embedding_audit']['cache_rows'])+'×'+str(s['embedding_audit']['dimensions'])+'，全部归一化；重编码检查全部 '+str(s['embedding_audit']['positive_unique_chunks'])+' 个独立标注片段并加抽样，缓存与重新编码最小余弦 '+format(s['embedding_audit']['cache_vs_reencoded_min_cosine'],'.8f')+'。检查范围内文档截断 '+str(s['embedding_audit']['truncated_audited_documents'])+'，查询截断 '+str(s['query_truncation_count'])+'，重排输入截断 '+str(s['truncated_reranker_pairs'])+'。</p><p>本次Top10与上轮已保存实际生产Top10逐题完全一致：'+str(s['production_top10_parity_cases'])+'/120。缓存一致性不能证明模型质量完美，但可排除本次检查涉及标注片段的缓存错行／陈旧向量等解释。</p><p>当前只诊断固定BGE、题目写法、片段表示与混合排序的组合；没有对比另一个Embedding模型，不能直接推出应替换BGE或微调。<a href="summary.json">汇总</a> · <a href="diagnosis.json">阶段归因与分组</a> · <a href="results.json">每题完整Top200／Top100和冻结证据全局名次</a> · <a href="embedding-audit.json">向量核验</a></p></section><h2>逐题查看</h2><select id="mode"><option value="miss5">最终Top5未完整覆盖</option><option value="pool">候选Top100未完整覆盖</option><option value="dense">BGE Top100未完整覆盖</option><option value="all">全部120题</option></select><select id="type"><option value="all">全部题型</option>'+''.join('<option value="'+k+'">'+esc(v)+'</option>' for k,v in TYPES.items())+'</select><input id="search" placeholder="搜索编号、题目或片段ID"><span id="count"></span>'+''.join(cards)+'''<script>const cards=[...document.querySelectorAll('article')];function update(){let m=document.querySelector('#mode').value,t=document.querySelector('#type').value,q=document.querySelector('#search').value.toLowerCase();cards.forEach(c=>c.classList.toggle('hidden',(m!=='all'&&c.dataset[m]!=='true')||(t!=='all'&&c.dataset.type!==t)||(q&&!c.textContent.toLowerCase().includes(q))));document.querySelector('#count').textContent='显示 '+cards.filter(c=>!c.classList.contains('hidden')).length+' / 120'}document.querySelectorAll('select').forEach(x=>x.onchange=update);document.querySelector('#search').oninput=update;update();</script></html>'''
 extra=''
 if (OUT/'bm25-df-audit.json').exists():
  df=read(OUT/'bm25-df-audit.json')
  extra+='<section><h2>已确认：BM25文档频率统计错误</h2><p><code>BM25Index.__init__</code> 使用 <code>document_frequency.update(frequencies)</code>，累加的是词出现次数，不是含词的文档数。该IDF公式要求按文档计数，应使用唯一词键。当前语料产生 '+str(df['negative_idf_terms'])+' 个负IDF词项，且rank会过滤总分≤0的片段。</p><p>已用3篇文档、每篇重复同词10次复现：当前IDF为-2.0314并返回空排名，按3篇文档计数应为+0.1335。独立实验只纠正内存索引的IDF，不修改生产源文件或配置。<a href="bm25-df-audit.json">全库词频核验</a></p></section>'
 if experiments:
  cells=[]
  if 'dense-pool-ablation.json' in experiments:
   a=experiments['dense-pool-ablation.json'];cells.append('<tr><th>仅改为BGE100候选→同一Qwen</th>'+''.join('<td>'+str(a['by_depth'][str(k)]['complete_cases'])+'/120</td>' for k in DEPTHS)+'</tr>')
  if 'bm25-df-counterfactual.json' in experiments:
   a=experiments['bm25-df-counterfactual.json']
   for stage,label in [('corrected_bm25','只纠正DF后的BM25'),('corrected_candidate100','只纠正DF后的混合候选'),('corrected_reranked','只纠正DF后→同一Qwen')]:cells.append('<tr><th>'+label+'</th>'+''.join('<td>'+str(a['by_stage'][stage][str(k)]['complete_cases'])+'/120</td>' for k in DEPTHS)+'</tr>')
   paired=a['paired_against_production'];extra+='<p>只纠正DF的逐题对照：Top5新增 '+esc(paired['5']['gained_cases'])+'，退化 '+esc(paired['5']['lost_cases'])+'；Top10新增 '+esc(paired['10']['gained_cases'])+'，退化 '+esc(paired['10']['lost_cases'])+'。修复实现错误不保证所有题目排序单调改善。</p>'
  extra+='<section><h2>本地受控证据对照</h2><p>只改变所说明的候选来源或DF统计，题目、语料、重排模型权重、提示、batch=4及2048长度限制保持不变。全部仅做相关性前向打分，没有文本作答或外部LLM调用；生产未切换。</p><div class="scroll"><table>'+head+''.join(cells)+'</table></div><p>逐题增益与退化清单见 <a href="diagnosis.json">阶段归因及受控实验记录</a>；候选集合改变也会改变并列分数的排序环境，不能将任何单项增益直接视为真实问答准确率提升。</p></section>'
 examples=[]
 for case_id in ['multihop120-022','multihop120-056','multihop120-027','multihop120-087','multihop120-093']:
  matches=[r for r in rows if r['case_id'].endswith(case_id[-3:])]
  if len(matches)!=1:continue
  r=matches[0]
  examples.append('<tr><th>'+esc(r['case_id'])+'</th><td>'+('完整' if r['metrics']['reranked']['5']['all_evidence'] else '未完整')+'</td><td>'+esc('；'.join('BGE '+str(g['ranks']['dense'])+' → 融合 '+str(g['ranks']['rrf_union'])+' → 重排 '+str(g['ranks']['reranked']) for g in r['gold']))+'</td></tr>')
 extra+='<section><h2>上轮重点题的证据位置</h2><table><tr><th>题号</th><th>Top5双证据</th><th>两条证据的逐阶段名次</th></tr>'+''.join(examples)+'</table><p>022的指定段落在BGE第192名，融合后落到第331名，截取200时丢失；056的PHAK证据在BGE第3名、候选第14名，但Qwen重排第33名。027和087的既有问题发生在证据取得之后，本轮只检索，不验证推理或交付问题是否解决。093保留原冻结证据口径，题意复核不改历史分数。</p></section>'
 extra+='<section><h2>后续修复顺序</h2><ol><li>纠正BM25的DF统计并重建词法索引；保留重复词最小复现，回归全部120题及旧题，不能只凭本批均值上线。</li><li>对照候选池策略，避免BGE已命中的证据被RRF截取与来源加权挤出；优先比较保留向量召回配额、融合权重及来源规则，不直接用本题集拟合参数。</li><li>检查056等局部重排退化，增加逐来源／子问题证据覆盖和显式文档章节定位；Qwen整体提高Top5覆盖，不应因个别漏项直接移除。</li><li>剩余BGE Top100外的5条证据再逐条检查题目表示、章节和分块，随后用独立题集对照其他Embedding模型，决定是否需要替换或微调。</li></ol><p>本轮交付是检索诊断与受控证据实验，未修改生产BM25源文件、配置、语料或模型；检索证据覆盖率不等于问答正确率。</p></section>'
 page=page.replace('<h2>逐题查看</h2>',extra+'<h2>逐题查看</h2>')
 (DEST/'REPORT.html').write_text(page,encoding='utf8');save(DEST/'manifest.json',{'files':{p.name:sha(p) for p in DEST.iterdir() if p.is_file() and p.name!='manifest.json'},'answers_generated':0,'external_llm_calls':0})
 save(ROOT/'docs/validation/20261125-rag-evidence-top100.json',{'summary':s,'diagnosis':diagnosis,'report':'output/rag-evidence-top100-20261125/REPORT.html'})
 print(json.dumps({'status':'reported','cases':120,'counts':trace,'top100_complete':{k:s['by_stage'][k]['100']['complete_cases'] for k in STAGES},'external_llm_calls':0},ensure_ascii=False))
if __name__=='__main__':main()
