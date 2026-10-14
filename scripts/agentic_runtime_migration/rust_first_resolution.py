"""Explicit first-new-environment Cargo locks, separate from historical recovery.

The two reference locks authenticate environment assets only. Runtime selection
and validation use the complete current candidate, never a replay label or an
original production-file hash.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import tomllib
from urllib.parse import urlsplit

import tomli_w

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git, tree_files
from .rust_lock_assets import BINDINGS, attested_bytes, reference_trees
from .rust_overlay import session_bytes

TASK = "github__prometheus__client_rust__pull_82__comment_956892586__complexity"
FONT_TASK = "github__googlefonts__fontations__pull_191__comment_1055650458__complexity"
SCYLLA_TASK = "github__scylladb__scylla-rust-driver__pull_509__comment_962978126__api_design"
ZEROCOPY_TASK = "github__google__zerocopy__pull_1752__comment_1777397723__complexity"
LIBSBP_TASK = "github__swift-nav__libsbp__pull_1218__comment_970220101__naming"
PROFILE = "prometheus82-first-new-locks-v1"
OPTION = "rust_new_environment_locks"
SCHEMA = "reviewbench-new-cargo-resolution-inputs/v1"
RUNTIME_SCHEMA = "reviewbench-cargo-first-new-resolution/v1"
PRIVATE_SCHEMA = "reviewbench-cargo-first-new-resolution-private/v1"
MARKER = "first_new_resolution"
TOOLS = "/opt/reviewbench-assets/rust-first-resolution-tools"
SPEC = "/opt/reviewbench-assets/rust-first-resolution.json"
PRIVATE = "/opt/reviewbench-assets/rust-first-resolution-private.json"
LOCK_PREFIX = "/opt/reviewbench-assets/rust-new-lock-"
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
GOLDEN = "eac0fadebf99c37106df628c4f33d5024d27da1d23ba7f88308dd8a6d47fc8ab"
HARNESS = "tests/oracle_session.rs"
HARNESS_SHA = "b7cbe2ba8083d0e6c932328d64a9b28691cb04f5387e0ff0786281983a11e421"
EXPECTED = ["registered_counter_is_encoded_through_the_public_registry"]
COMMAND = ["test", "--test", "oracle_session", "--", "--nocapture"]
CARGO_SHA = "9548937d530bf439ff1ba47a3b2bd26eeb9c3aff1961c20c01798613de922578"
RUSTC_SHA = "d32249a7c3bfcfc67b471460386e46323accae7125e344567a12d5664d99bb57"
DEFAULTS = {
    "start": {
        "lock_sha256": "518ed8cddda93657b5113d1c1576f1c88ea68c89ac4340324434b9b6f6247838",
        "selector_package": "prometheus-client-derive-text-encode",
        "packages": 369,
        "manifests": {
            "Cargo.toml": "0a77b68e5ef6ea7750f4d8ce3abfda105bec6ac2601c381b3ccb43b61c630953",
            "derive-text-encode/Cargo.toml": "adb3065f297e4510831eee2a33152924d4782336fc2e28c8c22add81941fc326",
        },
        "logs": (
            "709692efa1354165c6ff5e7c2c0b55133673b580e7de8586c0f94b7d8d086397",
            "5b3c6cddbd25083dcbb38fdc8125fa4791dc7ba1cf82850edb51fc5386975e21",
            "c905d23d50b9afea1e9614f03c381b656cfa3a536a78a13572c58e244faf01c3",
        ),
    },
    "golden": {
        "lock_sha256": "108fc2e95aa4f645c11e2167c95e7cff727ed1a20282a153bbb5353a4ad059a7",
        "selector_package": "prometheus-client-derive-encode",
        "packages": 406,
        "manifests": {
            "Cargo.toml": "71dfda294859add28f2e96b41b2b3ee2973f74cd23a840f78e79875ea12075f2",
            "derive-encode/Cargo.toml": "733232f081bbbc88de3fd54df40ba69f56adddd9ec0a10aeb15036bca785e16c",
        },
        "logs": (
            "7aa3ba12af9a9c180d90df630f32f1373b55161f3d1a14e4f5ed4a6aaa2f4fbc",
            "79b56e89b62a08ee139ea6657e0a897e3a8df9c0670b9a6c24feb7a82b429959",
            "93463da3256d5c52d781efa181e0e9090d30f80f49644eacaed57759d1bbd03b",
        ),
    },
}


def require(condition, message):
    if not condition:
        raise ContractError("first-new Cargo resolution: " + message)


def profile_for(task_id):
    """Reference bindings are data; runtime never switches mutable globals."""
    if task_id == TASK:
        return {
            "task_id": TASK, "profile": PROFILE, "golden": GOLDEN,
            "harness": HARNESS, "harness_sha256": HARNESS_SHA, "expected": EXPECTED,
            "command": COMMAND, "defaults": DEFAULTS, "allow_git": False,
            "virtual_workspace": False, "local_packages": 2, "input_index": 83,
            "public_package": None,
        }
    require(task_id in {FONT_TASK, SCYLLA_TASK, ZEROCOPY_TASK, LIBSBP_TASK}, "this profile does not admit another task")
    name = {FONT_TASK: "fontations", SCYLLA_TASK: "scylla", ZEROCOPY_TASK: "zerocopy", LIBSBP_TASK: "libsbp"}[task_id]
    path = Path(__file__).with_name("rust_first_resolution_" + name + ".json")
    profile = json.loads(path.read_text())
    expected = {FONT_TASK: "fontations45-first-new-locks-v1",
                SCYLLA_TASK: "scylla96-original-prepare-lock-v1",
                ZEROCOPY_TASK: "zerocopy44-first-new-shared-lock-v1",
                LIBSBP_TASK: "libsbp104-original-prepare-library-v1"}[task_id]
    require(profile["task_id"] == task_id and profile["profile"] == expected,
            "reference profile changed")
    profile["defaults"] = {variant: profile["defaults"][variant] for variant in ("start", "golden")}
    return profile


VIRTUAL_WARNING = (
    'warning: virtual workspace defaulting to `resolver = "1"` despite one or more workspace members '
    'being on edition 2021 which implies `resolver = "2"`\n'
    '  |\n'
    '  = note: to keep the current resolver, specify `workspace.resolver = "1"` in the workspace root\'s manifest\n'
    '  = note: to use the edition 2021 resolver, specify `workspace.resolver = "2"` in the workspace root\'s manifest\n'
    '  = note: for more details see https://doc.rust-lang.org/cargo/reference/resolver.html#resolver-versions\n'
)


def metadata_stderr_allowed(data, *, virtual=False):
    return not data.strip() or virtual and data.decode() == VIRTUAL_WARNING


def private_index_revalidation(cache):
    """Accept only the recorded index delta after complete source-object checks."""
    review = referenced(cache["exact_private_index_revalidation"], json_data=True)
    require(review["schema_version"] == "reviewbench-explicit-private-git-index-revalidation/v1"
            and review["complete"] is True and not review["added_files"] and not review["removed_files"]
            and review["new_cargo_resolves"] == review["new_git_fetches"] == 0
            and review["original_export_snapshot_rewritten"] is False
            and review["no_original_cache_edits"] is True
            and review["full_index_paths_modes_and_blobs_equal_frozen_commit_tree"] is True,
            "private index revalidation is incomplete")
    before = referenced(review["original_export_snapshot"], json_data=True)
    after = referenced(review["derived_post_inspection_snapshot"], json_data=True)
    require(before.keys() == after.keys(), "private cache gained/lost inputs during index inspection")
    changes = {name: {"before": before[name], "after": after[name]}
               for name in before if before[name] != after[name]}
    require(changes == review["observed_changes"] and len(changes) == 1,
            "index correction hides another cache change")
    name, change = next(iter(changes.items()))
    require(name.startswith("git/checkouts/") and name.endswith("/.git/index"),
            "corrected cache input is not a private checkout index")
    require(review["private_index_after"]["sha256"] == change["after"]["sha256"]
            and cache["preserved_file_manifest"]["sha256"]
            == review["derived_post_inspection_snapshot"]["sha256"], "private index snapshot binding changed")
    referenced(review["private_index_after"])
    index = referenced(review["index_entries"], json_data=True)
    tracked = referenced(review["tracked_tree"], json_data=True)
    require(index == {path: {"git_blob_oid": row["git_blob_oid"], "git_mode": row["git_mode"]}
                      for path, row in tracked.items()}, "index differs from the full frozen tracked tree")
    checkout = Path(cache["path"]) / Path(name).parents[1]
    revision = review["frozen_git_revision"]
    require(git(checkout, "rev-parse", revision + "^{tree}").decode().strip()
            == review["frozen_git_tree_oid"], "frozen Git tree identity changed")
    raw = git(checkout, "ls-tree", "-r", "-z", revision)
    observed = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        header, path = entry.split(b"\t", 1)
        mode, kind, oid = header.decode().split()
        require(kind == "blob", "unaccounted submodule/non-file in the revalidated tree")
        observed[path.decode()] = {"git_mode": mode, "git_blob_oid": oid}
    require(observed == index, "revalidated index omits or changes a frozen Git object")
    for path, row in tracked.items():
        require(file_hash(checkout / repo_path(path)) == row["sha256"],
                "private Git source differs from its revalidated tracked blob")
    return review


def referenced(row, *, limit=32 * 1024**2, json_data=False):
    require(isinstance(row, dict) and set(row) <= {"path", "sha256", "bytes"}
            and {"path", "sha256"} <= set(row), "invalid pinned file reference")
    require(isinstance(row["path"], str) and isinstance(row["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "invalid file identity")
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "missing regular pinned file")
    require(path.stat().st_size <= limit, "pinned file exceeds its byte bound")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == row["sha256"]
            and ("bytes" not in row or row["bytes"] == len(data)), "pinned file changed")
    return json.loads(data) if json_data else data


def requested(ctx, task):
    config = getattr(ctx, "config", {}).get("migration", {}).get(OPTION)
    if config is None:
        return False
    require(isinstance(config, dict), "opt-in must map task IDs to pinned input envelopes")
    if task.get("id") not in config:
        return False
    profile_for(task["id"])
    require(isinstance(config[task["id"]], dict), "invalid opt-in envelope")
    return True


def reference_packages(data, *, allow_git=False):
    """Validate an immutable default; runtime current-lock validation is separate."""
    parsed = tomllib.loads(data.decode())
    rows = parsed.get("package")
    require(isinstance(rows, list) and 2 <= len(rows) <= 4096, "invalid default lock graph")
    identities = set()
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("name"), str)
                and isinstance(row.get("version"), str), "malformed default package")
        identity = row["name"], row["version"], row.get("source")
        require(identity not in identities, "duplicate default package identity")
        identities.add(identity)
        if allow_git and isinstance(identity[2], str) and identity[2].startswith("git+"):
            parsed_source = urlsplit(identity[2][4:])
            require(parsed_source.scheme == "https" and bool(parsed_source.netloc)
                    and bool(parsed_source.path) and re.fullmatch(r"[0-9a-f]{40}", parsed_source.fragment)
                    and row.get("checksum") in (None, "<none>"), "unbound Git package")
        elif identity[2] is not None:
            require(identity[2] == CRATES_IO and isinstance(row.get("checksum"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", row["checksum"]), "unbound registry package")
    return parsed


def graph_summary(graph, lock, source, manifests, *, local_packages=2, virtual=False):
    """Check every default package, resolve node, edge, feature and local target."""
    rows = graph.get("packages")
    resolve = graph.get("resolve")
    require(type(graph.get("version")) is int and graph["version"] == 1
            and graph.get("workspace_root") == str(source)
            and isinstance(rows, list) and isinstance(resolve, dict), "incomplete metadata framing")
    nodes = resolve.get("nodes")
    require(isinstance(nodes, list) and len(rows) == len(nodes) == len(lock["package"]),
            "metadata does not cover the entire lock")
    by_id = {row["id"]: (row["name"], row["version"], row.get("source")) for row in rows}
    require(len(by_id) == len(rows) == len({row["id"] for row in nodes})
            and set(by_id) == {row["id"] for row in nodes}, "duplicate/missing metadata identity")
    require(set(by_id.values()) == {
        (row["name"], row["version"], row.get("source")) for row in lock["package"]
    }, "lock and metadata package identities differ")
    local = [row for row in rows if row.get("source") is None]
    require(len(local) == local_packages
            and set(graph.get("workspace_members", [])) == {row["id"] for row in local},
            "original workspace members differ")
    for row in local:
        path = Path(row["manifest_path"])
        require(path.is_relative_to(source) and path.relative_to(source).as_posix() in manifests,
                "local metadata manifest is outside the complete original source")
        require(file_hash(path) == manifests[path.relative_to(source).as_posix()],
                "local metadata manifest changed")
        require(isinstance(row.get("targets"), list) and row["targets"], "missing local targets")
        for target in row["targets"]:
            path = Path(target["src_path"])
            # The original command selects an integration test, not benches.
            # Its golden manifest declares an absent, unselected protobuf bench.
            unselected_bench = target.get("kind") == ["bench"] and target.get("test") is False
            require(path.resolve().is_relative_to(source.resolve()) and not path.is_symlink()
                    and (path.is_file() or unselected_bench),
                    "local target escapes the original source")
    root = [row for row in local if row["manifest_path"] == str(source / "Cargo.toml")]
    if virtual:
        require(not root and resolve.get("root") is None
                and "package" not in tomllib.loads((source / "Cargo.toml").read_text()),
                "wrong original virtual metadata root")
    else:
        require(len(root) == 1 and resolve.get("root") == root[0]["id"], "wrong metadata root")
    normalized = []
    for row in nodes:
        dependencies, deps, features = row.get("dependencies"), row.get("deps"), row.get("features")
        require(isinstance(dependencies, list) and len(dependencies) == len(set(dependencies))
                and set(dependencies) <= set(by_id) and isinstance(deps, list)
                and {dep["pkg"] for dep in deps} == set(dependencies), "missing/duplicate graph edge")
        require(isinstance(features, list) and all(isinstance(v, str) for v in features)
                and len(features) == len(set(features)), "invalid graph features")
        for dep in deps:
            require(isinstance(dep.get("name"), str) and isinstance(dep.get("dep_kinds"), list)
                    and all(isinstance(k, dict) and set(k) == {"kind", "target"}
                            and k["kind"] in {None, "dev", "build"}
                            and (k["target"] is None or isinstance(k["target"], str))
                            for k in dep["dep_kinds"]), "unbound typed dependency edge")
        normalized.append({
            "package": by_id[row["id"]], "features": sorted(features),
            "deps": sorted(
                [{"name": dep["name"], "package": by_id[dep["pkg"]], "kinds": dep["dep_kinds"]}
                 for dep in deps], key=canonical_hash),
        })
    return {"packages": len(rows), "resolve_nodes": len(nodes),
            "semantic_graph_sha256": canonical_hash(sorted(normalized, key=canonical_hash))}


def original_inputs(ctx, task, plan, oracle, patch):
    profile = profile_for(task.get("id"))
    pin = BINDINGS[profile["task_id"]]
    require(task.get("id") == profile["task_id"] and oracle.get("id") == profile["task_id"]
            and all(task.get(k) == oracle.get(k) == pin[k] for k in ("repo", "start_sha"))
            and oracle.get("input_index") == profile["input_index"]
            and canonical_hash(plan) == pin["plan_sha256"], "original task/oracle/plan changed")
    original = json.loads(attested_bytes(
        ctx.legacy / "rust", pin["artifacts"] + "/result.json", pin["oracle_file_sha256"]))
    require({k: v for k, v in oracle.items() if k != "lane_results_source"} == original,
            "original executable oracle changed")
    require(patch.get("sha256") == oracle.get("golden_patch_sha256") == profile["golden"]
            and file_hash(Path(patch["path"])) == profile["golden"], "original source-only golden changed")
    require(shlex.split(plan["command"]) == ["cargo", "+" + profile.get("toolchain", "1.93.0"),
                                           *profile["command"]]
            and plan.get("prepare_command") == profile.get("prepare_command")
            and oracle["pass_to_pass_tests"] == profile["expected"],
            "original unlocked test command/prepare/selection changed")
    if profile.get("repository_lib_tests"):
        require(not plan.get("session_files") and original["session_files"] == {"start": [], "golden": []}
                and plan["workdir"] == profile["workdir"] == "rust"
                and original["test_harness"]["kind"] == profile["harness_kind"],
                "original repository library tests/workdir changed or a surrogate harness was added")
    else:
        require(len(plan["session_files"]) == 1 and plan["session_files"][0]["path"] == profile["harness"]
                and hashlib.sha256(session_bytes(ctx, plan, oracle, plan["session_files"][0])).hexdigest()
                == profile["harness_sha256"], "original assertion bytes changed")
    trees = reference_trees(ctx, task, patch, {"Cargo.lock"})
    for variant, row in profile["defaults"].items():
        require(trees[variant][0] == row["manifests"] and trees[variant][1]["Cargo.lock"] is None,
                "original complete manifest tree or missing lock changed")
        runs = original["runs"][variant]
        require(len(runs) == 3, "missing original repetitions")
        for index, (run, digest) in enumerate(zip(runs, row["logs"]), 1):
            name = pin["artifacts"] + f"/logs/{variant}-run{index}-attempt1.log"
            require(run["log"] == name and run["passed_tests"] == profile["expected"]
                    and not run["failed_tests"] and run["attempts"]
                    and all(a["exit_code"] == 0 and not a["timed_out"] for a in run["attempts"]),
                    "original six-run success evidence changed")
            attested_bytes(ctx.legacy / "rust", name, digest)
    return trees


def prepared_inputs(envelope_ref):
    """Read the explicit parent-sealed envelope; never run a resolver/download."""
    envelope = referenced(envelope_ref, json_data=True)
    require(isinstance(envelope, dict)
            and set(envelope) == {"schema_version", "task_id", "root", "files"}
            and envelope["schema_version"] == SCHEMA,
            "unknown new-lock input envelope")
    profile = profile_for(envelope["task_id"])
    task_id = profile["task_id"]
    if task_id == LIBSBP_TASK:
        from .rust_libsbp_resolution import prepared_original_sequence
        return prepared_original_sequence(envelope, profile)
    if task_id == ZEROCOPY_TASK:
        from .rust_zerocopy_resolution import prepared_shared_first_lock
        return prepared_shared_first_lock(envelope, profile)
    if task_id == SCYLLA_TASK:
        from .rust_scylla_resolution import prepared_original_sequence
        return prepared_original_sequence(envelope, profile)
    root = Path(envelope["root"])
    require(root.is_absolute() and root.is_dir() and isinstance(envelope["files"], dict),
            "invalid prepared input root")

    def read(name, *, json_data=True):
        name = repo_path(name)
        require(name in envelope["files"], "unsealed prepared input: " + name)
        return referenced({"path": str(root / name), **envelope["files"][name]}, json_data=json_data)

    decision = read("first-resolution-decision.json")
    projection = read("original-source-projection.json")
    receipt = read("resolution-receipt.json")
    require(isinstance(receipt, dict)
            and receipt.get("schema_version") == "reviewbench-first-new-cargo-resolution/v1"
            and receipt.get("task_id") == task_id
            and receipt.get("status") == "new_environment_locks_full_metadata_and_registry_archives_prepared"
            and receipt.get("historical_lock_restoration") is False
            and receipt.get("original_prepare_command") is None
            and receipt.get("original_test_features_selectors_manifests_or_source_changed"
                            if profile["allow_git"] else "original_test_features_selectors_or_sources_changed") is False
            and receipt.get("version_or_git_selection_based_on_tests"
                            if profile["allow_git"] else "test_outcome_based_version_selection") is False
            and receipt.get("new_completed_benchmark_cases") == 0
            and all(receipt.get("execution_counts", {}).get(key) == value for key, value in {
                "blob_vendor_publications": 0, "generate_lockfile": 2,
                "locked_all_features_metadata": 2, "model_or_judge_API": 0,
                "public_calibrations": 0, "runtime_registrations": 0,
                "successful_generate_lockfile_per_variant": 1, "sut_check_build_test": 0, "update": 0,
            }.items()), "producer did not finish the exact pre-SUT initialization")
    if not profile["allow_git"]:
        require(len(receipt["execution_counts"]) == 9, "unexpected original registry-only execution")
    for key, name in (
        ("decision", "first-resolution-decision.json"),
        ("source_projection", "original-source-projection.json"),
        ("registry_archives", "archive-checksums.json"),
    ):
        require(receipt[key]["sha256"] == envelope["files"][name]["sha256"],
                "producer receipt points to another sealed input")
    terminal = referenced(receipt["resource_execution"]["container_result"], json_data=True)
    require(terminal["exit_status"] == 0 and terminal["outer_timeout"] is False
            and terminal["private_cargo_home_preserved"] is True,
            "producer has no preserved natural terminal result")
    if profile["allow_git"]:
        require(receipt["historical_git_revision_recovery"] is False
                and receipt["private_cargo_home"]["original_external_cache_files_unchanged"] is True,
                "new Git preparation changed old cache inputs")
        if not receipt["private_cargo_home"]["snapshot_byte_identical"]:
            require(receipt["private_cargo_home"]["snapshot_revalidated_after_private_index_refresh"] is True
                    and receipt["private_cargo_home"]["all_other_cache_file_bytes_identical"] is True
                    and receipt["private_cargo_home"]["original_export_snapshot_rewritten"] is False,
                    "unaccounted private Cargo cache change")
            private_index_revalidation(receipt["private_cargo_home"])
    else:
        require(receipt["resource_execution"]["cargo_generate_after_variant_first_success"] == 0,
                "producer regenerated an existing default")
    require(decision["task_id"] == projection["task_id"] == task_id
            and decision["historical_lock_restoration"] is False
            and decision["original_prepare_command"] is None
            and decision["new_environment_command"] == ["cargo", "+1.93.0", "generate-lockfile"]
            and decision["variants"] == ["start", "golden"]
            and decision["maximum_successful_initializations_per_variant"] == 1,
            "missing explicit first-new-environment decision")
    require(decision["source_projection"]["sha256"]
            == envelope["files"]["original-source-projection.json"]["sha256"]
            and projection["plan_sha256"] == decision["original_plan_sha256"] == BINDINGS[task_id]["plan_sha256"]
            and decision["original_golden"]["sha256"] == projection["original_golden"]["sha256"] == profile["golden"],
            "new-lock decision lost its original source binding")
    if profile["allow_git"]:
        require(decision.get("historical_git_revision_recovery") is False
                and decision["git_resolution"]["url"] == "https://github.com/linebender/peniko"
                and decision["git_resolution"]["branch_rev_tag_overrides"] is None
                and decision["git_resolution"]["no_version_or_commit_selection_based_on_tests"] is True,
                "missing original floating-Git first-new-resolution decision")
    toolchain = read("toolchain-binding.json")
    require(toolchain["cargo"]["sha256"] == CARGO_SHA and toolchain["rustc"]["sha256"] == RUSTC_SHA
            and toolchain["original_retained_receipt"]["status"] == "ready"
            and toolchain["original_retained_receipt"]["version"] == "1.93.0", "pinned compiler changed")
    require(file_hash(Path(toolchain["cargo"]["path"])) == CARGO_SHA
            and file_hash(Path(toolchain["rustc"]["path"])) == RUSTC_SHA, "compiler bytes changed")
    metadata = read("metadata-complete.json")
    require(metadata["new_generate_invocations"] == 2 and metadata["new_update_invocations"] == 0
            and metadata["historical_lock_restoration"] is False
            and metadata["original_prepare_command"] is None
            and metadata["sut_builds_checks_tests"] == metadata["calibrations_models_registrations"] == 0,
            "resolution receipt includes unapproved work or repeat initialization")
    locks, graph_audits, graphs, cache_roots, first_git = {}, {}, {}, set(), {}
    for variant, pin in profile["defaults"].items():
        data = read(f"first-success/{variant}/Cargo.lock", json_data=False)
        require(hashlib.sha256(data).hexdigest() == pin["lock_sha256"], "first-success default changed")
        lock = reference_packages(data, allow_git=profile["allow_git"])
        first = read(f"first-success/{variant}/receipt.json")
        generated = read(f"commands/generate-{variant}/result.json")
        measured = read(f"commands/metadata-{variant}/result.json")
        source = Path(generated["cwd"])
        require(first["schema_version"] == "reviewbench-first-new-cargo-lock/v1"
                and first["task_id"] == task_id and first["variant"] == variant
                and first["first_success_frozen"] is True
                and first["historical_lock_restoration"] is False
                and first["original_prepare_command"] is None
                and first["selection_uses_tests"] is False
                and first["generate_invocations"] == first["successful_generate_invocations"] == 1
                and first["lock"]["sha256"] == pin["lock_sha256"]
                and first["decision"]["sha256"] == receipt["decision"]["sha256"]
                and first["generate_record"]["sha256"]
                == envelope["files"][f"commands/generate-{variant}/result.json"]["sha256"],
                "default is not its separately recorded first successful lock")
        require(datetime.fromisoformat(generated["completed_at"])
                <= datetime.fromisoformat(first["captured_at"])
                < datetime.fromisoformat(measured["started_at"]),
                "first lock was not fixed before full metadata")
        for key, name in (
            ("lock", f"first-success/{variant}/Cargo.lock"),
            ("generate_record", f"commands/generate-{variant}/result.json"),
            ("metadata_record", f"commands/metadata-{variant}/result.json"),
        ):
            require(receipt["variants"][variant][key]["sha256"] == envelope["files"][name]["sha256"],
                    "variant receipt and envelope disagree")
        for kind, record, arguments in [
            ("generate", generated, ["generate-lockfile"]),
            ("metadata", measured, ["metadata", "--format-version", "1", "--locked", "--all-features"]),
        ]:
            require(record["argv"] == ["cargo", "+1.93.0", *arguments]
                    and record["exit_status"] == 0 and record["natural_exit"] is True
                    and record["stop_reason"] is None and record["cwd"] == str(source),
                    "unbound/incomplete initialization or full metadata command")
            require(datetime.fromisoformat(decision["recorded_at"])
                    < datetime.fromisoformat(record["started_at"]), "decision follows execution")
            for stream in ("stdout", "stderr"):
                contents = read(f"commands/{kind}-{variant}/{stream}", json_data=False)
                require(hashlib.sha256(contents).hexdigest() == record[stream + "_sha256"],
                        "original command stream changed")
            require(record["lock_after_sha256"] == pin["lock_sha256"], "command changed first-success lock")
        require(generated["lock_before_sha256"] is None
                and measured["lock_before_sha256"] == pin["lock_sha256"]
                and measured["environment"].get("CARGO_NET_OFFLINE") == "true",
                "metadata did not use the existing immutable offline lock")
        require(metadata_stderr_allowed(
            read(f"commands/metadata-{variant}/stderr", json_data=False),
            virtual=profile["virtual_workspace"]), "unexpected metadata diagnostic stream")
        expected_source = projection["sources"][variant]
        actual = tree_files(source)
        require(set(actual) == set(expected_source["files"]) | {"Cargo.lock"},
                "initialization added/removed original source files")
        for name, value in expected_source["files"].items():
            require(not actual[name].is_symlink() and file_hash(actual[name]) == value["sha256"],
                    "initialization altered original source")
            if "mode" in value:
                require(actual[name].stat().st_mode & 0o777 == value["mode"],
                        "initialization altered original source mode")
        require({name: row["sha256"] for name, row in expected_source["manifest_files"].items()}
                == pin["manifests"], "initialization changed original manifest set")
        graph = json.loads(read(f"commands/metadata-{variant}/stdout", json_data=False))
        graph_audits[variant] = graph_summary(
            graph, lock, source, pin["manifests"],
            local_packages=profile["local_packages"], virtual=profile["virtual_workspace"])
        require(graph_audits[variant]["packages"] == pin["packages"], "default full graph count changed")
        locks[variant] = {
            "path": str(root / f"first-success/{variant}/Cargo.lock"),
            "sha256": pin["lock_sha256"], "data": lock,
        }
        graphs[variant] = graph
        cache_roots.add(measured["environment"]["CARGO_HOME"])
        if profile["allow_git"]:
            require(first.get("historical_git_revision_recovery") is False,
                    "new Git selection was relabeled as historical")
            for selected in first["git_sources"]:
                source_id = selected["source"]
                require(source_id not in first_git or first_git[source_id] == selected,
                        "one Git source has contradictory first-success identities")
                require(source_id == "git+" + selected["url"] + "#" + selected["revision"]
                        and re.fullmatch(r"[0-9a-f]{40}", selected["revision"])
                        and selected["source_query"] is None, "unbound first-success Git object")
                first_git[source_id] = selected
    inventory = read("archive-checksums.json")
    require(inventory["missing"] == [] and isinstance(inventory["rows"], list),
            "prepared registry archive closure is incomplete")
    from .rust_vendor import locked_packages
    expected_packages = locked_packages([row["data"] for row in locks.values()])
    expected_registry = {key: value for key, value in expected_packages.items() if key[2] == CRATES_IO}
    expected_git = {key: value for key, value in expected_packages.items() if key[2].startswith("git+")}
    archives = {}
    for row in inventory["rows"]:
        key = row["name"], row["version"], row["package_identity"]["source"]
        require(key not in archives and expected_registry.get(key) == row["checksum"],
                "unexpected/duplicate archive source identity")
        require(row["retained"]["sha256"] == row["checksum"], "archive reference has another checksum")
        referenced(row["retained"], limit=256 * 1024**2)
        archives[key] = row["retained"]
    require(set(archives) == set(expected_registry), "missing archive from the full default registry union")
    require(len(cache_roots) == 1, "reference graphs use different private Cargo homes")
    cargo_cache = Path(next(iter(cache_roots))).resolve()
    git_packages = {}
    require(set(first_git) == {key[2] for key in expected_git}, "first-success Git source set differs from locks")
    for graph in graphs.values():
        for row in graph["packages"]:
            source_id = row.get("source") or ""
            if not source_id.startswith("git+"):
                continue
            key = row["name"], row["version"], source_id
            require(key in expected_git, "metadata has an unbound Git package")
            manifest = Path(row["manifest_path"]).resolve()
            require(manifest.is_file() and manifest.is_relative_to(cargo_cache / "git/checkouts"),
                    "Git metadata points outside the preserved private checkout")
            repository = Path(git(manifest.parent, "rev-parse", "--show-toplevel").decode().strip()).resolve()
            revision = git(repository, "rev-parse", "HEAD").decode().strip()
            require(repository.is_relative_to(cargo_cache / "git/checkouts")
                    and revision == source_id.rsplit("#", 1)[-1],
                    "preserved checkout differs from its first-success full revision")
            observed = {"manifest_path": str(manifest), "manifest_sha256": file_hash(manifest),
                        "repository": str(repository), "revision": revision, "source": source_id}
            require(key not in git_packages or git_packages[key] == observed,
                    "conflicting retained Git package paths")
            git_packages[key] = observed
    require(set(git_packages) == set(expected_git), "Git source closure is missing a locked package")
    return {
        "envelope": envelope, "root": root, "locks": locks, "archives": archives,
        "graph_audits": graph_audits, "receipt": receipt, "graphs": graphs,
        "profile": profile, "cargo_cache": cargo_cache, "git_packages": git_packages,
        "receipt_sha256": envelope["files"]["resolution-receipt.json"]["sha256"],
    }


def build_specification(ctx, task, plan, oracle, patch):
    original_inputs(ctx, task, plan, oracle, patch)
    profile = profile_for(task["id"])
    envelope_ref = ctx.config["migration"][OPTION][task["id"]]
    checked = prepared_inputs(envelope_ref)
    assets, defaults = {}, []
    variants = ["start"] if profile.get("shared_default") else list(profile["defaults"])
    for index, variant in enumerate(variants):
        pin = profile["defaults"][variant]
        original = checked["locks"][variant]
        asset = ctx.copy_asset(Path(original["path"]), "rust-first-new-lock")
        require(asset["sha256"] == original["sha256"], "copied first lock changed")
        assets[f"{index}/Cargo.lock"] = asset  # Asset labels, never candidate destination paths.
        defaults.append({
            "selector_package": pin["selector_package"], "asset_target": LOCK_PREFIX + str(index),
            "sha256": asset["sha256"],
        })
        if "selector_version" in pin:
            defaults[-1]["selector_version"] = pin["selector_version"]
    runtime = {
        "schema_version": RUNTIME_SCHEMA, "task_id": task["id"], "profile": profile["profile"],
        "toolchain": profile.get("toolchain", "1.93.0"),
        "cargo_sha256": checked.get("cargo_sha256", CARGO_SHA),
        "rustc_sha256": checked.get("rustc_sha256", RUSTC_SHA),
        "cpus": 4, "max_log_bytes": 32 * 1024**2, "metadata_timeout_seconds": 120,
        "receipt_sha256": checked["receipt_sha256"], "defaults": defaults,
    }
    if profile.get("command_timeout_seconds"):
        runtime["command_timeout_seconds"] = profile["command_timeout_seconds"]
    spec = {
        "schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": task["id"],
        "manifests": [], "locks": [], "private_members": [], "cpus": 4,
        MARKER: runtime, "first_new_resolution_inputs": envelope_ref,
    }
    atomic_json(ctx.output / "rust-first-resolution-admission.json", {
        "task_id": task["id"], "historical_restoration": False, "reference_graphs": checked["graph_audits"],
        "receipt_sha256": checked["receipt_sha256"], "manifest_rewrites": [],
        "candidate_lock_policy": "preserve existing; missing-only current local dependency selection",
        "new_resolver_calls": 0, "new_downloads": 0, "model_calls": 0,
    })
    return spec, assets


def vendor(ctx, spec, locks):
    """Use retained archives/Git objects; never resolve or download a source."""
    profile = profile_for(spec["task_id"])
    checked = prepared_inputs(spec["first_new_resolution_inputs"])
    variants = ["start"] if profile.get("shared_default") else list(profile["defaults"])
    for index, variant in enumerate(variants):
        require(file_hash(Path(locks[f"{index}/Cargo.lock"]["path"]))
                == checked["locks"][variant]["sha256"], "vendor input lock changed")
    from .rust_vendor import materialize, source_configuration
    metadata = {"packages": []}
    seen = {}
    for graph in checked["graphs"].values():
        for package in graph["packages"]:
            if not package.get("source"):
                continue
            key = package["name"], package["version"], package["source"]
            require(key not in seen or seen[key]["manifest_path"] == package["manifest_path"],
                    "source-aware reference metadata paths disagree")
            seen[key] = package
    metadata["packages"] = list(seen.values())
    with tempfile.TemporaryDirectory(prefix="new-lock-vendor-", dir=ctx.scratch) as temporary:
        source_aware = Path(temporary) / "source-aware"
        cargo_config, audit = materialize(
            [row["data"] for row in checked["locks"].values()], metadata,
            checked["cargo_cache"] if profile["allow_git"] else Path(temporary), source_aware,
            lambda name, version, checksum: Path(checked["archives"][(name, version, CRATES_IO)]["path"]),
        )
        roots = [p for p in source_aware.iterdir() if p.is_dir()]
        require(all(not row.get("omitted_vcs_metadata") for row in audit),
                "original registry archive files were omitted")
        config_asset = None
        if profile["allow_git"]:
            cargo_config = tomllib.loads(cargo_config)
            cargo_config["net"] = {"offline": True}
            config_file = ctx.output / "rust-first-resolution-cargo-sources.toml"
            config_file.write_text(tomli_w.dumps(cargo_config))
            config_asset = ctx.copy_asset(config_file, "rust-first-resolution-cargo-sources")
            _, source_roots = source_configuration({row["source"] for row in audit})
            spec[MARKER]["source_config_sha256"] = config_asset["sha256"]
            spec[MARKER]["vendor_sources"] = source_roots
            result = ctx.bundle(source_aware)
        else:
            require(len(roots) == 1, "registry-only union has multiple source roots")
            # Preserve the existing Prometheus flat source layout and command.
            result = ctx.bundle(roots[0])
        atomic_json(ctx.output / "rust-first-resolution-vendor.json", {
            "task_id": spec["task_id"], "packages": len(audit), "files": sum(row["files"] for row in audit),
            "source_audit": audit, "new_downloads": 0, "new_resolver_calls": 0,
            "asset": result, "source_config": config_asset,
        })
        return result, config_asset


def binding(ctx, spec, execute, public):
    from . import rust_first_resolution_entry as entry
    runtime = spec[MARKER]
    profile = profile_for(spec["task_id"])
    entry.validate(runtime)
    root = ctx.output / "rust-first-resolution-tools"
    shutil.copytree(ctx.output / "rust-build-tools", root, dirs_exist_ok=True)
    helper_names = ["rust_first_resolution_entry.py", "rust_test_compat_entry.py", "rust_api_entry.py"]
    if profile.get("repository_lib_tests"):
        helper_names += ["rust_libsbp_entry.py", "rust_wash_compat_entry.py"]
    for name in helper_names:
        shutil.copyfile(Path(__file__).with_name(name), root / name)
    runtime_path = ctx.output / "rust-first-resolution-spec.json"
    private_path = ctx.output / "rust-first-resolution-private.json"
    atomic_json(runtime_path, runtime)
    private_spec = {
        "schema_version": PRIVATE_SCHEMA, "task_id": spec["task_id"], "command": profile["command"],
        "harness": (None if profile.get("repository_lib_tests") else
                    {"path": profile["harness"], "sha256": profile["harness_sha256"]}),
        "expected_tests": profile["expected"],
    }
    if profile.get("repository_lib_tests"):
        private_spec["repository_test_blocks"] = profile["test_blocks"]
    atomic_json(private_path, private_spec)
    assets = [
        {**ctx.bundle(root, role="toolchain"), "kind": "archive",
         "visibility": "developer", "target": TOOLS},
        {**ctx.copy_asset(runtime_path, "rust-first-resolution-spec"), "kind": "file",
         "visibility": "developer", "target": SPEC},
        {**ctx.copy_asset(private_path, "rust-first-resolution-private"), "kind": "file",
         "visibility": "grader", "target": PRIVATE},
    ]

    def wrap(command, *, private=False):
        prefix = ["python3", TOOLS + "/rust_first_resolution_entry.py", "--spec", SPEC, "--cwd", "."]
        if private:
            prefix += ["--private-spec", PRIVATE]
        return shlex.join([*prefix, "--", *command])

    return wrap(execute, private=True), wrap(public), assets
