from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.tsjs_lane import reference_patch, success_pattern, node_worker_environment
from reviewbench_maintainability.agentic.contracts import ContractError


@pytest.mark.parametrize("signature,good,bad", [
    ("node-tap:tests=2,passed=2,failed=0", "# tests 2\n# pass 2\n# fail 0\n", "# tests 2\n# pass 1\n# fail 1\n"),
    ("jest:passed=2", "Tests: 2 passed, 2 total\n", "Tests: 1 passed, 1 failed, 2 total\n"),
    ("mocha:passed=2", "2 passing (2ms)\n", "1 passing (2ms)\n1 failing\n"),
    ("jasmine:passed=2", "2 specs, 0 failures\n", "2 specs, 1 failure\n"),
])
def test_javascript_completion_requires_the_fixed_success_count(signature, good, bad):
    pattern = success_pattern(signature)
    assert re.search(pattern, good, re.MULTILINE)
    assert not re.search(pattern, bad, re.MULTILINE)
    assert not re.search(pattern, "", re.MULTILINE)


def test_vitest_internal_pools_are_bounded_without_changing_test_selection():
    assert node_worker_environment(2) == {
        "VITEST_MAX_THREADS": "2", "VITEST_MIN_THREADS": "1",
        "VITEST_MAX_FORKS": "2", "VITEST_MIN_FORKS": "1",
    }
    assert set(node_worker_environment(1).values()) == {"1"}
    for value in (0, -1, True, "2"):
        with pytest.raises(ContractError, match="positive integer"):
            node_worker_environment(value)


def test_vitest_summary_accepts_colored_output_but_requires_exact_test_and_file_totals():
    pattern = success_pattern("vitest:files=2,tests=16")
    output = ("\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m2 passed\x1b[39m\x1b[22m\x1b[90m (2)\x1b[39m\n"
              "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m16 passed\x1b[39m\x1b[22m\x1b[90m (16)\x1b[39m\n")
    assert re.search(pattern, output, re.MULTILINE)
    assert not re.search(pattern, output.replace("(16)", "(17)"), re.MULTILINE)
    assert not re.search(pattern, output.replace("2 passed", "1 passed"), re.MULTILINE)
    assert not re.search(pattern, output.replace("16 passed", "15 passed | 1 failed"), re.MULTILINE)
    assert not re.search(pattern, "\n RUN v3.2.4\n", re.MULTILINE)


def test_js_source_only_reference_restores_tests_and_omits_gitlinks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "main.js").write_text("const before = 1;\n")
    (repo / "test.js").write_text("original frozen assertion\n")
    git("add", ".")
    git("commit", "-qm", "start")
    start = git("rev-parse", "HEAD")
    (repo / "main.js").write_text("const after = 1;\n")
    (repo / "test.js").write_text("changed assertion, excluded\n")
    git("add", ".")
    git("update-index", "--add", "--cacheinfo", "160000," + start + ",optional/component")
    git("commit", "-qm", "target")
    target = git("rev-parse", "HEAD")
    ctx = SimpleNamespace(output=tmp_path / "output", repo=lambda _: repo)
    result = reference_patch(ctx, {"start_sha": start, "target_sha": target},
                             {"source_only_restored_tests": [{"path": "test.js"}]})
    patch = Path(result["path"]).read_text()
    assert "main.js" in patch
    assert "test.js" not in patch
    assert "optional/component" not in patch
    other = reference_patch(ctx, {"start_sha": start, "target_sha": target},
                            {"source_only_restored_tests": []})
    assert other["path"] != result["path"]
    assert Path(result["path"]).read_text() == patch
