"""Read referenced split filenames only; distinguish current CSV membership from training proof."""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"


def members(path):
    result = {}
    with os.scandir(path) as entries:
        for index, entry in enumerate(entries):
            assert index < 250000, "partition_inventory_limit"
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(".csv"):
                result[entry.name[:-4]] = entry.stat().st_size
    return result


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    os.umask(0o077)
    target = OUT / "training-partition-audit.json"
    assert not target.exists()
    config_path = ROOT / "var/deploy/gpu/conda-migration/artifacts/config_v25.json"
    config = json.loads(config_path.read_text())
    directory = Path(config["train_tensor_path"]).parent
    train, valid = members(directory / "train/1"), members(directory / "valid/1")
    selected = "100002"
    source = directory / "SCAT_csv/scat20161015_20161021" / (selected + ".csv")
    checks = {}
    for label, inventory in [("train", train), ("valid", valid)]:
        path = directory / label / "1" / (selected + ".csv")
        checks[label] = {"filename_present": selected in inventory, "same_bytes_as_batch_source": path.is_file() and source.is_file() and sha(path) == sha(source), "bytes": path.stat().st_size if path.exists() else None}
    report = {"status": "current_partition_inventory_completed_training_lineage_not_proven", "config_sha256": sha(config_path), "referenced_tensor_files": {k: {"path": config[k], "exists": Path(config[k]).is_file(), "bytes": Path(config[k]).stat().st_size, "content_hash": "not_computed", "loaded": False} for k in ["train_tensor_path", "test_tensor_path"]}, "train_csv_count": len(train), "valid_csv_count": len(valid), "train_valid_filename_intersection_count": len(set(train) & set(valid)), "train_filename_size_manifest_sha256": hashlib.sha256(json.dumps(train, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), "valid_filename_size_manifest_sha256": hashlib.sha256(json.dumps(valid, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), "acceptance_flight_100002": checks, "checkpoint_to_tensor_and_csv_generation_manifest": "missing", "independent_holdout_admission": "not_satisfied", "scope": "current referenced directories only; matching filenames/CSV bytes do not prove which exact tensor members trained the checkpoint", "new_gpu_inference_calls": 0, "new_external_model_calls": 0}
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
