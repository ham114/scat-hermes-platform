"""Evidence-only report of frozen-score post-reranker comparisons."""
import hashlib
import html
import json
import shutil
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261125-rag-rerank-postprocess-v1'
DEST=ROOT/'output/rag-rerank-postprocess-20261125'
DEPTHS=(5,10,20,50,100)


def read(p):return json.loads(p.read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


def main():
    s=read(OUT/'summary.json');plan=read(OUT/'preregistration.json')
    assert s['status']=='completed' and s['new_model_inferences']==s['qa_calls']==s['external_llm_calls']==s['network_connections_attempted']==s['production_changes']==0
    for name,h in plan['protected_sha256'].items():assert sha(ROOT/name)==h,name
    policies=s['policies'];byid={p['policy']['id']:p for p in policies}
    results={pid:read(OUT/(pid+'-results.json'))['cases'] for pid in s['serial_order']}
    eligible=[p['policy']['id'] for p in policies if not any(p['paired_against_reranker']['old113'][str(k)]['lost'] for k in (5,10))]
    winner=max(eligible,key=lambda pid:(byid[pid]['metrics']['new120']['5']['complete_cases'],byid[pid]['metrics']['new120']['10']['complete_cases']))
    decision={'preferred_by_preregistered_gate':winner,'eligible_without_old_case_losses':eligible,
              'ranking_rule':'zero old113 Top5/10 case regressions, then new120 Top5 and Top10',
              'no_production_deployment':True,'no_new_model_inference':True,
              'recommendation':'Retain current reranker and blend its rank with the frozen retrieval rank using RRF weights4:1 and k20. Validate on independent queries before default deployment.',
              'limits':['This is frozen-score CPU replay on development/regression sets, not fresh end-to-end retrieval or answer accuracy.',
                        'The candidate pools are the weighted2 experimental retrieval pools, not current production pools.',
                        'All methods retain the same100 IDs; missing candidate evidence cannot be recovered.',
                        'MMR and source rules were tested at one predefined parameter setting only; failures do not rule out every variant.']}
    DEST.mkdir(parents=True,exist_ok=True)
    for name in ['summary.json','preregistration.json']+[pid+suffix for pid in s['serial_order'] for suffix in ['-results.json','-summary.json']]:shutil.copy2(OUT/name,DEST/name)
    save(DEST/'decision.json',decision)
    with zipfile.ZipFile(DEST/'experiment-source.zip','w',zipfile.ZIP_DEFLATED) as z:
        names=['scripts/rerank_postprocess_policies.py','scripts/run_rerank_postprocess_eval.py',
               'scripts/report_rerank_postprocess_eval.py','scripts/evidence_retrieval_metrics.py',
               'tests/rag_retrieval/test_rerank_postprocess.py']
        for name in names:z.write(ROOT/name,name)
        z.writestr('manifest.json',json.dumps({'files':{n:sha(ROOT/n) for n in names}},indent=2))
    esc=lambda x:html.escape(str(x),quote=True)
    header='<tr><th>方案</th>'+''.join('<th>Top'+str(k)+'</th>' for k in DEPTHS)+'</tr>'
    def table(cohort):
        return '<table>'+header+''.join('<tr><th>'+esc(p['policy']['label'])+'</th>'+''.join('<td>'+str(p['metrics'][cohort][str(k)]['complete_cases'])+'/'+str(120 if cohort=='new120' else 113)+'</td>' for k in DEPTHS)+'</tr>' for p in policies)+'</table>'
    changes=''.join('<section><h3>'+esc(p['policy']['label'])+'</h3>'+''.join('<p>'+h+' Top'+str(k)+' 新增：'+esc(p['paired_against_reranker'][h][str(k)]['gained'])+'；退化：'+esc(p['paired_against_reranker'][h][str(k)]['lost'])+'</p>' for h in ['new120','old113'] for k in (5,10))+'</section>' for p in policies[1:])
    latency='<table><tr><th>方案</th><th>题组</th><th>P50毫秒</th><th>P95毫秒</th></tr>'+''.join('<tr><th>'+esc(p['policy']['label'])+'</th><td>'+h+'</td><td>'+format(v['p50_ms'],'.3f')+'</td><td>'+format(v['p95_ms'],'.3f')+'</td></tr>' for p in policies for h,v in p['latency_cpu_postprocess'].items())+'</table>'
    cards=[]
    for i,base in enumerate(results['reranker']):
        lines=[]
        for p in policies:
            r=results[p['policy']['id']][i];assert r['id']==base['id']
            lines.append('<tr><th>'+esc(p['policy']['label'])+'</th><td>'+('完整' if r['metrics']['5']['all_evidence'] else '未完整')+'</td><td>'+('完整' if r['metrics']['10']['all_evidence'] else '未完整')+'</td><td><code>'+esc(r['gold_ranks'])+'</code></td></tr>')
        loss=any(base['id'] in p['paired_against_reranker'][base['cohort']][str(k)]['lost'] for p in policies[1:] for k in (5,10))
        cards.append('<article data-cohort="'+base['cohort']+'" data-loss="'+str(loss).lower()+'"><h3>'+esc(base['id'])+' · '+base['cohort']+'</h3><p>'+esc(base['question'])+'</p><div class=scroll><table><tr><th>方案</th><th>Top5</th><th>Top10</th><th>冻结证据最终名次</th></tr>'+''.join(lines)+'</table></div></article>')
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>零LLM调用重排组合串行测试</title><style>body{max-width:1350px;margin:auto;padding:26px;background:#f4f7fb;color:#20354a;font:16px/1.7 system-ui}section,article{background:white;border:1px solid #dce5ef;border-radius:12px;padding:22px;margin:18px 0}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid #dce4ee}code{font-size:12px;overflow-wrap:anywhere}.scroll{overflow:auto}.hidden{display:none}input,select{font:inherit;padding:8px;margin:6px}</style>
<h1>现有reranker＋后处理 · 五组串行测试</h1><p>2026-11-25 · 新120题与旧113题分开计分 · 完全本地CPU回放 · 新模型推理0、LLM调用0、作答0、生产变更0</p>
<section><h2>固定实验条件</h2><p>固定上一轮BGE权重2的RRF检索100候选，复用当时233题完整reranker分数。该候选方案尚未上线；本轮基线为它对应的79/97新题覆盖，不是生产原融合的76/92。所有方案使用完全相同的候选片段与分数，未重新检索、重新编码或调用reranker，未载入Torch、Transformers、OpenAI或Ragas。网络在进程内禁止。</p><p>排序函数仅接收题目、候选、既有分数、文档来源及缓存向量相似度，不接收标答、题号、题型、题组或标注来源。参数和代码哈希在查看结果前冻结。五组按顺序串行运行，没有根据结果调参。</p><ol><li>原reranker分数排序，保留原候选顺序作为同分规则。</li><li>排名RRF：4/(20＋reranker名次)＋1/(20＋召回名次)。融合排名而非直接相加不同量纲的原始分数。</li><li>MMR：λ=0.85，保护原Top1，候选需达到最高reranker分数的20%；对已选片段的最大正余弦相似度进行惩罚。</li><li>来源软限额：保护Top1，在reranker前30中优先每来源最多2条进入Top5，沿用20%相对分数门槛；不足则按原排序补位，不强凑来源。</li><li>显式来源保护＋MMR：仅当问题明确提及至少两份登记文档时，最多保护3份来源的最高分候选，仍需通过分数门槛，再接相同MMR；不通过标注识别所需来源。</li></ol></section>
<h2>重排后完整证据覆盖 · 新120题</h2><div class=scroll><!--new--></div><h2>重排后完整证据覆盖 · 旧113题</h2><div class=scroll><!--old--></div>
<section><h2>结论</h2><p>按旧题Top5/10逐题无退化、再优先新题Top5/10的预设门槛，推荐：<strong><!--winner--></strong>。本轮没有微调或部署。</p><p>RRF组合新题Top5新增7题、退化014题，净增6题；Top10新增007与094，无退化。旧题Top5补回110，无退化；Top10不变。014仍在Top10，具体名次见逐题表。有限重排策略已有收益，暂不需要以微调作为第一步。</p><p>MMR本设定下使20道原Top5完整新题退化，仅补回7题；旧题Top5与Top10各退化3题。来源软限额旧题总数不变，但有1增1减，不能视为逐题零退化。显式来源保护实际新增保留仅3题，整体指标与MMR一致，不能据此判定所有来源规则无效。以上是预定义参数的结果，不对全部MMR或多样性方法作一般结论。</p><p>候选池完全相同，故各方案Top100始终为新114/120、旧113/113。缺失的候选证据无法通过重排补回，不能将所有损失归为reranker模型问题。这里统计严格冻结ID覆盖，不是回答准确率；两批均为已使用的开发／回归集，仍需独立样本验证。</p></section>
<h2>逐题新增与退化</h2><!--changes-->
<section><h2>CPU后处理耗时</h2><p>仅本机缓存回放的排序计算，不含原reranker打分、共享相似度矩阵、文件读取、服务排队。共享相似度计算每题P50 <!--sim50-->ms、P95 <!--sim95-->ms；RRF组合不需要相似度矩阵。不能把下表当作完整RAG或线上响应耗时。</p><!--latency--></section>
<section><h2>复核材料</h2><p>11项策略测试通过，缓存向量哈希、归一化、原分数与排序一致性检查通过。原BM25、生产配置、语料及历史结果哈希不变。未调用模型、未生成答案、未使用标注驱动排序。</p><p><a href="summary.json">汇总</a> · <a href="decision.json">选择门槛与限制</a> · <a href="preregistration.json">冻结方案及参数</a> · <a href="experiment-source.zip">算法、执行代码与测试</a></p><p><!--links--></p></section>
<h2>逐题对照</h2><select id=cohort><option value=all>全部题组</option><option value=new120>新120题</option><option value=old113>旧113题</option></select><select id=mode><option value=all>全部题目</option><option value=loss>任一方案Top5/10退化</option></select><input id=search placeholder="搜索题号、题目或片段ID"><span id=count></span><!--cards-->
<script>const cards=[...document.querySelectorAll('article')];function update(){let h=document.querySelector('#cohort').value,m=document.querySelector('#mode').value,q=document.querySelector('#search').value.toLowerCase();cards.forEach(c=>c.classList.toggle('hidden',(h!=='all'&&c.dataset.cohort!==h)||(m==='loss'&&c.dataset.loss!=='true')||(q&&!c.textContent.toLowerCase().includes(q))));document.querySelector('#count').textContent='显示 '+cards.filter(c=>!c.classList.contains('hidden')).length+' / 233'}document.querySelectorAll('select').forEach(s=>s.onchange=update);document.querySelector('#search').oninput=update;update();</script></html>'''
    for key,value in {'new':table('new120'),'old':table('old113'),'winner':esc(byid[winner]['policy']['label']),
                      'changes':changes,'latency':latency,'sim50':format(s['shared_similarity_p50_ms'],'.3f'),
                      'sim95':format(s['shared_similarity_p95_ms'],'.3f'),'cards':''.join(cards),
                      'links':' · '.join('<a href="'+p['policy']['id']+'-results.json">'+esc(p['policy']['label'])+'完整排序</a>' for p in policies)}.items():page=page.replace('<!--'+key+'-->',value)
    (DEST/'REPORT.html').write_text(page,encoding='utf8')
    save(DEST/'manifest.json',{'files':{p.name:sha(p) for p in DEST.iterdir() if p.is_file() and p.name!='manifest.json'}})
    save(ROOT/'docs/validation/20261125-rag-rerank-postprocess.json',{'status':'completed_local_offline_not_deployed',
        'summary':s,'decision':decision,'unit_tests_passed':11,'report':'output/rag-rerank-postprocess-20261125/REPORT.html'})
    print(json.dumps({'status':'reported','winner':winner,'zero_model_calls':True},ensure_ascii=False))


if __name__=='__main__':main()
