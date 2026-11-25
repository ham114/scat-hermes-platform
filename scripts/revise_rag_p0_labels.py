"""Apply source-reviewed P0 label corrections and migrate citations to v6.

This is a post-baseline development revision, not an independent test set.
Broad table context migration is explicitly flagged; exact-chunk retrieval
coverage must not be compared with v1 until those row targets are refined.
"""
from __future__ import annotations

import copy
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V5 = ROOT / "var/rag/corpora/kg_20261123_aviation_v5"
V6 = ROOT / "var/rag/corpora/kg_20261124_aviation_v6_p0"
RULES = ROOT / "evaluations/rag_p0_repairs_20261124.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")


def evidence(row, quote=None):
    quote = quote or row["text"]
    start = row["text"].index(quote)
    return {k: row[k] for k in ("chunk_id", "source_id", "source_version", "text_sha256", "section_path", "locator")} | {
        "quote": quote, "start_char": start, "end_char": start + len(quote)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-draft", action="store_true")
    args = parser.parse_args()
    rules = read(RULES)
    mapping = read(V6 / "chunk-migration.json")
    rows = {r["chunk_id"]: r for r in map(json.loads, (V6 / "rag_chunks.v1.jsonl").read_text(encoding="utf8").splitlines())}
    baseline = ROOT / "evaluations/rag_aviation300_reviewed_20261124_v1"
    target = ROOT / "evaluations/rag_aviation300_reviewed_20261124_v2"
    old_p0 = ROOT / "evaluations/rag_p0_reviewed_20261123_v1"
    new_p0 = ROOT / "evaluations/rag_p0_reviewed_20261124_v2"
    if target.exists() or new_p0.exists():
        if not args.refresh_draft or (target / "ready.json").exists() or (new_p0 / "ready.json").exists():
            raise ValueError("versioned_labels_already_exist")
        for folder in (target, new_p0):
            manifest = read(folder / "manifest.json")
            assert manifest["external_model_calls"] == 0 and manifest.get("status") == "draft-pending-final-audit"
    protected = {p.relative_to(ROOT).as_posix(): sha(p) for folder in [baseline, old_p0] for p in folder.glob("*.json")}
    protected[(V5 / "rag_chunks.v1.jsonl").relative_to(ROOT).as_posix()] = sha(V5 / "rag_chunks.v1.jsonl")
    dataset = read(baseline / "evaluation.json")
    original = copy.deepcopy(dataset)
    corpus_sha = sha(V6 / "rag_chunks.v1.jsonl")
    changed, broad_cases = [], []
    for case in dataset["cases"]:
        qid = case["case_id"]
        before = copy.deepcopy(case)
        case["baseline_evidence"] = copy.deepcopy(case["claims"])
        broad = False
        for claim in case["claims"]:
            migrated = []
            for e in claim["evidence"]:
                candidates = [rows[cid] for cid in mapping[e["chunk_id"]]]
                exact = [r for r in candidates if e["quote"] in r["text"]]
                if exact:
                    migrated.extend(evidence(r, e["quote"]) for r in exact[:1])
                else:
                    # Full source context is conservative grounding; do not
                    # pretend this is a new minimal exact-chunk retrieval gold.
                    broad = True
                    migrated.extend(evidence(r) for r in candidates)
            claim["evidence"] = list({e["chunk_id"]: e for e in migrated}.values())

        rule = rules["aviation"].get(qid)
        if rule:
            case["question"] = rule.get("question", case["question"])
            case["reference"] = rule.get("reference", case["reference"])
            op = rule["operation"]
            if op == "keep_first_four_claims_move_circling_to_optional":
                case["optional_claims"] = case["claims"][4:]
                case["claims"] = case["claims"][:4]
                case["reference"] = "".join(c["text"] for c in case["claims"])
            elif op == "scope_existing_reference_to_named_edition":
                case["claims"][0]["text"] = "FAA-H-8083-15B的Glossary中，UHF条目写出的范围为962 MHz至1213 MHz。"
                case["source_scope_note"] = "该版本条目存在与其他FAA资料口径不一致的风险；只评价明确来源的复述，不评价通用频段定义。"
            elif op == "separate_airspace_minimum_and_traffic_clearance_conditions":
                minimum = rows["kco_b311d77d5f6fdedfd03f342457b6df83"]
                traffic = rows["kco_ccbff2c2c28507da40536efea87f79b4"]
                quote = "ATC will space any airplane without RNP 10 or RNP 4 capability such that at least 90 NM lateral separation is maintained with other airplanes in the Miami Oceanic CTA/FIR, and at least 100 NM separation is maintained in the Houston, Monterrey, and Merida CTA/FIRs."
                case["claims"] = [
                    {"text": "仅知未获RNP 10或RNP 4授权及在该区域运行，不能唯一确定90或100 NM，还需明确所在CTA/FIR。", "evidence": [evidence(minimum, quote)], "reasoning_type": "missing_input_from_explicit_source_cases"},
                    {"text": "Miami Oceanic CTA/FIR至少90 NM，Houston、Monterrey和Merida CTA/FIR至少100 NM。", "evidence": [evidence(minimum, quote)]},
                    {"text": "traffic allows限定偏好航路和/或高度能否获准，优先给予RNP 10或RNP 4航空器；不能单凭traffic选择90或100 NM。", "evidence": [evidence(traffic, before["claims"][1]["evidence"][0]["quote"])]},
                ]
                broad = False
            elif op == "recalculate_from_minus20_table_row":
                hits = [r for r in rows.values() if r["source_id"] == "faa-instrument-flying-handbook" and
                        r["locator"].get("doc_item_refs") == ["#/tables/0"] and
                        r["locator"].get("document_json", "").endswith("faa-h-8083-15b-p0099-0200/document.rag.json") and
                        5 in r["locator"].get("row_indices", [])]
                assert len(hits) == 1
                support = [evidence(hits[0])]
                case["claims"] = [
                    {"text": "高于机场的高度为1800-500=1300英尺。", "evidence": support, "derivation": "question inputs: 1800-500"},
                    {"text": "-20°C行在高于机场1000和1500英尺处分别给出140与210英尺修正量。", "evidence": support},
                    {"text": "线性插值修正量为140+(1300-1000)/500*(210-140)=182英尺。", "evidence": support, "derivation": "140+(1300-1000)/(1500-1000)*(210-140)"},
                    {"text": "未取整修正后高度为1982英尺，向上取整至100英尺为2000英尺。", "evidence": support, "derivation": "ceil((1800+182)/100)*100"},
                ]
                case["numeric_check"] = {"height_above_airport_ft": 1300, "temperature_c": -20,
                                         "table_points_ft": [[1000, 140], [1500, 210]], "correction_ft": 182,
                                         "unrounded_corrected_altitude_ft": 1982, "rounded_altitude_ft": 2000,
                                         "rounding": "ceil corrected altitude to 100 ft; explicit educational calculation"}
                broad = False
            elif op == "correct_nav_nvb_mapping_preserve_gnss_claims":
                hit = next(r for r in rows.values() if r["source_id"] == "eurocontrol-asterix-cat062" and r["locator"].get("doc_item_refs") == ["#/tables/99"])
                assert "C3 = = 0 | C4 = Trajectory Intent Data is available for this aircraft" in hit["text"]
                case["claims"][0] = {"text": "I062/380子字段#8中，bit-8（NAV）：0=可用，1=不可用。", "evidence": [evidence(hit)]}
                case["claims"][1] = {"text": "I062/380子字段#8中，bit-7（NVB）：0=有效，1=无效。", "evidence": [evidence(hit)]}
                gnss = next(r for r in rows.values() if "Original HTML paired paragraphs, Item 10a: G = GNSS (See Note 2)" in r["text"])
                note = next(r for r in rows.values() if r["source_id"] == "faa-aim" and r["locator"].get("document_json", "").endswith("aim-appendix_4/document.rag.json") and
                            'If the letter "G" is used, the types of external GNSS augmentation' in r["text"] and r["chunk_type"] == "text")
                case["claims"][2]["evidence"] = [evidence(gnss, "Original HTML paired paragraphs, Item 10a: G = GNSS (See Note 2)")]
                note_quote = 'If the letter "G" is used, the types of external GNSS augmentation, if any, are specified in Item 18 following the indicator NAV/ and separated by a space.'
                case["claims"][3]["evidence"] = [evidence(note, note_quote)]
                broad = False
            elif op == "restore_alternate_static_antecedent":
                hit = next(rows[cid] for cid in mapping["kco_da331635accf9991bd1ad434da9d06eb"])
                quote = "When the alternate static source pressure is used, the following instrument indications are observed:\nThe VSI shows a momentary climb and then stabilizes if the altitude is held constant."
                case["claims"][0] = {"text": "启用备用静压源后，若飞机实际高度保持不变，VSI先瞬时显示爬升，然后稳定。", "evidence": [evidence(hit, quote)]}
                case["claims"][2]["text"] = "正常静压系统下，仪表校准正确且稳定平飞时，VSI指示零。"
            else:
                raise ValueError(op)
            case["baseline_review"] = {k: before.get(k) for k in ["automatic_review", "human_review"]}
            case["automatic_review"] = {"verdict": "not_rerun", "method": "offline source correction; mechanical evidence audit only"}
            case["human_review"] = "assistant_revised_not_individually_human_approved"
            case["p0_revision"] = rule
            changed.append({"case_id": qid, "before": {k: before[k] for k in ["question", "reference", "required_claims"]},
                            "after": {"question": case["question"], "reference": case["reference"], "required_claims": [c["text"] for c in case["claims"]]}, "reason": rule["issue"]})
        if qid == "AV300-20261124-090":
            hit = next(r for r in rows.values() if "Parent measurand: Liquid precipitation intensity" in r["text"] and "C2[Class A] = 0.1 mm/h" in r["text"])
            for claim in case["claims"]:
                claim["evidence"] = [evidence(hit)]
            broad = False
        case["required_claims"] = [c["text"] for c in case["claims"]]
        case["supporting_evidence_groups"] = list({tuple(sorted(e["chunk_id"] for e in c["evidence"])) for c in case["claims"]})
        case["supporting_evidence_groups"] = [list(g) for g in sorted(case["supporting_evidence_groups"])]
        case["corpus_sha256"] = corpus_sha
        case["retrieval_gold_status"] = "broad_source_context_not_minimal_row_gold" if broad else "exact_quote_rebound_or_source_reviewed"
        if broad:
            broad_cases.append(qid)
        for claim in case["claims"]:
            assert claim["evidence"]
            for e in claim["evidence"]:
                assert rows[e["chunk_id"]]["text"][e["start_char"]:e["end_char"]] == e["quote"]

    dataset.update(dataset_version=target.name, corpus_sha256=corpus_sha,
                   scope="post-baseline P0 development correction; source context migrated to v6; not independent human validation",
                   baseline_dataset_sha256=sha(baseline / "evaluation.json"),
                   retrieval_label_policy="Broad migrated source contexts are not minimal retrieval targets. Exclude flagged cases from exact-chunk coverage comparisons until row targets are reannotated.")
    save(target / "evaluation.json", dataset)
    save(target / "label-changes.json", changed)
    save(target / "manifest.json", {"cases": 300, "content_revised_cases": [c["case_id"] for c in changed], "corpus_sha256": corpus_sha,
                                    "rules_sha256": sha(RULES), "files": {"evaluation.json": sha(target / "evaluation.json"), "label-changes.json": sha(target / "label-changes.json")},
                                    "broad_context_cases": broad_cases, "external_model_calls": 0, "qa_rerun": False,
                                    "baseline_scores_changed": False, "independent_human_approved_revisions": 0, "status": "draft-pending-final-audit"})

    p0 = read(old_p0 / "evaluation.json")
    case = next(c for c in p0["cases"] if c["case_id"] == "P0-20261123-024")
    p0_before = copy.deepcopy(case)
    case.update(optional_claims=case["claims"][1:], claims=case["claims"][:1], reference=rules["old_p0"][case["case_id"]]["reference"])
    case["required_claims"] = [c["text"] for c in case["claims"]]
    case["p0_revision"] = rules["old_p0"][case["case_id"]]
    case["human_review"] = "assistant_revised_not_individually_human_approved"
    p0.update(dataset_version=new_p0.name, scope="post-baseline label scope correction; v4 corpus pin retained")
    save(new_p0 / "evaluation.json", p0)
    save(new_p0 / "label-changes.json", [{"case_id": case["case_id"], "before": p0_before, "after": case}])
    (new_p0 / "regression.json").write_bytes((old_p0 / "regression.json").read_bytes())
    save(new_p0 / "manifest.json", {"corpus_sha256": p0["corpus_sha256"], "baseline_dataset_sha256": sha(old_p0 / "evaluation.json"),
                                    "content_revised_cases": [case["case_id"]], "rules_sha256": sha(RULES), "external_model_calls": 0, "status": "draft-pending-final-audit",
                                    "files": {n: sha(new_p0 / n) for n in ["evaluation.json", "label-changes.json", "regression.json"]}})
    assert 140 + (1300 - 1000) / (1500 - 1000) * (210 - 140) == 182
    assert len(dataset["cases"]) == len(original["cases"]) == 300
    assert {str(p): sha(ROOT / p) for p in protected} == protected
    save(ROOT / "output/rag-p0-repair-20261124/label-audit.json", {"status": "passed", "aviation_cases": 300, "aviation_content_revised": 6,
         "old_p0_content_revised": 1, "protected_files": protected, "broad_context_cases": broad_cases,
         "all_active_evidence_quotes_exact": True, "numeric_check": "140+(1300-1000)/500*70=182; 1800+182=1982; ceil to 100=2000",
         "external_model_calls": 0, "qa_rerun": False, "baseline_scores_changed": False})
    print(json.dumps({"status": "passed", "content_revisions": 7, "broad_context_cases": len(broad_cases)}))


if __name__ == "__main__":
    main()
