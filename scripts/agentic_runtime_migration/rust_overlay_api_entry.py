#!/usr/bin/env python3
"""Original Cargo build overlays, retained graph selection and fixed API probes."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

if __package__:
    from . import rust_image_entry as core
    from .rust_build_entry import (
        OverlayError, apply_manifest_operations, manifest_operations, overlay_files, synchronize_candidate, tomllib, tomli_w,
        relative_path,
    )
else:
    import image_locks as core
    from build import (
        OverlayError, apply_manifest_operations, manifest_operations, overlay_files, synchronize_candidate, tomllib, tomli_w,
        relative_path,
    )

SCHEMA = "reviewbench-original-cargo-overlay-api/v1"
SAME_TARGET_SCHEMA = "reviewbench-original-same-target-overlay-api/v1"
EVIDENCE_PREFIX = "ReviewBench original overlay API: "


def require(condition, message):
    core.require(condition, "overlay API: " + message)


def validate(public):
    if public.get("schema_version") == SAME_TARGET_SCHEMA:
        validate_same_target(public)
        return
    require(public["schema_version"] == SCHEMA and public["selection"] == core.SELECTION,
            "unbound original graph selection")
    require(public["package"] == "aries_vcx_core"
            and public["package_manifest"] == "aries/aries_vcx_core/Cargo.toml", "original package changed")
    for key in ("cargo", "rustc", "overlay", "source_config"):
        row = public[key]
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound " + key)
    require(Path(public["cargo"]["path"]).name == "cargo" and Path(public["rustc"]["path"]).name == "rustc",
            "wrong compiler executable")
    require(len(public["locks"]) == 2 and len({r["sha256"] for r in public["locks"]}) == 2,
            "two distinct retained locks required")
    for row in public["locks"]:
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound original lock")
    require(re.fullmatch(r"[0-9a-f]{64}", public["reference_seed_sha256"]), "unbound original committed seed")
    require(public["public_command"] == [
        "check", "--locked", "-p", "aries_vcx_core", "--features", "askar_wallet", "--message-format=json"],
        "original public package/features changed")
    for name, maximum in (("cpus", 4), ("timeout_seconds", 3600), ("metadata_timeout_seconds", 120),
                          ("max_log_bytes", 32 * 1024**2)):
        require(type(public[name]) is int and 0 < public[name] <= maximum, "invalid bound " + name)


def validate_same_target(public):
    kamu_task = "github__kamu-data__kamu-cli__pull_941__comment_1843087682__api_design"
    kamu = public.get("profile") == "kamu53-original-v1"
    if kamu or public.get("task_id") == kamu_task:
        require(kamu and public.get("task_id") == kamu_task and public.get("cpus") == 16
                and public.get("timeout_seconds") == 3600
                and public.get("package") == "kamu-core"
                and public.get("package_manifest") == "src/domain/core/Cargo.toml"
                and public.get("feature_args") == []
                and public.get("overlay_paths") == ["Cargo.toml", "src/domain/core/Cargo.toml"],
                "original Kamu same-target binding changed")
    require(public.get("selection") == core.SELECTION and isinstance(public.get("task_id"), str)
            and public["task_id"] and re.fullmatch(r"[A-Za-z0-9_-]+", public["package"]),
            "unbound original same-target package")
    manifest = Path(public["package_manifest"])
    require(not manifest.is_absolute() and ".." not in manifest.parts and manifest.name == "Cargo.toml",
            "original package manifest changed")
    features = public["feature_args"]
    require(features == [] or (len(features) == 2 and features[0] == "--features"
            and re.fullmatch(r"[A-Za-z0-9_/,.-]+", features[1])), "original feature args changed")
    require(public["public_command"] ==
            ["build", "--locked", "-p", public["package"], *features, "--lib", "--message-format=json"],
            "original public package build changed")
    require(len(public["locks"]) == 1 and re.fullmatch(r"[0-9a-f]{64}", public["reference_seed_sha256"]),
            "one original retained lock and its source seed are required")
    for row in [public[key] for key in ("cargo", "rustc", "overlay", "source_config")] + public["locks"]:
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound original same-target input")
    allowed_paths = ({"Cargo.toml", public["package_manifest"]} if kamu
                     else {"src/workspace-hack/Cargo.toml", public["package_manifest"]})
    require(len(public["overlay_paths"]) == len(set(public["overlay_paths"]))
            and set(public["overlay_paths"]) <= allowed_paths,
            "original environment overlay inventory changed")
    for key in ("private_spec_sha256", "private_tool_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", public[key]), "unbound original private API input")
    for field in ("public_test_feature_dependencies", "public_test_workspace_dependencies"):
        test_dependencies = public.get(field, [])
        require(isinstance(test_dependencies, list) and len(test_dependencies) <= 8
                and all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", name)
                        for name in test_dependencies)
                and len(test_dependencies) == len(set(test_dependencies)),
                "invalid public test-dependency feature inventory")
    inputs = public.get("build_inputs", [])
    require(isinstance(inputs, list) and len(inputs) <= 128
            and len({row["path"] for row in inputs}) == len(inputs), "duplicate/excess build inputs")
    for row in inputs:
        path = relative_path(row["path"])
        require(set(row) == {"path", "source", "mode"}
                and ("/src/http/static/" in path or "/src/http/static-dev/" in path)
                and Path(path).suffix in {".js", ".css", ".wasm"}
                and type(row["mode"]) is int and row["mode"] in {0o644, 0o664, 0o666, 0o755},
                "original static build input destination changed")
        source = row["source"]
        require(set(source) == {"path", "sha256"} and Path(source["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]), "unbound static build input")
    for key, maximum in (("cpus", 16 if kamu else 8), ("timeout_seconds", 3600), ("metadata_timeout_seconds", 180),
                         ("max_log_bytes", 32 * 1024**2)):
        require(type(public[key]) is int and 0 < public[key] <= maximum, "invalid same-target bound")


def same_target_overlay(source, public):
    document = json.loads(core.bounded(Path(public["overlay"]["path"])))
    require(document["schema_version"] == "reviewbench-cargo-build-overlay/v1"
            and document["task_id"] == public["task_id"] and document["locks"] == []
            and document["private_members"] == []
            and [row["path"] for row in document["manifests"]] == public["overlay_paths"],
            "original same-target overlay changed")
    for row in document["manifests"]:
        for operation in row["operations"]:
            original_kamu_members = (
                public.get("profile") == "kamu53-original-v1"
                and row["path"] == "Cargo.toml" and operation["key"] == ["workspace", "members"]
                and operation["kind"] == "list_edit"
            )
            require(operation["kind"] in {"ensure_table", "prune_empty", "remove", "add", "replace", "list_edit"}
                    and operation["key"] and (original_kamu_members or operation["key"][0] in {
                        "dependencies", "dev-dependencies", "build-dependencies", "target"}),
                    "environment overlay acquired unrelated package/workspace edits")
    spec = {key: document[key] for key in ("manifests", "locks", "private_members")}
    try:
        changed = overlay_files(source, spec)
    except OverlayError as error:
        # Never partially repair a candidate-owned configuration with reference
        # values or a retained lock. The original current inputs stand alone.
        require(str(error).startswith("candidate "), "invalid original overlay")
        return {"manifests": [], "locks": [], "private_members": [],
                "candidate_owned_manifest_inputs": True}, {}
    return spec, changed


def stage_build_inputs(public, mirror, before, expected):
    """Restore checksum-bound missing build data, never overwrite candidate files."""
    staged = []
    for row in public.get("build_inputs", []):
        name = relative_path(row["path"])
        source = Path(row["source"]["path"])
        require(source.is_file() and not source.is_symlink()
                and core.file_digest(source) == row["source"]["sha256"], "original static build asset changed")
        if name in before:
            continue
        destination = mirror / name
        require(not destination.exists() and not destination.is_symlink()
                and not any(parent.is_symlink() for parent in destination.parents if parent.is_relative_to(mirror)),
                "static build input destination redirected")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(row["mode"])
        require(core.file_digest(destination) == row["source"]["sha256"], "static build input changed while reading")
        expected[name] = ["file", row["source"]["sha256"], bool(row["mode"] & 0o111)]
        staged.append(name)
    return staged


def public_test_feature_args(public, mirror):
    """Use only features currently declared on identical direct test dependencies.

    A library-only build omits dev-dependency feature unification. For an
    explicitly bound dependency, reproduce its current test feature union
    without compiling private harnesses or changing any manifest. A changed
    identity/default/optional relationship is not repaired with reference data.
    """
    names = public.get("public_test_feature_dependencies", [])
    if public.get("schema_version") != SAME_TARGET_SCHEMA or not names:
        return []
    manifest = tomllib.loads(core.bounded(Path(mirror) / public["package_manifest"]).decode())
    result = []
    for name in names:
        normal = manifest.get("dependencies", {}).get(name)
        development = manifest.get("dev-dependencies", {}).get(name)
        if isinstance(normal, str):
            normal = {"version": normal}
        if isinstance(development, str):
            development = {"version": development}
        if not isinstance(normal, dict) or not isinstance(development, dict):
            continue
        identity = lambda row: {k: v for k, v in row.items()
                                if k not in {"features", "default-features", "optional"}}
        if (identity(normal) != identity(development) or normal.get("optional", False)
                or development.get("optional", False)
                or normal.get("default-features", True) != development.get("default-features", True)):
            continue
        for row in (normal, development):
            features = row.get("features", [])
            require(isinstance(features, list)
                    and all(isinstance(f, str) and re.fullmatch(r"[A-Za-z0-9_+-]+", f) for f in features),
                    "invalid current test-dependency features")
        result.extend(name + "/" + feature for feature in sorted(
            set(development.get("features", [])) - set(normal.get("features", []))))
    return ["--features", ",".join(sorted(result))] if result else []


def public_test_workspace_args(public, mirror):
    """Build explicitly bound, current path dev-dependency libraries as peers.

    Their normal dependency features then participate just as they do when
    building the original integration test. No unit/integration test target is
    selected, and changed candidate dependency declarations are not repaired.
    """
    names = public.get("public_test_workspace_dependencies", [])
    if public.get("schema_version") != SAME_TARGET_SCHEMA or not names:
        return []
    mirror = Path(mirror).resolve()
    path = mirror / public["package_manifest"]
    manifest = tomllib.loads(core.bounded(path).decode())
    result = []
    for name in names:
        development = manifest.get("dev-dependencies", {}).get(name)
        if (not isinstance(development, dict) or not isinstance(development.get("path"), str)
                or development.get("optional", False) or not development.get("default-features", True)):
            continue
        package = (path.parent / development["path"] / "Cargo.toml").resolve()
        if not package.is_relative_to(mirror) or not package.is_file():
            continue
        value = tomllib.loads(core.bounded(package).decode()).get("package", {})
        package_name = value.get("name")
        if package_name != development.get("package", name):
            continue
        require(isinstance(package_name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", package_name),
                "invalid current test workspace package")
        features = development.get("features", [])
        require(isinstance(features, list)
                and all(isinstance(f, str) and re.fullmatch(r"[A-Za-z0-9_+-]+", f) for f in features),
                "invalid current test workspace features")
        result += ["-p", package_name]
        if features:
            result += ["--features", ",".join(package_name + "/" + f for f in sorted(set(features)))]
    return result


def merge_tables(left, right):
    """A namespace move preserves candidate lint changes and rejects collisions."""
    result = copy.deepcopy(right)
    require(isinstance(left, dict) and isinstance(right, dict), "lint namespace is not a table")
    for key, value in left.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(result[key], dict):
            result[key] = merge_tables(value, result[key])
        else:
            require(result[key] == value, "candidate has conflicting lint namespaces")
    return result


def current_overlay(source, public):
    if public.get("schema_version") == SAME_TARGET_SCHEMA:
        return same_target_overlay(source, public)
    document = json.loads(core.bounded(Path(public["overlay"]["path"])))
    require(document["schema_version"] == "reviewbench-cargo-build-overlay/v1"
            and document["task_id"] == public["task_id"] and document["locks"] == []
            and document["private_members"] == [], "overlay acquired unrelated locks/private packages")
    require({r["path"] for r in document["manifests"]} ==
            {"Cargo.toml", public["package_manifest"]}, "original manifest overlay inventory changed")
    result, contents = [], {}
    for row in document["manifests"]:
        path = source / row["path"]
        before = tomllib.loads(core.bounded(path).decode())
        candidate = copy.deepcopy(before)
        operations = row["operations"]
        if row["path"] == "Cargo.toml":
            require(all(o["key"] == ["workspace", "members"] or o["key"][:1] == ["lints"]
                        or o["key"][:2] == ["workspace", "lints"] for o in operations),
                    "root overlay changes production dependencies")
            # The original text replacements move the headers, not lint values.
            # Preserve a model's new/edited lint values during that same move.
            workspace = candidate.setdefault("workspace", {})
            require(isinstance(workspace, dict), "current workspace is not a table")
            if "lints" in candidate:
                workspace["lints"] = merge_tables(candidate.pop("lints"), workspace.get("lints", {}))
            operations = [o for o in operations if o["key"] == ["workspace", "members"]]
        else:
            require(all(o["key"][:1] == ["dev-dependencies"] for o in operations),
                    "package overlay changes production dependencies")
        changed = apply_manifest_operations(candidate, operations)
        contents[row["path"]] = tomli_w.dumps(changed).encode()
        require(tomllib.loads(contents[row["path"]].decode()) == changed, "manifest roundtrip changed semantics")
        result.append({"path": row["path"], "operations": manifest_operations(before, changed)})
    spec = {"manifests": result, "locks": [], "private_members": []}
    # Derive bytes through the same emitter as synchronize_candidate. Moving a
    # table can reorder keys; two equivalent TOML serializations are not equal
    # byte hashes, so a second serialization must not define the expected tree.
    emitted = overlay_files(source, spec)
    require(set(emitted) == set(contents) and all(
        tomllib.loads(emitted[n].decode()) == tomllib.loads(contents[n].decode()) for n in contents),
        "shared overlay emitter changed candidate semantics")
    return spec, emitted


def load_private(path, tool, public, source):
    require(core.file_digest(tool) == public["private_tool_sha256"], "private API selector changed")
    raw = core.bounded(path)
    require(core.digest(raw) == public["private_spec_sha256"], "private original API spec changed")
    value = json.loads(raw)
    loader = importlib.util.spec_from_file_location("original_private_api", tool)
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    module.validate(value)
    require(value["task_id"] == public["task_id"] and value["workdir"] == ".", "private identity/cwd changed")
    require(len(value["expected_tests"]) == 1 and len(value["forms"]) == 2,
            "original test/target inventory changed")
    if public["schema_version"] == SAME_TARGET_SCHEMA:
        require(value["schema_version"] == module.SAME_TARGET_SCHEMA
                and value["package"] == public["package"] and value["package_manifest"] == public["package_manifest"]
                and value["cargo"] == public["cargo"] and value["cpus"] == public["cpus"]
                and value.get("profile") == public.get("profile"),
                "original same-target private bindings changed")
        module.verify_files(source, value)
        return value
    for form in value["forms"]:
        require(form["command"] == ["cargo", "test", "-p", "aries_vcx_core", "--features", "askar_wallet",
                    "--test", form["target"], *value["expected_tests"], "--", "--nocapture"],
                "original unlocked Cargo argv changed")
    module.verify_files(source, value)
    return value


def targets(graph, mirror, public, private):
    owners = [p for p in graph["packages"] if p.get("source") is None and p["name"] == public["package"]]
    require(len(owners) == 1 and Path(owners[0]["manifest_path"]).resolve() ==
            (mirror / public["package_manifest"]).resolve(), "original API package redirected")
    if public["schema_version"] == SAME_TARGET_SCHEMA:
        return  # Original target ownership is checked on each native compile artifact after staging.
    manifest = tomllib.loads(core.bounded(mirror / public["package_manifest"]).decode())
    for form in private["forms"]:
        matches = [t for t in owners[0]["targets"] if t["name"] == form["target"] and "test" in t["kind"]]
        require(len(matches) == 1 and Path(matches[0]["src_path"]).resolve() ==
                (mirror / form["harness"]).resolve(), "original test target redirected")
        require(not any(t.get("name") == form["target"] and t.get("harness") is False
                        for t in manifest.get("test", [])), "original test harness disabled")


def run_candidate(public, source, folder, private=None, *, private_path=None, private_tool=None, run=core.invoke):
    validate(public)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    require(not source.is_relative_to(folder) and not folder.is_relative_to(source), "source/cache overlap")
    evidence = {"schema_version": public["schema_version"], "task_id": public["task_id"], "status": "preparing",
                "role": "grading" if private else "public", "selection_uses_test_results": False,
                "new_lock_generation_commands": 0, "probes": [], "command_executions": 0}
    try:
        for row in [public[n] for n in ("cargo", "rustc", "overlay", "source_config")] + public["locks"]:
            require(core.file_digest(Path(row["path"])) == row["sha256"], "fixed compiler/overlay/lock changed")
        before = core.snapshot(source)
        require("Cargo.lock" in before and before["Cargo.lock"][0] == "file", "original candidate lock removed/redirected")
        overlay, changed = current_overlay(source, public)
        folder.mkdir(parents=True, exist_ok=True)
        mirror = folder / "source"
        synchronize_candidate(source, mirror, overlay)
        expected = dict(before)
        for name, data in changed.items():
            expected[name] = ["file", core.digest(data), before[name][2]]
        if public["schema_version"] == SAME_TARGET_SCHEMA:
            evidence["static_build_inputs_staged"] = stage_build_inputs(public, mirror, before, expected)
        require(core.snapshot(mirror) == expected, "overlay changed other current candidate files")
        owned = (before["Cargo.lock"][1] != public["reference_seed_sha256"]
                 or overlay.get("candidate_owned_manifest_inputs", False))
        locks = ([{"path": str(source / "Cargo.lock"), "sha256": before["Cargo.lock"][1]}]
                 if owned else public["locks"])
        home = folder / "cargo-home"
        require(not home.is_symlink(), "Cargo home redirected")
        home.mkdir(exist_ok=True)
        require(not (home / "config.toml").is_symlink(), "Cargo source config redirected")
        (home / "config.toml").write_bytes(core.bounded(Path(public["source_config"]["path"])))
        env = {**os.environ, "RUSTC": public["rustc"]["path"], "CARGO_HOME": str(home),
               "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0", "SOURCE_ROOT": str(mirror)}
        deadline = time.monotonic() + public["timeout_seconds"]
        compatible = []
        git_packages = sorted({(p["name"], p["version"], p["source"]) for lock in public["locks"]
            for p in tomllib.loads(core.bounded(Path(lock["path"])).decode())["package"]
            if p.get("source", "").startswith("git+")})
        evidence.update(source_before=before, candidate_lock_preserved=owned, mirror_overlay=expected)
        for number, lock in enumerate(locks):
            data = core.bounded(Path(lock["path"]))
            require(core.digest(data) == lock["sha256"], "retained/candidate lock changed")
            (mirror / "Cargo.lock").write_bytes(data)
            probe_expected = {**expected, "Cargo.lock": ["file", lock["sha256"], before["Cargo.lock"][2]]}
            command = [public["cargo"]["path"], "--offline", "metadata", "--format-version", "1", "--locked", "--all-features"]
            row = run(command, mirror, env, folder, f"metadata-{number}",
                      min(deadline, time.monotonic() + public["metadata_timeout_seconds"]), public["max_log_bytes"])
            observed = {"lock_sha256": lock["sha256"], "command_result": row}
            evidence["probes"].append(observed)
            core.complete(row)
            require(row["command"] == command and row["cwd"] == str(mirror), "metadata command/cwd changed")
            require(core.snapshot(source) == before and core.snapshot(mirror) == probe_expected,
                    "metadata changed current source or its lock")
            if row["returncode"] == 0:
                graph = core.graph_check(row["stdout"], mirror, probe_expected, data, locked_git=True)
                if private or public["schema_version"] == SAME_TARGET_SCHEMA:
                    targets(json.loads(row["stdout"]), mirror, public, private)
                observed["graph"] = graph
                compatible.append((lock, data, graph))
            else:
                require((core.catalog_lock_incompatible(row) if public["schema_version"] == SAME_TARGET_SCHEMA else
                         core.lock_incompatible(row, original_locked=True, locked_git_packages=git_packages)),
                        "metadata failed outside original lock compatibility")
        require(len(compatible) == 1, "no unique compatible original lock: " + str(len(compatible)))
        selected, data, graph = compatible[0]
        (mirror / "Cargo.lock").write_bytes(data)
        expected["Cargo.lock"] = ["file", selected["sha256"], before["Cargo.lock"][2]]
        command = ([sys.executable, str(private_tool), "--spec", str(private_path)] if private else
                   [public["cargo"]["path"], "--offline", *public["public_command"]])
        if not private:
            features = public_test_feature_args(public, mirror)
            workspace = public_test_workspace_args(public, mirror)
            command += features + workspace
            if public.get("public_test_feature_dependencies"):
                evidence["public_test_dependency_feature_args"] = features
            if public.get("public_test_workspace_dependencies"):
                evidence["public_test_workspace_args"] = workspace
        result = run(command, mirror, env, folder, "execution", deadline, public["max_log_bytes"])
        evidence.update(execution=result, command_executions=1, selected_lock_sha256=selected["sha256"], graph=graph)
        core.complete(result)
        require(result["command"] == command and result["cwd"] == str(mirror), "execution command/cwd changed")
        require(core.snapshot(source) == before and core.snapshot(mirror) == expected,
                "execution changed candidate source, overlay or selected lock")
        if private:
            found = core.RESULT.findall(result["stdout"])
            count, values = Counter(n for n, _ in found), dict(found)
            evidence["test_results"] = {n: result["returncode"] == 0 and count[n] == 1 and values.get(n) == "ok"
                                       for n in private["expected_tests"]}
        evidence.update(status="completed", exit_status=result["returncode"], source_after=core.snapshot(source),
                        mirror_after=core.snapshot(mirror), source_unchanged=True, mirror_unchanged=True)
        return evidence
    except (OverlayError, ValueError, OSError, KeyError, TypeError) as error:
        evidence.update(status="rejected", exit_status=2, error=type(error).__name__ + ": " + str(error))
        raise core.VariantError(str(error), evidence) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--private-tool", type=Path)
    args = parser.parse_args()
    public = json.loads(core.bounded(args.spec))
    require(public["task_id"] == args.task_id, "wrong task")
    require(bool(args.private) == bool(args.private_tool), "private spec/tool must be paired")
    root = Path("/opt/reviewbench-cache/rust-build/original-overlay-api")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cpus = sorted(os.sched_getaffinity(0))
        offset = int(core.digest(args.task_id.encode())[:16], 16) % len(cpus)
        os.sched_setaffinity(0, {cpus[(offset + i) % len(cpus)] for i in range(min(len(cpus), public["cpus"]))})
        try:
            private = load_private(args.private, args.private_tool, public, Path("/workspace")) if args.private else None
            evidence = run_candidate(public, Path("/workspace"), root, private,
                                     private_path=args.private, private_tool=args.private_tool)
        except core.VariantError as error:
            evidence = error.evidence or {"status": "rejected", "exit_status": 2, "error": str(error)}
        if "execution" in evidence:
            sys.stdout.write(evidence["execution"]["stdout"])
            sys.stderr.write(evidence["execution"]["stderr"])
        elif evidence.get("error"):
            sys.stderr.write(evidence["error"] + "\n")
            for probe in evidence.get("probes", [])[-2:]:
                sys.stderr.write(probe["command_result"]["stderr"][-4000:])
        print(EVIDENCE_PREFIX + json.dumps(evidence, sort_keys=True), flush=True)
        return evidence["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
