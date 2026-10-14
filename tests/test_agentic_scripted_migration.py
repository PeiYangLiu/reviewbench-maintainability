from pathlib import Path
from types import SimpleNamespace
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.scripted_lane import container_payload, completion_pattern, relocate_harness_paths
from reviewbench_maintainability.agentic.contracts import ContractError


def test_scripted_wrapper_keeps_payload_and_does_not_reuse_host_mounts():
    payload = 'out=$(printf "7\\ny\\n" | ruby main.rb); test "$?" -eq 0 && echo "$out"'
    import shlex
    command = ('rm -f {task_dir}/output; docker run --rm --user "$(id -u):$(id -g)" '
               '-e HOME=/work -e RUBYOPT=-r/oracle-tools/ruby_coverage.rb '
               '-v {repo}:{repo}:ro -v {work}:/work -w /work ruby:3.2-bookworm bash -lc ' +
               shlex.quote(payload))
    actual, env, cwd = container_payload(command, "ruby:3.2-bookworm")
    assert actual == payload
    assert cwd == "/work"
    assert env == {"HOME": "/work", "RUBYOPT": "-r/oracle-tools/ruby_coverage.rb"}
    assert "{repo}" not in actual and "$(id" not in actual


@pytest.mark.parametrize("suffix", ["; cat /host/file", "&& docker run other", ";"])
def test_scripted_wrapper_rejects_host_suffixes(suffix):
    with pytest.raises(ContractError):
        container_payload("docker run --rm -w /work image bash -lc 'ruby task.rb' " + suffix, "image")


def test_scripted_wrapper_rejects_undeclared_execution_flags():
    with pytest.raises(ContractError):
        container_payload("docker run --network=host image ruby task.rb", "image")


def test_scripted_harness_path_relocation_preserves_assertions():
    value = """require '/work/app'
raise 'work failure' unless output.include?('created')
puts 'ORACLE_PASS tests=1 assertions=2'
"""
    assert relocate_harness_paths(value) == value.replace("'/work/app'", "'/workspace/app'")
    assert relocate_harness_paths("'/workbench' '/working/dir'") == "'/workbench' '/working/dir'"


def test_scripted_completion_binds_all_six_assertion_counts(tmp_path):
    config = {"outcome_regex": r"ORACLE_PASS tests=(\d+) assertions=(\d+)"}
    oracle = {"runs": {}}
    for variant in ("start", "golden"):
        runs = []
        for number in range(3):
            name = f"{variant}-{number}.log"
            p = tmp_path / "scripted" / name
            p.parent.mkdir(exist_ok=True)
            p.write_text("extra output\nORACLE_PASS tests=2 assertions=7\n")
            runs.append({"exit_code": 0, "log": name})
        oracle["runs"][variant] = runs
    context = SimpleNamespace(legacy=tmp_path)
    expression = completion_pattern(context, oracle, config)
    assert re.search(expression, "ORACLE_PASS tests=2 assertions=7\n", re.MULTILINE)
    assert not re.search(expression, "ORACLE_PASS tests=2 assertions=6\n", re.MULTILINE)
    assert not re.search(expression, "", re.MULTILINE)
    p.write_text("ORACLE_PASS tests=2 assertions=6\n")
    with pytest.raises(ContractError, match="differs"):
        completion_pattern(context, oracle, config)
