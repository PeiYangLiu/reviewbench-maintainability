#!/usr/bin/env python3
"""Resolve every frozen Rust toolchain, installing only missing pinned versions."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1]/"src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import atomic_json, selected_tasks, read_jsonl
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import ResourceRequest, GiB
from reviewbench_maintainability.agentic.scheduling import output_lock


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--legacy-root", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--workers", type=int, default=2)
    a = p.parse_args()
    config = load_config(a.config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    oracles = read_jsonl(Path(config["oracles"]))
    lane = a.legacy_root/"rust"
    plans = {}
    for row in map(json.loads, (lane/"plans.jsonl").read_text().splitlines()):
        index = row["input_index"]
        if row.get("plan_file"):
            file = lane/row["plan_file"]
            assert hashlib.sha256(file.read_bytes()).hexdigest() == row["plan_file_sha256"]
            row = json.loads(file.read_text())
        plans[index] = row
    versions = sorted({plans[oracles[i]["input_index"]]["toolchain"] for i in tasks
                       if "/rust/results.jsonl" in str(oracles[i].get("lane_results_source"))})
    services = ExecutionServices(config)
    output = a.out_dir.resolve()
    with output_lock(output):
        rustup_home = output/"rustup"
        cargo_home = output/"cargo"
        roots = [Path("/home/v-peiyangliu/.rustup"),
                 *sorted((lane/"cache").glob("rustup*")), rustup_home]
        def resolve(version):
            entry = {"version": version, "status": "failed"}
            source = next((r/"toolchains"/(version+"-x86_64-unknown-linux-gnu") for r in roots
                           if (r/"toolchains"/(version+"-x86_64-unknown-linux-gnu")/"bin/rustc").is_file()), None)
            # rustup accepts a shortened stable alias; verify the actual rustc
            # version rather than silently replacing the requested compiler.
            if source is None and version.endswith(".0"):
                alias = version[:-2]
                source = next((r/"toolchains"/(alias+"-x86_64-unknown-linux-gnu") for r in roots
                               if (r/"toolchains"/(alias+"-x86_64-unknown-linux-gnu")/"bin/rustc").is_file()), None)
            try:
                if source is None:
                    with services.gate.acquire(ResourceRequest("rustup:"+version, cpus=1,
                                                               memory_bytes=GiB, scratch_bytes=2*GiB)):
                        env = {**os.environ, "RUSTUP_HOME": str(rustup_home), "CARGO_HOME": str(cargo_home)}
                        cmd = ["/home/v-peiyangliu/.cargo/bin/rustup", "toolchain", "install", version,
                               "--profile", "minimal", "--no-self-update"]
                        log = output/(version+".log")
                        with log.open("w") as stream:
                            result = subprocess.run(cmd, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=1200)
                        entry.update(command=cmd, exit_status=result.returncode, log=str(log))
                        if result.returncode:
                            raise RuntimeError(log.read_text()[-1200:])
                        source = rustup_home/"toolchains"/(version+"-x86_64-unknown-linux-gnu")
                observed = subprocess.check_output([str(source/"bin/rustc"), "--version"], text=True, timeout=30).strip()
                if not version.startswith("nightly-") and not observed.startswith("rustc "+version+" "):
                    raise ValueError("compiler version mismatch: "+observed)
                entry.update(status="ready", root=str(source), observed_rustc=observed)
            except Exception as exc:
                entry["error"] = str(exc)
            return entry
        records = {}
        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            for record in pool.map(resolve, versions):
                records[record["version"]] = record
                atomic_json(output/"toolchains.json", records)
                print(json.dumps(record), flush=True)
    return 0 if all(r["status"] == "ready" for r in records.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
