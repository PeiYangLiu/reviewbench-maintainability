"""Exact Bigtable profile and real offline Go1.22.2 nested-module controls.

The small Go programs below test transport semantics, not benchmark assertions.
No task test, selector, golden patch, dependency resolver, or model is replaced.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.go_contracts import GoContract, SHELL_PROFILES
from agentic_runtime_migration.go_modfile import (
    BIGTABLE_WRAPPER_SHA256, RUNTIME, install_shadow_transport, module_edits, repaired_recipe,
)
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_exec import recipe_for

PIN = "google.golang.org/genproto@v0.0.0-20220725144611-272f38e5d71b"
ORIGINAL_WRAPPER = """#!/usr/bin/env bash
set -euo pipefail

coverage_out="$1"
go mod edit -require=google.golang.org/genproto@v0.0.0-20220725144611-272f38e5d71b
go mod download all
exec go test \\
  -v \\
  -run '^TestSessionGarbageCollectionPolicyBehavior$' \\
  -count=1 \\
  -coverpkg=cloud.google.com/go/bigtable \\
  -coverprofile="$coverage_out" \\
  ./sessionoracle
"""
TASK_ID = "github__googleapis__google-cloud-go__pull_6314__comment_916947041__api_design"
START = "67b9e900945901b6b69c2868d549583d49d19132"


def original_contract():
    profile = SHELL_PROFILES[BIGTABLE_WRAPPER_SHA256]
    assert hashlib.sha256(ORIGINAL_WRAPPER.encode()).hexdigest() == BIGTABLE_WRAPPER_SHA256
    return GoContract(
        kind="module-pin-test", spec={}, command=["bash", "{task_dir}/run_oracle.sh", "{coverage_out}"],
        cwd="bigtable", module_cwd="bigtable",
        expected=["public garbage-collection policy formatting behavior"],
        signature=None, public_target=profile["target"],
        wrapper="run_oracle.sh", wrapper_text=ORIGINAL_WRAPPER,
        setup=list(profile["setup"]), module_pins=list(profile["pins"]),
    )


def test_exact_original_profile_enables_one_retained_require_edit():
    contract = original_contract()
    before = deepcopy(contract)
    assert module_edits(contract, "/irrelevant-support") == ["-require=" + PIN]
    assert contract == before
    assert contract.setup == ["go mod edit -require=" + PIN, "go mod download all"]


@pytest.mark.parametrize(("field", "value"), [
    ("wrapper_text", ORIGINAL_WRAPPER.replace("-count=1", "-count=2")),
    ("wrapper_text", ORIGINAL_WRAPPER.replace("download all", "download")),
    ("wrapper_text", ORIGINAL_WRAPPER + "# unreviewed\n"),
    ("wrapper_text", None),
    ("wrapper", "another.sh"),
    ("cwd", ""),
    ("cwd", "bigtable/../other"),
    ("module_cwd", ""),
    ("module_cwd", "other"),
    ("command", ["bash", "{task_dir}/run_oracle.sh"]),
    ("command", ["bash", "{task_dir}/run_oracle.sh", "{coverage_out}", "--extra"]),
    ("public_target", "./..."),
    ("module_pins", ["google.golang.org/genproto@latest"]),
    ("module_pins", []),
    ("setup", ["go mod edit -require=" + PIN]),
    ("setup", ["go mod edit -require=" + PIN, "go mod tidy"]),
    ("setup", ["go mod download all", "go mod edit -require=" + PIN]),
    ("build_flags", ["-tags=relaxed"]),
])
def test_unattested_profile_changes_fail_before_transport_creation(field, value):
    contract = original_contract()
    setattr(contract, field, value)
    with pytest.raises(ContractError, match="pinned Bigtable contract differs"):
        module_edits(contract, "/unused")


def test_other_contract_kinds_are_not_implicitly_enabled():
    for kind in ("direct-test", "automatic-public-tests", "generated-package-test", "unknown"):
        assert module_edits(SimpleNamespace(kind=kind), "/unused") == []


def test_nested_recipe_changes_only_transport_not_original_behavior_or_resources():
    old = recipe_for({"id": TASK_ID, "repo": "googleapis/google-cloud-go", "start_sha": START})
    old = replace(
        old, workdir="/workspace/bigtable",
        env={"REVIEWBENCH_GO": "/opt/reviewbench-assets/go/bin/go",
             "REVIEWBENCH_GO_MODULE_DIR": "/workspace/bigtable", "SOURCE_ROOT": "/workspace",
             "PATH": "/opt/reviewbench-assets/go/bin:/usr/bin:/bin", "GOFLAGS": "-mod=readonly -p=2",
             "GOPROXY": "file:///opt/reviewbench-assets/goproxy", "GOSUMDB": "off",
             "GOTOOLCHAIN": "local", "GOMAXPROCS": "2"},
        assets=[{"kind": "archive", "visibility": "developer", "target": RUNTIME,
                 "path": "/assets/original-runtime.json", "sha256": "a" * 64},
                {"kind": "archive", "visibility": "grader", "target": "/opt/reviewbench-assets/go-oracle",
                 "path": "/assets/original-oracle.json", "sha256": "b" * 64}],
        dev_checks=["cd /workspace/bigtable && /opt/reviewbench-assets/go/bin/go test -run '^$' "
                    "-count=1 cloud.google.com/go/bigtable"],
    )
    value = repaired_recipe(old, {"path": "/assets/new-runtime.json", "sha256": "c" * 64})
    assert value.grading == old.grading and value.bootstrap == old.bootstrap
    assert value.workdir == old.workdir == "/workspace/bigtable"
    assert value.cache_mounts == old.cache_mounts
    assert (value.cpus, value.memory_mb, value.build_scratch_mb, value.command_timeout_seconds) == (
        old.cpus, old.memory_mb, old.build_scratch_mb, old.command_timeout_seconds)
    assert value.assets[1:] == old.assets[1:]
    assert value.env["REVIEWBENCH_GO_MODULE_DIR"] == "/workspace/bigtable"
    assert value.env["GOFLAGS"] == old.env["GOFLAGS"]
    assert value.dev_checks == [old.dev_checks[0].replace(
        "/opt/reviewbench-assets/go/bin/go", "/opt/reviewbench-assets/go-runtime/go")]
    assert value.fingerprint != old.fingerprint


@pytest.fixture(scope="module")
def go_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("bigtable-modfile-go-cache")


@pytest.fixture
def nested(tmp_path, go_cache):
    go = os.environ.get("REVIEWBENCH_MODFILE_GO", "/usr/bin/go")
    assert subprocess.check_output([go, "version"], text=True).strip() == "go version go1.22.2 linux/amd64"
    root, module = tmp_path / "current", tmp_path / "current/bigtable"
    module.mkdir(parents=True)
    # The repository root is also a real but different module.
    (root / "go.mod").write_text("module example.com/root\n\ngo 1.22\n")
    (root / "go.sum").write_text("// root manifest sentinel\n")
    for name, version in (("olddep", 1), ("newdep", 7)):
        dependency = root / name
        dependency.mkdir()
        (dependency / "go.mod").write_text("module example.com/dependency\n\ngo 1.22\n")
        (dependency / "value.go").write_text(f"package dependency\nfunc Value() int {{ return {version} }}\n")
    (module / "go.mod").write_text(
        "module example.com/child\n\ngo 1.22\n\nrequire example.com/dependency v0.0.1\n\n"
        "replace example.com/dependency v0.0.1 => ../olddep\n"
        "replace example.com/dependency v0.0.2 => ../newdep\n")
    (module / "value.go").write_text(
        'package child\nimport "example.com/dependency"\nfunc Value() int { return dependency.Value() }\n')
    tests = module / "sessionoracle"
    tests.mkdir()
    (tests / "value_test.go").write_text(
        'package sessionoracle\nimport ("testing";"example.com/child")\n'
        'func TestValue(t *testing.T) { if child.Value()!=7 { t.Fatal("current candidate assertion") } }\n')
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    entry = Path(__file__).resolve().parents[1] / "scripts/agentic_runtime_migration/go_oracle_entry.sh"
    shutil.copy2(entry, runtime / "go_oracle_entry.sh")
    edit = "-require=example.com/dependency@v0.0.2"
    (runtime / "setup.sh").write_text("#!/bin/bash\nset -euo pipefail\ncd \"$REVIEWBENCH_GO_MODULE_DIR\"\n"
                                    "go mod edit " + edit + "\ngo mod download all\n")
    install_shadow_transport(runtime, [edit])
    env = {**os.environ, "SOURCE_ROOT": str(root), "REVIEWBENCH_GO_MODULE_DIR": str(module),
           "REVIEWBENCH_GO": str(runtime / "go"), "REVIEWBENCH_REAL_GO": go,
           "REVIEWBENCH_GO_SHADOW_DIR": str(root / ".reviewbench/go-module-shadow"),
           "REVIEWBENCH_GO_SHADOW_EDITS": str(runtime / "module-edits.args"),
           "PATH": str(runtime) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
           "GOFLAGS": "-mod=readonly -p=2", "GOTOOLCHAIN": "local", "GOMAXPROCS": "2",
           "GOPROXY": "off", "GOSUMDB": "off", "GOWORK": "off",
           "GOCACHE": str(go_cache), "GOMODCACHE": str(tmp_path / "modcache"), "TMPDIR": str(tmp_path)}
    env.pop("REVIEWBENCH_GO_PROTECTED_MODULES", None)
    return SimpleNamespace(root=root, module=module, runtime=runtime, env=env, edit=edit, go=go)


def execute(nested, *args, cwd=None, protected=False):
    command = [str(nested.runtime / "go"), *args]
    if protected:
        command = ["bash", str(nested.runtime / "go_oracle_entry.sh"), "bash", "-c", 'go "$@"', "--", *args]
    return subprocess.run(command, cwd=cwd or nested.module, env=nested.env,
                          text=True, capture_output=True, timeout=90)


def modules(nested):
    return {str(p.relative_to(nested.root)): p.read_bytes() if p.is_file() else None
            for p in (nested.root / "go.mod", nested.root / "go.sum",
                      nested.module / "go.mod", nested.module / "go.sum")}


def test_nested_require_and_original_download_preserve_parent_and_child_modules(nested):
    before = modules(nested)
    setup = subprocess.run(
        ["bash", str(nested.runtime / "go_oracle_entry.sh"), "bash", str(nested.runtime / "setup.sh")],
        cwd=nested.module, env=nested.env, text=True, capture_output=True, timeout=90)
    assert setup.returncode == 0, setup.stderr
    assert modules(nested) == before
    result = execute(nested, "test", "-count=1", "-v", "./sessionoracle")
    assert result.returncode == 0 and "--- PASS: TestValue" in result.stdout, result.stderr
    assert modules(nested) == before
    shadow = nested.root / ".reviewbench/go-module-shadow/go.mod"
    assert "module example.com/child" in shadow.read_text()
    assert "require example.com/dependency v0.0.2" in shadow.read_text()
    # Relative replacements still resolve from the original nested module root.
    assert "=> ../newdep" in shadow.read_text()
    gomod = execute(nested, "env", "GOMOD")
    # `go env` reports the discovered module root, not evidence of a read path.
    assert gomod.returncode == 0 and gomod.stdout.strip() == str(nested.module / "go.mod")
    selected = execute(nested, "list", "-m", "-json", "example.com/dependency")
    assert selected.returncode == 0, selected.stderr
    graph = json.loads(selected.stdout)
    assert graph["Version"] == "v0.0.2"
    assert Path(graph["Replace"]["Dir"]).resolve() == nested.root / "newdep"


def test_child_package_cwd_retains_complete_current_parent_module(nested):
    result = execute(nested, "test", "-count=1", "-v", ".", cwd=nested.module / "sessionoracle")
    assert result.returncode == 0 and "--- PASS: TestValue" in result.stdout, result.stderr


def test_candidate_behavior_failure_and_independent_production_error_do_not_fall_back(nested):
    before = modules(nested)
    source = nested.module / "value.go"
    original = source.read_bytes()
    assert execute(nested, "test", "-count=1", "./sessionoracle").returncode == 0
    source.write_text("package child\nfunc Value() int { return 99 }\n")
    behavior = execute(nested, "test", "-count=1", "./sessionoracle")
    assert behavior.returncode == 1 and "current candidate assertion" in behavior.stdout
    source.write_bytes(original)
    unrelated = nested.module / "unrelated.go"
    unrelated.write_text("package child\nvar CompileError = MissingProductionSymbol\n")
    compile_error = execute(nested, "test", "-count=1", "./sessionoracle")
    assert compile_error.returncode == 1 and "MissingProductionSymbol" in compile_error.stderr
    unrelated.unlink()
    restored = execute(nested, "test", "-count=1", "./sessionoracle")
    assert restored.returncode == 0, restored.stderr
    assert modules(nested) == before


def test_current_child_manifest_changes_invalidate_shadow_basis(nested):
    assert execute(nested, "test", "-count=1", "./sessionoracle").returncode == 0
    shadow_root = nested.root / ".reviewbench/go-module-shadow"
    basis = (shadow_root / "basis.sha256").read_bytes()
    mod = nested.module / "go.mod"
    mod.write_text(mod.read_text() + "\n// current candidate manifest\n")
    expected = modules(nested)
    result = execute(nested, "test", "-count=1", "./sessionoracle")
    assert result.returncode == 0, result.stderr
    assert (shadow_root / "basis.sha256").read_bytes() != basis
    assert "// current candidate manifest" in (shadow_root / "go.mod").read_text()
    assert modules(nested) == expected


def test_caller_manifest_edit_is_not_silently_redirected_in_nested_module(nested):
    before = modules(nested)
    result = execute(nested, "mod", "edit", "-go=1.22.0")
    assert result.returncode == 0, result.stderr
    after = modules(nested)
    assert after["bigtable/go.mod"] != before["bigtable/go.mod"]
    assert after["go.mod"] == before["go.mod"]


@pytest.mark.parametrize("code", [1, 2, 101, 124, 126, 127, 137, 139, 143])
def test_nested_compiler_failure_preserves_status_without_other_form_or_outcome_selection(nested, code):
    real = nested.runtime / "failed-compiler"
    log = nested.runtime / "failed-compiler-argv.jsonl"
    real.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                    f"with open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                    f"raise SystemExit({code})\n")
    real.chmod(0o755)
    nested.env["REVIEWBENCH_REAL_GO"] = str(real)
    before = modules(nested)
    result = execute(nested, "test", "-count=1", "./sessionoracle")
    assert result.returncode == code
    invocations = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(invocations) == 1 and invocations[0][:2] == ["mod", "edit"]
    assert modules(nested) == before
