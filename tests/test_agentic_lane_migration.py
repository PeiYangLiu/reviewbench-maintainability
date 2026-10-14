import importlib
from pathlib import Path
import sys
import threading
import time

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0, str(SCRIPTS))
from agentic_runtime_migration.scheduler import classified_jobs
from agentic_runtime_migration.go_lane import escape_module


def test_waiting_heavy_jobs_do_not_block_light_jobs():
    heavy_active = all_active = max_heavy = max_all = 0
    lock = threading.Lock()
    light_finished = threading.Event()
    def run(job):
        nonlocal heavy_active, all_active, max_heavy, max_all
        heavy = job < 6
        with lock:
            all_active += 1
            heavy_active += heavy
            max_heavy = max(max_heavy, heavy_active)
            max_all = max(max_all, all_active)
        if heavy:
            assert light_finished.wait(5), "light task starved behind queued heavy work"
        else:
            light_finished.set()
        time.sleep(.01)
        with lock:
            all_active -= 1
            heavy_active -= heavy
        return job
    result = list(classified_jobs(run, range(10), 4, lambda x:x<6, heavy_workers=2))
    assert sorted(x for x,_ in result) == list(range(10))
    assert max_heavy <= 2 and max_all <= 4


def test_classified_scheduler_covers_all_heavy_jobs_without_oversubmission():
    assert sorted(classified_jobs(lambda x:x*2, range(9), 5, lambda _:True, heavy_workers=2)) == [
        (x,x*2) for x in range(9)]


@pytest.mark.parametrize("module,escaped", [
    ("github.com/Azure/go-ansiterm", "github.com/!azure/go-ansiterm"),
    ("github.com/DATA-DOG/go-sqlmock", "github.com/!d!a!t!a-!d!o!g/go-sqlmock"),
    ("golang.org/x/sys", "golang.org/x/sys"),
])
def test_go_proxy_escape_preserves_module_identity(module, escaped):
    assert escape_module(module) == escaped


def test_lane_adapters_import_without_initializing_network_or_containers():
    importlib.import_module("agentic_runtime_migration.go_lane")
    importlib.import_module("agentic_runtime_migration.rust_lane")


def test_readonly_go_module_cache_cleanup_is_owned_and_does_not_follow_links(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import assets
    from reviewbench_maintainability.agentic.assets import AssetPool
    monkeypatch.setattr(assets, "containers_alive", lambda _: False)
    outside = tmp_path/"untouched"
    outside.mkdir()
    (outside/"keep").write_text("unchanged")
    outside.chmod(0o555)
    pool = AssetPool(None, tmp_path/"scratch", "a"*32, tmp_path)
    cache = pool.mutable_cache("gomod")
    module = cache/"example@v1"
    module.mkdir()
    (module/"source.go").write_text("package example\n")
    (module/"outside-link").symlink_to(outside, target_is_directory=True)
    module.chmod(0o555)
    pool.close()
    assert not pool.root.exists()
    assert (outside/"keep").read_text() == "unchanged"
    assert outside.stat().st_mode & 0o777 == 0o555
    outside.chmod(0o755)
