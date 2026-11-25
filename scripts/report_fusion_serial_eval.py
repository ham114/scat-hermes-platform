"""Publish serial fusion comparisons, keeping new and old cohorts separate."""
import hashlib
import html
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/"var/validation/20261125-rag-fusion-serial-v1"
DEST = ROOT/"output/rag-fusion-serial-20261125"
DEPTHS = (5, 10, 20, 50, 100)


def read(p):
    return json.loads(p.read_text(encoding="utf8"))


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def save(p, value):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf8")


def main():
    s = read(OUT/"summary.json")
    plan = read(OUT/"preregistration.json")
    units = read(OUT/"dataset.json")["cases"]
    assert s["status"] == "completed" and s["cases_per_policy"] == 233
    assert s["serial_order"] == plan["serial_order"]
    assert s["qa_calls"] == s["external_llm_calls"] == s["network_connections_attempted"] == s["production_changes"] == 0
    for name, h in {**plan["protected_files"], **plan["input_files_sha256"]}.items():
        assert sha(ROOT/name) == h
    assert sha(ROOT/"src/rag_retrieval/baseline.py") == plan["original_bm25_sha256"]
    policies = s["policies"]
    byid = {p["policy"]["id"]: p for p in policies}
    results = {pid: {r["id"]: r for r in read(OUT/(pid+"-results.json"))["cases"]}
               for pid in s["serial_order"]}
    # Strict default-selection gate: any old-case Top5/10 regression needs review.
    eligible = [p["policy"]["id"] for p in policies if p["policy"]["id"] != "baseline"
                and not any(p["paired_against_baseline"]["old113"][str(k)]["lost"] for k in (5, 10))]
    improvements = [pid for pid in eligible if (
        byid[pid]["metrics"]["new120"]["5"]["complete_cases"],
        byid[pid]["metrics"]["new120"]["10"]["complete_cases"]) > (
        byid["baseline"]["metrics"]["new120"]["5"]["complete_cases"],
        byid["baseline"]["metrics"]["new120"]["10"]["complete_cases"])]
    preferred = max(improvements, key=lambda pid: (
        byid[pid]["metrics"]["new120"]["5"]["complete_cases"],
        byid[pid]["metrics"]["new120"]["10"]["complete_cases"],
        byid[pid]["metrics"]["new120"]["100"]["complete_cases"])) if improvements else "baseline"
    fusion_improvements = [pid for pid in improvements if pid != "dense100"]
    preferred_fusion = max(fusion_improvements, key=lambda pid: (
        byid[pid]["metrics"]["new120"]["5"]["complete_cases"],
        byid[pid]["metrics"]["new120"]["10"]["complete_cases"],
        byid[pid]["metrics"]["new120"]["100"]["complete_cases"])) if fusion_improvements else "baseline"
    decision = {"gate": "No old113 case-level complete-evidence regression at either Top5 or Top10, then prioritize new120 Top5 followed by Top10. This is a development-set gate, not production authorization.",
                "eligible_without_old_case_regressions": eligible,
                "preferred_by_gate": preferred, "production_deployed": False,
                "preferred_fusion_by_gate": preferred_fusion,
                "fusion_scope": "Pure BGE is a diagnostic control. For the user's requested retention of BM25 in fusion, select among the three fusion alternatives using the same gate.",
                "original_bm25_preserved": True,
                "limits": ["Strict frozen IDs may exclude alternative equivalent passages.",
                           "Both sets are development/regression evidence, not an independent blind test.",
                           "No answer generation or answer-accuracy measurement was performed."]}
    DEST.mkdir(parents=True, exist_ok=True)
    for name in (["summary.json", "dataset.json", "preregistration.json", "run-identity.json"]+
                 [pid+suffix for pid in s["serial_order"] for suffix in ["-results.json", "-summary.json"]]):
        shutil.copy2(OUT/name, DEST/name)
    save(DEST/"leaves.json", {"cases": [read(OUT/"leaves"/(u["id"]+".json")) for u in units]})
    for name in ["selection-rule.json", "run.log", "test_fusion_policy_experiments.py", "completion-record.json"]:
        if (OUT/name).exists():
            shutil.copy2(OUT/name, DEST/name)
    save(DEST/"decision.json", decision)
    source_names = ["fusion_policy_experiments.py", "prepare_fusion_serial_eval.py",
                    "run_fusion_serial_eval.py", "complete_fusion_dense_control.py",
                    "report_fusion_serial_eval.py", "evidence_retrieval_metrics.py"]
    with zipfile.ZipFile(DEST/"experiment-source.zip", "w", zipfile.ZIP_DEFLATED) as z:
        manifest = {}
        for name in source_names:
            p = ROOT/"scripts"/name
            z.write(p, "scripts/"+name)
            manifest["scripts/"+name] = sha(p)
        for name in plan["protected_files"]:
            if name.startswith("src/rag_retrieval/"):
                z.write(ROOT/name, name)
                manifest[name] = sha(ROOT/name)
        z.writestr("manifest.json", json.dumps({"files": manifest}, indent=2))
    esc = lambda x: html.escape(str(x), quote=True)
    header = "<tr><th>方案</th>"+"".join("<th>Top"+str(k)+"</th>" for k in DEPTHS)+"</tr>"

    def matrix(cohort, stage):
        denominator = 120 if cohort == "new120" else 113
        return "<table>"+header+"".join("<tr><th>"+esc(p["policy"]["label"])+"</th>"+
            "".join("<td>"+str(p[stage][cohort][str(k)]["complete_cases"])+"/"+str(denominator)+"</td>"
                    for k in DEPTHS)+"</tr>" for p in policies)+"</table>"

    changes = []
    for p in policies[1:]:
        changes.append("<section><h3>"+esc(p["policy"]["label"])+"</h3>"+"".join(
            "<p>"+esc(cohort)+" Top"+str(k)+" 新增："+esc(p["paired_against_baseline"][cohort][str(k)]["gained"])+
            "；退化："+esc(p["paired_against_baseline"][cohort][str(k)]["lost"])+"</p>"
            for cohort in ["new120", "old113"] for k in (5, 10))+"</section>")
    cards = []
    for u in units:
        rows = []
        for policy in plan["policies"]:
            r = results[policy["id"]][u["id"]]
            ranks = "; ".join(cid+": "+str(r["gold_final_ranks"][cid] or "—") for cid in u["gold"])
            rows.append("<tr><th>"+esc(policy["label"])+"</th><td>"+
                        ("完整" if r["metrics"]["5"]["all_evidence"] else "未完整")+
                        "</td><td>"+("完整" if r["metrics"]["10"]["all_evidence"] else "未完整")+
                        "</td><td><code>"+esc(ranks)+"</code></td></tr>")
        regression = any(u["id"] in p["paired_against_baseline"][u["cohort"]][str(k)]["lost"]
                         for p in policies[1:] for k in (5, 10))
        cards.append('<article data-cohort="'+u["cohort"]+'" data-loss="'+str(regression).lower()+'"><h3>'+
                     esc(u["id"])+" · "+esc(u["cohort"])+"</h3><p>"+esc(u["question"])+
                     "</p><div class=scroll><table><tr><th>策略</th><th>Top5</th><th>Top10</th><th>冻结证据最终名次</th></tr>"+
                     "".join(rows)+"</table></div></article>")
    latency = "<table><tr><th>策略</th><th>题组</th><th>新打分观测</th><th>P50</th><th>P95</th></tr>"+"".join(
        "<tr><th>"+esc(p["policy"]["label"])+"</th><td>"+cohort+"</td><td>"+str(v["fresh_observations"])+
        "</td><td>"+(format(v["p50_seconds"], ".3f")+"s" if v["p50_seconds"] is not None else "复用排序")+
        "</td><td>"+(format(v["p95_seconds"], ".3f")+"s" if v["p95_seconds"] is not None else "复用排序")+"</td></tr>"
        for p in policies for cohort, v in p["latency"].items())+"</table>"
    preferred_label = byid[preferred_fusion]["policy"]["label"]
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>原BM25融合策略串行评测</title>
<style>body{max-width:1350px;margin:auto;padding:26px;font:16px/1.7 system-ui;background:#f4f7fb;color:#20354a}section,article{background:white;border:1px solid #dbe3ed;border-radius:12px;padding:20px;margin:18px 0}table{width:100%;border-collapse:collapse}th,td{padding:9px;text-align:left;border-bottom:1px solid #dce4ee}code{font-size:12px;overflow-wrap:anywhere}.scroll{overflow:auto}.hidden{display:none}input,select{font:inherit;padding:8px;margin:6px}</style>
<h1>原BM25 · 五组融合策略串行评测</h1><p>2026-11-25 · 固定v7语料 · 新120题与旧113题分别计分 · 不作答、不跑Ragas、不调用外部模型 · 生产未切换</p>
<section><p>原BM25源码哈希与生产一致，分词、参数和既有计分行为均保持不变。本轮只改变融合权重与候选保留策略；每组均为100条候选，BGE/BM25/标识符各召回最多200，原Qwen模型、batch=4、长度2048不变。</p><ol><li>原生产：等权RRF、截取200、来源加权2.2、再取100。</li><li>向量加权：BGE权重2，BM25和标识符各1，其余与原生产一致。</li><li>BGE80＋融合20：保护前80条，在完整融合并集中补齐其余20，来源加权不挤出保护部分。</li><li>70/20/10：先BGE70，再补20条不重复BM25、10条不重复标识符；不足按完整融合结果补齐100。</li><li>纯BGE100对照。</li></ol><p>五组按上述顺序串行完成，未依据结果修改参数。新120题的两组既有对照复用此前排序，均核对新构造的候选及其顺序；其余925题次实际本地打分。旧113题全部采用同一v7正文中的原冻结证据ID，已检查全部仍存在，不包含多轮问答重放。</p></section>
<h2>最终重排完整证据覆盖 · 新120题</h2><div class=scroll><!--newmatrix--></div>
<h2>最终重排完整证据覆盖 · 旧113题</h2><div class=scroll><!--oldmatrix--></div>
<section><h2>选择结论</h2><p>按“旧113题Top5/10逐题均不退化，再优先新120题Top5与Top10”的门槛，保留BM25参与融合时，当前融合候选为：<strong><!--preferred--></strong>。纯BGE作为诊断对照，另在decision.json保留包含对照的全方案筛选结果。</p><p>这是开发集与回归集上的筛选结果，没有执行生产切换。若某方案新题有收益但旧题发生退化，将在下方逐题列出；不以新旧题混合均值掩盖差异。严格片段覆盖不包含未标注的等价证据，也不等于问答准确率。</p><p>旧题原生产Top10排序与历史结果一致：<!--parity-->/113。原BM25保持原样并不撤销此前发现的DF实现问题；本轮将该问题作为独立事项，避免混入融合方案对照。</p></section>
<h2>候选池完整覆盖 · 新120题</h2><div class=scroll><!--newpool--></div>
<h2>候选池完整覆盖 · 旧113题</h2><div class=scroll><!--oldpool--></div>
<h2>相对原生产的逐题新增与退化</h2><!--changes-->
<section><h2>本地重排耗时</h2><p>仅统计本轮新打分部分，不含预先共享的查询编码／分支召回、服务排队和HTTP传输；复用对照无本轮耗时。各策略按序运行，以下不等于线上并发P95。</p><!--latency--></section>
<section><h2>证据与复核</h2><p>55项相关测试及3项历史结果格式适配测试通过，服务器原BM25环境的9项融合规则测试通过。生产配置、原BM25及相关源码、历史题集和模型调用账本哈希前后不变。评测进程网络尝试0，答案生成0，外部模型调用0；本轮没有运行BM25纠错版本。</p><p>最后一组纯BGE对照曾在读取旧结果格式时校验中止；独立适配器只补完该组，已完成的四组、冻结参数及标注不变，没有重新评分已完成题。原驱动、适配器、恢复记录和原日志均保留。</p><p><a href="summary.json">总汇总</a> · <a href="decision.json">选择门槛与限制</a> · <a href="preregistration.json">启动前冻结方案</a> · <a href="dataset.json">冻结问题与证据</a> · <a href="experiment-source.zip">实验源码与原检索实现</a> · <a href="leaves.json">233题原始分支召回</a> · <a href="selection-rule.json">查看替代方案结果前记录的筛选口径</a> · <a href="test_fusion_policy_experiments.py">融合规则测试</a> · <a href="completion-record.json">对照格式适配与恢复记录</a></p><p><!--resultlinks--></p></section>
<h2>逐题对照</h2><select id=cohort><option value=all>全部题组</option><option value=new120>新120题</option><option value=old113>旧113题</option></select><select id=mode><option value=all>全部题目</option><option value=loss>任一方案Top5/10退化</option></select><input id=search placeholder="搜索题号、题目或片段ID"><span id=count></span><!--cards-->
<script>const cards=[...document.querySelectorAll('article')];function update(){let h=document.querySelector('#cohort').value,m=document.querySelector('#mode').value,q=document.querySelector('#search').value.toLowerCase();cards.forEach(c=>c.classList.toggle('hidden',(h!=='all'&&c.dataset.cohort!==h)||(m==='loss'&&c.dataset.loss!=='true')||(q&&!c.textContent.toLowerCase().includes(q))));document.querySelector('#count').textContent='显示 '+cards.filter(c=>!c.classList.contains('hidden')).length+' / 233'}document.querySelectorAll('select').forEach(s=>s.onchange=update);document.querySelector('#search').oninput=update;update();</script></html>"""
    values = {"newmatrix": matrix("new120", "metrics"), "oldmatrix": matrix("old113", "metrics"),
              "newpool": matrix("new120", "candidate_metrics"), "oldpool": matrix("old113", "candidate_metrics"),
              "preferred": esc(preferred_label), "parity": str(byid["baseline"]["old_baseline_top10_parity"]),
              "changes": "".join(changes), "latency": latency, "cards": "".join(cards),
              "resultlinks": " · ".join('<a href="'+p["policy"]["id"]+'-results.json">'+esc(p["policy"]["label"])+"逐题完整排序</a>" for p in policies)}
    for key, value in values.items():
        page = page.replace("<!--"+key+"-->", value)
    (DEST/"REPORT.html").write_text(page, encoding="utf8")
    save(DEST/"manifest.json", {"files": {p.name: sha(p) for p in DEST.iterdir()
                                          if p.is_file() and p.name != "manifest.json"}})
    save(ROOT/"docs/validation/20261125-rag-fusion-serial.json", {
        "status": "completed_not_deployed", "summary": s, "decision": decision,
        "tests_passed": 55, "saved_control_adapter_tests_passed": 3,
        "server_fusion_rule_tests_passed": 9,
        "report": "output/rag-fusion-serial-20261125/REPORT.html"})
    print(json.dumps({"status": "reported", "decision": decision}, ensure_ascii=False))


if __name__ == "__main__":
    main()
