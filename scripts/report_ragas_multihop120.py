"""Package an auditable candidate set and compact local review page."""
import collections,datetime,hashlib,html,json,shutil,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];RAW=ROOT/'var/validation/20261124-ragas-multihop120';OUT=ROOT/'output/ragas-multihop120-20261124'
labels={'cross_document':'跨文档综合','cross_chapter':'同文档跨章节','numeric_conditions':'数值与适用条件','insufficient':'条件缺失与证据不足'}
sources={'faa-aim':'FAA AIM','faa-instrument-flying-handbook':'FAA 仪表飞行手册','faa-phak':'FAA 飞行员航空知识手册','faa-aviation-weather-handbook':'FAA 航空气象手册','wmo-no8-volume1':'WMO-No.8 第一卷','faa-pilot-controller-glossary':'FAA 飞行员/管制员术语表','eurocontrol-asterix-cat062':'EUROCONTROL ASTERIX CAT062','ecmwf-era5-documentation':'ECMWF ERA5'}
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,o):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(o,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def esc(x):return html.escape(str(x),quote=True)

def main():
 OUT.mkdir(parents=True,exist_ok=True);audit=read(RAW/'audit.json');sim=read(RAW/'similarity-audit.json');notes=read(RAW/'assistant-quality-notes.json')
 assert audit['status']=='passed' and audit['candidates']==120
 assert audit['candidates_sha256']==sim['candidates_sha256']==notes['candidates_sha256']==sha(RAW/'candidates.jsonl')
 cases=[json.loads(l) for l in (RAW/'candidates.jsonl').read_text().splitlines()];assert len(cases)==120
 pending=set(x['qid'] for x in sim['previous_topic_flags'])|{x[k] for x in sim['within_batch_pairs'] for k in ['left','right']}
 assert pending<=set(x['qid'] for x in notes['similarity_adjudications'])
 assert all(x['decision']=='retain' for x in notes['similarity_adjudications'])
 focus=notes['recommended_spot_checks'];assert len(focus)==3 and set(focus)<=set(c['qid'] for c in cases)
 for name in ['candidates.jsonl','ragas-testset.json','plan.json','policy.json','preflight.json','summary.json','audit.json','similarity-audit.json','assistant-quality-notes.json','assistant-label-revisions.json','source-risk-register.json','final-native-export.json','prompt-revision-2.json','prompt-revision-3.json','prompt-revision-4.json','prompt-revision-5.json','prompt-revision-6.json','prompt-revision-7.json','request-extension-approval.json','request-extension-applied.json','user-approval.json']:
  src=RAW/name
  if not src.exists():src=OUT/name
  if src.resolve()!=(OUT/name).resolve():shutil.copy2(src,OUT/name)
 jobs=OUT/'jobs';jobs.mkdir(exist_ok=True)
 for c in cases:
  job=c['generation']['job']
  for suffix in ['scenario','ragas','review']:shutil.copy2(RAW/'jobs'/f'{job}.{suffix}.json',jobs/f'{job}.{suffix}.json')
 close={'status':'generation-complete-needs-spot-review','completed_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),'candidates':120,'requests':audit['requests'],'known_provider_total_tokens':audit['provider_input_tokens']+audit['provider_output_tokens'],'conservative_charge_including_unknown':audit['provider_budget_charge'],'human_approved':0,'qa_system_calls':0,'production_changes':0,'training_runs':0,'source_corpus':read(RAW/'plan.json')['corpus_generation'],'candidates_sha256':audit['candidates_sha256']}
 save(OUT/'closeout.json',close);save(OUT/'priority-review-3.json',{'scope':'Recommended spot checks; no QA evaluation has been run','items':[c for c in cases if c['qid'] in focus]})
 package={'schema_version':'rag-multihop120-candidates/1.0','status':'assistant-reviewed-candidates-not-independent-benchmark','corpus_generation':close['source_corpus'],'corpus_sha256':audit['corpus_sha256'],'not_for_training':True,'cases':cases}
 save(ROOT/'evaluations/rag_multihop120_candidates_20261124_v1/evaluation.json',package)
 cards=[]
 for c in cases:
  evidence=''.join('<h4>必要要点 '+str(i+1)+'</h4><p>'+esc(cl['text'])+'</p>'+''.join('<blockquote>'+esc(e['quote'])+'</blockquote><p class="meta">'+esc(sources[e['source_id']])+' · '+esc(' / '.join(e['section_path']))+'<br><code>'+esc(e['chunk_id'])+'</code> · 字符 ['+str(e['start_char'])+','+str(e['end_char'])+') · '+esc(json.dumps(e['locator'],ensure_ascii=False))+'</p>' for e in cl['evidence']) for i,cl in enumerate(c['claims']))
  cards.append('<article data-type="'+c['category']+'" data-focus="'+str(c['qid'] in focus).lower()+'"><h3>'+esc(c['qid'])+' · '+esc(labels[c['category']])+'</h3><p>'+esc(c['question'])+'</p><p class="meta">'+esc(sources[c['primary_source']])+' · '+c['language']+' · 双证据 · 模型初审＋助手构题复核；独立人工未审</p><details><summary>参考答案与可追溯原文</summary><p>'+esc(c['reference'])+'</p><h4>证据关系（模型说明）</h4><p>'+esc(c['dependency_test']['bridge_relation'])+'</p>'+evidence+'</details></article>')
 tables=''.join('<tr><td>'+esc(labels[k])+'</td><td>'+str(n)+'</td></tr>' for k,n in audit['category_counts'].items())
 page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>v7 多证据题集 · 120题</title><style>body{font:16px/1.75 system-ui,sans-serif;background:#f5f6fa;color:#18263b;margin:auto;max-width:1100px;padding:30px}h1{font-size:30px}article,.panel{background:white;border:1px solid #dce2eb;border-radius:12px;padding:24px;margin:18px 0}h3{font-size:18px}h4{margin-bottom:5px}.meta{font-size:13px;color:#526178;overflow-wrap:anywhere}blockquote{border-left:3px solid #527cca;padding-left:16px;margin-left:0;color:#34475f}select,input{font:inherit;padding:8px;margin:6px}table{border-collapse:collapse}td{padding:5px 25px 5px 0}summary{cursor:pointer;color:#245ba5}.hidden{display:none}code{font-size:12px}</style><h1>v7 多证据题集 · 120题</h1><div class="panel"><p>聚焦此前薄弱的多跳、跨文档证据覆盖、章节定位及条件组合。120题均使用服务器已安装的 Ragas 0.4.3 原生 MultiHopSpecificQuerySynthesizer 生成，每题绑定两处正文证据。</p><p><strong>这是合成候选题集，尚未运行问答评测，不能据此推断当前准确率。</strong> 标答与必要要点经过模型初审及助手构题复核，未经独立领域专家逐题审核。原始生成、重试、删除理由及额度账本均保留。</p><table>'''+tables+'</table><p>跨文档共 '+str(audit['cross_source'])+' 题；与 '+str(sim['previous_question_count'])+' 条旧问法做本地精确与BGE-M3语义查重。相似度标记经语义裁决，不把话题相近自动当成重复。</p><p>本批 '+str(audit['requests'])+' 次请求；已知输入＋输出 '+format(close['known_provider_total_tokens'],',')+' token。未知/失败请求保守占额；未训练、未改生产、未调用问答系统。</p><p>建议优先抽查：'+esc('、'.join(focus))+'。下方默认显示这3题，可切换全部。</p><p><a href="candidates.jsonl">完整题集JSONL</a> · <a href="ragas-testset.json">Ragas原生导出</a> · <a href="assistant-quality-notes.json">构题与查重记录</a> · <a href="audit.json">审计记录</a></p></div><div><select id="mode"><option value="focus">只看3道抽查题</option><option value="all">全部120题</option></select><select id="type"><option value="all">全部题型</option>'+''.join('<option value="'+k+'">'+esc(v)+'</option>' for k,v in labels.items())+'</select><input id="query" placeholder="搜索编号、题目或证据"><span id="count"></span></div>'+''.join(cards)+'''<script>const cards=[...document.querySelectorAll('article')];function update(){let m=document.querySelector('#mode').value,t=document.querySelector('#type').value,q=document.querySelector('#query').value.toLowerCase();cards.forEach(c=>c.classList.toggle('hidden',(m==='focus'&&c.dataset.focus!=='true')||(t!=='all'&&t!==c.dataset.type)||(q&&!c.textContent.toLowerCase().includes(q))));document.querySelector('#count').textContent='显示 '+cards.filter(c=>!c.classList.contains('hidden')).length+' / 120'}document.querySelectorAll('select').forEach(s=>s.onchange=()=>{if(s.id==='type')document.querySelector('#mode').value='all';update()});document.querySelector('#query').oninput=()=>{document.querySelector('#mode').value='all';update()};update();</script></html>'''
 page=page.replace('<p>建议优先抽查：','<p>'+str(audit['assistant_revised_reference_count'])+' 道标答经过显式最小校正；<a href="assistant-label-revisions.json">原始与修订对照</a>保留全部修改，120道Ragas题干均未改动。<a href="source-risk-register.json">来源风险记录</a>另列。</p><p>建议优先抽查：')
 (OUT/'REVIEW.html').write_text(page,encoding='utf8')
 manifest={'corpus_sha256':audit['corpus_sha256'],'files':{p.relative_to(OUT).as_posix():sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name not in ['evidence-manifest.json','pilot-candidates.jsonl']},'excluded':['.env','old-question-fingerprints.json','rejected request bodies'],'candidate_status':'not-independent-benchmark'}
 save(OUT/'evidence-manifest.json',manifest)
 print(json.dumps(close,ensure_ascii=False))
if __name__=='__main__':main()
