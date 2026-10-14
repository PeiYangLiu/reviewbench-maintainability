#!/usr/bin/env python3
"""Choose a unique original retained lock using the full current Cargo graph."""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

if __package__:
    from .rust_build_entry import OverlayError, candidate_files, synchronize_candidate, tomllib
else:
    from build import OverlayError, candidate_files, synchronize_candidate, tomllib

SCHEMA = "reviewbench-cargo-original-retained-variants/v1"
REFERENCE_SCHEMA = "reviewbench-cargo-reference-seed-variants/v1"
WORKSPACE_REFERENCE_SCHEMA = "reviewbench-cargo-workspace-reference-seed-variants/v1"
LOCKED_WORKSPACE_SCHEMA = "reviewbench-cargo-locked-workspace-reference-seed-variants/v1"
CATALOG_WORKSPACE_SCHEMA = "reviewbench-cargo-catalog-workspace-reference-seed-variants/v1"
PRIVATE_SCHEMA = "reviewbench-cargo-original-retained-variants-private/v1"
WORKSPACE_PRIVATE_SCHEMA = "reviewbench-cargo-workspace-reference-seed-private/v1"
LOCKED_WORKSPACE_PRIVATE_SCHEMA = "reviewbench-cargo-locked-workspace-reference-seed-private/v1"
CATALOG_WORKSPACE_PRIVATE_SCHEMA = "reviewbench-cargo-catalog-workspace-reference-seed-private/v1"
EVIDENCE_PREFIX = "ReviewBench retained Cargo variants: "
SELECTION = "unique-full-current-candidate-locked-offline-metadata; never-test-outcomes"
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
MAX_INPUT = 16 * 1024**2
RESULT = re.compile(r"^test ([A-Za-z0-9_:]+) \.\.\. (ok|FAILED|ignored)\s*$", re.M)


class VariantError(OverlayError):
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


def require(condition, message):
    if not condition:
        raise VariantError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def bounded(path):
    require(path.is_file() and not path.is_symlink(), "retained input is not a regular file")
    with path.open("rb") as stream:
        data = stream.read(MAX_INPUT + 1)
    require(len(data) <= MAX_INPUT, "retained input exceeds its bound")
    return data


def validate(public, private=None):
    locked_workspace = public.get("schema_version") == LOCKED_WORKSPACE_SCHEMA
    catalog_workspace = public.get("schema_version") == CATALOG_WORKSPACE_SCHEMA
    git_workspace = locked_workspace or catalog_workspace
    workspace = git_workspace or public.get("schema_version") == WORKSPACE_REFERENCE_SCHEMA
    reference = workspace or public.get("schema_version") == REFERENCE_SCHEMA
    require(public.get("schema_version") in {
                SCHEMA, REFERENCE_SCHEMA, WORKSPACE_REFERENCE_SCHEMA, LOCKED_WORKSPACE_SCHEMA,
                CATALOG_WORKSPACE_SCHEMA}
            and public.get("selection") == SELECTION,
            "unbound retained Cargo selector")
    require(isinstance(public.get("task_id"), str) and public["task_id"],
            "missing retained Cargo task")
    require(public.get("workdir") == "." and public.get("lock_path") == "Cargo.lock",
            "retained Cargo root changed")
    for key in ("cargo", "rustc", "source_config"):
        row = public[key]
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound " + key)
    require(Path(public["cargo"]["path"]).name == "cargo"
            and Path(public["rustc"]["path"]).name == "rustc", "wrong compiler binding")
    locks = public.get("locks", [])
    require(len(locks) == 2 and len({r["sha256"] for r in locks}) == 2
            and len({r["path"] for r in locks}) == 2, "need two distinct retained lock assets")
    for row in locks:
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound retained lock")
    if reference:
        require(public.get("reference_seed_sha256") in {row["sha256"] for row in locks}
                and public.get("reference_seed_policy") == "replace-only-identical-committed-seed-in-private-mirror",
                "unbound committed reference seed")
        package = public.get("package")
        require(isinstance(package, str) and re.fullmatch(r"[A-Za-z0-9_-]+", package)
                and public.get("public_command") == ["build" if catalog_workspace else "check",
                                                     "--locked", "-p", package],
                "reference seed changed original public package command")
    else:
        require(not any(key in public for key in
                        ("reference_seed_sha256", "reference_seed_policy", "package", "public_command")),
                "missing-only schema cannot acquire a reference-seed override")
    if workspace:
        manifest = public.get("package_manifest")
        require(isinstance(manifest, str) and not Path(manifest).is_absolute()
                and ".." not in Path(manifest).parts and "." not in manifest.split("/")
                and len(Path(manifest).parts) > 1 and Path(manifest).name == "Cargo.toml",
                "workspace reference requires its current package manifest")
    else:
        require("package_manifest" not in public, "old schema cannot acquire a workspace-package override")
    if git_workspace:
        require(public.get("source_config_mode") == "isolated-cargo-home"
                and public.get("git_dependencies") == "full-locked-source-identities",
                "locked workspace requires its original-Cargo transport and locked Git identities")
    else:
        require("source_config_mode" not in public and "git_dependencies" not in public,
                "old schema cannot acquire a Cargo transport or Git dependency override")
    for name, minimum, maximum in (
        ("cpus", 1, 8 if catalog_workspace else 4),
        ("timeout_seconds", 1, 3600 if workspace else (2400 if reference else 1800)),
        ("metadata_timeout_seconds", 1, 180 if catalog_workspace else 120),
        ("max_log_bytes", 1024, (32 if catalog_workspace else 16) * 1024**2),
    ):
        require(type(public.get(name)) is int and minimum <= public[name] <= maximum,
                "invalid resource bound: " + name)
    if private is not None:
        private_schema = (CATALOG_WORKSPACE_PRIVATE_SCHEMA if catalog_workspace else
                          LOCKED_WORKSPACE_PRIVATE_SCHEMA if locked_workspace else
                          (WORKSPACE_PRIVATE_SCHEMA if workspace else PRIVATE_SCHEMA))
        require(private.get("schema_version") == private_schema
                and private.get("task_id") == public["task_id"], "unbound private original test")
        harness = private["harness"]
        prefix = (Path(public["package_manifest"]).parent / "tests").as_posix() + "/" if workspace else "tests/"
        require(set(harness) == {"path", "sha256"}
                and harness["path"].startswith(prefix)
                and Path(harness["path"]).suffix == ".rs"
                and ".." not in Path(harness["path"]).parts
                and re.fullmatch(r"[0-9a-f]{64}", harness["sha256"]), "invalid original harness")
        if workspace:
            require(private.get("package_manifest") == public["package_manifest"],
                    "workspace private test changed its package manifest")
        else:
            require("package_manifest" not in private, "old private schema acquired a workspace-package override")
        args = private["command"]
        require(args[0:1] == ["test"]
                and (("--locked" not in args) if reference and not locked_workspace else ("--locked" in args))
                and args.count("--test") == 1
                and args[args.index("--test") + 1] == Path(harness["path"]).stem
                and "--no-run" not in args and "--list" not in args,
                "original test argv changed")
        if locked_workspace:
            require(private.get("original_locked") is True and "original_unlocked" not in private
                    and args.count("--locked") == 1 and args.count("-p") == 1
                    and args[args.index("-p") + 1] == public["package"],
                    "locked reference-seed grading changed the original locked package command")
        elif reference:
            require(private.get("original_unlocked") is True and args.count("-p") == 1
                    and args[args.index("-p") + 1] == public["package"],
                    "reference-seed grading must preserve the original unlocked package command")
        else:
            require("original_unlocked" not in private, "original locked test acquired an unlocked override")
        if not locked_workspace:
            require("original_locked" not in private, "old private schema acquired a locked-workspace override")
        require(private["expected_tests"] and len(set(private["expected_tests"])) == len(private["expected_tests"])
                and all(re.fullmatch(r"[A-Za-z0-9_:]+", n) for n in private["expected_tests"]),
                "invalid original test identities")


def snapshot(root):
    result = {}
    for name, path in candidate_files(root).items():
        result[name] = (["symlink", os.readlink(path)] if path.is_symlink()
                        else ["file", file_digest(path), bool(path.stat().st_mode & 0o111)])
    return result


def invoke(command, cwd, env, folder, name, deadline, limit):
    paths = {kind: folder / (name + "." + kind) for kind in ("stdout", "stderr")}
    started, limited = time.monotonic(), None
    with paths["stdout"].open("wb") as stdout, paths["stderr"].open("wb") as stderr:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    limited = "deadline"
                    break
                if sum(path.stat().st_size for path in paths.values()) > limit:
                    limited = "output_limit"
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    if sum(path.stat().st_size for path in paths.values()) > limit:
        limited = limited or "output_limit"
    record = {"command": command, "cwd": str(cwd), "returncode": process.returncode,
              "limit": limited, "seconds": round(time.monotonic() - started, 6)}
    for kind, path in paths.items():
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        record[kind + "_total_bytes"] = path.stat().st_size
        record[kind + "_captured_bytes"] = len(data)
        record[kind + "_sha256"] = digest(data)
        try:
            record[kind] = data.decode()
            record[kind + "_utf8_valid"] = True
        except UnicodeDecodeError:
            record[kind] = data.decode(errors="replace")
            record[kind + "_utf8_valid"] = False
    return record


def complete(row):
    require(row.get("limit") is None, "Cargo command exceeded " + str(row.get("limit")))
    for name in ("stdout", "stderr"):
        raw = row[name].encode()
        require(row[name + "_utf8_valid"] and len(raw) == row[name + "_total_bytes"]
                == row[name + "_captured_bytes"] and digest(raw) == row[name + "_sha256"],
                "Cargo evidence is incomplete: " + name)


def graph_check(output, mirror, source_snapshot, lock_bytes, private=None, *, locked_git=False,
                omitted_workspace_package=None, workspace_root=None, omitted_locked_registry_packages=(),
                git_source_key=None):
    graph = json.loads(output)
    expected_workspace = mirror.resolve() if workspace_root is None else Path(workspace_root).resolve()
    require(expected_workspace.is_relative_to(mirror.resolve()), "Cargo workspace escaped complete candidate")
    require(Path(graph["workspace_root"]).resolve() == expected_workspace
            and graph.get("resolve") and graph.get("workspace_members"),
            "Cargo did not validate the complete root workspace")
    packages, nodes = graph["packages"], graph["resolve"]["nodes"]
    require(len({p["id"] for p in packages}) == len(packages)
            and {p["id"] for p in packages} == {n["id"] for n in nodes}
            and set(graph["workspace_members"]) <= {p["id"] for p in packages},
            "incomplete Cargo package/node graph")
    locks = tomllib.loads(lock_bytes.decode())["package"]
    require(git_source_key is None or (locked_git and callable(git_source_key)),
            "Git spelling comparison requires locked Git identities")

    def identity(package):
        source = package.get("source")
        if git_source_key is not None and isinstance(source, str) and source.startswith("git+"):
            source = git_source_key(source)
        return package["name"], package["version"], source

    expected = {identity(p) for p in locks}
    observed = {identity(p) for p in packages}
    require(len(expected) == len(locks), "Cargo graph differs from supplied lock")
    require(len(observed) == len(packages), "Cargo graph has duplicate semantic package identities")
    if omitted_workspace_package is not None:
        require(isinstance(omitted_workspace_package, tuple) and len(omitted_workspace_package) == 3
                and omitted_workspace_package[2] is None
                and omitted_workspace_package in expected and omitted_workspace_package not in observed,
                "public Cargo graph did not omit exactly its declared private workspace package")
        expected = expected - {omitted_workspace_package}
    if omitted_locked_registry_packages:
        # An opt-in, independently attested default-feature omission is not a
        # reason to rewrite the real Cargo.lock or narrow the workspace graph.
        omitted = set(omitted_locked_registry_packages)
        require(len(omitted) == len(omitted_locked_registry_packages)
                and all(isinstance(row, tuple) and len(row) == 3
                        and row[2] == CRATES_IO for row in omitted)
                and omitted <= expected and not omitted & observed,
                "undeclared or observed locked registry package omission")
        expected = expected - omitted
    require(observed == expected, "Cargo graph differs from supplied lock")
    local = []
    for package in packages:
        if package.get("source") is not None:
            source = package["source"]
            require(source == CRATES_IO or (locked_git and source.startswith("git+")
                    and re.search(r"#[0-9a-f]{40}$", source)), "unbound non-registry dependency")
            continue
        path = Path(package["manifest_path"]).resolve()
        require(path.is_relative_to(mirror.resolve()), "local dependency escaped complete candidate")
        name = path.relative_to(mirror.resolve()).as_posix()
        require(name in source_snapshot and Path(name).name == "Cargo.toml",
                "local package was not in current candidate manifests")
        local.append(name)
    if private is not None:
        targets = [t for p in packages if p.get("source") is None for t in p["targets"]
                   if t["name"] == Path(private["harness"]["path"]).stem and "test" in t["kind"]]
        require(len(targets) == 1 and Path(targets[0]["src_path"]).resolve()
                == (mirror / private["harness"]["path"]).resolve(),
                "original integration-test target is absent or redirected")
        manifest_file = mirror / "Cargo.toml"
        if private.get("schema_version") in {WORKSPACE_PRIVATE_SCHEMA, LOCKED_WORKSPACE_PRIVATE_SCHEMA,
                                             CATALOG_WORKSPACE_PRIVATE_SCHEMA}:
            manifest_file = mirror / private["package_manifest"]
            owner = [p for p in packages if any(t is targets[0] for t in p["targets"])]
            require(len(owner) == 1 and Path(owner[0]["manifest_path"]).resolve() == manifest_file.resolve()
                    and owner[0]["name"] == private["command"][private["command"].index("-p") + 1],
                    "original integration test belongs to a different current workspace package")
            require(manifest_file.is_file() and not manifest_file.is_symlink()
                    and manifest_file.resolve().is_relative_to(mirror.resolve()),
                    "current workspace test manifest escaped the candidate")
        manifest = tomllib.loads(manifest_file.read_text())
        require(not any(t.get("name") == targets[0]["name"] and t.get("harness") is False
                        for t in manifest.get("test", [])), "original libtest harness disabled")
    return {"packages": len(packages), "nodes": len(nodes),
            "workspace_members": len(graph["workspace_members"]), "local_manifests": sorted(local),
            "all_manifest_files": sum(Path(name).name == "Cargo.toml" for name in source_snapshot)}


def lock_incompatible(row, *, original_locked=False, locked_git_packages=()):
    if row["returncode"] != 101:
        return False
    text = row["stderr"]
    current_lock = str(Path(row["cwd"]) / "Cargo.lock")
    locked_193 = (
        "error: cannot update the lock file " + current_lock
        + " because --locked was passed to prevent this\n"
        "help: to generate the lock file without accessing the network, remove the --locked flag and use --offline instead.\n"
    )
    # Cargo1.60 renders the combination --locked --offline as --frozen.
    # Admit only its exact observed diagnostic for the new original-locked
    # schema and an actual locked, offline metadata invocation.
    frozen_160 = (
        "error: the lock file " + current_lock
        + " needs to be updated but --frozen was passed to prevent this\n"
        "If you want to try to generate the lock file without accessing the network, "
        "remove the --frozen flag and use --offline instead.\n"
    )
    if (original_locked and text == frozen_160
            and all(arg in row["command"] for arg in ("metadata", "--locked", "--offline"))):
        return True
    if original_locked and all(arg in row["command"] for arg in ("metadata", "--locked", "--offline")):
        # A source-only golden can introduce a pinned Git dependency absent
        # from the committed seed. Cargo1.60 emits this particular pre-resolution
        # error for that seed, before it can report the usual --frozen mismatch.
        # It is not a network/cache-error fallback: bind every part to a Git
        # package from the original lock union and the current candidate root.
        match = re.fullmatch(
            r"error: failed to get `(?P<name>[A-Za-z0-9_-]+)` as a dependency of package "
            r"`[A-Za-z0-9_-]+ v[0-9A-Za-z.+_-]+ \((?P<path>[^`\n]+)\)`\n\n"
            r"Caused by:\n  failed to load source for dependency `(?P=name)`\n\n"
            r"Caused by:\n  Unable to update (?P<url>[^\n]+)\n\n"
            r"Caused by:\n  the source (?P<key>reviewbench-original-[0-9a-f]{64}) "
            r"requires a lock file to be present first before it can be\n"
            r"  used against vendored source code\n\n"
            r"  remove the source replacement configuration, generate a lock file, and then\n"
            r"  restore the source replacement configuration to continue the build\n",
            text,
        )
        if match:
            owner = Path(match["path"])
            if owner.is_absolute() and owner.resolve().is_relative_to(Path(row["cwd"]).resolve()):
                for name, _version, source in locked_git_packages:
                    if (name == match["name"] and source.startswith("git+")
                            and re.search(r"#[0-9a-f]{40}$", source)
                            and source[4:].rsplit("#", 1)[0] == match["url"]
                            and "reviewbench-original-" + digest(source.encode()) == match["key"]):
                        return True
    return (("lock file" in text and "needs to be updated" in text and "--locked" in text)
            or text == locked_193
            or "failed to select a version for" in text
            or "no matching package named" in text)


def catalog_lock_incompatible(row):
    """Original nightly diagnostic, optionally preceded by unused-patch warnings."""
    ending = (
        "error: the lock file " + str(Path(row["cwd"]) / "Cargo.lock")
        + " needs to be updated but --locked was passed to prevent this\n"
        "If you want to try to generate the lock file without accessing the network, "
        "remove the --locked flag and use --offline instead.\n"
    )
    return (row["returncode"] == 101 and row["stderr"].endswith(ending)
            and row["stderr"].count("error:") == 1
            and all(arg in row["command"] for arg in ("metadata", "--locked", "--offline")))


def run_candidate(public, source, folder, private=None, *, run=invoke):
    validate(public, private)
    source, folder = source.resolve(), folder.resolve()
    require(not folder.is_relative_to(source) and not source.is_relative_to(folder),
            "retained lock work and source must be separate")
    evidence = {
        "schema_version": public["schema_version"], "task_id": public["task_id"], "status": "preparing",
        "role": "grading" if private else "public", "selection_uses_test_results": False,
        "new_lock_generation_commands": 0, "probes": [], "command_executions": 0,
    }
    try:
        for row in [public["cargo"], public["rustc"], public["source_config"], *public["locks"]]:
            path = Path(row["path"])
            require(path.is_file() and not path.is_symlink()
                    and file_digest(path) == row["sha256"], "fixed tool/config/lock bytes changed")
        before = snapshot(source)
        if private:
            harness = source / private["harness"]["path"]
            require(digest(bounded(harness)) == private["harness"]["sha256"],
                    "original harness bytes changed")
        evidence["source_before"] = before
        folder.mkdir(parents=True, exist_ok=True)
        mirror = folder / "source"
        evidence["synchronized"] = synchronize_candidate(source, mirror, {"manifests": [], "locks": []})
        require(snapshot(mirror) == before, "mirror did not preserve complete current candidate")
        supplied = "Cargo.lock" in before
        locked_workspace = public["schema_version"] == LOCKED_WORKSPACE_SCHEMA
        catalog_workspace = public["schema_version"] == CATALOG_WORKSPACE_SCHEMA
        git_workspace = locked_workspace or catalog_workspace
        locked_git_packages = sorted({
            (package["name"], package["version"], package["source"])
            for row in public["locks"]
            for package in tomllib.loads(bounded(Path(row["path"])).decode())["package"]
            if package.get("source", "").startswith("git+")
        }) if git_workspace else []
        reference = public["schema_version"] in {
            REFERENCE_SCHEMA, WORKSPACE_REFERENCE_SCHEMA, LOCKED_WORKSPACE_SCHEMA, CATALOG_WORKSPACE_SCHEMA}
        if git_workspace:
            require(supplied and before["Cargo.lock"][0] == "file",
                    "original locked workspace requires a current regular Cargo.lock")
        reference_seed = (reference and supplied
                          and before["Cargo.lock"][0] == "file"
                          and before["Cargo.lock"][1] == public["reference_seed_sha256"])
        owned = supplied and not reference_seed
        evidence["candidate_lock_supplied"] = supplied
        if reference:
            evidence["candidate_has_original_reference_seed"] = reference_seed
        candidates = ([{"path": str(source / "Cargo.lock"), "sha256": before["Cargo.lock"][1]}]
                      if owned else sorted(public["locks"], key=lambda row: row["sha256"]))
        prefix = [public["cargo"]["path"], "--offline"]
        if not git_workspace:
            prefix += ["--config", public["source_config"]["path"]]
        metadata = [*prefix, "metadata", "--format-version", "1", "--locked", "--all-features"]
        env = {**os.environ, "RUSTC": public["rustc"]["path"], "CARGO_NET_OFFLINE": "true",
               "CARGO_NET_RETRY": "0", "SOURCE_ROOT": str(mirror)}
        if git_workspace:
            # Cargo 1.60 does not accept stable --config. The same frozen source
            # configuration is carried through its documented Cargo-home file;
            # neither compiler nor the original test argv is upgraded.
            home = folder / "cargo-home"
            home.mkdir(exist_ok=True)
            config_file = home / "config.toml"
            require(not home.is_symlink() and not config_file.is_symlink(),
                    "isolated Cargo configuration escaped its working directory")
            config_file.write_bytes(bounded(Path(public["source_config"]["path"])))
            env["CARGO_HOME"] = str(home)
            evidence["source_config_mode"] = public["source_config_mode"]
        if catalog_workspace:
            env["CARGO_BUILD_JOBS"] = str(public["cpus"])
            evidence["test_results"] = {}
        deadline = time.monotonic() + public["timeout_seconds"]
        compatible = []
        for index, record in enumerate(candidates):
            data = bounded(Path(record["path"]))
            require(digest(data) == record["sha256"], "retained/default candidate lock changed")
            if not owned:
                (mirror / "Cargo.lock").write_bytes(data)
            expected = {**before, "Cargo.lock": ["file", record["sha256"],
                                               before["Cargo.lock"][2] if supplied else False]}
            require(snapshot(mirror) == expected, "probe changed candidate files other than missing lock")
            row = run(metadata, mirror, env, folder, "metadata-" + str(index),
                      min(deadline, time.monotonic() + public["metadata_timeout_seconds"]),
                      public["max_log_bytes"])
            observed = {"lock_sha256": record["sha256"], "command_result": row}
            evidence["probes"].append(observed)
            complete(row)
            require(row["command"] == metadata and row["cwd"] == str(mirror),
                    "metadata command/cwd changed")
            require(snapshot(source) == before and snapshot(mirror) == expected,
                    "metadata changed original candidate or supplied lock")
            if row["returncode"] == 0:
                observed["graph"] = graph_check(
                    row["stdout"], mirror, before, data, private, locked_git=git_workspace)
                compatible.append((record, data, observed["graph"]))
            else:
                mismatch = (catalog_lock_incompatible(row) if catalog_workspace else
                            lock_incompatible(row, original_locked=git_workspace,
                                              locked_git_packages=locked_git_packages))
                require(mismatch,
                        "Cargo metadata failed outside dependency compatibility")
                observed["compatible"] = False
        require(len(compatible) == 1, "no unique compatible retained lock: " + str(len(compatible)))
        selected, data, graph = compatible[0]
        if not owned:
            (mirror / "Cargo.lock").write_bytes(data)
        expected = {**before, "Cargo.lock": ["file", selected["sha256"],
                                           before["Cargo.lock"][2] if supplied else False]}
        require(snapshot(source) == before and snapshot(mirror) == expected,
                "selected lock did not preserve candidate inputs")
        evidence.update(selected_lock_sha256=selected["sha256"], graph=graph,
                        candidate_lock_preserved=owned, mirror_before=expected)
        command = [*prefix, *(private["command"] if private else
                             public.get("public_command", ["check", "--locked"]))]
        if catalog_workspace and private:
            # Preserve the original unlocked test argv exactly. The source
            # configuration and offline policy already live in its private home.
            command = [public["cargo"]["path"], *private["command"]]
        execution = run(command, mirror, env, folder, "execution", deadline, public["max_log_bytes"])
        evidence["execution"] = execution
        evidence["command_executions"] = 1
        complete(execution)
        require(execution["command"] == command and execution["cwd"] == str(mirror),
                "original command/cwd changed")
        after, mirror_after = snapshot(source), snapshot(mirror)
        evidence.update(source_after=after, mirror_after=mirror_after,
                        source_unchanged=after == before, mirror_unchanged=mirror_after == expected)
        require(after == before and mirror_after == expected, "Cargo command changed candidate inputs")
        if private:
            counts = Counter(name for name, _ in RESULT.findall(execution["stdout"]))
            values = dict(RESULT.findall(execution["stdout"]))
            finished = ("test result: ok." in execution["stdout"] or
                        "test result: FAILED." in execution["stdout"])
            evidence["test_results"] = {
                name: bool(execution["returncode"] == 0 and finished and counts[name] == 1
                           and values.get(name) == "ok") for name in private["expected_tests"]
            }
        exit_status = execution["returncode"]
        if catalog_workspace and private and not exit_status and not all(evidence["test_results"].values()):
            exit_status = 1
        evidence.update(status="completed", exit_status=exit_status)
        return evidence
    except (OverlayError, OSError, ValueError, KeyError, TypeError) as error:
        evidence.update(status="rejected", error=type(error).__name__ + ": " + str(error), exit_status=2)
        raise VariantError(str(error), evidence) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    parser.add_argument("--build-root", type=Path, default=Path("/opt/reviewbench-cache/rust-build"))
    args = parser.parse_args()
    public = json.loads(bounded(args.spec))
    private = json.loads(bounded(args.private)) if args.private else None
    validate(public, private)
    require(args.task_id == public["task_id"], "wrong original task")
    args.build_root.mkdir(parents=True, exist_ok=True)
    with (args.build_root / "retained-variants.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if hasattr(os, "sched_getaffinity"):
            cpus = sorted(os.sched_getaffinity(0))
            offset = int.from_bytes(hashlib.sha256(args.task_id.encode()).digest()[:8], "big") % len(cpus)
            os.sched_setaffinity(0, {cpus[(offset + n) % len(cpus)]
                                    for n in range(min(len(cpus), public["cpus"]))})
        try:
            name = ("catalog-workspace-original" if public["schema_version"] == CATALOG_WORKSPACE_SCHEMA else
                    "locked-workspace-original" if public["schema_version"] == LOCKED_WORKSPACE_SCHEMA else
                    ("workspace-reference-original" if public["schema_version"] == WORKSPACE_REFERENCE_SCHEMA else
                    ("reference-original" if public["schema_version"] == REFERENCE_SCHEMA else "image-original"))
                   )
            evidence = run_candidate(public, args.source, args.build_root / name, private)
        except VariantError as error:
            evidence = error.evidence or {"status": "rejected", "exit_status": 2, "error": str(error)}
        if "execution" in evidence:
            sys.stdout.write(evidence["execution"]["stdout"])
            sys.stderr.write(evidence["execution"]["stderr"])
        (args.build_root / "retained-variants-evidence.json").write_text(
            json.dumps(evidence, sort_keys=True) + "\n")
        print(EVIDENCE_PREFIX + json.dumps(evidence, sort_keys=True), flush=True)
        return evidence["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
