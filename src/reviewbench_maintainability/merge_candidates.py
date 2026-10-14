"""Deduplicate candidate JSONL files from one or more mining runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .jsonl import read_jsonl, write_jsonl


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="inputs", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records: dict[str, dict[str, Any]] = {}
    input_rows = 0
    input_files = []
    for source in args.inputs:
        for path in candidate_files(source):
            input_files.append(str(path))
            for record in read_jsonl(path):
                input_rows += 1
                task_id = str(record.get("id") or "")
                if task_id:
                    records[task_id] = record

    ordered = [records[task_id] for task_id in sorted(records)]
    write_jsonl(args.out, ordered)
    report = {
        "input_files": len(input_files),
        "input_rows": input_rows,
        "unique_candidates": len(ordered),
        "duplicates_removed": input_rows - len(ordered),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def candidate_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    batch_files = sorted(source.glob("batches/*/candidates.jsonl"))
    if batch_files:
        return batch_files
    return sorted(source.glob("**/candidates.jsonl"))


if __name__ == "__main__":
    raise SystemExit(main())
