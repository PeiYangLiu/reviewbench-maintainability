import importlib.util
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_open_model_agentic.py"
SPEC = importlib.util.spec_from_file_location("run_open_model_agentic", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_package_versions_fails_closed(monkeypatch):
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr="missing",
        ),
    )
    assert MODULE.package_versions() == {"error": "missing"}


def test_terminate_stops_only_supplied_process():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    MODULE.terminate(process)
    assert process.poll() is not None


def test_ids_file_is_exposed_by_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--model",
            "model",
            "--tasks",
            str(tmp_path / "tasks.jsonl"),
            "--repo-cache",
            str(tmp_path / "repos"),
            "--out-dir",
            str(tmp_path / "out"),
            "--ids-file",
            str(tmp_path / "ids.txt"),
        ],
    )

    assert MODULE.parse_args().ids_file == tmp_path / "ids.txt"


def test_restore_generation_checkpoint(tmp_path):
    source = tmp_path / "blob-run"
    destination = tmp_path / "local-run"
    result = source / "generation/model/generation_results.jsonl"
    patch = source / "generation/model/patches/task.patch"
    result.parent.mkdir(parents=True)
    patch.parent.mkdir(parents=True)
    result.write_text('{"id":"task","status":"generated"}\n')
    patch.write_text("patch\n")

    restored = MODULE.restore_generation_checkpoint(source, destination)

    assert restored == 2
    assert (
        destination / "generation/model/generation_results.jsonl"
    ).read_bytes() == result.read_bytes()
    assert (
        destination / "generation/model/patches/task.patch"
    ).read_bytes() == patch.read_bytes()


def test_restore_generation_checkpoint_without_source_is_noop(tmp_path):
    destination = tmp_path / "local-run"

    assert MODULE.restore_generation_checkpoint(None, destination) == 0
    assert (
        MODULE.restore_generation_checkpoint(tmp_path / "missing", destination)
        == 0
    )
    assert not destination.exists()


def test_restore_generation_checkpoint_preserves_local_state(tmp_path):
    source = tmp_path / "blob-run"
    destination = tmp_path / "local-run"
    remote = source / "generation/model/generation_results.jsonl"
    local = destination / "generation/model/generation_results.jsonl"
    remote.parent.mkdir(parents=True)
    local.parent.mkdir(parents=True)
    remote.write_text("remote\n")
    local.write_text("local\n")

    assert MODULE.restore_generation_checkpoint(source, destination) == 0
    assert local.read_text() == "local\n"
