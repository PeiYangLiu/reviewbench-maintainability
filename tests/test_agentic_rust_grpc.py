"""Original Cargo builds/tests; a distinct metadata reader retains the whole graph."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_grpc as host, rust_grpc_entry as entry, rust_lane
from test_agentic_rust_image import output, ref
from reviewbench_maintainability.agentic.contracts import ContractError


def fixture(tmp_path):
    source = tmp_path / "source"
    files = {
        "Cargo.toml": '[workspace]\nmembers=["interop","grpc-sys","other"]\n',
        "interop/Cargo.toml": '[package]\nname="interop"\nversion="0.1.0"\nedition="2018"\n',
        "interop/src/lib.rs": "pub fn value() -> u32 { 7 }\n",
        "interop/tests/tests.rs": "#[test] fn original_rpc() { assert_eq!(interop::value(), 7); }\n",
        "grpc-sys/Cargo.toml": '[package]\nname="grpcio-sys"\nversion="0.1.0"\n',
        "grpc-sys/src/lib.rs": "pub fn native() {}\n",
        "grpc-sys/grpc/CMakeLists.txt": "# complete pinned native root\n",
        "grpc-sys/grpc/third_party/retained/header.h": "#define ORIGINAL 7\n",
        "other/Cargo.toml": '[package]\nname="other"\nversion="0.1.0"\n',
        "other/src/lib.rs": "pub fn retained_member() {}\n",
        "other/tests/tests.rs": "#[test] fn unrelated_same_target_name() {}\n",
        "README.md": "Full candidate source, not only the tested package.\n",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for name in ("original/cargo", "original/rustc", "metadata/cargo", "sources.toml"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("fixed " + name)
    lock = tmp_path / "Cargo.lock"
    lock.write_text('version = 3\n' + "".join(
        '[[package]]\nname="' + name + '"\nversion="0.1.0"\n' for name in ("interop", "grpcio-sys", "other"))
        + '[[package]]\nname="inactive"\nversion="0.4.3"\nsource="' + entry.core.CRATES_IO + '"\nchecksum="' + "a" * 64 + '"\n')
    native = entry.core.snapshot(source / "grpc-sys/grpc")
    public = {
        "schema_version": entry.SCHEMA, "task_id": "fixture", "package": "interop",
        "package_manifest": "interop/Cargo.toml", "metadata_reader_role": "complete-locked-graph-only",
        "cargo": ref(tmp_path / "original/cargo"), "rustc": ref(tmp_path / "original/rustc"),
        "metadata_cargo": ref(tmp_path / "metadata/cargo"), "source_config": ref(tmp_path / "sources.toml"),
        "lock": ref(lock), "public_command": ["check", "--locked", "-p", "interop", "--message-format=json"],
        "native": {"path": "grpc-sys/grpc", "commit": "a" * 40, "files": len(native),
                   "snapshot_sha256": entry.digest_object(native)},
        "cpus": 16, "timeout_seconds": 3600, "metadata_timeout_seconds": 180, "max_log_bytes": 1024**2,
    }
    private = {"task_id": "fixture", "command": host.COMMAND[2:], "harness": "interop/tests/tests.rs",
               "files": {"interop/tests/tests.rs": ref(source / "interop/tests/tests.rs")["sha256"]},
               "expected_tests": ["original_rpc"]}
    return source, public, private


def runner(public, calls, *, fail=False, metadata_fault=None, test_stdout=None, mutation=None):
    def run(command, cwd, env, folder, label, deadline, limit):
        calls.append(list(command))
        assert env["RUSTC"] == public["rustc"]["path"] and env["CARGO_BUILD_JOBS"] == "16"
        assert env["CARGO_NET_OFFLINE"] == "true"
        if "metadata" in command:
            assert command == [public["metadata_cargo"]["path"], "--offline", "metadata", "--locked",
                               "--format-version", "1", "--all-features"]
            lock = entry.tomllib.loads((cwd / "Cargo.lock").read_text())
            packages = []
            for p in lock["package"]:
                row = {**p, "id": p["name"] + "@" + p["version"], "targets": []}
                if not p.get("source"):
                    directory = "grpc-sys" if p["name"] == "grpcio-sys" else p["name"]
                    row["manifest_path"] = str(cwd / directory / "Cargo.toml")
                    if directory in ("interop", "other"):
                        row["targets"] = [{"name": "tests", "kind": ["test"],
                                           "src_path": str(cwd / directory / "tests/tests.rs")}]
                packages.append(row)
            graph = {"workspace_root": str(cwd), "packages": packages,
                     "workspace_members": [p["id"] for p in packages if not p.get("source")],
                     "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}
            if metadata_fault == "omit-inactive":
                graph["packages"].pop()
                graph["resolve"]["nodes"].pop()
            elif metadata_fault == "omit-workspace":
                graph["packages"] = [p for p in packages if p["name"] != "other"]
            elif metadata_fault == "redirect-test":
                graph["packages"][0]["targets"][0]["src_path"] = str(cwd / "other/tests/tests.rs")
            if mutation:
                mutation(cwd)
            return output(command, cwd, json.dumps(graph))
        assert command[0] == public["cargo"]["path"]
        assert (cwd / "other/src/lib.rs").read_text() == "pub fn retained_member() {}\n"
        if "test" in command:
            assert command[1:] == host.COMMAND[2:]  # No extra locked/offline/test flags.
            text = test_stdout if test_stdout is not None else (
                "test original_rpc ... " + ("FAILED" if fail else "ok") +
                "\ntest result: " + ("FAILED." if fail else "ok.") + "\n")
            return output(command, cwd, text, code=101 if fail else 0)
        assert command[1:] == ["--offline", *public["public_command"]]
        return output(command, cwd, code=101 if fail else 0)
    return run


@pytest.mark.parametrize("private_run", [False, True])
def test_full_graph_reader_does_not_replace_original_build_compiler(tmp_path, private_run):
    source, public, private = fixture(tmp_path)
    before = entry.core.snapshot(source)
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "cache", private if private_run else None,
                                 run=runner(public, calls))
    assert result["exit_status"] == 0 and result["source_unchanged"]
    assert result["graph"]["packages"] == result["graph"]["nodes"] == 4
    assert result["graph"]["workspace_members"] == 3
    assert result["original_test_cargo"] == public["cargo"]
    assert result["metadata_reader"] == public["metadata_cargo"]
    assert entry.core.snapshot(source) == before and not (source / "Cargo.lock").exists()
    assert len(calls) == 2
    assert result["test_results"] == ({"original_rpc": True} if private_run else {})


def test_same_test_target_name_in_another_package_is_not_ambiguous(tmp_path):
    source, public, private = fixture(tmp_path)
    result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(public, []))
    assert result["test_results"] == {"original_rpc": True}


@pytest.mark.parametrize("fault", ["omit-inactive", "omit-workspace", "redirect-test"])
def test_incomplete_graphs_or_redirected_original_targets_stop_before_tests(tmp_path, fault):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "cache", private,
                            run=runner(public, calls, metadata_fault=fault))
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["bytes", "missing", "extra", "mode"])
def test_every_recursive_native_input_is_attested(tmp_path, change):
    source, public, private = fixture(tmp_path)
    path = source / "grpc-sys/grpc/third_party/retained/header.h"
    if change == "bytes":
        path.write_text("changed\n")
    elif change == "missing":
        path.unlink()
    elif change == "extra":
        (path.parent / "extra.h").write_text("extra\n")
    else:
        path.chmod(0o755)
    calls = []
    with pytest.raises(entry.core.VariantError, match="native source"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(public, calls))
    assert not calls


@pytest.mark.parametrize("key", ["cargo", "rustc", "metadata_cargo", "source_config", "lock"])
def test_fixed_inputs_are_checked_before_invoking_any_command(tmp_path, key):
    source, public, private = fixture(tmp_path)
    Path(public[key]["path"]).write_text("changed input")
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(public, calls))
    assert not calls


def test_candidate_owned_lock_and_all_source_edits_are_retained(tmp_path):
    source, public, private = fixture(tmp_path)
    lock = Path(public["lock"]["path"]).read_bytes() + b"\n# candidate owned\n"
    (source / "Cargo.lock").write_bytes(lock)
    (source / "interop/src/lib.rs").write_text("pub fn value() -> u32 { 8 }\n")
    result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(public, [], fail=True))
    assert result["exit_status"] == 1 and result["candidate_lock_preserved"]
    assert result["test_results"] == {"original_rpc": False}
    assert (source / "Cargo.lock").read_bytes() == (tmp_path / "cache/source/Cargo.lock").read_bytes() == lock
    assert (tmp_path / "cache/source/interop/src/lib.rs").read_text() == "pub fn value() -> u32 { 8 }\n"


@pytest.mark.parametrize("text", [
    "test result: ok.\n",
    "test original_rpc ... ignored\ntest result: ok.\n",
    "test original_rpc ... ok\ntest original_rpc ... ok\ntest result: ok.\n",
])
def test_missing_ignored_or_duplicate_original_tests_are_not_success(tmp_path, text):
    source, public, private = fixture(tmp_path)
    result = entry.run_candidate(public, source, tmp_path / "cache", private,
                                 run=runner(public, [], test_stdout=text))
    assert result["exit_status"] == 1 and result["test_results"] == {"original_rpc": False}


def test_command_changing_candidate_or_lock_is_retained_as_rejection(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="changed source or lock"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(
            public, calls, mutation=lambda p: (p / "Cargo.lock").write_text("altered by command")))
    assert len(calls) == 1


def test_large_binaries_are_not_read_using_the_metadata_size_bound(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path)
    binaries = {Path(public[k]["path"]) for k in ("cargo", "rustc", "metadata_cargo")}
    bounded = entry.core.bounded
    def metadata_only(path):
        assert Path(path) not in binaries
        return bounded(path)
    monkeypatch.setattr(entry.core, "bounded", metadata_only)
    assert entry.run_candidate(public, source, tmp_path / "cache", private,
                               run=runner(public, []))["exit_status"] == 0


def test_completed_command_output_is_required(tmp_path):
    source, public, private = fixture(tmp_path)
    wrapped = runner(public, [])
    def incomplete(*args):
        row = wrapped(*args)
        row["stdout_total_bytes"] += 1
        return row
    with pytest.raises(entry.core.VariantError, match="evidence is incomplete"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=incomplete)


def test_profile_and_preparation_scratch_do_not_change_other_lanes():
    ctx = SimpleNamespace(config={})
    assert not host.requested(ctx, {"id": host.ID})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.ID}) == 12 * 1024**3
    ctx.config = {"migration": {host.OPTION: {host.ID: {"path": "/bundle", "sha256": "a" * 64}}}}
    assert host.requested(ctx, {"id": host.ID})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.ID}) == 2 * 1024**3
    assert rust_lane.migration_scratch_bytes(ctx, {"id": "other"}) == 12 * 1024**3
    ctx.config["migration"][host.OPTION]["other"] = {}
    with pytest.raises(ContractError, match="unknown"):
        host.requested(ctx, {"id": host.ID})


@pytest.mark.parametrize("change", ["metadata-role", "command", "cpu"])
def test_original_commands_and_resource_bounds_are_explicit(tmp_path, change):
    _, public, private = fixture(tmp_path)
    if change == "metadata-role":
        public["metadata_reader_role"] = "build-and-test"
    elif change == "command":
        private["command"] = ["test", "--locked", *private["command"][1:]]
    else:
        public["cpus"] = 17
    with pytest.raises(entry.core.VariantError):
        entry.validate(public, private)
