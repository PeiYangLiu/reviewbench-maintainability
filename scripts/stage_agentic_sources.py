#!/usr/bin/env python3
"""Stage a full task scope using local hot files and immutable Blob archive parts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reviewbench_maintainability.agentic.contracts import selected_tasks
from reviewbench_maintainability.agentic.resources import ResourceGate, ResourceLimits
from reviewbench_maintainability.agentic.scheduling import output_lock
from reviewbench_maintainability.agentic.sources import stage_sources
from reviewbench_maintainability.agentic.storage import MiB, open_cache


def main() -> int:
    import yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--scope-ids", type=Path)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--storage-config", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--part-mib", type=int, default=256)
    args = parser.parse_args()
    config = yaml.safe_load(args.storage_config.read_text())
    storage = config["storage"]
    tasks = selected_tasks(args.tasks, args.scope_ids)
    with output_lock(args.out_dir):
        gate = ResourceGate(Path(config["execution"]["gate_dir"]), Path(storage["scratch_dir"]),
                            ResourceLimits(**config["execution"].get("resource_limits", {})))
        result = stage_sources(tasks, args.repo_cache, open_cache(storage), Path(storage["scratch_dir"]),
                               args.out_dir, gate, workers=args.workers, part_bytes=args.part_mib * MiB)
        print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
