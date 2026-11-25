"""Local final report checks and packaging, with no model or network calls."""
from collections import Counter
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/ragas-aviation300-evaluation-20261124'
def read(n):return json.loads((OUT/n).read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(n,v):(OUT/n).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def main():
    a=read('qa-audit.json');f=read('qa-final.json');answers=read('qa-answers.json');r=read('retrieval-summary.json')
    assert a['status']=='passed' and f['status']=='completed' and a['checked_cases']==len(answers)==300
    assert len({c['case_id'] for c in answers})==300
    for n,h in read('evidence-manifest.json')['files'].items():assert sha(OUT/n)==h,n
    s=f['summary'];counts=Counter(c['review'].get('verdict','unavailable') for c in answers)
    assert counts['pass']==s['model_review_passed'] and counts['fail']==s['model_review_failed']
    assert counts['unavailable']==s['model_review_unavailable']
    assert sum(c['execution_passed'] for c in answers)==s['execution_passed']
    assert sum(c['human_review']=='spot_check_approved' for c in answers)==5
    for c in answers:
        assert all((cl.get('claim') or cl.get('text')) for cl in c['actual_claims'])
    rubric=read('scoring-rubric.json');optional=sum(bool(c['optional_explanations']) for c in rubric['cases'])
    summary={'date':'2026-11-24','status':'completed','qa':s,'groups':f['groups'],
        'retrieval':{k:v for k,v in r.items() if k!='cases'},'requests':a['requests'],
        'known_provider_input_output_tokens':a['provider_input_tokens']+a['provider_output_tokens'],
        'budget_charge_including_unknown_reserves':a['provider_budget_charge'],'request_states':a['states'],
        'answer_quotes_checked':a['exact_answer_citations_checked'],'optional_explanation_cases':optional,
        'individual_human_spot_checks_approved':5,'diagnostic_notes':read('diagnostic-adjudication.json'),
        'raw_scores_unchanged':True,'independent_human_accuracy':False,
        'ready_for_accuracy_claim':False,'reason':'Known question/reference/judge concerns in 034,199,236,261,263; partial gold-scope concern in003; retain raw baseline without post-hoc adjusted score',
        'scope':'Fixed synthetic document QA, production-source isolated fast, concurrency2; not auto/deep/web/API acceptance; same-provider fresh-context grading'}
    save('results-summary.json',summary)
    text=f'''新增官方航空资料300题问答评测（2026-11-24）

打开 REPORT.html 查看结果；默认仅显示需关注题，可切换全部300题。

300道单轮题（中文240、英文60），使用固定v5知识库8,173片段。编题阶段5道抽查由用户批准，其余由助手筛选，不能称为300题逐一人工验收。
执行成功：{s['execution_passed']}/300。
模型初审：通过{s['model_review_passed']}，未通过{s['model_review_failed']}，不可用{s['model_review_unavailable']}。
通过/全部：{s['pass_rate_all_cases']:.2%}；通过/有效评分：{s['pass_rate_valid_reviews']:.2%}。

复核发现034、199、236、261、263存在题目/标答/评分问题，003另有部分必答范围争议；本轮原始通过率属于待校正基线，暂不宜作为优化后准确率的正式结论。
本结果评估固定合成题集，是同供应商另开上下文进行的模型初审，不等于独立人工准确率或真实用户总体准确率。题目由原生Ragas生成，本轮评分使用项目固定口径的另上下文审查，不将其冒称Ragas通用指标。新旧两批题目分布不同，不能直接作为同一题集的前后对比。

执行范围：与生产源码一致的隔离原生fast流程，并发2题，每题模型调用预算18次/240秒，原生主Agent最多9次迭代；未验收auto/deep路由、网页或生产API全链路。未训练、未执行旧题回归、未修改生产配置。
全部300题在任何实际作答前冻结最小充分评分点，其中{optional}题单列了可选解释。原始题目、候选参考答案和要点均保留；可选说明缺少不扣分，主动扩展事实仍须正确且有引用支持。作答进程只收到题目，没有收到标答、评分点和标准证据编号。

检索成功300/300；严格证据组覆盖Top5 {r['group_coverage_at_5']:.2%}，Top10 {r['group_coverage_at_10']:.2%}；完整命中分别{r['complete_at_5']}/300、{r['complete_at_10']}/300。按冻结chunk ID计算，未计入语义等价替代证据，不能直接当作回答正确率。

034：题目只说“该术语表”，没有明确指定FAA-H-8083-15B；实际答案引用其他FAA资料的UHF范围。单列来源指代与口径争议，原始未通过保留，不直接据此认定系统通用数值知识错误。
003：一般复飞规则覆盖和最终引文支持存在未通过记录；盘旋条件是否应列为本题必答另有复查意见。不能因一项范围争议把整个未通过改判通过。
199：评分漏看实际已覆盖的交通条件，并忽略了相邻章节有据说明；单列评分争议。
236：题干-20°C，标答套用-50°C例题；单列参数不一致，不能据此认定系统核心计算错误。
261：原始PDF确认协议0/1位值标答反写，当前抽取文本还丢失了列关系。
263：原始PDF确认瞬态指示的备用静压源切换前提未进入题干。
source-review/保留两处原始PDF完整页的核对图。
diagnostic-adjudication.json 是助手观察实际答案之后的复查，不是独立人工验收；没有修改分数或重算修正通过率。

已逐条核验实际答案引文{a['exact_answer_citations_checked']}处，并核对冻结数据、源码、全部模型请求事件及预算台账。机械核验不等于语义支持判断。
共{a['requests']}次请求；已知provider输入加输出{a['provider_input_tokens']+a['provider_output_tokens']} token；含未知请求预留的预算计量{a['provider_budget_charge']}。缓存已含在input，不重复相加。上限为2,400次、1,800万provider token、1.2亿保守单位、2小时；失败计入，没有重置台账或追加限额。

文件：
REPORT.html：逐题查看、分组和筛选。
qa-answers.json：真实回答、原始参考、最小评分点、原文引用与评分。
qa-final.json / results-summary.json：原始及便于阅读的汇总。
qa-audit.json：独立离线完整性审计，无模型调用。
retrieval-summary.json / retrieval-analysis.json：检索基线与分组诊断。
scoring-rubric.json / scoring-rubric-manifest.json：作答前冻结的最小评分标准。
frozen-dataset/：冻结题集、5题用户批准、清单。
qa/、judge/、gold-calibration/、gold-rubric/：本轮逐题原生输出与初审溯源。
qa-ledger.sqlite：总调用台账，不含密钥。
evidence-manifest.json：服务器证据包SHA；delivery-manifest.json：最终交付文件SHA。
sources/：执行、预算、审计、导出与报告脚本。

服务器原始目录：/home/h3c/project/python/scat-hermes-platform/var/validation/20261124-rag-aviation300
'''
    (OUT/'README.txt').write_text(text,encoding='utf-8')
    for name in ['report_aviation300_qa.cjs','finalize_aviation300_report.py']:shutil.copy2(ROOT/'scripts'/name,OUT/'sources'/name)
    subprocess.run(['node',str(ROOT/'scripts/report_aviation300_qa.cjs')],cwd=ROOT,check=True)
    from bs4 import BeautifulSoup
    soup=BeautifulSoup((OUT/'REPORT.html').read_text(encoding='utf-8'),'html.parser')
    cards=soup.select('article');assert len(cards)==300
    assert {c['data-id'] for c in cards}=={c['case_id'] for c in answers}
    assert len(soup.select('article[data-state="pass"]'))==s['model_review_passed']
    assert len(soup.select('article[data-verdict="fail"]'))==s['model_review_failed']
    for link in soup.select('a[href]'):
        u=link['href']
        if not u.startswith(('https://','http://')):assert (OUT/u).is_file(),u
    js=OUT/'report-client.js';js.write_text(soup.script.string,encoding='utf-8')
    subprocess.run(['node','--check',str(js)],check=True)
    save('report-verification.json',{'status':'passed','cards':300,'raw_scores_match':True,
        'local_links':'exist','client_javascript_syntax':'passed','external_model_calls':0})
    save('delivery-manifest.json',{'scope':summary['scope'],'files':{p.relative_to(OUT).as_posix():sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='delivery-manifest.json'}})
    archive=ROOT/'output/Ragas-aviation-300-evaluation-20261124.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT.rglob('*')):
            if p.is_file():z.write(p,Path(OUT.name)/p.relative_to(OUT))
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None
    print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,'sha256':sha(archive),'summary':s}))
if __name__=='__main__':main()
