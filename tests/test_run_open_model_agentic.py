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
