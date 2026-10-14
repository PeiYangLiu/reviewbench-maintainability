"""Read Zerocopy's single pre-SUT NEW lock without resolving or changing source."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat

from reviewbench_maintainability.agentic.contracts import file_hash, repo_path
from reviewbench_maintainability.agentic.snapshots import tree_files

TASK = "github__google__zerocopy__pull_1752__comment_1777397723__complexity"
LOCK_SHA = "0045265663ddfc81b32e87acab52a19676f40fb56d4d18a624c7eabd5693ce8b"
DECISION_SHA = "6aac36f58cf166705daba7a050ec12fc8e282efa24d584b706675c8e2e837427"
CARGO_SHA = "0e654dccd3501e5feb68aa570e5f780d5f7921c820e558ac5d3ef60016b5c0a7"
RUSTC_SHA = "91f9ba0819d622cbb0f4f3298580d939173eb3258d95f7755b301a394eeb2ae0"
GENERATE = ["cargo", "+1.81.0", "generate-lockfile"]
METADATA = ["cargo", "+1.81.0", "metadata", "--format-version", "1", "--locked", "--all-features"]


def verify_original_tree(source, expected):
    """Preserve all original files and literal links, not just selected manifests."""
    from .rust_first_resolution import require
    source = Path(source).resolve()
    actual = tree_files(source)
    require(set(actual) == set(expected) | {"Cargo.lock"}, "Zerocopy complete original source set changed")
    links = 0
    for name, row in expected.items():
        path = actual[name]
        mode = path.lstat().st_mode
        if row["kind"] == "symlink":
            links += 1
            require(stat.S_ISLNK(mode) and os.readlink(path) == row["link_target"]
                    and path.resolve(strict=True).is_relative_to(source),
                    "Zerocopy original literal link changed or escapes complete source")
            data = os.fsencode(os.readlink(path))
        else:
            require(row["kind"] == "file" and stat.S_ISREG(mode),
                    "Zerocopy original file kind changed")
            data = path.read_bytes()
        require(len(data) == row["bytes"] and hashlib.sha256(data).hexdigest() == row["sha256"]
                and stat.S_IMODE(mode) == row["mode"]
                and hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == row["git_oid"],
                "Zerocopy original blob/mode changed")
    require(len(expected) == 552 and links == 155, "Zerocopy original full tree was pruned")
    lock = actual["Cargo.lock"]
    require(not lock.is_symlink() and file_hash(lock) == LOCK_SHA, "Zerocopy shared new root lock changed")


def prepared_shared_first_lock(envelope, profile):
    from .rust_first_resolution import (
        BINDINGS, CRATES_IO, graph_summary, reference_packages, referenced, require,
    )
    from .rust_vendor import locked_packages
    require(envelope["task_id"] == profile["task_id"] == TASK
            and profile["toolchain"] == "1.81.0" and profile["shared_default"] is True
            and profile["allow_git"] is False, "unknown Zerocopy shared-lock profile")
    root = Path(envelope["root"])
    require(root.is_absolute() and root.is_dir(), "Zerocopy prepared root is absent")

    def read(name, *, json_data=True):
        name = repo_path(name)
        require(name in envelope["files"], "unsealed Zerocopy prepared input: " + name)
        return referenced({"path": str(root / name), **envelope["files"][name]}, json_data=json_data)

    require(envelope["files"]["first-new-lock-decision.json"]["sha256"] == DECISION_SHA,
            "Zerocopy recorded first-new decision changed")
    decision = read("first-new-lock-decision.json")
    projection = read("original-source-projection.json")
    receipt = read("resolution-receipt.json")
    require(decision["task_id"] == projection["task_id"] == receipt["task_id"] == TASK
            and decision["first_new_command"] == GENERATE and decision["metadata_command"] == METADATA
            and decision["original_prepare"] is None and decision["historical_lock_restoration"] is False
            and decision["first_new_successes_max"] == decision["attempts_this_assignment"] == 1
            and decision["metadata_offline"] is True and decision["max_metadata_calls"] == 2
            and decision["no_repeat_generate_update_after_success"] is True,
            "Zerocopy initialization policy changed")
    require(decision["projection"]["sha256"] == envelope["files"]["original-source-projection.json"]["sha256"]
            and projection["plan_sha256"] == receipt["plan_sha256"] == BINDINGS[TASK]["plan_sha256"]
            and projection["original_golden"]["sha256"] == profile["golden"]
            and projection["complete_manifest_trees_equal"] is True
            and profile["defaults"]["start"]["manifests"] == profile["defaults"]["golden"]["manifests"],
            "Zerocopy shared new lock lost its original manifest/plan bindings")
    require(receipt["schema_version"] == "reviewbench-zerocopy-first-new-prepared/v1"
            and receipt["status"] == "lock_and_full_offline_metadata_prepared_not_SUT_calibrated"
            and receipt["first_new_environment"] is True and receipt["historical_lock_restoration"] is False
            and receipt["first_generate_calls"] == receipt["successful_generate_calls"] == 1
            and receipt["metadata_calls"] == 2
            and receipt["generate_after_success"] == receipt["update_calls"] == receipt["source_SUT_public_fixture_model_calls"] == 0
            and receipt["dependency_versions_changed_after_freeze"] is False
            and receipt["nested_tools_lock_resolved"] is False,
            "Zerocopy producer did not finish the exact pre-SUT preparation")
    for key, filename in (
        ("decision", "first-new-lock-decision.json"), ("projection", "original-source-projection.json"),
        ("metadata_equal", "metadata-equivalence.json"), ("archives", "archive-checksums.json"),
    ):
        require(receipt[key]["sha256"] == envelope["files"][filename]["sha256"],
                "Zerocopy receipt refers to another prepared input")
    for binary, checksum in (("cargo", CARGO_SHA), ("rustc", RUSTC_SHA)):
        require(decision["tools"][binary]["sha256"] == checksum
                and file_hash(Path(decision["tools"][binary]["path"])) == checksum,
                "Zerocopy original compiler bytes changed")
    for phase in ("resolve", "metadata"):
        terminal = read(f"runs/{phase}/result.json")
        container = read(f"runs/{phase}/container-terminal-inspect.json")
        require(terminal["exit_status"] == 0 and terminal["outer_timeout"] is False
                and terminal["private_cache_preserved"] is True,
                "Zerocopy preparation has no natural terminal/cache result")
        require(isinstance(container, list) and len(container) == 1
                and container[0]["State"]["ExitCode"] == 0 and container[0]["State"]["OOMKilled"] is False
                and container[0]["HostConfig"]["NetworkMode"] == ("bridge" if phase == "resolve" else "none"),
                "Zerocopy preparation environment or metadata network changed")
    data = read("first-success/Cargo.lock", json_data=False)
    require(hashlib.sha256(data).hexdigest() == LOCK_SHA
            and receipt["lock"]["sha256"] == LOCK_SHA,
            "Zerocopy first successful default changed")
    lock = reference_packages(data)
    require(len(lock["package"]) == 61, "Zerocopy full root lock graph changed")
    first = read("first-success/receipt.json")
    generated = read("commands/generate-start/result.json")
    require(first["origin"] == "first-successful-explicit-NEW-environment-generate-lockfile"
            and first["historical_lock_restoration"] is False and first["original_prepare"] is None
            and first["generate_invocations"] == first["successful_generate_invocations"] == 1
            and first["selection_uses_tests"] is False and first["lock"]["sha256"] == LOCK_SHA
            and first["decision"]["sha256"] == DECISION_SHA
            and first["command"]["sha256"] == envelope["files"]["commands/generate-start/result.json"]["sha256"],
            "Zerocopy new default was not frozen at its first natural success")
    require(generated["source_before"]["root_lock"] is None,
            "Zerocopy initial generate replaced an existing lock")
    require({p.name for p in (root / "commands").iterdir()} == {
        "generate-start", "metadata-start", "metadata-golden"},
        "Zerocopy initialization command set changed")

    def command(label, row, argv, source):
        require(row["argv"] == argv and row["cwd"] == str(source)
                and row["exit_status"] == 0 and row["natural_exit"] is True
                and row["stop_reason"] is None and row["root_lock"]["sha256"] == LOCK_SHA,
                "Zerocopy recorded Cargo command/result changed")
        require(datetime.fromisoformat(decision["recorded_at"])
                < datetime.fromisoformat(row["started_at"])
                <= datetime.fromisoformat(row["completed_at"]),
                "Zerocopy command did not follow the recorded decision")
        for stream in ("stdout", "stderr"):
            contents = read(f"commands/{label}/{stream}", json_data=False)
            require(hashlib.sha256(contents).hexdigest() == row[stream + "_ref"]["sha256"]
                    and len(contents) == row[stream + "_ref"]["bytes"],
                    "Zerocopy actual Cargo output changed")

    command("generate-start", generated, GENERATE, root / "work/start")
    require(datetime.fromisoformat(generated["completed_at"]) <= datetime.fromisoformat(first["recorded_at"]),
            "Zerocopy lock freeze precedes successful generation")
    metadata_result = read("metadata-result.json")
    require(metadata_result["ready"] is True and not metadata_result["failures"]
            and metadata_result["metadata_calls"] == 2 and metadata_result["generate_calls_this_phase"] == 0
            and metadata_result["SUT_public_fixture_model_calls"] == 0
            and metadata_result["lock_unchanged"] is True, "Zerocopy offline metadata incomplete")
    locks, graphs, audits = {}, {}, {}
    for variant in ("start", "golden"):
        pin = profile["defaults"][variant]
        require(pin["lock_sha256"] == LOCK_SHA and pin["packages"] == 61
                and pin["manifests"] == projection["sources"][variant]["manifest_files"],
                "Zerocopy original manifest/default binding changed")
        row = read(f"commands/metadata-{variant}/result.json")
        source = root / "work" / variant
        command("metadata-" + variant, row, METADATA, source)
        require(row["environment"]["CARGO_NET_OFFLINE"] == "true" and row["network_requests"] == 0
                and row["source_before"]["root_lock"]["sha256"] == LOCK_SHA
                and datetime.fromisoformat(first["recorded_at"]) < datetime.fromisoformat(row["started_at"]),
                "Zerocopy metadata did not preserve the first frozen offline lock")
        require(read(f"commands/metadata-{variant}/stderr", json_data=False) == b"",
                "Zerocopy metadata had an unexpected diagnostic")
        verify_original_tree(source, projection["sources"][variant]["files"])
        graph = json.loads(read(f"commands/metadata-{variant}/stdout", json_data=False))
        audits[variant] = graph_summary(graph, lock, source, pin["manifests"], local_packages=3, virtual=False)
        require(audits[variant]["packages"] == 61, "Zerocopy metadata omitted part of the root graph")
        graphs[variant] = graph
        locks[variant] = {"path": str(root / "first-success/Cargo.lock"), "sha256": LOCK_SHA, "data": lock}
    require(audits["start"]["semantic_graph_sha256"] == audits["golden"]["semantic_graph_sha256"],
            "Zerocopy identical manifests produced different prepared dependency graphs")
    inventory = read("archive-checksums.json")
    require(inventory["missing"] == [] and inventory["registry_packages"] == 58,
            "Zerocopy exact archive closure is incomplete")
    expected = locked_packages([lock])
    require(len(expected) == 58 and all(key[2] == CRATES_IO for key in expected),
            "Zerocopy root lock has an unplanned external source")
    archives = {}
    for row in inventory["rows"]:
        key = row["name"], row["version"], row["source"]
        require(key not in archives and expected.get(key) == row["checksum"]
                and row["retained"]["sha256"] == row["checksum"], "Zerocopy archive identity changed")
        referenced(row["retained"], limit=256 * 1024**2)
        archives[key] = row["retained"]
    require(set(archives) == set(expected), "Zerocopy archive closure was pruned")
    return {
        "envelope": envelope, "root": root, "profile": profile, "locks": locks,
        "archives": archives, "graphs": graphs, "graph_audits": audits, "receipt": receipt,
        "cargo_cache": root / "cargo-home", "git_packages": {},
        "cargo_sha256": CARGO_SHA, "rustc_sha256": RUSTC_SHA,
        "receipt_sha256": envelope["files"]["resolution-receipt.json"]["sha256"],
    }
