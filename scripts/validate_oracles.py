"""Validate stable PASS_TO_PASS and changed-line coverage for curated tasks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import evaluate_e2e as ee
import trackA_filter as ta
import trackA_go as tg


ORACLE_SCHEMA_VERSION = "reviewbench-oracle/v1-stable-covered"


def validate_task(record: dict[str, Any], repos_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": record.get("id"),
        "repo": record.get("repo"),
        "evaluation_schema_version": ORACLE_SCHEMA_VERSION,
        "golden_correctness_ok": False,
        "changed_lines_covered": False,
    }
    context = ee.setup_and_p2p(record, repos_dir)
    if not context.get("ok"):
        result["setup_error"] = context.get("error")
        return result

    repo_dir = Path(context["repo_dir"])
    path = str(context["path"])
    changed = ta.changed_old_lines(
        repo_dir,
        str(context["start"]),
        str(context["target"]),
        path,
    )
    if context["lang"] == "go":
        go_dir = Path(context["go_dir"])
        cover_out = go_dir / ".bench_go_cover.out"
        cover_out.unlink(missing_ok=True)
        tg.go_test(
            go_dir,
            str(context["pkg"]),
            run_names=list(context["p2p"]),
            cover_out=str(cover_out),
        )
        executed = tg.go_covered_lines(go_dir, cover_out, path)
        hit = sorted(changed & executed)
        coverage = {
            "checked": True,
            "covered": bool(hit),
            "changed_old_lines": len(changed),
            "executed_hit": len(hit),
        }
    else:
        coverage = ta.coverage_of_changed(
            str(context["runner"]),
            repo_dir,
            list(context["p2p"]),
            path,
            changed,
        )

    result.update(
        {
            "language": context["lang"],
            "pass_to_pass": len(context["p2p"]),
            "golden_correctness_ok": True,
            "coverage": coverage,
            "changed_lines_covered": coverage.get("covered") is True,
            "flaky_excluded": int(context.get("flaky_excluded") or 0),
            "source_changed": list(context.get("source_changed") or []),
        }
    )
    ta.git(["reset", "--hard", "--quiet", str(context["start"])], repo_dir)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repos-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records = [
        json.loads(line)
        for line in args.input.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        records = records[: args.limit]
    completed: dict[str, dict[str, Any]] = {}
    if args.resume and args.out.exists():
        for line in args.out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if item.get("evaluation_schema_version") == ORACLE_SCHEMA_VERSION:
                    completed[str(item.get("id") or "")] = item

    output = []
    for index, record in enumerate(records, start=1):
        task_id = str(record.get("id") or "")
        if task_id in completed:
            result = completed[task_id]
        else:
            try:
                result = validate_task(record, args.repos_dir)
            except Exception as exc:  # noqa: BLE001 - persist per-task failure
                result = {
                    "id": task_id,
                    "repo": record.get("repo"),
                    "evaluation_schema_version": ORACLE_SCHEMA_VERSION,
                    "golden_correctness_ok": False,
                    "changed_lines_covered": False,
                    "setup_error": f"exception:{str(exc)[:300]}",
                }
        output.append(result)
        write_jsonl(args.out, output)
        print(
            f"[{index}/{len(records)}] {task_id}: "
            f"golden={result.get('golden_correctness_ok')} "
            f"p2p={result.get('pass_to_pass')} "
            f"covered={result.get('changed_lines_covered')} "
            f"error={result.get('setup_error')}",
            flush=True,
        )
    return 0


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records)
        + ("\n" if records else ""),
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
