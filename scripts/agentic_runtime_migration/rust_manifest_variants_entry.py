#!/usr/bin/env python3
"""Full-source original manifest forms selected by public compilation, never assertions."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time

if __package__:
    from . import rust_image_entry as core
    from .rust_build_entry import (
        OverlayError, apply_manifest_operations, manifest_operations, overlay_files,
        relative_path, same_value, synchronize_candidate, tomllib, tomli_w,
    )
else:
    import image_locks as core
    from build import (
        OverlayError, apply_manifest_operations, manifest_operations, overlay_files,
        relative_path, same_value, synchronize_candidate, tomllib, tomli_w,
    )

SCHEMA = "reviewbench-original-multi-manifest-variants/v1"
ROOT_SCHEMA = "reviewbench-original-root-manifest-variants/v1"
EVIDENCE_PREFIX = "ReviewBench manifest variants: "
SELECTION = "full-current-metadata-and-public-compile; execute-all-compatible-original-tests"


def require(condition, message):
    core.require(condition, "manifest variants: " + message)


def reference(row):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and core.file_digest(path) == row["sha256"], "fixed input changed")
    return core.bounded(path)


def dependency(document, key):
    value = document
    for part in key:
        require(isinstance(value, dict) and part in value, "current candidate removed a controlled dependency")
        value = value[part]
    return value


def source_identity(value):
    if isinstance(value, str):
        return {"version": value}
    require(isinstance(value, dict), "invalid current dependency declaration")
    return {k: v for k, v in value.items() if k not in {"features", "default-features", "optional"}}


def set_version(document, key, version):
    require(isinstance(version, str) and version, "missing original dependency version")
    parent = document
    for part in key[:-1]:
        parent = parent[part]
    current = parent[key[-1]]
    if isinstance(current, str):
        parent[key[-1]] = version
    else:
        require(isinstance(current, dict) and "version" in current, "current dependency has no version slot")
        parent[key[-1]] = {**current, "version": version}


def validate(public, private=None):
    if public.get("schema_version") == ROOT_SCHEMA:
        validate_root(public, private)
        return
    require(public["schema_version"] == SCHEMA and public["selection"] == SELECTION, "unbound selection")
    require(re.fullmatch(r"[A-Za-z0-9_-]+", public["package"]), "invalid package")
    manifest = relative_path(public["package_manifest"])
    require(manifest.endswith("/Cargo.toml"), "original package manifest changed")
    require(public["public_command"] == ["check", "--locked", "-p", public["package"], "--message-format=json"],
            "original public package command changed")
    require(len(public["forms"]) == 2 and len({f["name"] for f in public["forms"]}) == 2,
            "two original forms required")
    paths = [r["path"] for r in public["manifests"]]
    require(1 <= len(paths) <= 8 and len(set(paths)) == len(paths) and manifest in paths,
            "invalid manifest inventory")
    require(all(Path(relative_path(p)).name == "Cargo.toml" for p in paths), "non-manifest overlay")
    require(public["slots"] and len(public["slots"]) <= 16, "missing original dependency slots")
    for slot in public["slots"]:
        require(slot["path"] in paths and slot["key"][:1] == ["dependencies"] and len(slot["key"]) == 2,
                "unbound production dependency slot")
    for form in public["forms"]:
        require([r["path"] for r in form["manifests"]] == paths, "original form manifest inventory changed")
    for name, maximum in (("cpus", 4), ("timeout_seconds", 3600), ("metadata_timeout_seconds", 120),
                          ("max_log_bytes", 32 * 1024**2)):
        require(type(public[name]) is int and 0 < public[name] <= maximum, "invalid bound " + name)
    require(re.fullmatch(r"[0-9a-f]{64}", public["reference_seed_sha256"]), "unbound source lock seed")
    if private is not None:
        require(private["task_id"] == public["task_id"] and private["package_manifest"] == manifest,
                "private task/package changed")
        require(private["command"] == ["test", "--locked", "-p", public["package"], "--test",
                                      Path(private["harness"]["path"]).stem, "--", "--nocapture"],
                "original test argv changed")
        require(private["expected_tests"] and len(set(private["expected_tests"])) == len(private["expected_tests"]),
                "original test names changed")


def validate_root(public, private=None):
    require(public.get("selection") == SELECTION and public.get("package_manifest") == "Cargo.toml",
            "unbound root manifest selection")
    require(isinstance(public.get("task_id"), str) and public["task_id"]
            and re.fullmatch(r"[A-Za-z0-9_-]+", public["package"]), "invalid root task/package")
    features = public["feature_args"]
    require(len(features) == 3 and features[:2] == ["--no-default-features", "--features"]
            and isinstance(features[2], str) and re.fullmatch(r"[A-Za-z0-9_,/-]+", features[2]),
            "original feature arguments changed")
    require(public["public_command"] == ["build", "--locked", *features, "--lib", "--message-format=json"],
            "root production build command changed")
    require(len(public["forms"]) == 2 and len({row["name"] for row in public["forms"]}) == 2
            and len({row["lock"]["sha256"] for row in public["forms"]}) == 2
            and [row["path"] for row in public["manifests"]] == ["Cargo.toml"],
            "root original manifest pair changed")
    for form in public["forms"]:
        require([row["path"] for row in form["manifests"]] == ["Cargo.toml"], "form manifest changed")
    for row in [public["cargo"], public["rustc"], public["source_config"],
                public["manifests"][0]["reference"],
                *[item for form in public["forms"] for item in (form["lock"], form["manifests"][0]["asset"])]]:
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound root input")
    require(re.fullmatch(r"[0-9a-f]{64}", public["reference_seed_sha256"]), "unbound root lock seed")
    for key, maximum in (("cpus", 4), ("timeout_seconds", 3600), ("metadata_timeout_seconds", 180),
                         ("max_log_bytes", 32 * 1024**2)):
        require(type(public[key]) is int and 0 < public[key] <= maximum, "invalid root bound")
    if private is not None:
        harness = private["harness"]
        path = relative_path(harness["path"])
        require(private["task_id"] == public["task_id"] and private["package_manifest"] == "Cargo.toml"
                and Path(path).parent == Path("tests") and Path(path).suffix == ".rs"
                and re.fullmatch(r"[0-9a-f]{64}", harness["sha256"]), "original root test changed")
        name = private["test_filter"]
        require(re.fullmatch(r"[A-Za-z0-9_:]+", name) and private["expected_tests"] == [name],
                "original root test filter changed")
        require(private["session_files"] == [harness]
                and private["command"] == ["test", "--locked", *features, "--test", Path(path).stem,
                                           name, "--", "--nocapture"], "original root test argv changed")


def root_build_forms(public, source, before):
    """Apply original deltas only while controlled manifest values remain original.

    Unrelated candidate edits survive. A candidate that owns any controlled
    value gets only its current manifest, never a partially repaired reference.
    Original unedited inputs use the exact original manifest bytes.
    """
    reference_bytes = reference(public["manifests"][0]["reference"])
    original = tomllib.loads(reference_bytes.decode())
    current_bytes = core.bounded(source / "Cargo.toml")
    current = tomllib.loads(current_bytes.decode())
    declared = [(form, reference(form["manifests"][0]["asset"])) for form in public["forms"]]
    require(any(data == reference_bytes for _, data in declared), "reference manifest is not an original form")
    changes = [manifest_operations(original, tomllib.loads(data.decode())) for _, data in declared]
    keys = {tuple(op["key"]) for ops in changes for op in ops if op["kind"] not in {"ensure_table", "prune_empty"}}
    missing = object()

    def value(doc, key):
        for part in key:
            if not isinstance(doc, dict) or part not in doc:
                return missing
            doc = doc[part]
        return doc

    eligible = before["Cargo.lock"][1] == public["reference_seed_sha256"] and all(
        (value(original, key) is missing and value(current, key) is missing)
        or same_value(value(original, key), value(current, key)) for key in keys)
    forms = []
    candidates = list(zip(declared, changes)) if eligible else [(({"name": "current-candidate"}, current_bytes), [])]
    for (form, data), operations in candidates:
        if not eligible:
            emitted = current_bytes
        elif current_bytes == reference_bytes:
            emitted = data
        else:
            desired = apply_manifest_operations(current, operations)
            emitted = tomli_w.dumps(desired).encode()
            require(same_value(tomllib.loads(emitted.decode()), desired), "root overlay changed semantic data")
        identity = {"manifests": tomllib.loads(emitted.decode()), "public_command": public["public_command"]}
        cache = core.digest(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())[:20]
        item = {"name": form["name"], "spec": {"manifests": [], "locks": [], "private_members": []},
                "contents": {"Cargo.toml": emitted}, "cache": "form-" + cache,
                "candidate_owned": not eligible}
        if eligible:
            item["lock"] = form["lock"]  # Original manifest/lock pairs stay paired.
        forms.append(item)
    return forms


def root_metadata_mismatch(row, mirror, form):
    """Known native graph conflicts, not a generic retry of any exit101."""
    if row["returncode"] != 101 or row.get("limit"):
        return False
    if core.catalog_lock_incompatible(row):
        return True
    text = row["stderr"]
    version = re.match(r"error: failed to select a version for `([A-Za-z0-9_-]+)`\.\n", text)
    if version:
        return (text.count("error:") == 1 and str(mirror) in text
                and "versions that meet the requirements " in text
                and "previously selected package" in text
                and text.rstrip().endswith("which could resolve this conflict"))
    missing = re.fullmatch(
        r"error: failed to load manifest for workspace member `([^`\n]+)`\n\n"
        r"Caused by:\n  failed to load manifest for dependency `([A-Za-z0-9_-]+)`\n\n"
        r"Caused by:\n  failed to read `([^`\n]+)`\n\n"
        r"Caused by:\n  No such file or directory \(os error 2\)\n", text)
    if not missing or Path(missing[1]).resolve() != mirror.resolve():
        return False
    document = tomllib.loads(form["contents"]["Cargo.toml"].decode())
    dependency = document.get("dependencies", {}).get(missing[2])
    if not isinstance(dependency, dict) or not isinstance(dependency.get("path"), str):
        return False
    path = (mirror / dependency["path"] / "Cargo.toml").resolve()
    return path.is_relative_to(mirror.resolve()) and path == Path(missing[3]).resolve() and not path.exists()


def build_forms(public, source, before):
    if public.get("schema_version") == ROOT_SCHEMA:
        return root_build_forms(public, source, before)
    originals = {r["path"]: tomllib.loads(reference(r["reference"]).decode()) for r in public["manifests"]}
    current = {n: tomllib.loads(core.bounded(source / n).decode()) for n in originals}
    semantic = json.loads(reference(public["semantic_forms"]))
    require(semantic["task_id"] == public["task_id"] and len(semantic["common"]) == len(originals),
            "unbound common original overlay")
    common = {}
    for row in semantic["common"]:
        require(row["path"] in originals and all(op["key"][:1] == ["dev-dependencies"]
                for op in row["operations"]), "common overlay changes production inputs")
        common[row["path"]] = row["operations"]
    require(set(common) == set(originals), "common overlay is incomplete")
    base = {n: apply_manifest_operations(doc, common[n]) for n, doc in originals.items()}
    declared = []
    for form in public["forms"]:
        docs = {r["path"]: tomllib.loads(reference(r["asset"]).decode()) for r in form["manifests"]}
        expected = copy.deepcopy(base)
        for slot in public["slots"]:
            version = source_identity(dependency(docs[slot["path"]], slot["key"])).get("version")
            set_version(expected[slot["path"]], slot["key"], version)
        require(expected == docs, "original form changes more than declared version slots/common overlay")
        declared.append((form, docs))
    eligible = before["Cargo.lock"][1] == public["reference_seed_sha256"] and all(
        source_identity(dependency(current[s["path"]], s["key"])) ==
        source_identity(dependency(originals[s["path"]], s["key"])) for s in public["slots"])
    choices = declared if eligible else [({"name": "current-candidate"}, None)]
    forms = []
    for form, docs in choices:
        desired = {n: apply_manifest_operations(doc, common[n]) for n, doc in current.items()}
        if docs is not None:
            for slot in public["slots"]:
                version = source_identity(dependency(docs[slot["path"]], slot["key"]))["version"]
                set_version(desired[slot["path"]], slot["key"], version)
        changes = {"manifests": [{"path": n, "operations": manifest_operations(current[n], desired[n])}
                                for n in current], "locks": [], "private_members": []}
        emitted = overlay_files(source, changes)
        require(all(tomllib.loads(data.decode()) == desired[n] for n, data in emitted.items()),
                "shared manifest emitter changed semantics")
        identity = [{"path": s["path"], "key": s["key"],
                     "value": source_identity(dependency(desired[s["path"]], s["key"]))} for s in public["slots"]]
        cache = core.digest(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())[:20]
        forms.append({"name": form["name"], "spec": changes, "contents": emitted, "cache": "form-" + cache})
    return forms


def public_api_mismatch(row, workspace, snapshot):
    if row["returncode"] != 101 or row.get("limit"):
        return False
    errors, finished = [], False
    for line in row["stdout"].splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get("reason") == "build-finished":
            finished = value.get("success") is False
        message = value.get("message", {})
        if value.get("reason") != "compiler-message" or message.get("level") != "error":
            continue
        target = value.get("target", {})
        path = Path(target.get("src_path", ""))
        if (not path.is_absolute() or not path.resolve().is_relative_to(workspace)
                or path.relative_to(workspace).as_posix() not in snapshot
                or not set(target.get("kind", [])) & {"lib", "rlib", "bin", "proc-macro"}
                or set(target.get("kind", [])) & {"test", "bench", "custom-build"}):
            return False
        spans = [s for s in message.get("spans", []) if s.get("is_primary")]
        if not spans:
            if message.get("code") is None and re.fullmatch(
                    r"aborting due to (?:\d+ )?previous errors?(?:; \d+ warnings? emitted)?", message.get("message", "")):
                continue
            return False
        if not isinstance(message.get("code"), dict) or not re.fullmatch(r"E\d{4}", message["code"].get("code", "")):
            return False
        for span in spans:
            raw = Path(span["file_name"])
            candidates = [raw] if raw.is_absolute() else [workspace / raw, Path(value.get("manifest_path", "")).parent / raw]
            if not any(p.resolve().is_relative_to(workspace) and p.is_file()
                       and p.resolve().relative_to(workspace).as_posix() in snapshot
                       and p.suffix == ".rs" and p.name != "build.rs"
                       and not {"tests", "benches"} & set(p.relative_to(workspace).parts) for p in candidates):
                return False
        errors.append(message)
    return finished and bool(errors)


def run_candidate(public, source, folder, private=None, *, run=core.invoke):
    validate(public, private)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    require(not source.is_relative_to(folder) and not folder.is_relative_to(source), "source/cache overlap")
    root_mode = public["schema_version"] == ROOT_SCHEMA
    evidence = {"schema_version": public["schema_version"], "task_id": public["task_id"], "role": "grading" if private else "public",
                "selection_uses_test_results": False, "new_lock_generation_commands": 0,
                "status": "preparing", "probes": [], "public_compiles": [], "executions": []}
    try:
        for name in ("cargo", "rustc", "source_config"):
            require(core.file_digest(Path(public[name]["path"])) == public[name]["sha256"], "fixed compiler/config changed")
        before = core.snapshot(source)
        require("Cargo.lock" in before and before["Cargo.lock"][0] == "file", "candidate lock removed/redirected")
        if private:
            for row in private["session_files"]:
                require(core.digest(core.bounded(source / row["path"])) == row["sha256"], "original session input changed")
        forms = build_forms(public, source, before)
        owned = before["Cargo.lock"][1] != public["reference_seed_sha256"]
        if root_mode:
            owned = owned or forms[0]["candidate_owned"]
        locks = ([{"path": str(source / "Cargo.lock"), "sha256": before["Cargo.lock"][1]}] if owned else
                 [f["lock"] for f in public["forms"]])
        for row in locks:
            reference(row)
        folder.mkdir(parents=True, exist_ok=True)
        mirror, home = folder / "source", folder / "cargo-home"
        require(not home.is_symlink(), "Cargo home redirected")
        home.mkdir(exist_ok=True)
        require(not (home / "config.toml").is_symlink(), "source config redirected")
        (home / "config.toml").write_bytes(reference(public["source_config"]))
        env = {**os.environ, "RUSTC": public["rustc"]["path"], "CARGO_HOME": str(home), "SOURCE_ROOT": str(mirror),
               "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0"}
        target = os.environ.get("CARGO_TARGET_DIR", str(folder / "target"))
        deadline, compatible = time.monotonic() + public["timeout_seconds"], []
        evidence.update(source_before=before, candidate_lock_preserved=owned)

        def install(form, lock):
            synchronize_candidate(source, mirror, form["spec"])
            if root_mode:
                (mirror / "Cargo.toml").write_bytes(form["contents"]["Cargo.toml"])
            (mirror / "Cargo.lock").write_bytes(reference(lock))
            expected = {**before, **{n: ["file", core.digest(data), before[n][2]]
                                     for n, data in form["contents"].items()},
                        "Cargo.lock": ["file", lock["sha256"], before["Cargo.lock"][2]]}
            require(core.snapshot(mirror) == expected and core.snapshot(source) == before,
                    "environment preparation changed unrelated candidate files")
            return expected

        for number, form in enumerate(forms):
            matches = []
            env["CARGO_TARGET_DIR"] = str(Path(target) / form["cache"])
            selected_locks = [form["lock"]] if root_mode and "lock" in form else locks
            for index, lock in enumerate(selected_locks):
                expected = install(form, lock)
                command = [public["cargo"]["path"], "--offline", "metadata", "--format-version", "1", "--locked", "--all-features"]
                row = run(command, mirror, env, folder, f"metadata-{number}-{index}",
                          min(deadline, time.monotonic() + public["metadata_timeout_seconds"]), public["max_log_bytes"])
                item = {"form": form["name"], "lock_sha256": lock["sha256"], "command_result": row}
                evidence["probes"].append(item)
                core.complete(row)
                require(row["command"] == command and row["cwd"] == str(mirror), "metadata argv/cwd changed")
                require(core.snapshot(source) == before and core.snapshot(mirror) == expected, "metadata changed source")
                if row["returncode"] == 0:
                    graph_private = ({**private, "schema_version": core.PRIVATE_SCHEMA if root_mode
                                      else core.LOCKED_WORKSPACE_PRIVATE_SCHEMA} if private else None)
                    graph = core.graph_check(row["stdout"], mirror, expected, reference(lock), graph_private,
                                             locked_git=root_mode)
                    if root_mode:
                        roots = [p for p in json.loads(row["stdout"])["packages"] if p.get("source") is None
                                 and Path(p["manifest_path"]).resolve() == (mirror / "Cargo.toml").resolve()]
                        require(len(roots) == 1 and roots[0]["name"] == public["package"], "root package redirected")
                        if private:
                            require(any(t["name"] == Path(private["harness"]["path"]).stem
                                        and "test" in t["kind"] for t in roots[0]["targets"]),
                                    "original root integration test belongs to another workspace package")
                    item["graph"] = graph
                    matches.append((lock, graph))
                else:
                    mismatch = (root_metadata_mismatch(row, mirror, form) if root_mode else
                                core.lock_incompatible(row, original_locked=True))
                    require(mismatch, "metadata failed outside graph compatibility")
            require(len(matches) <= 1, "original form has ambiguous locked graphs")
            if not matches:
                continue
            lock, graph = matches[0]
            expected = install(form, lock)
            command = [public["cargo"]["path"], "--offline", *public["public_command"]]
            row = run(command, mirror, env, folder, f"public-{number}", deadline, public["max_log_bytes"])
            evidence["public_compiles"].append({"form": form["name"], "command_result": row, "target_cache": form["cache"]})
            core.complete(row)
            require(row["command"] == command and row["cwd"] == str(mirror), "public argv/cwd changed")
            require(core.snapshot(source) == before and core.snapshot(mirror) == expected, "public compiler changed source")
            if row["returncode"] == 0:
                compatible.append((form, lock, graph, row))
            else:
                require(public_api_mismatch(row, mirror, expected), "public failure is not a current source API mismatch")
        require(compatible, "no original dependency form compiles the current candidate")
        evidence.update(compatible_forms=[f["name"] for f, *_ in compatible],
                        selected_lock_sha256=compatible[0][1]["sha256"], graph=compatible[0][2])
        results = {name: True for name in private["expected_tests"]} if private else {}
        for number, (form, lock, graph, compiled) in enumerate(compatible):
            expected = install(form, lock)
            env["CARGO_TARGET_DIR"] = str(Path(target) / form["cache"])
            if private:
                command = [public["cargo"]["path"], *private["command"]]
                row = run(command, mirror, env, folder, f"original-test-{number}", deadline, public["max_log_bytes"])
                core.complete(row)
                require(row["command"] == command and row["cwd"] == str(mirror), "original test argv/cwd changed")
                found = core.RESULT.findall(row["stdout"])
                counts, values = Counter(n for n, _ in found), dict(found)
                for test in results:
                    results[test] &= bool(row["returncode"] == 0 and "test result: ok." in row["stdout"]
                                          and counts[test] == 1 and values.get(test) == "ok")
            else:
                row = compiled
            require(core.snapshot(source) == before and core.snapshot(mirror) == expected, "execution changed source")
            evidence["executions"].append({"form": form["name"], "lock_sha256": lock["sha256"], "graph": graph,
                                           "command_result": row, "target_cache": form["cache"]})
        evidence.update(status="completed", source_after=core.snapshot(source), source_unchanged=True,
                        test_results=results, exit_status=0 if not private or all(results.values()) else 1)
        return evidence
    except (OverlayError, ValueError, OSError, KeyError, TypeError) as error:
        evidence.update(status="rejected", exit_status=2, error=type(error).__name__ + ": " + str(error))
        raise core.VariantError(str(error), evidence) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--private", type=Path)
    args = parser.parse_args()
    public = json.loads(core.bounded(args.spec))
    private = json.loads(core.bounded(args.private)) if args.private else None
    require(public["task_id"] == args.task_id, "wrong task")
    root = Path("/opt/reviewbench-cache/rust-build/manifest-variants")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cpus = sorted(os.sched_getaffinity(0))
        offset = int(core.digest(args.task_id.encode())[:16], 16) % len(cpus)
        os.sched_setaffinity(0, {cpus[(offset + i) % len(cpus)] for i in range(min(len(cpus), public["cpus"]))})
        try:
            result = run_candidate(public, Path("/workspace"), root, private)
        except core.VariantError as error:
            result = error.evidence or {"status": "rejected", "exit_status": 2, "error": str(error)}
        if private and result["status"] == "completed":
            for name, passed in result["test_results"].items():
                print("test " + name + " ... " + ("ok" if passed else "FAILED"))
            print("test result: " + ("ok." if result["exit_status"] == 0 else "FAILED."))
        if result.get("error"):
            sys.stderr.write(result["error"] + "\n")
            for compile_row in result.get("public_compiles", []):
                for line in compile_row["command_result"]["stdout"].splitlines():
                    try:
                        message = json.loads(line).get("message", {})
                    except ValueError:
                        continue
                    if message.get("level") == "error":
                        sys.stderr.write((message.get("rendered") or message.get("message", ""))[:4000] + "\n")
        else:
            for row in result["executions"]:
                sys.stderr.write(row["command_result"]["stderr"])
        print(EVIDENCE_PREFIX + json.dumps(result, sort_keys=True), flush=True)
        return result["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
