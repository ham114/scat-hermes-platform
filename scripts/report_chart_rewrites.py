"""Publish source/rewrite comparison, data limits, and deployment evidence."""
import json,html,hashlib
from pathlib import Path
from collections import Counter
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'output/rag-chart-rewrite-20261124';TARGET=ROOT/'var/rag/corpora/kg_20261124_aviation_v7_charts_final'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def main():
 data=read(OUT/'reviewed-rewrites.json');build=read(TARGET/'build-summary.json');validation=read(OUT/'rewrite-verification.json');retrieval=read(OUT/'retrieval-summary.json')
 activation=read(OUT/'production-activation.json') if (OUT/'production-activation.json').exists() else {'status':'candidate-ready-production-unchanged'}
 record={'schema_version':'reviewed-chart-ingest/1','generation_id':TARGET.name,'corpus_sha256':build['corpus_sha256'],
         'regions':29,'reviewed_units':sum(len(x['units']) for x in data),'classification':dict(Counter(x['kind'] for x in data)),
         'build':build,'structural_verification':validation,'retrieval':{k:v for k,v in retrieval.items() if k!='comparisons'},'production':activation,
         'production_approval':read(OUT/'user-production-approval.json') if (OUT/'user-production-approval.json').exists() else None,
         'post_activation':read(OUT/'post-activation-verification.json') if (OUT/'post-activation-verification.json').exists() else None,
         'source_pdf_hashes':{x['source_pdf']:x['source_sha256'] for x in data},
         'published_source_anomalies':{'count':6,'region':8,'description':'Figure 11-24: 2000 lb, pressure altitudes 6000/7000/8000 ft, 20 C and 30 C ground-roll values. Retained as printed with explicit anomaly notice; no guessed correction.'},
         'historical_evaluation_modified':False,'qa_accuracy_remeasured':False,'external_model_calls':0,
         'evaluation_note':'Frozen test sets and scores remain bound to earlier corpora; no new accuracy claim or implicit benchmark migration.',
         'retrieval_limit_note':'The specific takeoff-distance probe matched at rank 10. Top-10 access is verified; default top-5 retrieval does not guarantee every numerical parameter combination is covered.',
         'production_approval_note':'This v7 production switch was rejected by automatic approval review pending explicit user authorization for configuration changes and RAG/main-container restarts. The earlier v6 approval does not satisfy this v7 gate.' if activation.get('status')!='activated' else 'Production switch explicitly approved and executed.',
         'review':'Assistant inspected all 29 original rendered chart regions and native PDF text; complete page footnotes checked for WMO tables 7.2 and 12.5. Not independent human validation.',
         'implementation_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['scripts/inspect_quarantined_charts.py','scripts/author_chart_rewrites.py','scripts/build_chart_rewrite_corpus.py','scripts/verify_chart_rewrites.py','scripts/deploy_chart_rewrites.py','scripts/report_chart_rewrites.py']}}
 save(OUT/'RECORD.json',record);save(ROOT/'docs/validation/20261124-rag-chart-rewrite.json',record)
 esc=lambda x:html.escape(str(x));cards=[]
 for r in data:
  cards.append(f'<details><summary>{r["index"]+1:02d} · {esc(r["title"])}</summary><p>原PDF页码：{r["page_numbers"][0]}；处理方式：{esc(r["kind"])}</p><img loading="lazy" src="{r["image"]}" alt="原始图表"><h3>改写内容</h3>'+''.join('<p>'+esc(u)+'</p>' for u in r['units'])+f'<p>共同条件：{esc(r["common_context"])}</p><p>注意：{esc(r["notes"])}</p></details>')
 status='已上线' if activation.get('status')=='activated' else '已入新版本语料，待生产切换'
 text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>问题图表改写入库记录</title><style>body{{font:16px/1.7 system-ui,sans-serif;color:#172b40;background:#f4f7fb;max-width:1100px;margin:auto;padding:28px}}details,section{{border:1px solid #d8e1ec;background:white;border-radius:10px;padding:18px;margin:14px 0}}summary{{cursor:pointer;font-weight:650}}img{{max-width:100%;height:auto}}p{{overflow-wrap:anywhere}}a{{color:#1458a5}}</style><h1>问题图表改写入库</h1><p>2026-11-24 · {status}</p><section><p>29处问题区域已逐图核验，整理为{record['reviewed_units']}条结构化语义单元，生成{build['new_chunks']}个新片段，保留{build['retained_chunks']}个未变化片段。新语料共{build['chunks']}个片段。目录、术语表、公式、证书示例和数值表分别处理；原始图表始终存档。</p><p>旧的隔离表格片段已由完整改写替换。起飞距离示例中6个疑似原文异常单元保留原值并逐条标记，不推算替换。</p><p>原始语料、题集和历史评测保持不变。本轮未重新作答、未调用外部模型，检索回归不代表新的问答准确率。</p><p><a href="RECORD.json">完整记录</a> · <a href="reviewed-rewrites.json">全部改写与出处</a> · <a href="retrieval-summary.json">检索回归</a> · <a href="rewrite-verification.json">覆盖校验</a></p></section>{''.join(cards)}</html>'''
 (OUT/'REPORT.html').write_text(text,encoding='utf8')
 print(json.dumps({'status':status,'regions':29,'units':record['reviewed_units']}))
if __name__=='__main__':main()
