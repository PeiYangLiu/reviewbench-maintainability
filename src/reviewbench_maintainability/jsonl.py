"""Shared JSONL I/O for the non-agentic benchmark pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield records in order, ignoring whitespace-only lines."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def write_jsonl(
    path: Path,
    records: list[dict[str, Any]],
    *,
    atomic: bool = False,
) -> None:
    """Write sorted-key UTF-8 records; an empty list produces an empty file.

    Atomic writes retain the callers' sibling ``<suffix>.tmp`` replacement
    behavior, including leaving the temporary file behind if replacement fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    destination = path.with_suffix(path.suffix + ".tmp") if atomic else path
    destination.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records)
        + ("\n" if records else ""),
        encoding="utf-8",
    )
    if atomic:
        destination.replace(path)
