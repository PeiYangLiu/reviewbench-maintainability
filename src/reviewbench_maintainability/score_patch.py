"""CLI for comparing static maintainability metrics across before/after trees."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .static_metrics import compare_paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-dir", type=Path, required=True)
    parser.add_argument("--after-dir", type=Path, required=True)
    parser.add_argument("--paths", nargs="+", required=True)
    parser.add_argument("--category", choices=("complexity", "duplication", "dead_code", "manual"), default=None)
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = compare_paths(args.before_dir, args.after_dir, args.paths, args.category)
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
