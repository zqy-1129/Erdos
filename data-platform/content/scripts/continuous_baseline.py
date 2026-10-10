"""据实生成连续实施基线指纹与检查点状态。只读；不修改任何业务文件。"""
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CONTENT = Path(os.path.abspath(__file__)).resolve().parent.parent
REPORTS = Path("D:/Erdos/reports/trae")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def fingerprint_tree(root: Path):
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(CONTENT)).replace("\\", "/")] = sha256_of(p)
    return out


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = {
        "config/competition_registry.json": CONTENT / "config/competition_registry.json",
        "config/taxonomy_v1.json": CONTENT / "config/taxonomy_v1.json",
        "config/stage03_parent_evidence.json": CONTENT / "config/stage03_parent_evidence.json",
    }
    for rel in ["bundle.json", "sample_manifest.json", "provenance.json", "assets.json"]:
        files["normalized/stage03/cumcm/" + rel] = CONTENT / "normalized/stage03/cumcm" / rel

    fingerprints = {rel: sha256_of(p) if p.exists() else None for rel, p in files.items()}
    fingerprints["schemas__v1"] = fingerprint_tree(CONTENT / "schemas" / "v1")
    fingerprints["out__pack"] = fingerprint_tree(CONTENT / "out" / "pack")
    fingerprints["out__inventory__source_files"] = sha256_of(CONTENT / "out/inventory/source_files.jsonl")

    try:
        head = subprocess.run(["git", "-C", "D:/Erdos", "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as exc:
        head = "git-unavailable: " + str(exc)

    baseline = {
        "generated_at": now,
        "scope": "continuous stages 04-08",
        "python": "E:/Anaconda/envs/pytorch/python.exe",
        "git_head": head,
        "fingerprints": fingerprints,
        "stage03_manifest_sha256": fingerprints.get("normalized/stage03/cumcm/sample_manifest.json"),
        "stage03_bundle_sha256": fingerprints.get("normalized/stage03/cumcm/bundle.json"),
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "continuous_run_baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    state = {
        "generated_at": now,
        "stages": {
            "stage04": "pending", "stage05": "pending", "stage06": "pending",
            "stage07": "pending", "stage08": "pending", "final": "pending",
        },
        "checkpoint": {"last_input_sha": None, "command": None, "outputs": []},
    }
    (REPORTS / "continuous_run_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("baseline written. head=" + head)
    print("schemas files:", len(fingerprints["schemas__v1"]))
    print("out/pack files:", len(fingerprints["out__pack"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
