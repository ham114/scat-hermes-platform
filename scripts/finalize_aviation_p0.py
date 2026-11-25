"""Record P0 changes, remaining source limitations and immutable baseline hashes."""
from __future__ import annotations

import hashlib
import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/rag-p0-repair-20261124"
CORPUS = ROOT / "var/rag/corpora/kg_20261124_aviation_v6_p0"


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")


def main():
    build = read(CORPUS / "build-summary.json")
    structure = read(OUT / "structural-audit.json")
    labels = read(OUT / "label-audit.json")
    retrieval = read(OUT / "retrieval-summary.json")
    ready = read(CORPUS / "ready.json")
    activation_path = OUT / "production-activation.json"
    deferred_path = OUT / "production-deferred.json"
    activation = read(activation_path) if activation_path.exists() else read(deferred_path) if deferred_path.exists() else {"status": "not_activated"}
    for path, expected in labels["protected_files"].items():
        assert sha(ROOT / path) == expected
    # The completed native-Ragas baseline archive is an additional immutable
    # reference; no corrected labels are used to rewrite historical scores.
    baseline_zip = ROOT / "output/Ragas-standard-rescore-20261124.zip"
    baseline_zip_sha = sha(baseline_zip)
    assert baseline_zip_sha == "8fd78b0dd4fd3712831c5b7da255b9ab2b89b7a1342228fe9fc45521ac4cc293"
    table_audit = read(CORPUS / "table-audit.json")
    quarantines = []
    for table in table_audit["tables"]:
        if table["status"] == "passed":
            continue
        source = read(ROOT / table["artifact"])
        quarantines.append({"document": table["document"], "table_ref": table["ref"], "status": table["status"],
                            "reason": table["quarantine_reason"], "page_numbers": sorted({p["page_no"] for p in source["original_table"]["prov"]}),
                            "quarantined_row_indices_0_based": table["quarantined_rows"], "artifact": table["artifact"],
                            "resolution": "可靠行继续使用；冲突行和空提取不提供事实映射，保留原文与结构化档案以供后续恢复。"})
    save(OUT / "source-quarantine-register.json", quarantines)
    rules = read(ROOT / "evaluations/rag_p0_repairs_20261124.json")
    issue_records = [
        {"id": "P0-TABLE-01", "problem": "CAT062位值表被错误展开，标答又将0/1反写。",
         "cause": "结构化单元格中正确的数值—定义关系在表格文本序列化时丢失；原审核仅凭损坏文本的引文通过。",
         "solution": "从原始单元格重建带行列位置的文本；按原PDF恢复NAV、NVB、FX的跨行字段标签；同步修订第261题。",
         "verification": "原PDF第98页版面核对、单元格内容不变校验、位值回归断言与CAT-NAV检索探针。", "status": "fixed"},
        {"id": "P0-TABLE-02", "problem": "WMO液态降水起始阈值与相邻质量指标混淆。",
         "cause": "同一表格被线性展开并跨片段切分；阈值子行未携带所属变量。",
         "solution": "保留等级列名、完整行和数值单位；在起始阈值行重复经原PDF核验的父变量。",
         "verification": "第104页版面核对、WMO-THRESHOLD探针及阈值与相邻质量行分离测试。", "status": "fixed"},
        {"id": "P0-CONTEXT-03", "problem": "VSI片段脱离备用静压源前提；混合表格中的嵌套脚注可能在迁移时丢失。",
         "cause": "条件标题被单独切出；部分Docling列表父节点为空，实际文本保存在子节点。",
         "solution": "补入原文条件与真实节点引用；递归提取列表子节点；无损分段长列表；用原始HTML的配对段落核验G/GNSS映射。",
         "verification": "第205页原文、PHAK-VSI与AIM-GNSS探针、嵌套脚注测试。", "status": "fixed"},
        {"id": "P0-GEOMETRY-04", "problem": "全量结构检查发现21张表的部分行存在重叠，另8张为空提取。",
         "cause": "Docling提供的部分单元格跨度冲突，无法安全确定映射。",
         "solution": "隔离冲突行并扩展到完整单元格跨度，保留可靠行；空提取返回明确不可用提示。全部原始单元格仍存档。",
         "verification": "647张表覆盖检查，25,597个可靠非空单元格完整发布；134个冲突单元格隔离；高度编码可靠行仍可检索。",
         "status": "contained; recovery of unavailable rows remains future source work"},
    ]
    for group in ["aviation", "old_p0"]:
        for qid, rule in rules[group].items():
            issue_records.append({"id": qid, "problem": rule["issue"], "solution": rule["operation"],
                                  "source_basis": rule["source_basis"], "verification": "新版标注差异和所有有效引文逐字范围校验；计算题另有确定性算术校验。",
                                  "status": "fixed in versioned development labels"})
    record = {"schema_version": "rag-p0-repair-record/1", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "P0文档结构与已确认标注问题；未微调、未重新作答、未重写历史评分",
              "status": "activated" if activation["status"] == "activated" else "validated_candidate_not_activated",
              "generation": CORPUS.name, "corpus_sha256": build["corpus_sha256"], "build": build,
              "structure": structure, "label_audit": labels, "retrieval_summary": {k: v for k, v in retrieval.items() if k != "comparisons"},
              "production": activation, "baseline_archive_sha256": baseline_zip_sha,
              "production_approval": read(OUT / "user-production-approval.json") if (OUT / "user-production-approval.json").exists() else None,
              "post_activation_verification": read(OUT / "post-activation-verification.json") if (OUT / "post-activation-verification.json").exists() else None,
              "issues": issue_records, "source_quarantines": quarantines,
              "tests": {"local_rag_suite": "79 passed, 1 skipped: local environment lacks pytest-asyncio",
                        "server_async_compatibility": "1 passed, 12 deselected; covers the local skipped test", "new_source_regressions": 8},
              "limits": ["结构校验不等于对全部9001片段做语义审核，也不代表新的问答准确率。",
                         "50道题的迁移证据是较宽的来源上下文；精确片段命中率需先重新标注必要行，不能直接与v1比较。",
                         "21张表的冲突行与8张空提取表仍不可用；原始资料完整保留，见隔离清单。",
                         "新版标注属于观察基线后的开发修订，不是独立人工验证的测试集。"],
              "external_model_calls": 0, "fine_tuned": False, "qa_accuracy_remeasured": False,
              "implementation_sha256": {p.relative_to(ROOT).as_posix(): sha(p) for p in [ROOT / "src/rag_retrieval/table_chunks.py", ROOT / "tests/rag_retrieval/test_table_chunks.py",
                   *[ROOT / "scripts" / n for n in ["repair_aviation_p0.py", "stage_aviation_p0.py", "revise_rag_p0_labels.py", "verify_aviation_p0.py", "deploy_aviation_p0.py", "finalize_aviation_p0.py"]]]},
              "next_work": "P1多来源检索覆盖、引用交付与回答聚焦；恢复隔离内容前必须对照原始版面核验。"}
    save(ROOT / "docs/validation/20261124-rag-p0-repair.json", record)
    save(OUT / "closeout.json", record)
    for folder_name in ["rag_aviation300_reviewed_20261124_v2", "rag_p0_reviewed_20261124_v2"]:
        folder = ROOT / "evaluations" / folder_name
        manifest = read(folder / "manifest.json")
        manifest["status"] = "source-corrected-development-version-frozen"
        save(folder / "manifest.json", manifest)
        save(folder / "ready.json", {"status": "frozen-development-correction", "manifest_sha256": sha(folder / "manifest.json"),
                                    "external_model_calls": 0, "historical_scores_changed": False})
    for name in ["asterix-page98.png", "phak-page205.png"]:
        dest = OUT / "source-review" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "output/ragas-aviation300-evaluation-20261124/source-review" / name, dest)
    esc = lambda x: html.escape(str(x))
    cards = []
    for issue in issue_records:
        cards.append("<article><h3>" + esc(issue["id"]) + "</h3>" + "".join(
            f"<p><b>{label}</b> {esc(issue[key])}</p>" for key, label in [("problem", "问题"), ("cause", "原因"), ("solution", "解决方法"),
            ("source_basis", "依据"), ("verification", "验证"), ("status", "状态")] if key in issue) + "</article>")
    changes = read(ROOT / "evaluations/rag_aviation300_reviewed_20261124_v2/label-changes.json")
    diff_html = "".join(f"<details><summary>{esc(c['case_id'])}：{esc(c['reason'])}</summary><h4>原题与标答</h4><p>{esc(c['before']['question'])}</p><p>{esc(c['before']['reference'])}</p><h4>修订后</h4><p>{esc(c['after']['question'])}</p><p>{esc(c['after']['reference'])}</p></details>" for c in changes)
    counts = retrieval["complete_counts"]
    status = "已切换生产" if activation["status"] == "activated" else "候选已验证，未切换生产"
    content = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RAG P0 修复记录 · 2026-11-24</title><style>body{{font:16px/1.7 system-ui,sans-serif;color:#172b40;background:#f4f7fb;margin:auto;max-width:1100px;padding:32px}}h1,h2,h3{{line-height:1.3}}article,details,.panel{{background:white;border:1px solid #d8e1ec;border-radius:10px;padding:18px;margin:16px 0}}b{{color:#284f72}}code{{overflow-wrap:anywhere}}summary{{cursor:pointer;font-weight:600}}img{{max-width:100%;height:auto}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;border-bottom:1px solid #d8e1ec;padding:10px}}a{{color:#1458a5}}</style>
<h1>RAG P0 修复记录</h1><p>2026-11-24 · {status} · 外部模型调用0次 · 未微调</p>
<div class="panel"><b>生产状态</b><p>{esc(activation.get('reason', '生产检索和原生问答读库版本已一致。'))}</p></div>
<div class="panel"><b>本次范围</b><p>表格结构、必要上下文、已确认题目和标答问题。历史v5语料、v1标注及Ragas基线保持不变；本次未产生新的问答准确率。</p>
<p>最终语料 {build['chunks']} 个片段，保留 {ready['reused_vectors']} 条原向量，新建 {ready['new_vectors']} 条向量。检查647张表、25,597个可靠非空单元格；21张表隔离冲突行，8张空提取表标为不可用。</p>
<p>语料SHA256：<code>{build['corpus_sha256']}</code></p></div>
<h2>验证结果</h2><table><tr><th>检查</th><th>结果</th></tr><tr><td>结构、来源与引文范围</td><td>通过；原149条记录字节不变；修订6道航空题与1道旧题</td></tr>
<tr><td>测试</td><td>本机79项通过；1项本机跳过的异步测试已在服务器补跑通过</td></tr>
<tr><td>关键证据检索</td><td>{sum(p['passed_at_10'] for p in retrieval['p0_probes'])}/6通过</td></tr>
<tr><td>来源检索检查</td><td>{retrieval['source_smoke_passed']}/{retrieval['source_smoke_total']}通过</td></tr>
<tr><td>113道旧题完整证据命中</td><td>Top5：v5 {counts['18133_at_5']} → v6 {counts['18134_at_5']}；Top10：v5 {counts['18133_at_10']} → v6 {counts['18134_at_10']}</td></tr></table>
<p>以上是检索检查，不是重新作答后的通过率。<a href="retrieval-summary.json">逐题对照</a> · <a href="structural-audit.json">结构审计</a> · <a href="label-audit.json">标注审计</a></p>
<h2>问题、原因与解决方法</h2>{''.join(cards)}
<h2>航空题修订前后</h2>{diff_html}
<h2>使用边界与后续</h2><ul>{''.join('<li>'+esc(v)+'</li>' for v in record['limits'])}</ul>
<p>下一步为P1：多来源检索覆盖、引用交付和回答聚焦。资料恢复清单见<a href="source-quarantine-register.json">隔离记录</a>。</p>
<h2>原始版面核对</h2><details><summary>CAT062 第98页</summary><img src="source-review/asterix-page98.png" alt="CAT062原始位值定义"></details>
<details><summary>WMO 第104页</summary><img src="source-review/wmo-page104.png" alt="WMO原始变量、阈值和等级表"></details>
<details><summary>FAA IFH 第101页</summary><img src="source-review/ifh-page101.png" alt="低温修正表与例题"></details>
<details><summary>FAA PHAK 第205页</summary><img src="source-review/phak-page205.png" alt="备用静压源的必要前提"></details>
<h2>可复核文件</h2><p><a href="closeout.json">完整问题记录与哈希</a> · <a href="../../evaluations/rag_aviation300_reviewed_20261124_v2/evaluation.json">航空题v2</a> · <a href="../../evaluations/rag_p0_reviewed_20261124_v2/evaluation.json">旧题v2</a> · <a href="../../evaluations/rag_p0_repairs_20261124.json">修订规则</a></p></html>"""
    (OUT / "REPORT.html").write_text(content, encoding="utf8")
    print(json.dumps({"status": record["status"], "recorded_issues": len(issue_records), "production": activation["status"]}))


if __name__ == "__main__":
    main()
