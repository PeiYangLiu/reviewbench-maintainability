#!/usr/bin/env python3
"""Report why a model failed to produce a patch, split by cause.

The four gates say whether a patch was good. They do not distinguish a model
that tried and failed from one that never touched the file, and those two want
opposite fixes: the first is a capability problem, the second is a stopping
problem. The RL session hit exactly this -- their gate pass rate fell across
RFT rounds while "never edited" doubled -- so the split is reported here
rather than rederived by hand each time.

`finish_reason=length` is reported alongside because a truncated round looks
like a model that gave up (P82), and telling those apart needs both columns.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator


TRUNCATED = "length"


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def round_actions(record: dict[str, Any]) -> list[str]:
    actions = []
    for entry in record.get("rounds") or []:
        action = entry.get("action")
        if isinstance(action, dict) and action.get("action"):
            actions.append(str(action["action"]))
    return actions


def round_truncated(response: dict[str, Any]) -> bool | None:
    """Was this response cut off? None when the deployment does not say.

    The two endpoints report it differently, and reading only the chat spelling
    makes every responses-API model look clean rather than unmeasured -- which
    is worse than reporting nothing, because it silently rules out the very
    cause being investigated (P82).
    """
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        reasons = [
            choice.get("finish_reason")
            for choice in choices
            if choice.get("finish_reason") is not None
        ]
        if reasons:
            return any(reason == TRUNCATED for reason in reasons)
    status = response.get("status")
    if status is not None:
        if status == "incomplete":
            details = response.get("incomplete_details") or {}
            reason = details.get("reason") if isinstance(details, dict) else None
            return reason in (None, "max_output_tokens")
        return False
    return None


def truncation(record: dict[str, Any]) -> tuple[int, int]:
    """Return (rounds whose response was cut off, rounds that reported either way)."""
    cut = total = 0
    for entry in record.get("rounds") or []:
        response = entry.get("raw_api_response")
        if not isinstance(response, dict):
            continue
        verdict = round_truncated(response)
        if verdict is None:
            continue
        total += 1
        if verdict:
            cut += 1
    return cut, total


def summarize(path: Path) -> dict[str, Any]:
    stats = {
        "tasks": 0,
        "generated": 0,
        "candidate_missing": 0,
        "never_edited": 0,
        "edited_no_patch": 0,
        "truncated_rounds": 0,
        "scored_rounds": 0,
        "tasks_with_truncation": 0,
    }
    for record in iter_records(path):
        stats["tasks"] += 1
        cut, total = truncation(record)
        stats["truncated_rounds"] += cut
        stats["scored_rounds"] += total
        if cut:
            stats["tasks_with_truncation"] += 1
        if record.get("status") == "generated" and record.get("apply_ok"):
            stats["generated"] += 1
            continue
        # No usable patch. Split by whether the model ever tried to edit: a run
        # that never called `edit` did not fail at editing, it declined to.
        stats["candidate_missing"] += 1
        if "edit" in round_actions(record):
            stats["edited_no_patch"] += 1
        else:
            stats["never_edited"] += 1
    return stats


def rate(numerator: int, denominator: int) -> str:
    if not denominator:
        return "-"
    return f"{100 * numerator / denominator:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        action="append",
        required=True,
        metavar="NAME=GENERATION_RESULTS_JSONL",
        help="label and path for one model or arm",
    )
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    rows = {}
    for spec in args.model:
        label, _, raw = spec.partition("=")
        if not raw:
            parser.error(f"expected NAME=PATH, got {spec!r}")
        path = Path(raw)
        if not path.exists():
            parser.error(f"{label}: no such file {path}")
        rows[label] = summarize(path)

    header = (
        f"{'model':30s} {'tasks':>6s} {'gen':>6s} {'missing':>8s} "
        f"{'never_edit':>11s} {'edit_nopatch':>13s} {'trunc_rounds':>13s}"
    )
    print(header)
    print("-" * len(header))
    for label, stats in rows.items():
        print(
            f"{label:30s} {stats['tasks']:6d} {stats['generated']:6d} "
            f"{stats['candidate_missing']:8d} {stats['never_edited']:11d} "
            f"{stats['edited_no_patch']:13d} "
            f"{rate(stats['truncated_rounds'], stats['scored_rounds']):>13s}"
        )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
