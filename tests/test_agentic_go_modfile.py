"""Real offline Go transport controls; these fixtures are not benchmark Oracles."""
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
from agentic_runtime_migration.go_modfile import (
    PROTECTED, RUNTIME, install_shadow_transport, module_edits, repaired_recipe, shadow_environment,
)
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_exec import recipe_for


@pytest.fixture(scope="session")
def go_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("go-modfile-cache")


@pytest.fixture
def module(tmp_path, go_cache):
    go = os.environ.get("REVIEWBENCH_MODFILE_GO", "/usr/bin/go")
    assert subprocess.check_output([go, "version"], text=True).strip() == "go version go1.22.2 linux/amd64"
    source = tmp_path / "current"
    source.mkdir()
    dependency = tmp_path / "external"
    dependency.mkdir()
    (dependency / "go.mod").write_text("module example.com/dependency\n\ngo 1.22\n")
    (dependency / "value.go").write_text("package dependency\nfunc Value() int { return 7 }\n")
    (source / "go.mod").write_text("module example.com/app\n\ngo 1.22\n\nrequire example.com/dependency v0.0.0\n")
    (source / "value.go").write_text(
        'package app\nimport "example.com/dependency"\nfunc Value() int { return dependency.Value() }\n')
    (source / "value_test.go").write_text(
        'package app\nimport "testing"\nfunc TestValue(t *testing.T) { if Value()!=7 { t.Fatal("actual candidate changed") } }\n')
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    original_entry = Path(__file__).resolve().parents[1] / "scripts/agentic_runtime_migration/go_oracle_entry.sh"
    shutil.copy2(original_entry, runtime / "go_oracle_entry.sh")
    edit = "-replace=example.com/dependency=" + str(dependency)
    install_shadow_transport(runtime, [edit])
    env = {**os.environ, "SOURCE_ROOT": str(source), "REVIEWBENCH_GO_MODULE_DIR": str(source),
           "REVIEWBENCH_GO": str(runtime / "go"), "REVIEWBENCH_REAL_GO": go,
           "REVIEWBENCH_GO_SHADOW_DIR": str(source / ".reviewbench/go-module-shadow"),
           "REVIEWBENCH_GO_SHADOW_EDITS": str(runtime / "module-edits.args"),
           "PATH": str(runtime) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
           "GOFLAGS": "-mod=readonly -p=2", "GOTOOLCHAIN": "local", "GOMAXPROCS": "2",
           "GOPROXY": "off", "GOSUMDB": "off", "GOWORK": "off",
           "GOCACHE": str(go_cache), "GOMODCACHE": str(tmp_path / "modcache"), "TMPDIR": str(tmp_path)}
    env.pop("REVIEWBENCH_GO_PROTECTED_MODULES", None)
    return SimpleNamespace(source=source, runtime=runtime, edit=edit, env=env, go=go, temporary=tmp_path)


def run(module, *args, protected=False, env=None):
    command = ([str(module.runtime / "go"), *args] if not protected else
               ["bash", str(module.runtime / "go_oracle_entry.sh"), "bash", "-c", 'go "$@"', "--", *args])
    return subprocess.run(command, cwd=module.source, env=env or module.env,
                          text=True, capture_output=True, timeout=90)


@pytest.mark.parametrize("with_sum", [False, True])
def test_original_setup_edits_only_shadow_and_public_test_executes_current_source(module, with_sum):
    root = module.source
    if with_sum:
        (root / "go.sum").write_text("")
    original = {name: (root / name).read_bytes() if (root / name).exists() else None for name in ("go.mod", "go.sum")}
    setup = run(module, "mod", "edit", module.edit, protected=True)
    assert setup.returncode == 0, setup.stderr
    shadow = root / ".reviewbench/go-module-shadow/go.mod"
    assert "replace example.com/dependency" in shadow.read_text()
    good = run(module, "test", "-count=1", "-v", "./...")
    assert good.returncode == 0 and "--- PASS: TestValue" in good.stdout, good.stderr
    (root / "value.go").write_text("package app\nfunc Value() int { return 9 }\n")
    bad = run(module, "test", "-count=1", "./...")
    assert bad.returncode == 1 and "actual candidate changed" in bad.stdout
    (root / "value.go").write_text(
        'package app\nimport "example.com/dependency"\nfunc Value() int { return dependency.Value() }\n')
    restored = run(module, "test", "-count=1", "./...")
    assert restored.returncode == 0, restored.stderr
    for name, data in original.items():
        assert ((root / name).read_bytes() if (root / name).exists() else None) == data


def test_candidate_manifest_edits_refresh_shadow_instead_of_reusing_old_source(module):
    assert run(module, "test", "-count=1", "./...").returncode == 0
    mod = module.source / "go.mod"
    mod.write_text(mod.read_text().replace("v0.0.0", "v0.0.2") + "\n// candidate manifest edit\n")
    changed = mod.read_bytes()
    tested = run(module, "test", "-count=1", "./...")
    assert tested.returncode == 0, tested.stderr
    shadow = module.source / ".reviewbench/go-module-shadow/go.mod"
    assert "v0.0.2" in shadow.read_text() and "// candidate manifest edit" in shadow.read_text()
    assert mod.read_bytes() == changed


def test_explicit_agent_mod_edit_changes_current_source_then_refreshes(module):
    before = (module.source / "go.mod").read_bytes()
    changed = run(module, "mod", "edit", "-require=example.com/dependency@v0.0.3")
    assert changed.returncode == 0, changed.stderr
    assert (module.source / "go.mod").read_bytes() != before
    assert "v0.0.3" in (module.source / "go.mod").read_text()
    tested = run(module, "test", "-count=1", "./...")
    assert tested.returncode == 0, tested.stderr
    assert "v0.0.3" in (module.source / ".reviewbench/go-module-shadow/go.mod").read_text()


def test_nested_go_process_does_not_inherit_refresh_lock(module):
    (module.source / "nested_test.go").write_text(
        'package app\nimport ("testing";"os/exec")\n'
        'func TestNestedGo(t *testing.T) { if b,e:=exec.Command("go","version").CombinedOutput(); e!=nil { t.Fatalf("%s: %v",b,e) } }\n')
    result = run(module, "test", "-count=1", "-run", "TestNestedGo", "./...")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("target", [".reviewbench", ".reviewbench/go-module-shadow"])
def test_symlinked_shadow_parents_are_rejected_without_source_changes(module, target):
    before = (module.source / "go.mod").read_bytes()
    path = module.source / target
    path.parent.mkdir(parents=True, exist_ok=True)
    external = module.temporary / "outside"
    external.mkdir()
    path.symlink_to(external, target_is_directory=True)
    result = run(module, "test", "./...")
    assert result.returncode != 0
    assert (module.source / "go.mod").read_bytes() == before
    assert list(external.iterdir()) == []


def test_conflicting_alternate_modfile_is_not_silently_overridden(module):
    env = {**module.env, "GOFLAGS": module.env["GOFLAGS"] + " -modfile=other.mod"}
    result = run(module, "test", "./...", env=env)
    assert result.returncode == 2 and "conflicting GOFLAGS" in result.stderr


def test_local_replacement_and_unknown_profile_bounds():
    contract = SimpleNamespace(kind="local-replace-test", setup=[
        'go mod edit -replace="example.com/private=${REVIEWBENCH_GO_SUPPORT}/dependency-stubs/private"'])
    assert module_edits(contract, "/assets") == ["-replace=example.com/private=/assets/dependency-stubs/private"]
    contract.kind = "module-pin-test"
    with pytest.raises(ContractError, match="pinned Bigtable contract differs"):
        module_edits(contract, "/assets")
    contract.kind = "unknown-module-pin-test"
    assert module_edits(contract, "/assets") == []
    contract.kind = "local-replace-test"
    contract.setup = ["go mod tidy"]
    with pytest.raises(ContractError):
        module_edits(contract, "/assets")


def test_transport_retains_entry_except_protected_scope_and_original_setup(tmp_path):
    entry = tmp_path / "go_oracle_entry.sh"
    original = "#!/usr/bin/env bash\nset -euo pipefail\n\"$@\"\n"
    entry.write_text(original)
    setup = tmp_path / "setup.sh"
    setup.write_text("go mod edit -replace=example.com/private=/asset/private\n")
    before = setup.read_bytes()
    install_shadow_transport(tmp_path, ["-replace=example.com/private=/asset/private"])
    assert entry.read_text() == original.replace("set -euo pipefail\n", "set -euo pipefail\n" + PROTECTED)
    assert setup.read_bytes() == before
    assert os.access(tmp_path / "go", os.X_OK)
    with pytest.raises(ContractError, match="already wrapped"):
        install_shadow_transport(tmp_path, ["-replace=x=y"])


def test_recipe_repair_keeps_original_assertions_goldens_inputs_and_resource_limits():
    task = {"id": "fixture", "repo": "fixture/repo", "start_sha": "0" * 40}
    old = recipe_for(task)
    env = {"REVIEWBENCH_GO": "/opt/reviewbench-assets/go/bin/go",
           "REVIEWBENCH_GO_MODULE_DIR": "/workspace", "SOURCE_ROOT": "/workspace",
           "GOFLAGS": "-mod=readonly -p=2", "PATH": "/opt/reviewbench-assets/go/bin:/usr/bin:/bin"}
    assets = [{"kind": "archive", "visibility": "developer", "target": RUNTIME,
               "path": "/asset/old.json", "sha256": "a" * 64}]
    old = replace(old, env=env, assets=assets,
                  dev_checks=["cd /workspace && /opt/reviewbench-assets/go/bin/go test -run '^$' ./..."])
    new = repaired_recipe(old, {"path": "/asset/new.json", "sha256": "b" * 64})
    assert new.grading == old.grading and new.bootstrap == old.bootstrap
    assert new.image == old.image and new.image_audit == old.image_audit
    assert (new.cpus, new.memory_mb, new.build_scratch_mb) == (old.cpus, old.memory_mb, old.build_scratch_mb)
    assert new.env["REVIEWBENCH_REAL_GO"] == old.env["REVIEWBENCH_GO"]
    assert new.env["REVIEWBENCH_GO"] == RUNTIME + "/go"
    assert new.fingerprint != old.fingerprint
    assert old.assets == assets and old.env == env


def test_already_shadowed_environment_cannot_be_wrapped_twice():
    original = {"REVIEWBENCH_GO": "/opt/reviewbench-assets/go/bin/go",
                "REVIEWBENCH_GO_MODULE_DIR": "/workspace", "SOURCE_ROOT": "/workspace",
                "GOFLAGS": "-modfile=/elsewhere.mod", "PATH": "/usr/bin"}
    with pytest.raises(ContractError, match="alternate modfile"):
        shadow_environment(original)
