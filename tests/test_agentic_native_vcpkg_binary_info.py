"""Original-plan byte guards and synthetic transport tests, not task calibration."""
import copy
import json
from pathlib import Path
import shlex
import shutil
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_api as adapter, native_api_entry as entry, native_lane
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from test_agentic_native_dependency_closure import context, oracle, repository


FIXTURE = Path(__file__).parent / "fixtures/native-vcpkg91"
PLAN = json.loads((FIXTURE / "original-plan.json").read_text())
PROFILE = "vcpkg-binary-info"
HARNESS = "vcpkg_binary_package_info.cpp"


@pytest.fixture
def binary_fixture(tmp_path, monkeypatch):
    plan = copy.deepcopy(PLAN)
    repo = tmp_path / "candidate"
    pin = repository(repo, {"public.hpp": "// synthetic\n", "tests/original.cpp": "// original\n"})
    ctx = context(tmp_path, repo, plan)
    task = {"id": "synthetic-native-binary-info", "repo": plan["repo"], "start_sha": pin}
    shutil.copyfile(FIXTURE / HARNESS, ctx.legacy / "native/harnesses" / HARNESS)
    monkeypatch.setattr(adapter, "source_dependencies", lambda *args: ([], []))

    def copy_asset(source, category):
        destination = ctx.output / category / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return {"path": str(destination), "sha256": file_hash(destination)}

    ctx.copy_asset = copy_asset
    return ctx, task, plan


def test_original_binary_info_plan_and_harness_are_hash_bound(binary_fixture):
    ctx, task, plan = binary_fixture
    assert canonical_hash(plan) == "e4510b470fb2a1633c3ffba71180408347af02c78652b5dc1ebc6a9514ed2c96"
    assert file_hash(FIXTURE / HARNESS) == "c494c5ee0dde8e95a1b6613758752b11ae35f4f02912842ee3d20fa5f536dc3c"
    assert adapter.attested(ctx, task, plan) == (
        PROFILE, plan, {HARNESS: file_hash(FIXTURE / HARNESS)})


@pytest.mark.parametrize("field", [
    "configure_command", "build_command", "test_command", "real_translation_units",
    "docker_image", "build_jobs", "timeout_seconds",
])
def test_binary_info_plan_drift_rejects_before_packaging(binary_fixture, field):
    ctx, task, plan = binary_fixture
    value = plan[field]
    plan[field] = value + 1 if type(value) is int else value[:-1] if isinstance(value, list) else value + " "
    with pytest.raises(ContractError, match="complete original"):
        adapter.attested(ctx, task, plan)
    assert ctx.bundles == []


def test_binary_info_original_harness_drift_rejects(binary_fixture):
    ctx, task, plan = binary_fixture
    (ctx.legacy / "native/harnesses" / HARNESS).write_text("int main() { return 0; }\n")
    with pytest.raises(ContractError, match="harness changed"):
        adapter.attested(ctx, task, plan)


def test_public_build_covers_all_eleven_production_units_and_same_fmt():
    public, grader, scratch = adapter.specifications(PROFILE, PLAN, {})
    assert len(public["forms"]) == len(grader["forms"]) == 1
    commands = public["forms"][0]["probe"]["command"].split("; ")[1:]
    assert len(commands) == 12 and not public["forms"][0].get("build")
    sources = PLAN["real_translation_units"] + ["build-native-api-deps/fmt9/src/format.cc"]
    for index, (source, command) in enumerate(zip(sources, commands)):
        arguments = shlex.split(command)
        assert arguments[0] == "g++-11"
        assert arguments[-4:] == ["-c", source, "-o", f"build-native-public/production-{index}.o"]
        assert all(flag in arguments for flag in (
            "-std=c++17", "-O0", "-g", "--coverage", "-ffunction-sections", "-fdata-sections", "-fpermissive"))
        assert "-include" in arguments and "vcpkg/base/fmt.h" in arguments
        assert source in public["forms"][0]["probe"]["command"]
    assert "/harnesses/" not in json.dumps(public)
    assert "package-abi" not in json.dumps(public)
    assert all(name not in public for name in ("test_command", "file_bindings", "expected_tests"))
    assert "oracle-info" in scratch
    assert "ORACLE_VARIANT" not in json.dumps(public) + json.dumps(grader)


def test_original_private_build_only_relocates_same_fmt_source():
    public, grader, _ = adapter.specifications(PROFILE, PLAN, {})
    expected = PLAN["build_command"].replace("{root}", adapter.ROOT)
    expected = expected.replace("build-oracle/_deps/fmt-src", "build-native-api-deps/fmt9")
    assert not grader["forms"][0].get("build")
    assert grader["forms"][0]["probe"] == {"command": expected}
    assert grader["test_command"] == PLAN["test_command"]
    assert grader["test_command"].endswith("test \"$out\" = 'package-abi 1.2.3'")
    for spec in (public, grader):
        assert spec["setup"][0].startswith(PLAN["configure_command"] + " ")
        assert spec["setup"][0][len(PLAN["configure_command"]):] == (
            " -DFETCHCONTENT_SOURCE_DIR_FMT=$PWD/build-native-api-deps/fmt9"
            " -DFETCHCONTENT_SOURCE_DIR_CMAKERC=$PWD/build-native-api-deps/cmrc")
        assert [guard["path"] for guard in spec["source_guards"]] == [
            "cmake/Findfmt.cmake", "cmake/FindCMakeRC.cmake"]
        entry.validate(spec)


def test_native_recipe_retains_private_id_golden_and_original_test_bytes(binary_fixture):
    ctx, task, plan = binary_fixture
    before = (ctx.legacy / "native/harnesses" / HARNESS).read_bytes()
    recipe = native_lane.migrate(ctx, task, oracle(plan))
    assert recipe.grading["commands"][0]["expected_tests"] == ["fixture assertion"]
    assert recipe.grading["golden_patch"] == {"path": "/fixture/patch", "sha256": "c" * 64}
    assert recipe.grading["immutable_paths"] == ["tests/public.cpp"]
    assert recipe.start_sha == task["start_sha"] and recipe.cpus == 4 and recipe.memory_mb == 8192
    assert recipe.command_timeout_seconds == PLAN["timeout_seconds"]
    public_ref = next(a for a in recipe.assets if a["target"] == adapter.PUBLIC)
    grader_ref = next(a for a in recipe.assets if a["target"] == adapter.PRIVATE)
    assert public_ref["visibility"] == "developer" and grader_ref["visibility"] == "grader"
    public = json.loads(Path(public_ref["path"]).read_text())
    grader = json.loads(Path(grader_ref["path"]).read_text())
    assert "/harnesses/" not in json.dumps(public)
    assert grader["file_bindings"] == [{
        "path": adapter.ROOT + "/harnesses/" + HARNESS, "sha256": file_hash(FIXTURE / HARNESS)}]
    assert (ctx.legacy / "native/harnesses" / HARNESS).read_bytes() == before
    assert ctx.bundles and all(b["role"] == "grader" for b in ctx.bundles)


def test_binary_info_reuses_only_fmt9_and_cmrc_with_pinned_receipts(tmp_path, monkeypatch):
    import reviewbench_maintainability.agentic.assets as assets

    references, descriptors = {}, {}
    for index, (name, pin) in enumerate((("fmt9", adapter.FMT9_SHA512), ("cmrc", adapter.CMRC_SHA512))):
        ref = {"path": str(tmp_path / (name + ".json")), "sha256": str(index + 1) * 64}
        descriptor = {
            "source_kind": "dependency", "files": 12, "archive": {"input_bytes": 2048},
            "tree_sha256": str(index + 3) * 64,
        }
        receipt = {
            "status": "source_ready", "source_pin": "sha512:" + pin, "asset": ref,
            "source_tree_sha256": descriptor["tree_sha256"],
        }
        path = tmp_path / (name + "-receipt.json")
        path.write_text(json.dumps(receipt))
        references[name] = {**receipt, "receipt": {"path": str(path), "sha256": file_hash(path)}}
        descriptors[ref["path"]] = descriptor
    references["fmt11"] = {"deliberately_invalid_unneeded_asset": True}
    ctx = SimpleNamespace(output=tmp_path, config={"migration": {"native_api_sources": references}})
    monkeypatch.setattr(assets, "asset_descriptor", lambda ref, _: descriptors[ref["path"]])
    attached, copies = adapter.source_dependencies(ctx, {}, PROFILE)
    assert [a["target"] for a in attached] == [adapter.DEPS + "/fmt9", adapter.DEPS + "/cmrc"]
    assert copies == [
        {"source": adapter.DEPS + "/" + name, "destination": "build-native-api-deps/" + name}
        for name in ("fmt9", "cmrc")]
    references["fmt9"]["source_pin"] = "sha512:" + "0" * 128
    with pytest.raises(ContractError):
        adapter.source_dependencies(ctx, {}, PROFILE)


@pytest.mark.parametrize("bad_unit", [None, 0, 5, 10])
def test_real_compiler_public_projection_rejects_any_bad_current_unit(tmp_path, monkeypatch, bad_unit):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("synthetic current-source projection fixture needs a local C++ compiler")
    public, _, _ = adapter.specifications(PROFILE, PLAN, {})
    # This synthetic fixture tests the projected compiler input coverage; it is
    # not a substitute for the original GCC11/CMake/whole-task calibration.
    public["setup"] = ["mkdir -p build-native-public"]
    public["source_copies"] = []
    for guard in public["source_guards"]:
        path = tmp_path / guard["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(guard["required_literals"]))
    header = tmp_path / "include/vcpkg/base/fmt.h"
    header.parent.mkdir(parents=True)
    header.write_text("// synthetic forced include\n")
    sources = PLAN["real_translation_units"] + ["build-native-api-deps/fmt9/src/format.cc"]
    for index, source in enumerate(sources):
        path = tmp_path / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is invalid C++\n" if index == bad_unit else
                        f"int synthetic_current_unit_{index}() {{ return {index}; }}\n")
    form = public["forms"][0]
    form["probe"]["command"] = form["probe"]["command"].replace("g++-11 ", shlex.quote(compiler) + " ")
    monkeypatch.chdir(tmp_path)
    if bad_unit is None:
        result = entry.prepare(public, tmp_path)
        assert result["selected"] == ["production"] and result["builds_completed"]
        assert len(list((tmp_path / "build-native-public").glob("*.o"))) == 12
    else:
        with pytest.raises(ValueError, match="no frozen harness API"):
            entry.prepare(public, tmp_path)
    assert not (tmp_path / "build-native-api-grader").exists()


@pytest.mark.parametrize("value", ["package-abi 1.2.3", "wrong-abi 1.2.3"])
def test_single_form_build_once_then_unchanged_assertion_without_reselection(tmp_path, monkeypatch, value):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("synthetic single-form execution fixture needs a local C++ compiler")
    _, grader, _ = adapter.specifications(PROFILE, PLAN, {})
    # A tiny standalone fixture exercises this new single-build runner shape,
    # not original vcpkg source or its GCC11 calibration.
    grader["setup"] = ["mkdir -p oracle-info"]
    grader["source_guards"] = []
    grader["source_copies"] = []
    source = tmp_path / "synthetic.cpp"
    source.write_text('#include <iostream>\nint main() { std::cout << "' + value + '\\n"; }\n')
    grader["forms"][0]["probe"]["command"] = (
        shlex.quote(compiler) + " synthetic.cpp -o oracle-info/oracle")
    monkeypatch.chdir(tmp_path)
    result = entry.prepare(grader, tmp_path)
    assert result["selected"] == ["original-binary-info"] and result["builds_completed"]
    assert entry.run_assertions(grader, tmp_path) == (0 if value.startswith("package-abi") else 1)
    state = tmp_path / "build-native-api-grader"
    outcome = json.loads((state / "assertions.json").read_text())
    assert len(outcome["runs"]) == 1 and outcome["runs"][0]["command"] == PLAN["test_command"]
    assert len(list(state.glob("*/probe/command.json"))) == 1
    assert not list(state.glob("*/build-*/command.json"))
