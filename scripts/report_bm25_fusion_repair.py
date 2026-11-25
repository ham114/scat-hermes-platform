"""Publish evidence-only repair results without touching historical artifacts."""
import hashlib
import html
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261125-rag-bm25-fusion-repair"
BASE = ROOT / "output/rag-evidence-top100-20261125"
DEST = ROOT / "output/rag-bm25-fusion-repair-20261125"
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
    rows = read(OUT/"results.json")["cases"]
    plan = read(OUT/"plan.json")
    assert s["status"] == "completed" and len(rows) == 120
    assert s["qa_calls"] == s["external_llm_calls"] == s["network_connections_attempted"] == 0
    assert s["protected_files_unchanged"] and s["fresh_candidate_parity_cases"] == 120
    for name, expected in {**plan["protected_files"], **plan["inputs_sha256"]}.items():
        assert sha(ROOT/name) == expected
    production = read(BASE/"summary.json")["by_stage"]["reranked"]
    corrected = read(BASE/"bm25-df-counterfactual.json")["by_stage"]["corrected_reranked"]
    dense = read(BASE/"dense-pool-ablation.json")["by_depth"]
    original = {r["case_id"]: r for r in read(BASE/"results.json")["cases"]}
    controls = {"production100": production, "corrected100": corrected, "dense100": dense}
    comparison = {
        "metric": "Strict complete-two-evidence cases out of120; not answer accuracy.",
        "controls_reused": True, "controls": controls, "new150": s["by_stage"]["reranked150"],
        "paired": s["paired_against_saved_controls"],
        "candidate150": s["by_stage"]["candidate150"],
        "candidate_extra_gold_evidence_over_dense100": s["by_stage"]["candidate150"]["150"]["evidence_hits"]-235,
        "remaining_candidate_misses": [{"case_id": r["case_id"], "chunk_id": cid}
                                        for r in rows for cid in r["gold_chunk_ids"]
                                        if cid not in r["pool"]],
        "not_ready_for_production": "Only the new120 set was evaluated this round; old-question regression and deployment verification remain.",
    }
    DEST.mkdir(parents=True, exist_ok=True)
    for name in ["summary.json", "results.json", "plan.json", "benchmark.json", "candidate-summary.json",
                 "candidate-results.json", "source-snapshot.zip"]:
        shutil.copy2(OUT/name, DEST/name)
    for name in ["run_bm25_fusion_repair_eval.py", "report_bm25_fusion_repair.py"]:
        shutil.copy2(ROOT/"scripts"/name, DEST/name)
    save(DEST/"comparison.json", comparison)
    rollback_files = {name: expected for name, expected in plan["protected_files"].items()
                      if name.startswith("src/rag_retrieval/") or
                      name in {"services/rag/qwen3-production-v1.json",
                               "services/rag/real-agent-production-v1.json"}}
    with zipfile.ZipFile(DEST/"pre-repair-production-code.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in rollback_files:
            z.write(ROOT/name, name)
        z.writestr("manifest.json", json.dumps({"files": rollback_files}, indent=2))
    save(DEST/"rollout-record.json", {
        "deployed": False, "old113_regression_completed": False,
        "candidate_config": "services/rag/qwen3-aviation-v7-bm25-dense150-candidate.json",
        "rollback_snapshot": "pre-repair-production-code.zip",
        "rollback_note": "A future full rollback must restore both retrieval source and production configs before restarting. Reverting only candidate_limit would not undo the BM25 DF change.",
        "production_files_sha256": rollback_files,
    })
    esc = lambda x: html.escape(str(x), quote=True)
    labels = {"production100": "原生产混合100（保存对照）", "corrected100": "纠正BM25＋混合100（保存对照）",
              "dense100": "纯BGE100＋Qwen（保存对照）", "new150": "修正BM25＋保留BGE100并补充50（本轮实测）"}
    table = "".join("<tr><th>"+label+"</th>"+"".join(
        "<td>"+str(metrics[str(k)]["complete_cases"])+"/120</td>" for k in DEPTHS)+"</tr>"
        for label, metrics in [(labels[n], controls[n]) for n in controls]+
        [(labels["new150"], s["by_stage"]["reranked150"])] )
    cards = []
    for r in rows:
        dense_ranks = {g["chunk_id"]: g["ranks"]["dense"]
                       for g in original[r["case_id"]]["gold"]}
        gold = "".join("<tr><td><code>"+esc(cid)+"</code></td><td>"+esc(
            dense_ranks[cid])+"</td><td>"+esc(
            (r["gold_ranks"][cid] or {}).get("candidate_rank", "—"))+"</td><td>"+esc(
            r["gold_final_ranks"][cid] or "—")+"</td></tr>" for cid in r["gold_chunk_ids"])
        cards.append('<article data-miss="'+str(not r["metrics"]["reranked150"]["5"]["all_evidence"]).lower()+'"><h3>'+esc(r["case_id"])+"</h3><p>"+esc(r["question"])+
                     "</p><table><tr><th>冻结证据</th><th>BGE全库名次（冻结对照）</th><th>候选名次</th><th>最终重排名次</th></tr>"+gold+"</table></article>")
    pair = "".join("<h3>对照："+esc(labels[name])+"</h3>"+"".join(
        "<p>Top"+str(k)+" 新增："+esc(changes[str(k)]["gained_cases"])+"；退化："+
        esc(changes[str(k)]["lost_cases"])+"</p>" for k in (5, 10))
        for name, changes in s["paired_against_saved_controls"].items())
    bench = s["benchmark"]
    perf = "".join("<tr><th>"+esc(name)+"</th><td>"+format(v["p50_seconds"], ".3f")+
                   "s</td><td>"+format(v["p95_seconds"], ".3f")+"s</td><td>"+str(v["observations"])+
                   "</td></tr>" for name, v in bench.items())
    v = s["by_stage"]["reranked150"]
    recommendation = ("本轮150条方案未在Top5或Top10超过已有纯BGE100对照，且补充50条没有带来新增冻结证据，当前不建议默认扩大到150条。保留BM25正确性修复与策略实现，扩大候选池的默认值应在旧题与独立资料上验证后再决定。"
                      if v["5"]["complete_cases"] <= dense["5"]["complete_cases"] and
                      v["10"]["complete_cases"] <= dense["10"]["complete_cases"] else
                      "本轮150条方案相对纯BGE100部分指标有收益，仍需结合退化题、时延和旧题回归决定是否部署。")
    candidate = s["by_stage"]["candidate150"]
    coverage_text = f"Top100: {candidate['100']['complete_cases']}/120, {candidate['100']['evidence_hits']}/240; Top150: {candidate['150']['complete_cases']}/120, {candidate['150']['evidence_hits']}/240. Final Top150: {v['150']['complete_cases']}/120."
    memory = f"allocated {s['gpu_peak_allocated_bytes']/1024**3:.2f} GiB; reserved {s['gpu_peak_reserved_bytes']/1024**3:.2f} GiB"
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BM25与融合候选修复评测</title>
<style>body{max-width:1280px;margin:auto;padding:28px;background:#f4f7fb;color:#20354a;font:16px/1.65 system-ui}section,article{background:white;padding:22px;border:1px solid #d9e2ec;border-radius:12px;margin:18px 0}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid #dce4ed}code{font-size:12px;overflow-wrap:anywhere}.scroll{overflow:auto}.hidden{display:none}select,input{font:inherit;padding:8px;margin:5px}</style>
<h1>BM25修复与候选保留 · 新120题证据评测</h1><p>2026-11-25 · v7固定语料 · 不作答、不运行Ragas、不调用外部模型 · 生产未切换</p>
<section><p>已修正BM25文档频率统计；线上入口、原生检索与离线评测共用候选构造模块。新策略保留BGE前100条，在三路完整并集上融合，从剩余结果补充最多50条；来源加权不能挤掉保留部分。采用独立候选配置，原生产源文件及配置未上传覆盖。</p><p>主指标为两条冻结片段ID全部命中，分母120；逐证据分母240。不同Top深度与候选池大小分别报告。三个对照复用此前冻结检索与同模型重排结果，本轮150条逐题重新检索和重排，全部120题候选与独立冻结召回构造结果一致。</p><p>本轮只在新120题上评测，未运行113旧题回归，尚未达到生产发布条件。</p></section>
<h2>重排后双证据完整覆盖</h2><div class="scroll"><table><tr><th>方案</th><!--headers--></tr><!--table--></table></div>
<section><h2>候选覆盖与结论</h2><p><!--coverage--></p><p><!--recommendation--></p><p>150条候选的覆盖不等于同预算Top100提升，证据覆盖也不等于问答正确率。</p></section>
<section><h2>逐题增益与退化</h2><!--paired--></section>
<section><h2>配对耗时抽测</h2><p>固定均匀抽取12题，每题两次，100/150交替顺序；模型、batch=4、长度2048保持一致。每组24个观测，仅统计本地Qwen重排耗时，不含查询编码、服务排队或HTTP传输，不能视为线上P95。</p><table><tr><th>候选方案</th><th>P50</th><th>P95</th><th>观测数</th></tr><!--perf--></table><p>评测进程PyTorch峰值：<!--memory-->；这不是整卡总占用。</p></section>
<section><h2>记录与限制</h2><p>46项相关测试在本地与服务器隔离快照通过。生产源文件、两份配置和旧模型调用账本前后SHA256相同；测试进程网络尝试0，答案生成0，外部模型调用0。原题、标注、语料、模型和历史成绩不变。该开发题集已用于诊断，结果不能替代独立题集的泛化验收。</p><p>未来完整回滚需恢复检索源文件及配置后重启；只调回候选数量不能撤销BM25修复。本轮未部署。</p><p><a href="summary.json">汇总</a> · <a href="comparison.json">增益退化与比较口径</a> · <a href="results.json">逐题完整排序与分数</a> · <a href="benchmark.json">配对耗时原始数据</a> · <a href="source-snapshot.zip">隔离代码、测试与候选配置</a> · <a href="plan.json">冻结计划与哈希</a> · <a href="rollout-record.json">部署状态与回滚说明</a> · <a href="pre-repair-production-code.zip">修复前生产代码配置快照</a></p></section>
<h2>逐题证据位置</h2><select id="mode"><option value="miss">Top5未完整覆盖</option><option value="all">全部题目</option></select><input id="search" placeholder="搜索题号、题目、片段ID"><span id="count"></span><!--cards-->
<script>const cards=[...document.querySelectorAll('article')];function update(){const m=document.querySelector('#mode').value,q=document.querySelector('#search').value.toLowerCase();cards.forEach(c=>c.classList.toggle('hidden',(m==='miss'&&c.dataset.miss!=='true')||(q&&!c.textContent.toLowerCase().includes(q))));document.querySelector('#count').textContent='显示 '+cards.filter(c=>!c.classList.contains('hidden')).length+' / 120'}document.querySelector('#mode').onchange=update;document.querySelector('#search').oninput=update;update();</script></html>"""
    for marker, value in {"headers": "".join("<th>Top"+str(k)+"</th>" for k in DEPTHS),
                          "table": table, "coverage": esc(coverage_text),
                          "recommendation": recommendation, "paired": pair,
                          "perf": perf, "memory": esc(memory), "cards": "".join(cards)}.items():
        page = page.replace("<!--"+marker+"-->", value)
    (DEST/"REPORT.html").write_text(page, encoding="utf8")
    save(DEST/"manifest.json", {"files": {p.name: sha(p) for p in DEST.iterdir()
                                          if p.is_file() and p.name != "manifest.json"},
                               "qa_calls": 0, "external_llm_calls": 0})
    save(ROOT/"docs/validation/20261125-rag-bm25-fusion-repair.json", {
        "status": "completed_offline_not_deployed", "summary": s, "comparison": comparison,
        "unit_tests_passed": 46, "old113_regression_this_round": False,
        "report": "output/rag-bm25-fusion-repair-20261125/REPORT.html",
        "production_changes": 0})
    print(json.dumps({"status": "reported", "new150": v, "recommendation": recommendation}, ensure_ascii=False))


if __name__ == "__main__":
    main()
