"""Synthetic compatibility/transaction tests; never task calibration proofs."""
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import shutil
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_api as adapter, native_api_entry as entry, native_lane
from agentic_runtime_migration.native_api_sources import unpack_source, header_closure
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from test_agentic_native_dependency_closure import context, oracle, repository
from test_agentic_native_api_inputs import ORIGINAL_PLANS


@pytest.fixture(params=ORIGINAL_PLANS, ids=lambda plan: str(plan["input_index"]))
def native_fixture(request, tmp_path, monkeypatch):
    plan = copy.deepcopy(request.param)
    repo = tmp_path / "candidate"
    pin = repository(repo, {"public.hpp": "// public fixture\n", "tests/public.cpp": "// original test\n"})
    ctx = context(tmp_path, repo, plan)
    task = {"id": "native-api-fixture-" + str(plan["input_index"]), "repo": plan["repo"], "start_sha": pin}
    hashes = dict(adapter.HARNESS_HASHES)
    for name in set(re.findall(r"/harnesses/([A-Za-z0-9_.-]+)", plan["build_command"])):
        source = ctx.legacy / "native/harnesses" / name
        source.write_text("// PRIVATE_SYNTHETIC_ASSERTIONS " + name + "\n")
        hashes[name] = file_hash(source)
    monkeypatch.setattr(adapter, "HARNESS_HASHES", hashes)
    monkeypatch.setattr(adapter, "source_dependencies", lambda *args: ([], []))
    monkeypatch.setattr(adapter, "date_submodule", lambda *args: [
        {"path": "externals/date", "commit": "8" * 40,
         "asset": {"path": "/fixture/date.json", "sha256": "d" * 64}}])

    def copy_asset(source, category):
        destination = ctx.output / category / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return {"path": str(destination), "sha256": file_hash(destination)}
    ctx.copy_asset = copy_asset
    return ctx, task, plan


def test_all_six_original_operational_plans_are_hash_bound():
    assert len(ORIGINAL_PLANS) == 6
    assert {plan["input_index"] for plan in ORIGINAL_PLANS} == {1, 2, 17, 48, 92, 100}
    for plan in ORIGINAL_PLANS:
        assert canonical_hash(plan) == adapter.PROFILES[(plan["repo"], plan["input_index"])][1]


def test_six_recipes_preserve_oracle_and_separate_public_build(native_fixture):
    ctx, task, plan = native_fixture
    before = copy.deepcopy(plan)
    recipe = native_lane.migrate(ctx, task, oracle(plan))
    assert plan == before
    assert recipe.start_sha == task["start_sha"]
    assert recipe.grading["multifile_build"]
    assert recipe.grading["golden_patch"] == {"path": "/fixture/patch", "sha256": "c" * 64}
    assert recipe.grading["commands"][0]["expected_tests"] == ["fixture assertion"]
    assert recipe.grading["immutable_paths"] == ["tests/public.cpp"]
    assert "--phase run" in recipe.grading["commands"][0]["command"]
    public_asset = next(asset for asset in recipe.assets if asset["target"] == adapter.PUBLIC)
    private_asset = next(asset for asset in recipe.assets if asset["target"] == adapter.PRIVATE)
    assert public_asset["visibility"] == "developer" and private_asset["visibility"] == "grader"
    public, private = (json.loads(Path(asset["path"]).read_text()) for asset in (public_asset, private_asset))
    assert private["test_command"] == plan["test_command"]
    assert len(private["forms"]) == 2
    assert "ORACLE_VARIANT" not in json.dumps(public) + json.dumps(private)
    assert "/harnesses/" not in json.dumps(public)
    assert "test_command" not in public and "file_bindings" not in public
    for asset in recipe.assets:
        if asset["visibility"] == "developer" and asset.get("kind") == "file":
            assert "PRIVATE_SYNTHETIC_ASSERTIONS" not in Path(asset["path"]).read_text()
    assert all(ctx.bundles[i]["role"] == "grader" for i in range(len(ctx.bundles)))
    record = json.loads(next((ctx.output / "native-api-admission").glob("*.json")).read_text())
    assert record["source_changes"] == record["test_changes"] == record["golden_changes"] == []
    assert set(private["file_bindings"][i]["sha256"] for i in range(len(private["file_bindings"]))) == set(
        record["original_harness_sha256"].values())
    if plan["input_index"] == 100:
        assert recipe.submodules[0]["asset"] and not recipe.submodules[0].get("omit")


@pytest.mark.parametrize("field", ["configure_command", "build_command", "test_command", "timeout_seconds",
                                  "build_jobs", "real_translation_units"])
def test_each_original_plan_field_drift_fails_before_execution(native_fixture, field):
    ctx, task, plan = native_fixture
    if field in {"timeout_seconds", "build_jobs"}:
        plan[field] += 1
    elif field == "real_translation_units":
        plan[field] = []
    else:
        plan[field] = (plan[field] or "") + "; true"
    with pytest.raises(ContractError, match="complete original"):
        adapter.attested(ctx, task, plan)


def test_assertion_byte_drift_is_not_a_new_compatible_form(native_fixture):
    ctx, task, plan = native_fixture
    source = next((ctx.legacy / "native/harnesses").glob("*"))
    source.write_text("removed assertion\n")
    with pytest.raises(ContractError, match="harness changed"):
        adapter.attested(ctx, task, plan)


def test_other_native_tasks_remain_outside_the_adapter():
    assert adapter.attested(None, {"repo": "AutoPas/AutoPas"}, {"input_index": 3}) is None
    assert adapter.attested(None, {"repo": "microsoft/vcpkg-tool"}, {"input_index": 93}) is None
    assert adapter.attested(None, {"repo": "uliegecsm/kokkos-execution"}, {"input_index": 126}) is None


def test_original_build_graph_and_public_options_are_retained():
    for plan in ORIGINAL_PLANS:
        profile = adapter.PROFILES[(plan["repo"], plan["input_index"])][0]
        public, private, _ = adapter.specifications(profile, plan, {})
        text = json.dumps(private)
        for source in plan["real_translation_units"]:
            # CMake builds the complete AutoPas repository library; inline
            # public headers enter through the unmodified original harness.
            if source.endswith((".cpp", ".cc")) and plan["input_index"] not in {1}:
                assert source in text
        flags_text = text + json.dumps(public)
        assert "-O0" in flags_text and "-g" in flags_text and "--coverage" in flags_text
        assert "g++-11" in text
        if plan["input_index"] == 1:
            assert "-DAUTOPAS_BUILD_TESTS=OFF" in json.dumps(public)  # Exact original setting.
            assert "-DAUTOPAS_STANDALONE_BUILD=OFF" in json.dumps(public)
            assert all("-DAUTOPAS_ORACLE_HARNESS=" in form["build"][0] for form in private["forms"])
            assert all("--target autopas_traversal_oracle -j2" in form["build"][0] for form in private["forms"])
        if plan["input_index"] == 92:
            assert "-DBUILD_TESTING=OFF" in public["setup"][0]
            assert "-DVCPKG_DEVELOPMENT_WARNINGS=OFF" in public["setup"][0]
            assert "curl" not in json.dumps(public) + text


def compiler_fixture(tmp_path, monkeypatch, shape="both", second=7):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("synthetic compile-only integration fixture needs a local C++ compiler")
    monkeypatch.chdir(tmp_path)
    header = ""
    if shape in {"both", "old"}:
        header += "inline int old_value() { return 7; }\n"
    if shape in {"both", "new"}:
        header += f"inline int new_value() {{ return {second}; }}\n"
    (tmp_path / "public.hpp").write_text(header)
    (tmp_path / "assertions.cpp").write_text(
        '#include "public.hpp"\n#include <iostream>\nint main() {\n'
        "#ifdef CURRENT_NEW_API\nstd::cout << new_value();\n"
        "#else\nstd::cout << old_value();\n#endif\nstd::cout << '\\n'; }\n")
    spec = adapter._spec("grader", 20)
    spec["test_command"] = 'set -e; out=$(./oracle-api/oracle); printf \'%s\\n\' "$out"; test "$out" = 7'
    for name, flags in (("old-interface", ""), ("new-interface", "-DCURRENT_NEW_API")):
        command = shlex.quote(compiler) + " -std=c++17 -O0 " + flags + " assertions.cpp"
        spec["forms"].append({"name": name, "probe": {"command": command + " -fsyntax-only"},
                              "build": ["mkdir -p oracle-api && " + command + " -o oracle-api/oracle"],
                              "executable": "oracle-api/oracle"})
    entry.validate(spec)
    return spec


@pytest.mark.parametrize("shape", ["old", "new", "both"])
@pytest.mark.parametrize("reference_label", ["start", "golden", "unrelated"])
def test_real_compiler_selects_only_current_api_not_reference_label(tmp_path, monkeypatch, shape, reference_label):
    spec = compiler_fixture(tmp_path, monkeypatch, shape)
    monkeypatch.setenv("ORACLE_VARIANT", reference_label)
    selection = entry.prepare(spec, tmp_path)
    expected = {"old": ["old-interface"], "new": ["new-interface"],
                "both": ["old-interface", "new-interface"]}[shape]
    assert selection["selected"] == expected
    assert entry.run_assertions(spec, tmp_path) == 0
    outcome = json.loads((tmp_path / "build-native-api-grader/assertions.json").read_text())
    assert outcome["selected"] == expected and len(outcome["runs"]) == len(expected)
    assert all(r["returncode"] == 0 and r["command"] == spec["test_command"] for r in outcome["runs"])


def test_all_compatible_forms_run_and_failure_is_not_fallback(tmp_path, monkeypatch):
    spec = compiler_fixture(tmp_path, monkeypatch, second=3)
    selection = entry.prepare(spec, tmp_path)
    assert selection["selected"] == ["old-interface", "new-interface"]
    assert entry.run_assertions(spec, tmp_path) == 1
    outcome = json.loads((tmp_path / "build-native-api-grader/assertions.json").read_text())
    assert [r["returncode"] for r in outcome["runs"]] == [0, 1]
    assert not outcome["success"]
    assert len(list((tmp_path / "build-native-api-grader").glob("*/probe"))) == 2


def test_first_assertion_failure_does_not_skip_other_selected_form(tmp_path, monkeypatch):
    spec = compiler_fixture(tmp_path, monkeypatch, second=3)
    spec["forms"].reverse()
    entry.prepare(spec, tmp_path)
    assert entry.run_assertions(spec, tmp_path) == 1
    outcome = json.loads((tmp_path / "build-native-api-grader/assertions.json").read_text())
    assert [r["returncode"] for r in outcome["runs"]] == [1, 0]


def test_compatible_form_build_failure_does_not_change_selection(tmp_path, monkeypatch):
    spec = compiler_fixture(tmp_path, monkeypatch)
    spec["forms"][1]["build"] = ["exit 9"]
    with pytest.raises(ValueError, match="mandatory build"):
        entry.prepare(spec, tmp_path)
    selection = json.loads((tmp_path / "build-native-api-grader/selection.json").read_text())
    assert selection["selected"] == ["old-interface", "new-interface"]
    assert not selection["builds_completed"]
    with pytest.raises(ValueError, match="complete hash-bound"):
        entry.run_assertions(spec, tmp_path)


def test_attempt_is_not_silently_rebuilt_or_reprobed(tmp_path, monkeypatch):
    spec = compiler_fixture(tmp_path, monkeypatch, "old")
    entry.prepare(spec, tmp_path)
    with pytest.raises(ValueError, match="attempt already exists"):
        entry.prepare(spec, tmp_path)


@pytest.mark.parametrize("tamper", ["spec", "binary"])
def test_assertions_require_exact_fresh_compiled_binding(tmp_path, monkeypatch, tamper):
    spec = compiler_fixture(tmp_path, monkeypatch, "old")
    entry.prepare(spec, tmp_path)
    if tamper == "spec":
        spec["test_command"] = "true"
    else:
        (tmp_path / "build-native-api-grader/old-interface/compiled-executable").write_text("fake\n")
    with pytest.raises(ValueError):
        entry.run_assertions(spec, tmp_path)


def test_no_compatible_api_fails_closed_without_assertions(tmp_path, monkeypatch):
    spec = compiler_fixture(tmp_path, monkeypatch, "neither")
    with pytest.raises(ValueError, match="no frozen harness API"):
        entry.prepare(spec, tmp_path)
    assert not (tmp_path / "build-native-api-grader/assertions.json").exists()


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(state_directory="../old-build"),
    lambda s: s.update(forms=[]),
    lambda s: s.update(forms=s["forms"] * 2),
    lambda s: s.update(timeout_seconds=0),
    lambda s: s.update(max_log_bytes=2**40),
    lambda s: s["forms"][0]["probe"].update(command='test "$ORACLE_VARIANT" = golden'),
    lambda s: s.update(kind="public", state_directory="build-native-api-public"),
])
def test_spec_rejects_unbounded_or_reference_selected_work(tmp_path, monkeypatch, mutate):
    spec = compiler_fixture(tmp_path, monkeypatch)
    mutate(spec)
    with pytest.raises(ValueError):
        entry.validate(spec)


@pytest.mark.parametrize("prefix", ["", "ccache "])
def test_compilation_database_probe_preserves_flags_but_never_links(tmp_path, prefix):
    source = tmp_path / "private.cpp"
    database = tmp_path / "compile_commands.json"
    command = prefix + "g++-11 -std=c++17 -O0 -g --coverage -Iinclude -DFORM=1 -MMD -MF dep.d -o private.o -c " + str(source)
    database.write_text(json.dumps([{"directory": str(tmp_path), "file": str(source), "command": command}]))
    probe = entry.syntax_command({"compile_database": "compile_commands.json", "source": str(source)}, tmp_path)
    words = shlex.split(probe)
    assert all(flag in words for flag in ["-std=c++17", "-O0", "-g", "--coverage", "-Iinclude", "-DFORM=1"])
    assert "-fsyntax-only" in words and "-o" not in words and "-c" not in words and "-MF" not in words
    database.write_text(json.dumps([{"directory": str(tmp_path), "file": str(source), "command": command}] * 2))
    with pytest.raises(ValueError, match="unambiguous"):
        entry.syntax_command({"compile_database": "compile_commands.json", "source": str(source)}, tmp_path)


def test_compiler_timeout_is_fatal_not_an_incompatible_form(tmp_path):
    spec = adapter._spec("grader", 1)
    with pytest.raises(ValueError, match="infrastructure"):
        entry.run_logged("sleep 3", tmp_path, "probe", spec, echo=False)
    receipt = json.loads((tmp_path / "probe/command.json").read_text())
    assert receipt["timed_out"]


def test_compiler_log_limit_is_fatal_not_a_successful_probe(tmp_path):
    spec = adapter._spec("grader", 2)
    spec["max_log_bytes"] = 1024
    with pytest.raises(ValueError, match="infrastructure"):
        entry.run_logged("printf '%2048s' x", tmp_path, "probe", spec, echo=False)
    assert json.loads((tmp_path / "probe/command.json").read_text())["log_limit_exceeded"]


@pytest.mark.parametrize("name", ["../escape", "/absolute", "source/../../escape"])
def test_source_archive_cannot_escape(tmp_path, name):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        item = tarfile.TarInfo(name)
        item.size = 1
        stream.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(ContractError):
        unpack_source(archive, tmp_path / "source")


def test_source_archive_preserves_tests_and_rejects_duplicate_members(tmp_path):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name in ["release/include/public.h", "release/tests/original.cpp"]:
            item = tarfile.TarInfo(name)
            item.size = 1
            stream.addfile(item, io.BytesIO(b"x"))
    unpack_source(archive, tmp_path / "source")
    assert (tmp_path / "source/tests/original.cpp").read_bytes() == b"x"
    with tarfile.open(archive, "w:gz") as stream:
        for _ in range(2):
            item = tarfile.TarInfo("release/header.h")
            item.size = 1
            stream.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(ContractError, match="duplicate"):
        unpack_source(archive, tmp_path / "other")


def test_fmt11_requires_the_complete_original_header_closure(tmp_path):
    original, fresh = tmp_path / "original", tmp_path / "fresh"
    for directory in (original, fresh):
        directory.mkdir()
        for index in range(5):
            (directory / f"header{index}.h").write_text("public header\n")
    assert len(header_closure(fresh, original)) == 5
    (fresh / "header4.h").write_text("different version\n")
    with pytest.raises(ContractError, match="original header closure"):
        header_closure(fresh, original)


def test_zip_source_is_pinned_and_preserves_complete_source(tmp_path):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("source/include/header.h", "header\n")
        stream.writestr("source/tests/original.cpp", "test\n")
    spec = {"source_zips": [{"path": "source.zip", "sha256": file_hash(archive), "destination": "dependency"}]}
    entry.dependencies(spec, tmp_path)
    assert (tmp_path / "dependency/tests/original.cpp").read_text() == "test\n"
    archive.write_bytes(b"different pin")
    with pytest.raises(ValueError, match="pinned bundled"):
        entry.dependencies(spec, tmp_path)


def test_dependency_guard_is_not_a_production_implementation_selector(tmp_path):
    (tmp_path / "dependency.cmake").write_text("version 9.1.0 SHA512=abc")
    spec = {"source_guards": [{"path": "dependency.cmake", "required_literals": ["9.1.0", "SHA512=abc"]}]}
    entry.dependencies(spec, tmp_path)
    (tmp_path / "dependency.cmake").write_text("version 11 SHA512=def")
    with pytest.raises(ValueError, match="dependency declaration"):
        entry.dependencies(spec, tmp_path)


def test_prepared_dependency_requires_receipt_tree_and_original_source_pin(tmp_path, monkeypatch):
    import reviewbench_maintainability.agentic.assets as assets

    descriptor = {"source_kind": "dependency", "files": 12, "git_commit":
                  "00c8c7b0d5c722d2212568d915a39ea73b08b973", "tree_sha256": "a" * 64,
                  "archive": {"input_bytes": 2048}}
    reference = {"path": "/fixture/source.json", "sha256": "c" * 64}
    receipt = {"status": "source_ready", "source_pin": "git:" + descriptor["git_commit"],
               "asset": reference, "source_tree_sha256": descriptor["tree_sha256"]}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    binding = {key: receipt[key] for key in ("source_pin", "asset", "source_tree_sha256")}
    binding["receipt"] = {"path": str(path), "sha256": file_hash(path)}
    ctx = SimpleNamespace(output=tmp_path, config={"migration": {"native_api_sources": {"fast-float": binding}}})
    monkeypatch.setattr(assets, "asset_descriptor", lambda *args: descriptor)
    attached, copies = adapter.source_dependencies(ctx, {}, "numere-formats")
    assert attached[0]["visibility"] == "developer" and not copies
    descriptor["tree_sha256"] = "b" * 64
    with pytest.raises(ContractError, match="prepared archive tree"):
        adapter.source_dependencies(ctx, {}, "numere-formats")
    descriptor["tree_sha256"] = "a" * 64
    path.write_text(json.dumps({**receipt, "source_pin": "git:" + "d" * 40}))
    with pytest.raises(ContractError, match="hash-bound preparation receipt"):
        adapter.source_dependencies(ctx, {}, "numere-formats")


def test_lmms_public_link_probe_contains_no_frozen_assertions():
    assert "PlaybackState" in adapter.LMMS_LINK_PROBE
    assert "17" not in adapter.LMMS_LINK_PROBE and "cout" not in adapter.LMMS_LINK_PROBE
    assert "varyingPitch" not in adapter.LMMS_LINK_PROBE and "backwards" not in adapter.LMMS_LINK_PROBE
