#!/usr/bin/env python3
"""Recover deleted correctness source rows from normalized raw-record embeds.

Finalization deliberately embeds every source record in its normalized row.
That makes a cleaned working directory recoverable without rerunning expensive
oracles. The original runner-provenance content cannot be reconstructed; this
tool writes an explicit recovery provenance that preserves its recorded hash
instead of pretending a contemporary runner file is the original.
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_open_model_correctness",
    REPO / "scripts" / "run_open_model_correctness.py",
)
CORRECTNESS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CORRECTNESS)


def recover(output: Path) -> dict:
    manifest_path = output / "correctness-manifest.json"
    normalized_root = output / "correctness-normalized"
    modern_root = output / "modern/correctness"
    provenance_path = modern_root / "runner-provenance.json"
    if not manifest_path.is_file() or not normalized_root.is_dir():
        raise ValueError(f"{output} has no finalized correctness to recover")
    existing_lanes = sorted(modern_root.glob("*.jsonl"))
    if existing_lanes or provenance_path.exists():
        raise ValueError(
            f"refusing to overwrite existing modern sources in {modern_root}"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_source: dict[Path, list[dict]] = collections.defaultdict(list)
    seen = set()
    normalized_hashes = {}
    for path in sorted(normalized_root.glob("*.jsonl")):
        normalized_hashes[path.name] = CORRECTNESS.sha256(path)
        for row in CORRECTNESS.read_jsonl(path):
            raw = row.get("raw_record")
            source = str(row.get("raw_record_source") or "")
            if not isinstance(raw, dict):
                raise ValueError(f"{path}: normalized row has no raw_record")
            if not source.startswith("modern/correctness/"):
                raise ValueError(
                    f"{path}: source {source!r} is not a deleted modern lane"
                )
            if CORRECTNESS.canonical_hash(raw) != row.get("raw_record_sha256"):
                raise ValueError(
                    f"{path}: embedded raw record failed its SHA-256 check"
                )
            key = (raw.get("model"), raw.get("id"))
            if key in seen:
                raise ValueError(f"duplicate embedded source pair: {key}")
            seen.add(key)
            by_source[output / source].append(raw)

    expected = manifest.get("records")
    if expected is not None and len(seen) != expected:
        raise ValueError(
            f"normalized embeds cover {len(seen)} pairs, manifest says {expected}"
        )
    if not seen:
        raise ValueError("no normalized source rows found")

    for path, rows in sorted(by_source.items(), key=lambda item: str(item[0])):
        rows.sort(key=lambda row: (str(row.get("id")), str(row.get("model"))))
        CORRECTNESS.atomic_jsonl(path, rows)

    recovery = {
        "schema_version": "reviewbench-open-correctness-recovery-provenance/v1",
        "recovered_records": len(seen),
        "recovered_sources": [
            str(path.relative_to(output)) for path in sorted(by_source)
        ],
        "source_manifest_sha256": CORRECTNESS.sha256(manifest_path),
        "original_runner_provenance_sha256": manifest.get(
            "runner_provenance_sha256"
        ),
        "normalized_source_sha256": normalized_hashes,
        "note": (
            "Raw source rows were reconstructed from normalized raw_record "
            "embeds after the working directory was deleted. The original "
            "runner-provenance content was unavailable; its hash is preserved "
            "above and is not represented as recovered content."
        ),
    }
    CORRECTNESS.atomic_json(provenance_path, recovery)
    return recovery


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = recover(args.output.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
