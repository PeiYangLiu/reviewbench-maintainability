import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_api
from agentic_runtime_migration import rust_api_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def inputs(tmp_path):
    files = [
        {"path": "tests/oracle_start.rs", "content": "frozen original-API assertion"},
        {"path": "tests/oracle_golden.rs", "content": "frozen new-API assertion"},
    ]
    commands = {v: f"cargo +1.85.0 test --test oracle_{v} -- --nocapture --test-threads=1"
                for v in ("start", "golden")}
    plan = {"toolchain": "1.85.0", "command": commands["start"],
            "golden_command": commands["golden"], "session_files": files}
    evidence = [{"path": f["path"], "sha256": sha(f["content"].encode())} for f in files]
    oracle = {"environment": {"variant_commands": commands, "variant_toolchains": {
        v: "1.85.0" for v in commands}}, "session_files": {v: copy.deepcopy(evidence) for v in commands},
        "pass_to_pass_tests": ["works"]}
    ctx = SimpleNamespace(output=tmp_path / "output")
    ctx.output.mkdir()
    return SimpleNamespace(**locals())


def test_only_original_harness_target_is_a_permitted_cargo_difference(inputs):
    f = inputs
    before = copy.deepcopy((f.plan, f.oracle))
    spec = rust_api.specification(f.ctx, {"id": "fixture"}, f.plan, f.oracle)
    assert [form["target"] for form in spec["forms"]] == ["oracle_start", "oracle_golden"]
    assert spec["expected_tests"] == ["works"]
    assert spec["selection"] == "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results"
    assert (f.plan, f.oracle) == before


def test_existing_nonvariant_and_null_override_plans_keep_their_original_route(inputs):
    f = inputs
    f.plan.pop("golden_command")
    assert rust_api.specification(f.ctx, {"id": "fixture"}, f.plan, f.oracle) is None
    f.plan["golden_command"] = None
    assert rust_api.specification(f.ctx, {"id": "fixture"}, f.plan, f.oracle) is None


@pytest.mark.parametrize("mutation", ["toolchain", "features", "target", "identity", "source", "variant-source", "evidence"])
def test_unbound_or_semantically_different_alternatives_are_not_silently_selected(inputs, mutation):
    f = inputs
    if mutation == "toolchain":
        f.oracle["environment"]["variant_toolchains"]["golden"] = "1.86.0"
    elif mutation == "features":
        f.plan["golden_command"] += " other-filter"
        f.oracle["environment"]["variant_commands"]["golden"] = f.plan["golden_command"]
    elif mutation == "target":
        f.plan["golden_command"] = f.plan["golden_command"].replace("oracle_golden", "missing")
        f.oracle["environment"]["variant_commands"]["golden"] = f.plan["golden_command"]
    elif mutation == "identity":
        f.oracle["pass_to_pass_tests"] *= 2
    elif mutation == "source":
        f.oracle["session_files"]["golden"][0]["sha256"] = "f" * 64
    elif mutation == "variant-source":
        f.plan["session_files"][0]["golden_content"] = "different source"
    else:
        f.oracle["environment"]["variant_commands"].pop("golden")
    with pytest.raises(ContractError):
        rust_api.specification(f.ctx, {"id": "fixture"}, f.plan, f.oracle)


def runtime_spec(root):
    (root / "tests").mkdir(parents=True)
    forms, files = [], []
    for name in ("oracle_start", "oracle_golden"):
        path = "tests/" + name + ".rs"
        (root / path).write_text("frozen " + name)
        files.append({"path": path, "sha256": sha((root / path).read_bytes())})
        forms.append({"name": name, "target": name, "harness": path,
                      "command": ["cargo", "--offline", "test", "--test", name, "--", "--nocapture"]})
    return {"schema_version": entry.SCHEMA, "task_id": "fixture", "workdir": ".", "forms": forms,
            "files": files, "expected_tests": ["works"], "max_log_bytes": 1024 * 1024,
            "timeout_seconds": 60, "cpus": 1,
            "selection": "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results"}


def record(code=0, stdout="", stderr="", limit=None):
    return {"returncode": code, "stdout": stdout, "stderr": stderr, "limit": limit}


def mismatch(root, form, *, primary=None, kind="test", abort=True):
    path = root / "tests" / (form + ".rs")
    data = {"reason": "compiler-message", "manifest_path": str(root / "Cargo.toml"),
            "target": {"kind": [kind], "name": form, "src_path": str(path)},
            "message": {"level": "error", "message": "no such item", "code": {"code": "E0425"},
                        "spans": [{"is_primary": True, "file_name": str(primary or path)}]}}
    rows = [data]
    if abort:
        summary = copy.deepcopy(data)
        summary["message"] = {"level": "error", "message": "aborting due to 1 previous error",
                              "code": None, "spans": []}
        rows.append(summary)
    return record(101, "\n".join(json.dumps(value) for value in rows))


def test_old_cargo_numberless_abort_summary_does_not_hide_the_real_private_api_error(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    runtime_spec(root)
    monkeypatch.chdir(root)
    value = mismatch(root, "oracle_golden")
    messages = [json.loads(line) for line in value["stdout"].splitlines()]
    messages[-1]["message"]["message"] = "aborting due to previous error"
    value["stdout"] = "\n".join(json.dumps(message) for message in messages)
    assert entry.private_api_mismatch(value, "tests/oracle_golden.rs", root)
    value["stdout"] = json.dumps(messages[-1])
    assert not entry.private_api_mismatch(value, "tests/oracle_golden.rs", root)


@pytest.mark.parametrize("selected,stage", [("oracle_start", "golden"), ("oracle_golden", "start")])
def test_selection_uses_current_candidate_compilation_not_reference_stage(tmp_path, monkeypatch, selected, stage):
    root = tmp_path / "candidate"
    spec = runtime_spec(root)
    monkeypatch.chdir(root)
    monkeypatch.setenv("ORACLE_VARIANT", stage)
    calls = []
    def run(command, *args):
        target = command[command.index("--test") + 1]
        calls.append(("probe" if "--no-run" in command else "test", target))
        if "--no-run" in command:
            return record() if target == selected else mismatch(root, target)
        return record(stdout="test works ... ok\ntest result: ok. 1 passed; 0 failed\n")
    result = entry.select_and_test(spec, root, tmp_path, run=run)
    assert result["selected"] == [selected] and result["results"] == {"works": True}
    assert calls == [("probe", "oracle_start"), ("probe", "oracle_golden"), ("test", selected)]


def test_failed_assertion_never_selects_a_more_favorable_harness(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    spec = runtime_spec(root)
    monkeypatch.chdir(root)
    calls = []
    def run(command, *args):
        target = command[command.index("--test") + 1]
        calls.append(("probe" if "--no-run" in command else "test", target))
        if "--no-run" in command:
            return record() if target == "oracle_start" else mismatch(root, target)
        return record(101, "test works ... FAILED\ntest result: FAILED. 0 passed; 1 failed\n")
    result = entry.select_and_test(spec, root, tmp_path, run=run)
    assert result["results"] == {"works": False}
    assert calls[-1] == ("test", "oracle_start") and len(calls) == 3


@pytest.mark.parametrize("second_passes", [False, True])
def test_both_compatible_forms_are_run_and_all_assertions_must_pass(tmp_path, monkeypatch, second_passes):
    root = tmp_path / "candidate"
    spec = runtime_spec(root)
    monkeypatch.chdir(root)
    calls = []
    def run(command, *args):
        target = command[command.index("--test") + 1]
        calls.append(("probe" if "--no-run" in command else "test", target))
        if "--no-run" in command:
            return record()
        passed = target == "oracle_start" or second_passes
        return record(0 if passed else 101, "test works ... " + ("ok" if passed else "FAILED") +
                      "\ntest result: " + ("ok." if passed else "FAILED.") + "\n")
    result = entry.select_and_test(spec, root, tmp_path, run=run)
    assert calls == [("probe", "oracle_start"), ("probe", "oracle_golden"),
                     ("test", "oracle_start"), ("test", "oracle_golden")]
    assert result["results"] == {"works": second_passes}


@pytest.mark.parametrize("failure", ["production", "killed", "deadline", "output-limit", "missing-diagnostics"])
def test_non_harness_and_resource_errors_do_not_trigger_api_fallback(tmp_path, monkeypatch, failure):
    root = tmp_path / "candidate"
    spec = runtime_spec(root)
    monkeypatch.chdir(root)
    data = {
        "production": mismatch(root, "oracle_start", primary=root / "src/lib.rs", kind="lib"),
        "killed": record(-9),
        "deadline": record(-15, limit="deadline"),
        "output-limit": record(-15, limit="output_limit"),
        "missing-diagnostics": record(101, stderr="missing dependency"),
    }[failure]
    calls = []
    def run(*args):
        calls.append(args)
        return data
    with pytest.raises(entry.SelectionError):
        entry.select_and_test(spec, root, tmp_path, run=run)
    assert len(calls) == 1


@pytest.mark.parametrize("text", ["", "test works ... ignored\ntest result: ok.\n",
                                 "test works ... ok\ntest works ... ok\ntest result: ok.\n",
                                 "test unrelated ... ok\ntest result: ok.\n"])
def test_skipped_missing_or_duplicate_fixed_ids_are_not_success(text):
    assert entry.test_results(record(stdout=text), ["works"]) == {"works": False}


def test_altered_or_redirected_frozen_harness_is_rejected_before_compilation(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    spec = runtime_spec(root)
    monkeypatch.chdir(root)
    path = root / spec["files"][0]["path"]
    path.write_text("changed assertion")
    with pytest.raises(entry.SelectionError, match="bytes changed"):
        entry.select_and_test(spec, root, tmp_path, run=lambda *args: pytest.fail("compiler invoked"))
    outside = tmp_path / "outside.rs"
    outside.write_text("frozen oracle_start")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(entry.SelectionError, match="path changed"):
        entry.select_and_test(spec, root, tmp_path, run=lambda *args: pytest.fail("compiler invoked"))


@pytest.mark.parametrize("api,expected_forms,passes", [
    ("old", ["oracle_start"], True), ("new", ["oracle_golden"], True),
    ("both", ["oracle_start", "oracle_golden"], True), ("broken", ["oracle_start"], False),
])
def test_real_offline_cargo_selects_and_tests_current_multifile_candidate(tmp_path, api, expected_forms, passes):
    cargo = Path(os.environ.get("REVIEWBENCH_TEST_CARGO", ""))
    if not cargo.is_file():
        pytest.skip("pinned offline Cargo fixture not configured")
    root = tmp_path / "candidate"
    spec = runtime_spec(root)
    (root / "src").mkdir()
    (root / "Cargo.toml").write_text('[package]\nname="adapter_fixture"\nversion="0.0.0"\nedition="2021"\n')
    names = ["old_api"] if api in {"old", "broken"} else ["new_api"] if api == "new" else ["old_api", "new_api"]
    (root / "src/helper.rs").write_text("pub fn value() -> u32 { " + ("7" if api == "broken" else "42") + " }\n")
    (root / "src/lib.rs").write_text("mod helper;\n" + "\n".join(
        "pub fn " + name + "() -> u32 { helper::value() }" for name in names))
    for form, name in zip(spec["forms"], ("old_api", "new_api")):
        path = root / form["harness"]
        path.write_text("#[test]\nfn works() { assert_eq!(adapter_fixture::" + name + "(), 42); }\n")
    for row in spec["files"]:
        row["sha256"] = sha((root / row["path"]).read_bytes())
    source_files = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.rs")}
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    environment = dict(os.environ, PATH=str(cargo.parent) + ":" + os.environ.get("PATH", ""),
                       RUSTC=str(cargo.with_name("rustc")), CARGO_HOME=str(tmp_path / "cargo-home"),
                       CARGO_TARGET_DIR=str(tmp_path / "target"), CARGO_NET_OFFLINE="true",
                       CARGO_TERM_COLOR="never", RUSTFLAGS="", CARGO_BUILD_JOBS="1",
                       ORACLE_VARIANT="golden" if api == "old" else "start")
    result = subprocess.run([sys.executable, entry.__file__, "--spec", str(path), "--source", str(root)],
                            cwd=root, env=environment, capture_output=True, text=True, timeout=90)
    assert result.returncode == (0 if passes else 1), result.stdout + "\n" + result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("ReviewBench Rust API evidence: "))
    evidence = json.loads(line.split(": ", 1)[1])
    assert evidence["selected"] == expected_forms and evidence["results"] == {"works": passes}
    assert [row["form"] for row in evidence["tests"]] == expected_forms
    assert all((root / name).read_bytes() == data for name, data in source_files.items())
