#!/usr/bin/env python3
"""Count rounds that produced no executable action.

A round is wasted when the model returned nothing the harness could run:
the API rejected the request, or the text was not a parseable action. It
costs a round of budget and yields nothing.

This is the most sensitive measure of whether training took, because it
does not pass through correctness or the judge panel. The four-gate score
survives two lossy stages before it is read, so a real change can vanish
into the boundary noise (P104); a malformed response is mechanical and
unambiguous. Training drops the waste rate roughly sevenfold at
p=2e-08 while the correctness gate does not move at all, which is only
visible if the two are counted separately.

The buckets matter as much as the total. `context` means the request or
conversation exceeded the window and points at budget; `malformed` means
the model could not emit valid JSON and points at the model.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json

BUCKETS = ("context", "malformed", "other")


def bucket(error: str) -> str:
    # A rejected HTTP request and a conversation the harness itself declined
    # to send are the same problem seen from either side of the wire, so they
    # share a bucket -- splitting them would suggest two different fixes.
    if error.startswith("HTTP") or "did not fit" in error:
        return "context"
    if "valid JSON action" in error:
        return "malformed"
    return "other"


def tally(paths):
    per = collections.defaultdict(collections.Counter)
    for path in paths:
        for line in open(path, encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            model = record.get("model") or "?"
            for entry in record.get("rounds") or []:
                per[model]["rounds"] += 1
                if isinstance(entry.get("action"), dict):
                    continue
                per[model]["wasted"] += 1
                per[model][bucket(str(entry.get("error") or ""))] += 1
    return per


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", help="generation_results.jsonl paths or globs")
    args = parser.parse_args()
    paths = [p for pattern in args.results for p in sorted(glob.glob(pattern))] or args.results
    per = tally(paths)
    if not per:
        raise SystemExit("no rounds found")

    print(f"{'model':34s} {'rounds':>8s} {'wasted':>8s} {'rate':>7s}" + "".join(f"{b:>11s}" for b in BUCKETS))
    print("-" * (34 + 8 + 8 + 7 + 11 * len(BUCKETS)))
    for model, counts in sorted(per.items(), key=lambda kv: kv[1]["wasted"] / max(kv[1]["rounds"], 1)):
        rate = counts["wasted"] / counts["rounds"] if counts["rounds"] else 0.0
        print(
            f"{model:34s} {counts['rounds']:8d} {counts['wasted']:8d} {100 * rate:6.1f}%"
            + "".join(f"{counts[b]:11d}" for b in BUCKETS)
        )


if __name__ == "__main__":
    main()
