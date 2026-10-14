"""MQT94 source projections and real compiler selection on synthetic fixtures."""
import copy
import json
from pathlib import Path
import shlex
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_api as adapter, native_api_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash

PLAN = {
    "repo": "munich-quantum-toolkit/core", "input_index": 94, "configure_command": None,
    "build_command": "set -e; rm -rf oracle-qasm; mkdir oracle-qasm; variant_flag=''; "
        'if test "$ORACLE_VARIANT" = golden; then variant_flag=-DMQT_ORACLE_CONSOLIDATED_QASM; fi; '
        "g++-11 -std=c++17 -O0 -g --coverage -Iinclude $variant_flag "
        "'{root}/harnesses/mqt_standard_operation_qasm.cpp' src/operations/Operation.cpp "
        "src/operations/StandardOperation.cpp --coverage -o oracle-qasm/oracle",
    "test_command": "set -e; out=$(./oracle-qasm/oracle); printf '%s\\n' \"$out\"; test \"$out\" = '38 39'",
    "real_translation_units": ["src/operations/Operation.cpp", "src/operations/StandardOperation.cpp"],
    "docker_image": "reviewbench-native-real-mqt-core:latest", "timeout_seconds": 900, "build_jobs": 1,
}


def test_original_mqt_plan_bound_without_changing_other_profiles():
    assert canonical_hash(PLAN) == adapter.PROFILES[(PLAN["repo"], 94)][1]
    assert (PLAN["repo"], 95) not in adapter.PROFILES and (PLAN["repo"], 96) not in adapter.PROFILES


@pytest.mark.parametrize("field", ["configure_command", "build_command", "test_command",
                                  "real_translation_units", "docker_image", "timeout_seconds", "build_jobs"])
def test_mqt_original_plan_drift_rejected_before_any_build(tmp_path, field):
    from types import SimpleNamespace
    plan = copy.deepcopy(PLAN)
    plan[field] = (plan[field] + 1 if isinstance(plan[field], int) else
                   [] if isinstance(plan[field], list) else str(plan[field]) + " changed")
    with pytest.raises(ContractError, match="complete original"):
        adapter.attested(SimpleNamespace(legacy=tmp_path), {"repo": PLAN["repo"]}, plan)


def test_public_build_contains_both_production_units_and_no_private_harness():
    before = copy.deepcopy(PLAN)
    public, private, scratch = adapter.specifications("mqt-qasm", PLAN, {
        "mqt_standard_operation_qasm.cpp": adapter.HARNESS_HASHES["mqt_standard_operation_qasm.cpp"],
    })
    entry.validate(public)
    entry.validate(private)
    assert PLAN == before
    assert private["test_command"] == PLAN["test_command"]
    assert "ORACLE_VARIANT" not in json.dumps(public) + json.dumps(private)
    assert "/harnesses/" not in json.dumps(public)
    assert "test_command" not in public
    assert "oracle-qasm" in scratch
    for name in PLAN["real_translation_units"]:
        assert name in public["forms"][0]["probe"]["command"]
        assert all(name in form["build"][0] for form in private["forms"])
    assert all("-fsyntax-only" in form["probe"]["command"] for form in private["forms"])
    assert [form["name"] for form in private["forms"]] == ["separate-qasm", "consolidated-qasm"]
    assert "-DMQT_ORACLE_CONSOLIDATED_QASM" not in private["forms"][0]["build"][0]
    assert "-DMQT_ORACLE_CONSOLIDATED_QASM" in private["forms"][1]["build"][0]


@pytest.mark.parametrize("shape", ["separate", "consolidated", "both"])
@pytest.mark.parametrize("label", ["start", "golden", "candidate"])
def test_actual_mqt_forms_selected_by_compiler_only(tmp_path, monkeypatch, shape, label):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler required for synthetic interface test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORACLE_VARIANT", label)
    include = tmp_path / "include/operations"
    include.mkdir(parents=True)
    methods = ""
    if shape in {"separate", "both"}:
        methods += "int separate() const { return 7; }\n"
    if shape in {"consolidated", "both"}:
        methods += "int consolidated() const { return 7; }\n"
    (include / "StandardOperation.hpp").write_text("struct Operation {" + methods + "};\n")
    for name in PLAN["real_translation_units"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "operations/StandardOperation.hpp"\n')
    harness = tmp_path / "harness.cpp"
    harness.write_text(
        '#include "operations/StandardOperation.hpp"\n#include <iostream>\nint main() { Operation op;\n'
        "#ifdef MQT_ORACLE_CONSOLIDATED_QASM\nint n = op.consolidated();\n"
        "#else\nint n = op.separate();\n#endif\n"
        'if (n != 7) return 1; std::cout << "38 39\\n"; }\n')
    _, private, _ = adapter.specifications("mqt-qasm", PLAN, {})
    for form in private["forms"]:
        def local(command):
            return command.replace("g++-11", shlex.quote(compiler)).replace(
                adapter.ROOT + "/harnesses/mqt_standard_operation_qasm.cpp", str(harness))
        form["probe"]["command"] = local(form["probe"]["command"])
        form["build"] = [local(command) for command in form["build"]]
    selection = entry.prepare(private, tmp_path)
    assert selection["selected"] == {
        "separate": ["separate-qasm"], "consolidated": ["consolidated-qasm"],
        "both": ["separate-qasm", "consolidated-qasm"],
    }[shape]
    assert entry.run_assertions(private, tmp_path) == 0
    assert file_hash(harness)  # The selected builds did not edit the fixture.
