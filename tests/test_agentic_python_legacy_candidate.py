"""Real file/subprocess regression fixtures, never full402 readiness proofs."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration import python_candidate_entry as entry
from agentic_runtime_migration import python_legacy as legacy
from agentic_runtime_migration.python_candidate import external_site
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


def make_candidate(root):
    workspace, site = root / "workspace", root / "external-site"
    site.mkdir(parents=True)
    write(workspace, "project/pyproject.toml",
          '[project]\nname = "toy"\nversion = "1.2.3"\ndependencies = ["external==1.0"]\n')
    write(workspace, "project/toy/__init__.py",
          "from importlib.metadata import version\n__version__ = version('toy')\n")
    write(workspace, "project/toy/helper.py", "def number(): return 40\n")
    write(workspace, "project/toy/worker.py",
          "from .helper import number\nfrom .generated import amount\n"
          "def answer(): return number() + amount\n")
    write(workspace, "project/schema.json", '{"amount": 2}\n')
    write(workspace, "project/compile.py",
          "import json\nfrom pathlib import Path\n"
          "p=Path(__file__).parent\n"
          "(p/'toy/generated.py').write_text('amount = '+str(json.loads((p/'schema.json').read_text())['amount'])+'\\n')\n"
          "print('ACTUAL_COMPILER: generated current schema')\n")
    write(workspace, "tests/test_public.py", "def test_fixed(): assert True\n")
    write(workspace, "data/fixture.txt", "retained non-Python fixture\n")
    oracle = write(root, "oracle/fixed.py",
                   "from toy.worker import answer\nfrom toy import __version__\n"
                   "assert __version__ == '1.2.3'\nassert answer() == 42\n"
                   "print('ACTUAL_BEHAVIOR: 42')\n")
    return workspace, site, oracle


def execute(workspace, site, operation, *args):
    command = [sys.executable, "-I", "-B", str(Path(entry.__file__)), operation,
               "--workspace", str(workspace), "--site", str(site), "--project", "project",
               "--packages", "toy", *args]
    env = dict(os.environ)
    env.pop("PYTHONOPTIMIZE", None)
    return subprocess.run(command, text=True, capture_output=True, timeout=30, env=env)


def build_candidate(workspace, site):
    return execute(workspace, site, "build", "--compiler", "project/compile.py")


def test_fresh_candidate_compiler_metadata_multifile_behavior_and_byte_identity(tmp_path):
    workspace, site, oracle = make_candidate(tmp_path)
    before = entry.inventory(workspace, workspace=True)
    result = build_candidate(workspace, site)
    assert result.returncode == 0, result.stderr
    assert "ACTUAL_COMPILER: generated current schema" in result.stdout
    report = json.loads((workspace / entry.SCRATCH / "build.json").read_text())
    assert report["inputs"] == before
    assert report["metadata"]["version"] == "1.2.3"
    assert report["metadata"]["requirements"] == ["external==1.0"]
    assert set(report["generated"]) == {"project/toy/generated.py"}
    result = execute(workspace, site, "run", "--script", str(oracle))
    assert result.returncode == 0, result.stderr
    assert "ACTUAL_BEHAVIOR: 42" in result.stdout
    assert entry.inventory(workspace, workspace=True) == before
    assert not (workspace / "project/toy/generated.py").exists()
    origins = json.loads((workspace / entry.SCRATCH / "run-origins.json").read_text())
    assert origins["toy.helper"] == ["project/toy/helper.py"]
    assert origins["toy.worker"] == ["project/toy/worker.py"]
    assert origins["toy.generated"] == ["project/toy/generated.py"]


def test_candidate_helper_change_really_changes_behavior_not_old_installed_code(tmp_path):
    workspace, site, oracle = make_candidate(tmp_path)
    write(workspace, "project/toy/helper.py", "def number(): return 41\n")
    # A convenient old wheel that would pass must not hide current source.
    write(site, "toy/__init__.py", "__version__ = '1.2.3'\n")
    write(site, "toy/worker.py", "def answer(): return 42\n")
    assert build_candidate(workspace, site).returncode == 0
    result = execute(workspace, site, "run", "--script", str(oracle))
    assert result.returncode == 2 and "AssertionError" in result.stderr
    assert "ACTUAL_BEHAVIOR: 42" not in result.stdout


def test_staged_or_original_input_changes_fail_closed(tmp_path):
    workspace, site, oracle = make_candidate(tmp_path)
    assert build_candidate(workspace, site).returncode == 0
    write(workspace, entry.SCRATCH + "/source/project/toy/helper.py", "def number(): return 40 + 0\n")
    result = execute(workspace, site, "run", "--script", str(oracle))
    assert result.returncode == 2 and "overwrote supplied" in result.stderr
    assert build_candidate(workspace, site).returncode == 0
    write(workspace, "tests/test_public.py", "def test_fixed(): assert False\n")
    result = execute(workspace, site, "run", "--script", str(oracle))
    assert result.returncode == 2 and "changed since private build" in result.stderr


def test_compiler_cannot_overwrite_candidate_supplied_generated_module(tmp_path):
    workspace, site, _ = make_candidate(tmp_path)
    write(workspace, "project/toy/generated.py", "amount = 3\n")
    result = build_candidate(workspace, site)
    assert result.returncode == 2 and "overwrote supplied" in result.stderr
    assert (workspace / "project/toy/generated.py").read_text() == "amount = 3\n"


def test_staging_preserves_helper_symlinks_and_rejects_escapes(tmp_path):
    workspace, site, _ = make_candidate(tmp_path)
    (workspace / "data/alias.txt").symlink_to("fixture.txt")
    assert build_candidate(workspace, site).returncode == 0
    source = workspace / entry.SCRATCH / "source"
    assert (source / "data/alias.txt").is_symlink()
    assert (source / "data/alias.txt").read_bytes() == (workspace / "data/fixture.txt").read_bytes()
    (workspace / "escape").symlink_to(site)
    result = build_candidate(workspace, site)
    assert result.returncode == 2 and "symlink escapes" in result.stderr


def test_build_scratch_symlink_is_never_removed_or_used(tmp_path):
    workspace, site, _ = make_candidate(tmp_path)
    sentinel = write(site, "sentinel", "untouched\n")
    (workspace / entry.SCRATCH).symlink_to(site)
    result = build_candidate(workspace, site)
    assert result.returncode == 2 and "scratch is a symlink" in result.stderr
    assert sentinel.read_text() == "untouched\n"


def test_import_origin_rejects_foreign_owned_package(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setitem(sys.modules, "owned", SimpleNamespace(__file__=str(tmp_path / "old/owned.py")))
    with pytest.raises(ValueError, match="current candidate"):
        entry.check_origins(source, ["owned"])


@pytest.mark.parametrize("text", [
    '[project]\nname = "toy"\ndynamic = ["version"]\n',
    '[project]\nname = "toy"\nversion = "1.2.3"\ndynamic = ["dependencies"]\n',
    '[project]\nname = "toy\\nInjected"\nversion = "1.2.3"\n',
])
def test_candidate_metadata_is_not_guessed_or_injected(tmp_path, text):
    workspace, site, _ = make_candidate(tmp_path)
    write(workspace, "project/pyproject.toml", text)
    result = build_candidate(workspace, site)
    assert result.returncode == 2


@pytest.mark.parametrize("path", ["../old", "/old", "x/../old", "x\\old", ".git/x", ".reviewbench/x"])
def test_no_external_source_path_mapping(path):
    with pytest.raises(ValueError):
        entry.relative(path)


def make_site(root):
    venv = root / "venv"
    write(venv, "pyvenv.cfg", "version = 3.11.14\ninclude-system-site-packages = false\n")
    site = venv / "lib/python3.11/site-packages"
    write(site, "external/__init__.py", "VALUE=1\n")
    write(site, "external-1.0.dist-info/METADATA", "Name: external\nVersion: 1.0\n")
    write(site, "deq/__init__.py", "OLD_CODE=1\n")
    write(site, "deq.cpython-311-x86_64-linux-gnu.so", "not a runtime input\n")
    write(site, "deq-0.3.2.dist-info/METADATA", "Name: deq\nVersion: 0.3.2\n")
    write(site, "deq-0.3.2.dist-info/direct_url.json", '{"dir_info":{"editable":true},"url":"file:///old/deq"}\n')
    write(site, "__editable___deq_finder.py", "raise Exception('old code hook')\n")
    write(site, "external.pth", "import bad_hook\n")
    write(site, "old.egg-link", "/old/deq\n")
    write(site, "deq/__pycache__/cache.pyc", "old candidate bytecode\n")
    write(site, "external/__pycache__/cache.pyc", "old external bytecode\n")
    return venv, site


def test_source_free_snapshot_excludes_all_candidate_code_metadata_and_hooks(tmp_path):
    venv, site = make_site(tmp_path)
    before = entry.inventory(venv)
    inventory, files = external_site(venv, owned_distributions=("deq",), owned_packages=("deq",))
    assert files == ["external-1.0.dist-info/METADATA", "external/__init__.py"]
    assert [d["name"] for d in inventory["distributions"]] == ["external"]
    assert inventory["excluded_distribution_dirs"] == ["deq-0.3.2.dist-info"]
    assert inventory["python_version"] == "3.11.14"
    assert entry.inventory(venv) == before


@pytest.mark.parametrize("kind", ["editable", "local", "metadata-link", "dependency-link", "system-site"])
def test_unbound_dependency_inputs_stay_blocked(tmp_path, kind):
    venv, site = make_site(tmp_path)
    if kind in {"editable", "local"}:
        write(site, "external-1.0.dist-info/direct_url.json",
              json.dumps({"dir_info": {"editable": kind == "editable"}, "url": "file:///old/external"}))
    elif kind == "metadata-link":
        p = site / "external-1.0.dist-info/METADATA"
        p.unlink()
        p.symlink_to("../deq-0.3.2.dist-info/METADATA")
    elif kind == "dependency-link":
        (site / "external/alias.py").symlink_to("__init__.py")
    else:
        write(venv, "pyvenv.cfg", "version = 3.11.14\ninclude-system-site-packages = true\n")
    with pytest.raises(ContractError):
        external_site(venv, owned_distributions=("deq",), owned_packages=("deq",))


def test_all_nine_ids_match_release_and_no_new_fixed_assertions_are_invented(tmp_path):
    tasks = {v["id"]: v for line in (ROOT / "data/releases/hard/20260808/tasks.jsonl").read_text().splitlines()
             if (v := json.loads(line))["id"] in legacy.COHORT}
    oracles = {v["id"]: v for line in (ROOT / "data/releases/hard/20260808/oracles.jsonl").read_text().splitlines()
               if (v := json.loads(line))["id"] in legacy.COHORT}
    assert len(tasks) == 9
    assert canonical_hash(tasks[legacy.QDK]) == legacy.QDK_TASK
    assert canonical_hash(oracles[legacy.QDK]) == legacy.QDK_ORACLE
    for ident in legacy.LEGACY_INPUTS:
        row = legacy.legacy_preflight(tasks[ident], oracles[ident], tmp_path / "legacy")
        assert row["status"] == "blocked" and row["runtime_verified"] is False
        assert row["cause"] == "original_harness_or_golden_missing"
        assert "recipe" not in row
        assert row["declared_tests"] == oracles[ident].get("pass_to_pass_tests")


def test_qdk_declared_behavior_not_replaced_with_compile_or_version_marker():
    source = Path(legacy.__file__).read_text()
    assert '"public noisy-gadget JIT compilation behavior"' not in source  # read from frozen Oracle
    assert '"success_pattern": "^P2P_SIGNATURE="' in source
    assert '"deq.circuit.parser", "deq.transpiler.jit_library_builder"' in source
    assert "copy_source" not in source and "target_sha" not in source


def test_legacy_input_file_hash_reopened_not_a_success_claim(tmp_path):
    p = write(tmp_path, "original.py", "assert True\n")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    assert legacy.pinned(p, digest) == b"assert True\n"
    p.write_text("assert False\n")
    with pytest.raises(ContractError, match="hash differs"):
        legacy.pinned(p, digest)
    p.unlink()
    with pytest.raises(ContractError, match="absent"):
        legacy.pinned(p, digest)
