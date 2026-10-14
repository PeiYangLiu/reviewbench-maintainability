"""Bound recovery of retained Cargo inputs, never reconstruction from a cache.

The session lock for DataFusion has an original Oracle digest. The gRPC lock
does not: its digest and run-log digests below are *recovery pins*, checked
against both retained worktrees and every reference Cargo manifest. Successful
old runs alone are not a lockfile attestation or proof of candidate compatibility.
The latter is checked offline, on the full candidate, by rust_lock_entry.py.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git

MAX_INPUT_BYTES = 16 * 1024 * 1024

# These are the eight named remaining inputs, not a search over mutable caches.
# (index, repository, PR, comment, category, start, effective plan, original result)
_CASES = (
    (23, "apache/datafusion", 7400, 1324946837, "complexity",
     "a7f4fee1ad5662168e0b0a3836a2982eb95893bd",
     "8db5c21efc3cf980cae02487320d376de3ff68259e40af13f066c54cc814ef5d",
     "77f8464717257fef8b5898b21f969deaca1392e504e614a7fa81348ada372ccf"),
    (44, "google/zerocopy", 1752, 1777397723, "complexity",
     "fa664c3387b3931c4b419db7921e62403c81f880",
     "07df7db13c72bd3db0cd83eec5dce7a18aa7df1f9b85d0378f5c1a3d637edf76",
     "1d7fc357fb98c3135d57cb137242aadc1cba74fb4a4f6fe3ea460a5c24637fcb"),
    (45, "googlefonts/fontations", 191, 1055650458, "complexity",
     "6d694d0019cdac493e70efa0d485ddbee45d234a",
     "e51d6f777fd9c9418b4359e277ece5b34ad16d40ccc1cf1429abefdad67a8f98",
     "0bc8f0a049c1276f5de32bcf68b5ae706f2bb3c33ac36faeba1b824274d11e2b"),
    (56, "libbpf/blazesym", 70, 1134655780, "complexity",
     "a891ccda739e09ee0ca8db18e7a79ca047d02de8",
     "ccdfa7886ab4ee18fcbd2cf9d64f87b6918730a624cc43019b497d8e6f5085ba",
     "67fe5ab5f7dff165d0b3354702fa268165c98e52ffa237052bf9ef52161fe7c6"),
    (83, "prometheus/client_rust", 82, 956892586, "complexity",
     "501147539c064097c7f842fac050defeaf792533",
     "bbed5a133219d614172e397fec58632482b4ac237785e2f24101eace777bcc6e",
     "ba2637a1381cf656c8329a62c6c77631a0acb21fa6f335801861b3f9c8c66b6a"),
    (96, "scylladb/scylla-rust-driver", 509, 962978126, "api_design",
     "f07ca60c2437a48b5f00c7de386ada757e9b7715",
     "ae12d25183079a0c220ff95bd2946acf376489d0111c3c8ff7c816be8821b087",
     "80811e8b744d78e83f68973f3254318ce399ab956fd42edb8fb88169180f87bc"),
    (104, "swift-nav/libsbp", 1218, 970220101, "naming",
     "ecaa47437bb9ad22cfcae7dc3d180110f752645f",
     "d708b6c08bfd230c747789c315a3515e8eed3c1bb6dc12f2d7ac7cdd290f2421",
     "99615c8634ed93ead626cc42607279be51a71c6b326b8cc17b3c92578e7741e2"),
    (105, "tikv/grpc-rs", 615, 1205702187, "modularity",
     "8609072ad5b5c99a6f8f6737b69d42629c8d1dff",
     "2e9141c2357f119d2465a94dffe02498b429866f19abcdebf4261d5de813485b",
     "fbd11a5bbbd4d13fd8f61e7d5617e354559df06b34f4a4c28ed952ac969e63fc"),
)
BINDINGS = {
    f"github__{repo.replace('/', '__')}__pull_{pr}__comment_{comment}__{category}": {
        "input_index": index, "repo": repo, "start_sha": start,
        "plan_sha256": plan, "oracle_file_sha256": oracle,
        "artifacts": f"artifacts/{index:03}-{repo.replace('/', '__')}-pr{pr}-c{comment}",
    }
    for index, repo, pr, comment, category, start, plan, oracle in _CASES
}
for _binding in BINDINGS.values():
    if _binding["input_index"] == 23:
        _binding.update(
            lock_path="tests/oracle-datafusion/Cargo.lock",
            source="session/023/oracle-Cargo.lock",
            lock_sha256="da30a12d6ce21980c9597515865637fbc1b7b2e80b2a1472cd9413c870f1c0ab",
            public_cwd="datafusion-cli", public_package="datafusion",
            public_lock_path="datafusion-cli/Cargo.lock",
            public_lock_sha256="2d38192e9f4806f2d838598cb55cbc67eaf8180b99ed2718d04357e1fdd6fc30",
        )
    elif _binding["input_index"] == 105:
        _binding.update(
            lock_path="Cargo.lock", retained=True,
            lock_sha256="56cae4e3886938d447ce0d95d41a2e808b51cb05a0e50699f8d79f70d5814ed5",
            logs={
                "start-run1-attempt1.log": "563dd71de4508ca9ffceaf573eab45e520341a6448e11b4aebffc1cbf04a5509",
                "start-run2-attempt1.log": "7c401bcf9971eb1b1357972bcdd4c877aba49ff2f7fb827bac6d915ade60a14e",
                "start-run3-attempt1.log": "eabba2b2aafbda9c9ff5d1a5f90604ff366eb1407d9ce29ad6c08989e2ddb20f",
                "golden-run1-attempt1.log": "9ac189f3065ffdf7f0a7559a1474bd43e2bf12dd92f607d435b0a917981eb955",
                "golden-run2-attempt1.log": "05aafbe36cd65c733516e3f9a2d04fc7c82a66d3aeb1859598c9cd34d0b8dd41",
                "golden-run3-attempt1.log": "6633c25c0efed2546d9f0d98dad9645e564ca5479eb7408bde2dd3b8af8bbc54",
            },
        )


def attested_bytes(root, name, expected):
    path = root / repo_path(name)
    if (root.is_symlink() or not path.is_file() or path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
            or any(p.is_symlink() for p in path.parents if p != root and p.is_relative_to(root))):
        raise ContractError("Rust frozen lock evidence is absent/not a regular input: " + str(path))
    with path.open("rb") as stream:
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ContractError("Rust frozen lock evidence exceeds its input bound: " + str(path))
    if hashlib.sha256(data).hexdigest() != expected:
        raise ContractError("Rust frozen lock evidence hash changed: " + str(path))
    return data


def _tree_inputs(repository, revision, lock_names):
    manifests, locks = {}, dict.fromkeys(lock_names)
    for entry in git(repository, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not entry:
            continue
        info, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode()
        if Path(name).name != "Cargo.toml" and name not in locks:
            continue
        if info.split()[0] not in {b"100644", b"100755"}:
            raise ContractError("Rust reference manifest/lock is not a regular file: " + name)
        data = git(repository, "show", revision + ":" + repo_path(name))
        if len(data) > MAX_INPUT_BYTES:
            raise ContractError("Rust reference input exceeds its bound: " + name)
        if name in locks:
            locks[name] = data
        else:
            manifests[name] = hashlib.sha256(data).hexdigest()
    if not manifests or len(manifests) > 2048:
        raise ContractError("Rust recovery needs a bounded complete reference manifest tree")
    return manifests, locks


def reference_trees(ctx, task, patch, lock_names):
    """Use a private index; include added/deleted golden manifests, not just root."""
    repository = ctx.repo(task)
    start = _tree_inputs(repository, task["start_sha"], lock_names)
    with tempfile.TemporaryDirectory(prefix="rust-lock-reference-", dir=ctx.scratch) as temporary:
        private = Path(temporary)
        git(private, "init", "--bare", "--quiet")
        objects = git(repository, "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()
        (private / "objects/info/alternates").write_text(objects + "\n")
        git(private, "read-tree", task["start_sha"])
        git(private, "apply", "--cached", "--binary", "--whitespace=nowarn",
            input_bytes=Path(patch["path"]).read_bytes())
        revision = git(private, "write-tree").decode().strip()
        golden = _tree_inputs(private, revision, lock_names)
    return {"start": start, "golden": golden}


def _validate_lock(data):
    lock = tomllib.loads(data.decode())
    if not isinstance(lock.get("package"), list) or not lock["package"]:
        raise ContractError("retained Rust lock has no package graph")
    seen = set()
    for package in lock["package"]:
        identity = package["name"], package["version"], package.get("source", "")
        if identity in seen:
            raise ContractError("retained Rust lock has duplicate package identities")
        seen.add(identity)
        source = identity[2]
        if source.startswith("registry+"):
            checksum = package.get("checksum") or lock.get("metadata", {}).get(
                f"checksum {identity[0]} {identity[1]} ({source})")
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ContractError("retained Rust registry package has no checksum")
        elif source and (not source.startswith("git+") or not re.search(r"#[0-9a-f]{40}$", source)):
            raise ContractError("retained Rust package has no immutable source")


def _recover(ctx, task, plan, oracle, patch, pin, audit):
    root = ctx.legacy / "rust"
    identity = ("repo", "start_sha")
    if (any(task.get(k) != pin[k] or oracle.get(k) != pin[k] for k in identity)
            or oracle.get("id") != task["id"]
            or plan.get("input_index") != pin["input_index"]
            or oracle.get("input_index") != pin["input_index"]):
        raise ContractError("Rust frozen lock recovery has a wrong task/start/input index")
    if canonical_hash(plan) != pin["plan_sha256"]:
        raise ContractError("Rust frozen lock recovery plan changed")
    original = json.loads(attested_bytes(
        root, pin["artifacts"] + "/result.json", pin["oracle_file_sha256"]))
    # The release adds this provenance field; no executable Oracle field may differ.
    bound = {k: v for k, v in oracle.items() if k != "lane_results_source"}
    if canonical_hash(bound) != canonical_hash(original):
        raise ContractError("Rust frozen lock recovery differs from the original Oracle")
    expected_patch = original.get("golden_patch_sha256")
    if (not expected_patch or patch.get("sha256") != expected_patch
            or file_hash(Path(patch["path"])) != expected_patch):
        raise ContractError("Rust frozen lock recovery accepted patch changed")
    if (plan.get("environment_replacements") or plan.get("environment_file_replacements")
            or plan.get("test_compatibility_replacements")):
        raise ContractError("retained Rust lock recovery must preserve all manifests and assertions")
    audit.update(input_index=pin["input_index"], start_sha=pin["start_sha"],
                 plan_sha256=pin["plan_sha256"], oracle_file_sha256=pin["oracle_file_sha256"],
                 golden_patch_sha256=expected_patch, original_prepare_command=plan.get("prepare_command"),
                 oracle_declares_lock_sha256=bool(pin.get("source")))
    if "lock_sha256" not in pin:
        cwd = plan.get("workdir", ".")
        names = {"Cargo.lock", (Path(cwd) / "Cargo.lock").as_posix()}
        paths = [root / pin["artifacts"] / variant / name
                 for variant in ("start", "golden") for name in sorted(names)]
        audit["retained_paths"] = [{"path": str(p), "exists": p.is_file()} for p in paths]
        audit["session_lock_paths"] = [e["path"] for e in plan.get("session_files", [])
                                       if Path(e["path"]).name == "Cargo.lock"]
        raise ContractError(
            "Rust frozen lock evidence gap for " + task["id"] +
            ": no original lock digest or pinned retained snapshot; inspected " +
            ", ".join(str(p) for p in paths))

    from .rust_overlay import session_bytes
    sessions = {entry["path"]: session_bytes(ctx, plan, oracle, entry)
                for entry in plan.get("session_files", [])}
    name = repo_path(pin["lock_path"])
    public_name = pin.get("public_lock_path", name)
    trees = reference_trees(ctx, task, patch, {name, public_name})
    if any(values[1][name] is not None for values in trees.values()):
        raise ContractError("recovered Rust lock must be absent from both reference trees")
    sources = {}
    if pin.get("source"):
        data = attested_bytes(root, pin["source"], pin["lock_sha256"])
        entries = [e for e in plan.get("session_files", [])
                   if e["path"] == name and e.get("source") == pin["source"]]
        if len(entries) != 1 or sessions.get(name) != data:
            raise ContractError("recovered Rust session lock has a different path/source binding")
        sources[name] = (root / pin["source"], data)
    else:
        observed_logs = {}
        for variant, (manifests, _) in trees.items():
            snapshot = root / pin["artifacts"] / variant
            if git(snapshot, "rev-parse", "HEAD").decode().strip() != task["start_sha"]:
                raise ContractError("retained Rust lock worktree has a different start")
            names = {n.decode() for n in git(snapshot, "ls-files", "-z", "--cached", "--others",
                                            "--exclude-standard", "--", "*Cargo.toml").split(b"\0")
                     if n and Path(n.decode()).name == "Cargo.toml"}
            if names != manifests.keys():
                raise ContractError("retained Rust lock has a different complete manifest tree")
            for manifest, digest in manifests.items():
                attested_bytes(snapshot, manifest, digest)
            data = attested_bytes(snapshot, name, pin["lock_sha256"])
            sources.setdefault(name, (snapshot / name, data))
            runs = original.get("runs", {}).get(variant, [])
            if len(runs) != 3:
                raise ContractError("retained Rust lock lacks three frozen runs per reference")
            for run in runs:
                log = run["log"]
                digest = pin["logs"].get(Path(log).name)
                if (not digest or log != pin["artifacts"] + "/logs/" + Path(log).name
                        or run.get("ok") is not True or run.get("failed") != 0
                        or run.get("passed_tests") != original["pass_to_pass_tests"]
                        or not run.get("attempts") or run["attempts"][-1].get("exit_code") != 0
                        or run["attempts"][-1].get("timed_out") is not False):
                    raise ContractError("retained Rust lock lacks its original successful run evidence")
                attested_bytes(root, log, digest)
                observed_logs[log] = digest
        if len(observed_logs) != len(pin["logs"]):
            raise ContractError("retained Rust lock run-log bindings are incomplete")
        audit["recovery_log_pins"] = observed_logs
        audit["retained_lock_paths"] = [
            str(root / pin["artifacts"] / v / name) for v in ("start", "golden")]

    if public_name != name:
        data = trees["start"][1][public_name]
        if (data is None or data != trees["golden"][1][public_name]
                or hashlib.sha256(data).hexdigest() != pin["public_lock_sha256"]):
            raise ContractError("Rust public build lock differs from the unchanged committed input")
        sources[public_name] = (None, data)
    for _, data in sources.values():
        _validate_lock(data)
    defaults = {}
    for relative, (source, data) in sorted(sources.items()):
        if source is None:
            with tempfile.TemporaryDirectory(prefix="rust-public-lock-", dir=ctx.scratch) as temporary:
                source = Path(temporary) / "Cargo.lock"
                source.write_bytes(data)
                asset = ctx.copy_asset(source, "rust-frozen-locks")
        else:
            asset = ctx.copy_asset(source, "rust-frozen-locks")
        digest = hashlib.sha256(data).hexdigest()
        if asset.get("sha256") != digest or file_hash(Path(asset["path"])) != digest:
            raise ContractError("copied Rust frozen lock asset changed")
        defaults[relative] = asset
    cwd = plan.get("workdir", ".")
    public_cwd = pin.get("public_cwd", cwd)
    manifests = [values[0] for values in trees.values()]
    session_manifests = {n: hashlib.sha256(data).hexdigest() for n, data in sessions.items()
                         if Path(n).name == "Cargo.toml"}
    spec = {
        "schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": task["id"],
        "manifests": [], "private_members": [], "cpus": 4,
        "locks": [
            {"path": n, "asset_target": "/opt/reviewbench-assets/rust-lock-" + str(i),
             "sha256": a["sha256"], "allow_missing": n == name, "replace_if_sha256": []}
            for i, (n, a) in enumerate(sorted(defaults.items()))
        ],
        "frozen_lock_recovery": {
            "task_id": task["id"], "entrypoints": {cwd: name, public_cwd: public_name},
            "public_cwd": public_cwd, "public_package": pin.get("public_package"),
            "reference_manifest_trees": sorted({
                canonical_hash(tree) for m in manifests for tree in (m, {**m, **session_manifests})}),
        },
    }
    audit.update(status="bound", locks=defaults, reference_manifests={v: x[0] for v, x in trees.items()},
                 lock_compatibility="pending full-candidate locked offline metadata",
                 source_lock_path=name, source_lock_sha256=pin["lock_sha256"],
                 source=str(sources[name][0]), public_cwd=public_cwd)
    return spec, defaults


def recover(ctx, task, plan, oracle, patch):
    """Return build-only defaults, or a precise gap; other Rust tasks are untouched."""
    pin = BINDINGS.get(task["id"])
    if pin is None:
        return None
    audit = {"task_id": task["id"], "status": "rejected", "scoring_result": False,
             "production_changes": [], "test_changes": []}
    try:
        return _recover(ctx, task, plan, oracle, patch, pin, audit)
    except (OSError, ValueError, KeyError) as exc:
        audit["status"] = "rejected"
        audit["gap"] = str(exc)
        raise ContractError(str(exc)) from exc
    finally:
        atomic_json(ctx.output / "rust-lock-recovery" / (task["id"] + ".json"), audit)
