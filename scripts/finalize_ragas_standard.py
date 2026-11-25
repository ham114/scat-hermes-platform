"""Validate the local native-Ragas report and make a portable delivery archive."""
from collections import Counter
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'output/ragas-standard-rescore-20261124'
def read(n):return json.loads((OUT/n).read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(n,v):(OUT/n).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def main():
    audit=read('audit.json');final=read('final.json');summary=read('summary.json')
    assert audit['status']=='passed' and final['status']=='completed' and audit['cases']==426 and audit['turns']==439
    for n,h in read('evidence-manifest.json')['files'].items():assert sha(OUT/n)==h,n
    rows=[json.loads(s) for s in (OUT/'inputs.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(rows)==439 and len({r['case_id'] for r in rows})==426
    statuses=Counter(r['status'] for r in final['results'])
    assert len(final['results'])==2195 and statuses['scored']==audit['native_metric_formulas_independently_recomputed']
    assert statuses['not_applicable_no_delivered_answer']==3
    assert sha(OUT/'inputs.jsonl')==read('input-manifest.json')['inputs_sha256']
    if (OUT/'README.txt').exists() and not (OUT/'PREPARATION.txt').exists():shutil.copy2(OUT/'README.txt',OUT/'PREPARATION.txt')
    lines=['Ragas原生标准指标复评（2026-11-24，已完成）','',
        '打开REPORT.html查看。默认10轮抽查，可按批次和题型查看全部439轮。',
        '426题：旧批83题（70单轮+13双轮）、旧回归43题、新航空300题，共439轮，438轮有成功交付答案。',
        '严格复用已有真实问答与上下文，新问答调用0。旧答案来自v4，新航空答案来自v5，分组报告。',
        '','本次直接调用服务器已有Ragas0.4.3 metrics.collections的Faithfulness、AnswerRelevancy、ContextPrecision、ContextRecall、FactualCorrectness.ascore，原生指标算法保持不变。',
        '回答相关性strictness=3，采用已存在的CPU BGE-M3（固定检查点、最长2048token），不请求外部嵌入服务。事实正确性mode=f1，atomicity=low，coverage=low。',
        '','重要解释：这些是连续指标，均值不是通过率或真实准确率，不能直接与上一轮96.7%相比。仍为同供应商模型裁判，需要人工抽查校准。',
        '参考事实F1衡量与冻结参考的一致性和覆盖，额外有据内容或参考简略也可能降低该分数。忠实度衡量答案是否受当时已见上下文支持，不等于每条最终引文都足够完整。',
        '回答相关性中的noncommittal判断可能压低正确资料不足回答，须分题型看。双轮按真实轮次计算标准单轮指标，未冒称原生多轮指标；该组单独报告。',
        '','上下文口径：ContextPrecision为排序相关的平均精确率AP，不是简单Precision@5。检索指标用原生记录中的实际初始Top5全文及顺序；忠实度只用实际回读全文和未回读240字符预览，不把未读全文补进模型证据。',
        '参考口径：使用原问答前已冻结的最小必答点，双轮用原先逐轮参考；保留原始完整参考，不根据本轮分数重写。',
        '已知争议P0-024及航空003/034/199/236/261/263在本轮首次调用前登记；全部适用诊断结果保留，相关项不混入主汇总。未标出问题不等于已全面人工验收。',
        'AV300-172无交付答案，三项答案指标不适用，不拿草稿或标答补齐；其检索指标仍可计算。','',
        f"指标项2195，状态：{json.dumps(dict(statuses),ensure_ascii=False)}。",
        f"独立离线重算{audit['native_metric_formulas_independently_recomputed']}项原生指标，核对{audit['contexts_verified']}处上下文和13份实际前轮历史；声明/判定数量不齐警告{len(audit['claim_alignment_warnings'])}项。",
        f"评分请求{audit['requests']}次；已知provider输入加输出{audit['provider_input_tokens']+audit['provider_output_tokens']} token；含未知预留计量{audit['provider_budget_charge']}。缓存已含input，不重复相加。",
        '独立上限7500请求、3200万输入加输出token、2亿保守单位、启动起2小时；试跑复用、失败计入，未重置计数或自动重放未知评分。',
        '','分组均值（已知相关争议隔离后；括号内为有效n）：']
    for name,g in summary['groups'].items():
        lines.append(name+'：'+'；'.join(f"{m}={v['known_issue_isolated']['mean']:.4f} (n={v['known_issue_isolated']['n']})" if v['known_issue_isolated']['mean'] is not None else m+'=不可用' for m,v in g['metrics'].items()))
    lines+=['','文件：REPORT.html交互查看；summary.json分组均值/有效分母；final.json原始逐指标结果；metric-details.json原生声明判定和离线复算分解；audit.json完整性与用量审计；inputs.jsonl真实固定输入；input-manifest.json语料版本、输入SHA和前置隔离规则；calls/及calls.sqlite原生请求证据和用量；embeddings.sqlite为本地嵌入缓存；sources/为编排、审计与报告脚本。',
        '','服务器目录：/home/h3c/project/python/ragas-evaluation/reports/standard-reeval-20261124']
    (OUT/'README.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    for name in ['report_ragas_standard.cjs','finalize_ragas_standard.py']:shutil.copy2(ROOT/'scripts'/name,OUT/'sources'/name)
    subprocess.run(['node',str(ROOT/'scripts/report_ragas_standard.cjs')],cwd=ROOT,check=True)
    from bs4 import BeautifulSoup
    soup=BeautifulSoup((OUT/'REPORT.html').read_text(encoding='utf-8'),'html.parser')
    assert len(soup.select('article'))==439 and len(soup.select('article[data-focus="true"]'))==10
    assert {e['data-id'] for e in soup.select('article')}=={r['unit_id'] for r in rows}
    for e in soup.select('a[href]'):
        u=e['href']
        if not u.startswith(('https://','http://')):assert (OUT/u).is_file(),u
    js=OUT/'report-client.js';js.write_text(soup.script.string,encoding='utf-8')
    subprocess.run(['node','--check',str(js)],check=True)
    save('report-verification.json',{'status':'passed','turn_cards':439,'focus_cards':10,'metric_jobs':2195,'raw_scores_preserved':True,'local_links':'exist','javascript_syntax':'passed','external_model_calls':0})
    save('delivery-manifest.json',{'cases':426,'turns':439,'files':{p.relative_to(OUT).as_posix():sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='delivery-manifest.json'}})
    archive=ROOT/'output/Ragas-standard-rescore-20261124.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT.rglob('*')):
            if p.is_file():z.write(p,Path(OUT.name)/p.relative_to(OUT))
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None
    print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,'sha256':sha(archive),'statuses':dict(statuses)}))
if __name__=='__main__':main()
