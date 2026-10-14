"""Original-wrapper conformance plus offline, whole-candidate subprocess tests."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration import go_lane
from agentic_runtime_migration.go_contracts import (
    SHELL_PROFILES, generation_setup, load_contract, test_flags as parse_flags,
    transport_wrapper, working_directory,
)
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.evaluation import parse_tests, validate_grading

FIXTURES = ROOT / "tests/fixtures/go_wrappers"
ENTRY = ROOT / "scripts/agentic_runtime_migration/go_oracle_entry.sh"
CASES = [
    ("053-optimism", "inline-module-test", "op-chain-ops"),
    ("059-google-cloud-go", "module-pin-test", "bigtable"),
    ("067-go-azure-sdk", "cli-boundary-test", ""),
    ("120-poktroll", "generated-package-test", ""),
    ("146-weave-gitops-enterprise", "local-replace-test", ""),
    ("149-yaprmtrc", "covered-cli", ""),
]


def test_checked_in_original_fixture_bytes_match_their_retained_hashes():
    manifest = json.loads((FIXTURES / "ORIGINALS.json").read_text())
    assert manifest["kind"] == "original-oracle-test-fixture-bytes"
    for record in manifest["files"]:
        assert hashlib.sha256((FIXTURES / record["path"]).read_bytes()).hexdigest() == record["sha256"]
    assert set(SHELL_PROFILES) == {
        record["sha256"] for record in manifest["files"] if record["path"].endswith("/run_oracle.sh")}


def write(path, text, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if executable:
        path.chmod(0o755)
    return path


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()


def commit(root):
    git(root, "init", "-q")
    git(root, "config", "user.email", "go-fixture@example.invalid")
    git(root, "config", "user.name", "Go fixture")
    git(root, "add", ".")
    git(root, "commit", "-qm", "whole candidate fixture")
    return git(root, "rev-parse", "HEAD")


def fixture(tmp_path, name):
    directory = tmp_path / "original" / name
    shutil.copytree(FIXTURES / name, directory)
    spec = json.loads((directory / "session-spec.json").read_text())
    cwd = next(c for n, _, c in CASES if n == name)
    repository = tmp_path / "candidate"
    write(repository / cwd / "go.mod", "module example.invalid/current\n\ngo 1.22\n")
    write(repository / cwd / "source.go", "package current\n")
    revision = commit(repository)
    if name == "120-poktroll":
        for binary in ("ignite", "mockgen"):
            write(directory / "bin" / binary, "#!/bin/sh\nexit 0\n", True)
    oracle = {"pass_to_pass": len(spec["pass_to_pass_tests"]),
              "pass_to_pass_tests": spec["pass_to_pass_tests"], "runs": {}}
    for phase in ("start", "golden"):
        oracle["runs"][phase] = []
        for number in (1, 2, 3):
            signature = '{"fixture":true}'
            write(directory / f"{phase}-{number}.log", "P2P_SIGNATURE=" + signature + "\n")
            oracle["runs"][phase].append(
                {"run": number, "ok": True, "exit_code": 0, "signature": signature})
    task = {"id": "go-" + name, "repo": "fixture/" + name, "start_sha": revision,
            "locality": {"path": (cwd + "/" if cwd else "") + "source.go"}}
    scratch, output = tmp_path / "scratch", tmp_path / "output"
    scratch.mkdir()
    output.mkdir()
    bundles = []

    def bundle(root, names=None, **kwargs):
        captured = tmp_path / "assets" / str(len(bundles))
        captured.mkdir(parents=True)
        for path in [root / n for n in names] if names is not None else root.rglob("*"):
            if path.is_file():
                target = captured / path.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        bundles.append((captured, kwargs.get("role", "dependency")))
        return {"path": str(captured), "sha256": "b" * 64}

    ctx = SimpleNamespace(
        config={}, legacy=tmp_path, scratch=scratch, output=output, bundles=bundles,
        repo=lambda _: repository, task_directory=lambda *args: directory, bundle=bundle,
        image=lambda _: ("sha256:" + "c" * 64, {"path": "image-audit", "sha256": "d" * 64}),
        modules=lambda _: [], immutable_tests=lambda _: [],
        golden=lambda *args: {"path": "original-golden.patch", "sha256": "e" * 64},
    )
    return ctx, task, oracle, directory


def assembled(monkeypatch, ctx, task, oracle):
    monkeypatch.setattr(go_lane, "compiler", lambda *args: ({"path": "compiler", "sha256": "a" * 64}, "1.22.2"))
    monkeypatch.setattr(go_lane, "module_proxy", lambda *args, **kwargs: {
        "path": "proxy", "sha256": "f" * 64, "resource_hint": go_lane.proxy_resource_hint([])})
    return go_lane.migrate(ctx, task, oracle)


@pytest.mark.parametrize("name,kind,cwd", CASES)
def test_original_shell_families_assemble_whole_candidate_recipes(tmp_path, monkeypatch, name, kind, cwd):
    ctx, task, oracle, directory = fixture(tmp_path, name)
    contract = load_contract(ctx, task, oracle)
    assert contract.kind == kind
    assert contract.module_cwd == cwd
    assert len([p for p in contract.provenance if p["role"] == "original-run"]) == 6
    recipe = assembled(monkeypatch, ctx, task, oracle)
    validate_grading(recipe)
    assert recipe.cpus == 2 and recipe.memory_mb == 4096
    assert recipe.env["GOMAXPROCS"] == "2" and "-p=2" in recipe.env["GOFLAGS"]
    assert recipe.env["GOPROXY"] == "file:///opt/reviewbench-assets/goproxy"
    assert recipe.env["COVERAGE_OUT"] == "/workspace/.reviewbench/coverage.out"
    assert recipe.grading["multifile_build"] is True
    assert recipe.grading["commands"][0]["expected_tests"] == oracle["pass_to_pass_tests"]
    assert "go test" not in recipe.dev_checks[0] or ".go " not in recipe.dev_checks[0]
    grader = next(path for path, role in ctx.bundles if role == "grader")
    record = json.loads((grader / "go-contract.json").read_text())
    assert record["command"] == json.loads((directory / "session-spec.json").read_text())["command"]
    assert record["historical_production_overlay"] is False
    for item in contract.spec.get("inject_files", []):
        assert (grader / item["source"]).read_bytes() == (directory / item["source"]).read_bytes()
    if contract.wrapper:
        assert (grader / "original" / contract.wrapper).read_bytes() == (directory / contract.wrapper).read_bytes()
        text, _ = transport_wrapper(contract)
        assert (grader / contract.wrapper).read_text() == text
    if kind == "inline-module-test":
        setup = next(path for path, role in ctx.bundles if role == "toolchain") / "setup.sh"
        assert "genesis_compat.go" in setup.read_text()
        assert "session_oracle_test.go" not in setup.read_text()
    if kind == "local-replace-test":
        assert any("go-support" == Path(asset["target"]).name and asset["visibility"] == "developer"
                   for asset in recipe.assets)


@pytest.mark.parametrize("phase,number", [("start", 2), ("golden", 1), ("golden", 3)])
def test_all_six_original_logs_are_bound(tmp_path, phase, number):
    ctx, task, oracle, directory = fixture(tmp_path, "059-google-cloud-go")
    (directory / f"{phase}-{number}.log").write_text("P2P_SIGNATURE=changed\n")
    with pytest.raises(ContractError, match="signature_log_differs"):
        load_contract(ctx, task, oracle)


def test_original_shell_hash_not_a_filename_allowlist(tmp_path):
    ctx, task, oracle, directory = fixture(tmp_path, "059-google-cloud-go")
    with (directory / "run_oracle.sh").open("a") as stream:
        stream.write("\necho fake-success\n")
    with pytest.raises(ContractError, match="shell_profile_pending"):
        load_contract(ctx, task, oracle)


def test_original_wrapper_argument_contract_is_fixed(tmp_path):
    ctx, task, oracle, directory = fixture(tmp_path, "067-go-azure-sdk")
    path = directory / "session-spec.json"
    spec = json.loads(path.read_text())
    spec["command"][-1] = "{repo}/different-process-boundary"
    path.write_text(json.dumps(spec))
    with pytest.raises(ContractError, match="wrapper_arguments_differ"):
        load_contract(ctx, task, oracle)


@pytest.mark.parametrize("key,value", [("id", "foreign-task"), ("repo", "foreign/repo"), ("start_sha", "a" * 40)])
def test_original_oracle_belongs_to_this_candidate(tmp_path, key, value):
    ctx, task, oracle, directory = fixture(tmp_path, "059-google-cloud-go")
    oracle[key] = value
    with pytest.raises(ContractError, match="oracle_task_binding_differs"):
        load_contract(ctx, task, oracle)


@pytest.mark.parametrize("value", ["../outside", "/host", "{root}", "{repo}/../outside", "{repo}/${HOME}"])
def test_only_candidate_relative_workdirs(value):
    with pytest.raises(ContractError):
        working_directory(value)


@pytest.mark.parametrize("suffix", [";echo BAD", " && echo BAD", " | cat", "\necho BAD", " $(echo BAD)"])
def test_inline_contract_does_not_accept_unmodeled_shell_steps(tmp_path, suffix):
    ctx, task, oracle, directory = fixture(tmp_path, "053-optimism")
    path = directory / "session-spec.json"
    spec = json.loads(path.read_text())
    spec["command"][2] += suffix
    path.write_text(json.dumps(spec))
    with pytest.raises(ContractError, match="inline_shell_shape_pending"):
        load_contract(ctx, task, oracle)


@pytest.mark.parametrize("args,expected", [
    (["-tags=sqlite,netgo", "-run=TestX", "./pkg"], ["-tags=sqlite,netgo"]),
    (["-tags", "sqlite netgo", "-race", "-coverprofile", "cover.out", "./pkg"], ["-tags", "sqlite netgo", "-race"]),
    (["-ldflags=-X main.version=fixture", "-count=1", "./pkg"], ["-ldflags=-X main.version=fixture"]),
])
def test_public_checks_keep_build_flags_not_test_selectors(args, expected):
    flags, targets = parse_flags(args)
    assert flags == expected
    assert targets == ["./pkg"]


def test_file_list_build_is_not_a_package_adapter():
    with pytest.raises(ContractError, match="whole_candidate_package"):
        parse_flags(["-v", "anchor.go", "session_oracle_test.go"])


def test_public_compile_check_splits_coverpkg_list_not_the_original_command(tmp_path, monkeypatch):
    ctx, task, oracle, directory = fixture(tmp_path, "067-go-azure-sdk")
    path = directory / "session-spec.json"
    spec = json.loads(path.read_text())
    spec["command"] = ["go", "test", "-v", "-tags=netgo", "-run", "^TestSession$",
                       "-coverpkg=example.invalid/one,example.invalid/two", "./sessionoracle"]
    path.write_text(json.dumps(spec))
    recipe = assembled(monkeypatch, ctx, task, oracle)
    assert "example.invalid/one example.invalid/two" in recipe.dev_checks[0]
    assert "-tags=netgo" in recipe.dev_checks[0]
    assert "-coverpkg=example.invalid/one,example.invalid/two" in recipe.grading["commands"][0]["command"]


def test_inventory_keeps_the_original_scope_and_all_gap_ids(tmp_path, monkeypatch):
    import inspect_go_runtime_contracts as inspection
    ctx, task, oracle, directory = fixture(tmp_path, "067-go-azure-sdk")
    task["inferred_language"] = "Go"
    tasks = {task["id"]: task, "python": {**task, "id": "python", "inferred_language": "Python"},
             "legacy-go": {**task, "id": "legacy-go"}}
    oracles = {ident: oracle for ident in tasks}
    gaps = {"scope": 402, "remaining": 3, "gaps": [
        {"id": ident, "lane": "gopy", "legacy": ident == "legacy-go", "latest": {"kind": "configuration"}}
        for ident in tasks
    ]}
    monkeypatch.setattr(inspection, "frozen_inputs", lambda *args, **kwargs: SimpleNamespace(
        modules={}, manifests=[], local_modules=set()))
    monkeypatch.setattr(inspection, "find_compiler", lambda *args: (Path("/fixture/go"), "go version go1.22.2 linux/amd64"))
    report = inspection.inventory(gaps, tasks, oracles, ctx)
    assert report["scope"] == 402 and report["remaining_input"] == 3
    assert report["input_gap_ids"] == list(tasks)
    assert report["modern_go_configuration_tasks"] == 1
    assert report["runtime_verified"] == 0 and report["denominator_changed"] is False


def test_missing_flag_value_is_an_explicit_contract_error():
    with pytest.raises(ContractError, match="flag_value_missing"):
        parse_flags(["-tags"])


def test_symlink_injected_asset_is_not_dereferenced(tmp_path):
    ctx, task, oracle, directory = fixture(tmp_path, "059-google-cloud-go")
    source = directory / "session_oracle_test.go"
    outside = tmp_path / "outside.go"
    source.rename(outside)
    source.symlink_to(outside)
    with pytest.raises(ContractError, match="artifact_symlink"):
        load_contract(ctx, task, oracle)


def test_arbitrary_non_test_injection_needs_its_original_boundary_profile(tmp_path):
    ctx, task, oracle, directory = fixture(tmp_path, "053-optimism")
    write(directory / "genesis_compat.go", "package genesis\nfunc HistoricalSource() {}\n")
    with pytest.raises(ContractError, match="compile_boundary_profile_pending"):
        load_contract(ctx, task, oracle)


def test_published_count_and_frozen_identities_must_agree(tmp_path):
    ctx, task, oracle, directory = fixture(tmp_path, "059-google-cloud-go")
    oracle["pass_to_pass"] = 2
    with pytest.raises(ContractError, match="test_count_differs"):
        load_contract(ctx, task, oracle)


FAKE_GO = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["GO_TRACE"], "a") as stream:
    stream.write(json.dumps({"args":args, "cwd":os.getcwd(), "path":os.environ["PATH"]})+"\n")
if args[0] == "build":
    binary = pathlib.Path(args[args.index("-o")+1])
    text = os.environ.get("AGENT_OUTPUT", "unknown flags: [unexpected]")
    code = os.environ.get("AGENT_STATUS", "7")
    binary.write_text("#!/bin/sh\nprintf '%s\\n' '"+text+"'\nexit "+code+"\n")
    binary.chmod(0o755)
elif args[:2] == ["tool", "covdata"]:
    output = next(x[3:] for x in args if x.startswith("-o="))
    pathlib.Path(output).write_text("mode: set\n")
elif args[0] == "generate":
    for name in ["testutil/mockclient/block_client_mock.go", "testutil/mockrelayer/account/query_client.go"]:
        path = pathlib.Path(name);path.parent.mkdir(parents=True, exist_ok=True);path.write_text("generated mock\n")
sys.exit(int(os.environ.get("GO_TEST_EXIT", "0")) if args[0] == "test" else 0)
'''


def shell_environment(tmp_path, module):
    binary = write(tmp_path / "pinned-bin/go", "#!" + sys.executable + "\n" + FAKE_GO, True)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    env = dict(os.environ, REVIEWBENCH_GO=str(binary), REVIEWBENCH_GO_MODULE_DIR=str(module),
               TMPDIR=str(temporary), GO_TRACE=str(tmp_path / "trace.jsonl"),
               COVERAGE_OUT=str(tmp_path / "coverage.out"), GOMAXPROCS="2")
    return env


def shell_command(contract, directory, tmp_path):
    context = {"repo": str(tmp_path / "candidate"), "task_dir": str(directory),
               "coverage_out": str(tmp_path / "coverage.out")}
    return [go_lane.relocate(arg, context) for arg in contract.command]


@pytest.mark.parametrize("name,kind,cwd", [c for c in CASES if c[1] != "generated-package-test"])
def test_real_original_shell_argv_and_path_transport(tmp_path, name, kind, cwd):
    ctx, task, oracle, directory = fixture(tmp_path, name)
    contract = load_contract(ctx, task, oracle)
    env = shell_environment(tmp_path, ctx.repo(task) / cwd)
    if contract.wrapper:
        (directory / contract.wrapper).write_text(transport_wrapper(contract)[0])
    result = subprocess.run([str(ENTRY), *shell_command(contract, directory, tmp_path)],
                            env=env, cwd=ctx.repo(task) / contract.cwd,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in Path(env["GO_TRACE"]).read_text().splitlines()]
    assert all(event["cwd"] == str(ctx.repo(task) / cwd) for event in events)
    if kind == "module-pin-test":
        assert [e["args"][:2] for e in events] == [["mod", "edit"], ["mod", "download"], ["test", "-v"]]
        assert "-require=google.golang.org/genproto@v0.0.0-20220725144611-272f38e5d71b" in events[0]["args"]
        assert events[-1]["args"][-1] == "./sessionoracle"
    elif kind == "cli-boundary-test":
        assert events[0]["path"] == str(directory / "fake-bin") + ":/usr/bin:/bin"
        assert events[0]["args"][-1] == "./sdk/auth/sessionoracle"
    elif kind == "local-replace-test":
        assert events[0]["args"][2] == "-replace=github.com/weaveworks/weave-gitops-enterprise-credentials=" + str(
            directory / "dependency-stubs/enterprise-credentials")
    elif kind == "covered-cli":
        assert events[0]["args"][-1] == "./cmd/agent"
        assert events[1]["args"][:3] == ["tool", "covdata", "textfmt"]
        assert 'P2P_SIGNATURE={"exit_status":"nonzero","rejected_arguments":["unexpected"]}' in result.stdout
    else:
        assert events[0]["args"][-1] == "./cmd/withdrawals"


@pytest.mark.parametrize("status,diagnostic", [("0", "unknown flags: [unexpected]"), ("7", "different diagnostic")])
def test_cli_assertions_are_not_weakened_by_transport(tmp_path, status, diagnostic):
    ctx, task, oracle, directory = fixture(tmp_path, "149-yaprmtrc")
    contract = load_contract(ctx, task, oracle)
    (directory / contract.wrapper).write_text(transport_wrapper(contract)[0])
    env = shell_environment(tmp_path, ctx.repo(task))
    env.update(AGENT_STATUS=status, AGENT_OUTPUT=diagnostic)
    result = subprocess.run([str(ENTRY), *shell_command(contract, directory, tmp_path)],
                            cwd=ctx.repo(task), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "P2P_SIGNATURE=" not in result.stdout


def test_shell_nonzero_go_exit_is_preserved(tmp_path):
    ctx, task, oracle, directory = fixture(tmp_path, "067-go-azure-sdk")
    contract = load_contract(ctx, task, oracle)
    env = shell_environment(tmp_path, ctx.repo(task))
    env["GO_TEST_EXIT"] = "23"
    result = subprocess.run([str(ENTRY), *shell_command(contract, directory, tmp_path)],
                            cwd=ctx.repo(task), env=env, capture_output=True, timeout=30)
    assert result.returncode == 23


def test_generation_uses_current_candidate_and_restores_its_not_heads_lock(tmp_path):
    ctx, task, oracle, directory = fixture(tmp_path, "120-poktroll")
    contract = load_contract(ctx, task, oracle)
    candidate = ctx.repo(task)
    before = "module current.candidate/changed\n\ngo 1.22\n"
    (candidate / "go.mod").write_text(before)
    write(candidate / "go.sum", "current candidate lock bytes\n")
    write(candidate / "schema.proto", "CURRENT PROTO INPUT")
    ignite = "#!" + sys.executable + "\n" + r'''
from pathlib import Path
p = Path("x/application/types/query.pb.go");p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("generated from "+Path("schema.proto").read_text())
Path("go.mod").write_text("generator side effect")
Path("go.sum").write_text("generator side effect")
'''
    write(directory / "bin/ignite", ignite, True)
    (directory / contract.wrapper).write_text(transport_wrapper(contract)[0])
    env = shell_environment(tmp_path, candidate)
    result = subprocess.run([str(ENTRY), *shell_command(contract, directory, tmp_path)],
                            cwd=candidate, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (candidate / "go.mod").read_text() == before
    assert (candidate / "go.sum").read_text() == "current candidate lock bytes\n"
    assert (candidate / "x/application/types/query.pb.go").read_text() == "generated from CURRENT PROTO INPUT"
    events = [json.loads(line)["args"] for line in Path(env["GO_TRACE"]).read_text().splitlines()]
    assert [e[:1] for e in events] == [["generate"], ["generate"], ["test"]]
    prefix = generation_setup(contract)
    assert "TestSession" not in prefix and "go test" not in prefix
    assert "git checkout" not in prefix
    assert "reviewbench_restore_candidate_modules" in prefix


def test_direct_route_builds_neighboring_current_candidate_source(tmp_path, monkeypatch):
    go = shutil.which("go")
    if not go:
        pytest.skip("preinstalled Go required for offline whole-package test")
    ctx, task, oracle, directory = fixture(tmp_path, "067-go-azure-sdk")
    candidate = ctx.repo(task)
    (candidate / "source.go").unlink()
    write(candidate / "go.mod", "module example.invalid/current\n\ngo 1.20\n")
    write(candidate / "pkg/main.go", "package pkg\nfunc Value() int { return 4 + Neighbor() }\n")
    neighbor = write(candidate / "pkg/neighbor.go", "package pkg\nfunc Neighbor() int { return 3 }\n")
    spec = json.loads((directory / "session-spec.json").read_text())
    spec["command"] = ["go", "test", "-v", "-run", "^TestSessionValue$", "-count=1",
                       "-coverpkg=example.invalid/current/pkg", "./pkg"]
    spec["inject_files"] = [{"source": "session_oracle_test.go", "destination": "pkg/session_oracle_test.go"}]
    write(directory / "session_oracle_test.go", '''package pkg
import ("fmt"; "testing")
func TestSessionValue(t *testing.T) {
    if Value() != 7 { t.Fatal("current neighboring source changed public value") }
    fmt.Println("P2P_SIGNATURE={\\"fixture\\":true}")
}
''')
    (directory / "session-spec.json").write_text(json.dumps(spec))
    git(candidate, "add", ".")
    git(candidate, "commit", "-qm", "two production source files")
    task["start_sha"] = git(candidate, "rev-parse", "HEAD")
    task["locality"]["path"] = "pkg/main.go"
    recipe = assembled(monkeypatch, ctx, task, oracle)
    replacements = {"/workspace": str(candidate), "/opt/reviewbench-assets/go": str(Path(go).resolve().parent.parent),
                    "/opt/reviewbench-cache": str(tmp_path / "cache")}
    for asset in recipe.assets:
        if asset["path"] not in {"compiler", "proxy"}:
            replacements[asset["target"]] = asset["path"]

    def relocated(text):
        for old in sorted(replacements, key=len, reverse=True):
            text = text.replace(old, replacements[old])
        return text

    environment = dict(os.environ, **{k: relocated(v) for k, v in recipe.env.items()})
    environment["GOPROXY"] = "off"
    for command in [*recipe.bootstrap, *recipe.grading["bootstrap"]]:
        result = subprocess.run(["bash", "-c", relocated(command)], cwd=candidate, env=environment,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    command = recipe.grading["commands"][0]
    original_main = (candidate / "pkg/main.go").read_bytes()
    for changed in (False, True):
        if changed:
            neighbor.write_text("package pkg\nfunc Neighbor() int { return 30 }\n")
        result = subprocess.run(["bash", "-c", relocated(command["command"])], cwd=candidate, env=environment,
                                capture_output=True, text=True, timeout=180)
        parsed = parse_tests("regex", result.stdout + result.stderr, command_ok=result.returncode == 0,
                             expected=command["expected_tests"], success_pattern=command["success_pattern"])
        assert all(parsed.values()) is not changed, result.stderr
    assert (candidate / "pkg/main.go").read_bytes() == original_main
    assert "return 30" in neighbor.read_text()
