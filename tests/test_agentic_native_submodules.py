"""Synthetic Git fixtures; these tests are not task calibration evidence."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_lane, native_submodules as adapter
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.snapshots import git, gitlinks
from test_agentic_native_dependency_closure import context, oracle, repository


def declarations(paths):
    return "".join(f'[submodule "{path}"]\n path = {path}\n'
                   f' url = https://example.invalid/{Path(path).name}.git\n' for path in paths)


@pytest.fixture
def fixture(tmp_path):
    nested = tmp_path / "nested"
    nested_pin = repository(nested, {"header.hpp": "// pinned nested header\n",
                                      "tests/nested.cpp": "// retained nested test\n"})
    dependency = tmp_path / "dependency"
    pin = repository(dependency, {
        ".gitmodules": declarations(["nested"]),
        ".gitattributes": "tests export-ignore\n",
        "public.hpp": "// pinned header\n", "tests/public.cpp": "// retained test\n",
    }, [("nested", nested_pin)])
    (dependency / "public.hpp").write_text("// dirty checkout is not an input\n")
    (dependency / "build").mkdir()
    (dependency / "build/old.o").write_bytes(b"historical compilation")
    paths = ("deps/nlohmann-json", "deps/range-v3", "deps/fmtlib")
    root = tmp_path / "root"
    sha = repository(root, {
        ".gitmodules": declarations(paths),
        "CMakeLists.txt": "# public project\n", "main.cpp": "int main() { return 0; }\n",
    }, [(path, pin) for path in paths])
    plan = {
        "repo": "argotorg/solidity", "input_index": 34, "docker_image": "fixture-native",
        "configure_command": "set -e; git submodule update --init deps/nlohmann-json deps/range-v3 deps/fmtlib; "
                             "cmake -S . -B build-oracle -DTESTS=OFF -DTOOLS=OFF",
        "build_command": "set -e; cmake --build build-oracle --target evmasm langutil -j12; "
                         "g++-11 main.cpp '{root}/harnesses/assertion.cpp' -o build-oracle/oracle",
        "test_command": "./build-oracle/oracle",
    }
    modules = [{**link, "omit": True, "reason": "original optional policy"} for link in gitlinks(root, sha)]
    ctx = context(tmp_path, root, plan, modules)
    locations = {path: {pin: str(dependency)} for path in paths}
    locations.update({path + "/nested": {nested_pin: str(nested)} for path in paths})
    ctx.config = {"migration": {"native_submodule_repositories": {plan["repo"]: locations}}}
    task = {"id": "native-pinned-fixture", "repo": plan["repo"], "start_sha": sha}
    return ctx, task, plan, dependency, pin


def test_nested_exact_sources_preserve_tests_and_public_build(fixture):
    ctx, task, plan, dependency, pin = fixture
    original = json.loads(json.dumps(plan))
    recipe = native_lane.migrate(ctx, task, oracle(plan))
    assert plan == original
    assert all(item["commit"] == pin and "omit" not in item for item in recipe.submodules)
    assert all(item["omit"] for item in ctx.modules(task))
    sources = [item for item in ctx.bundles if item["role"] == "submodule"]
    assert len(sources) == 3
    for source in sources:
        assert source["files"]["public.hpp"] == b"// pinned header\n"
        assert source["files"]["tests/public.cpp"] == b"// retained test\n"
        assert source["files"]["nested/tests/nested.cpp"] == b"// retained nested test\n"
        assert not any("build/" in name or ".git/" in name for name in source["files"])
    assert (dependency / "public.hpp").read_text() == "// dirty checkout is not an input\n"
    assert recipe.bootstrap[-1].endswith("cmake -S . -B build-oracle -DTESTS=OFF -DTOOLS=OFF")
    assert recipe.dev_checks == ["set -e; cmake --build build-oracle --target evmasm langutil -j4"]
    assert recipe.grading["commands"][0]["expected_tests"] == ["fixture assertion"]
    assert recipe.grading["commands"][0]["command"] == plan["test_command"]
    assert all(asset["visibility"] == "grader" for asset in recipe.assets)
    record = json.loads(next((ctx.output / "native-public-submodules").glob("*.json")).read_text())
    assert len(record["closure"]) == 6 and record["nested_gitlinks_complete"]
    assert record["source_changes"] == record["test_changes"] == []


def test_unknown_profile_keeps_original_blocker(fixture):
    ctx, task, plan, _, _ = fixture
    task["repo"] = plan["repo"] = "fixture/unsupported"
    (ctx.legacy / "native/plans.jsonl").write_text(json.dumps(plan) + "\n")
    with pytest.raises(ContractError, match="requires pinned submodule sources"):
        native_lane.migrate(ctx, task, oracle(plan))


@pytest.mark.parametrize("change", [
    lambda text: text.replace("--init ", "--remote --init "),
    lambda text: text.replace("set -e; ", "printf '"),
    lambda text: text.replace("; cmake", " --force; cmake"),
    lambda text: text + "; git submodule update --init other",
])
def test_literal_prefix_rejects_unaccounted_operations(fixture, change):
    ctx, task, plan, _, _ = fixture
    plan = {**plan, "configure_command": change(plan["configure_command"])}
    with pytest.raises(ContractError, match="literal|unsupported"):
        adapter.selected_paths(task, plan, gitlinks(ctx.repo(task), task["start_sha"]))


def test_catalog_pin_mismatch_is_not_overridden(fixture):
    ctx, task, plan, _, _ = fixture
    ctx.modules(task)[0]["commit"] = "a" * 40
    with pytest.raises(ContractError, match="catalog.*Git pins"):
        adapter.adapt(ctx, task, plan)


def test_missing_nested_pin_is_not_omitted(fixture):
    ctx, task, plan, _, _ = fixture
    configured = ctx.config["migration"]["native_submodule_repositories"][task["repo"]]
    del configured["deps/nlohmann-json/nested"]
    with pytest.raises(ContractError, match="missing: deps/nlohmann-json/nested@"):
        adapter.adapt(ctx, task, plan)
    assert not ctx.bundles


def test_missing_commit_in_configured_repo_is_not_head_fallback(fixture):
    ctx, task, plan, _, pin = fixture
    configured = ctx.config["migration"]["native_submodule_repositories"][task["repo"]]
    configured["deps/nlohmann-json"] = {"b" * 40: str(ctx.repo(task))}
    with pytest.raises(ContractError, match="exact submodule pin"):
        adapter.adapt(ctx, task, plan)
    assert not ctx.bundles


@pytest.mark.parametrize("limit,value", [("MAX_DEPTH", 1), ("MAX_MODULES", 1),
                                        ("MAX_FILES", 1), ("MAX_INPUT_BYTES", 1)])
def test_closure_is_bounded_before_archiving(fixture, monkeypatch, limit, value):
    ctx, task, plan, _, _ = fixture
    monkeypatch.setattr(adapter, limit, value)
    with pytest.raises(ContractError, match="bound"):
        adapter.adapt(ctx, task, plan)
    assert not ctx.bundles


@pytest.mark.parametrize("key", list(adapter.PROFILES))
def test_each_profile_changes_only_its_literal_submodule_prefix(key):
    profile = adapter.PROFILES[key]
    paths = profile.paths or ("third_party/one", "resources/second")
    links = [{"path": path, "commit": "a" * 40} for path in paths]
    plan = {"input_index": key[1], "build_command": "cmake --build build-oracle"}
    plan[profile.field] = profile.prefix + profile.operation + "; cmake -S . -B build-oracle"
    assert adapter.selected_paths({"repo": key[0]}, plan, links) == paths


def test_t8code_cache_is_fresh_public_build_not_general_cache_opt_in():
    task = {"repo": "DLR-AMR/t8code"}
    plan = {"input_index": 6, "configure_command": "dep='{cache}/deps'; ./bootstrap",
            "build_command": "dep='{cache}/deps'; make"}
    assert adapter.task_local_cache(task, plan)
    assert not adapter.task_local_cache(task, {**plan, "build_command": "{root}/cache/old.a"})
    assert not adapter.task_local_cache(task, {**plan, "env": {"PRIVATE": "input"}})
    assert not adapter.task_local_cache(task, {**plan, "configure_command": "dep='{cache}/deps'; cp {cache}/old.a ."})
    assert not adapter.task_local_cache({"repo": "other/repo"}, plan)


def test_transaction(fixture):
    """Run on baseline/modified/restored native_lane without editing the fork."""
    ctx, task, plan, _, _ = fixture
    lane = native_lane
    location = os.environ.get("NATIVE_SUBMODULES_LANE_FILE")
    if location:
        spec = importlib.util.spec_from_file_location("agentic_runtime_migration.transaction_lane", location)
        lane = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lane)
    expected = os.environ.get("NATIVE_SUBMODULES_EXPECT", "materialized")
    if expected == "blocked":
        with pytest.raises(ContractError, match="native build requires pinned submodule sources") as result:
            lane.migrate(ctx, task, oracle(plan))
        print("STATE=blocked; RESULT=" + str(result.value))
    else:
        recipe = lane.migrate(ctx, task, oracle(plan))
        assert len(recipe.submodules) == 3
        assert all("asset" in module and not module.get("omit") for module in recipe.submodules)
        assert recipe.grading["commands"][0]["expected_tests"] == ["fixture assertion"]
        print("STATE=materialized; ROOTS=3; NESTED=3; FIXED_IDS=1; SOURCE_EDITS=0")


def test_preparer_resolves_relative_repository_urls():
    from agentic_runtime_migration.native_submodules_prepare import public_url
    assert public_url("../child.git", "https://github.com/owner/parent.git") == "https://github.com/owner/child.git"
    assert public_url("../../other/child.git", "https://github.com/owner/parent.git") == "https://github.com/other/child.git"


@pytest.mark.parametrize("url", [
    "file:///tmp/source", "git@github.com:owner/repo.git", "ext::shell",
    "https://user:secret@github.com/owner/repo", "https://github.com/owner/repo?ref=main",
    "https://example.invalid/owner/repo",
])
def test_preparer_rejects_non_public_fetch_transport(url):
    from agentic_runtime_migration.native_submodules_prepare import public_url
    with pytest.raises(ContractError, match="public HTTPS"):
        public_url(url, "https://github.com/owner/parent.git")


def test_preparer_reuses_exact_local_git_without_network(fixture, tmp_path, monkeypatch):
    from agentic_runtime_migration.native_submodules_prepare import PinnedSources
    ctx, task, _, dependency, pin = fixture
    preparer = PinnedSources(tmp_path / "cache", tmp_path / "private-sources", tmp_path / "evidence")
    preparer.urls[str(ctx.repo(task).resolve())] = "https://github.com/fixture/root.git"
    monkeypatch.setattr(preparer, "fetch", lambda *args: pytest.fail("local exact pin must not be fetched"))
    result = preparer.resolve(ctx.repo(task), {"url": "https://github.com/fixture/dependency.git",
                                             "name": "deps/one"},
                              "deps/one", "deps/one", pin, {"deps/one": {pin: str(dependency)}})
    assert result == dependency
    assert preparer.events[-1]["kind"] == "reuse_exact_local_commit"


def test_promisor_missing_blob_fails_without_fetching_or_writing_shared_repo(fixture, tmp_path, monkeypatch):
    _, _, _, dependency, pin = fixture
    blob = git(dependency, "rev-parse", pin + ":public.hpp").decode().strip()
    (dependency / ".git/objects" / blob[:2] / blob[2:]).unlink()
    git(dependency, "config", "remote.origin.url", "https://example.invalid/no-fetch-allowed")
    git(dependency, "config", "remote.origin.promisor", "true")
    objects = dependency / ".git/objects"
    before = {str(path.relative_to(objects)): path.read_bytes() for path in objects.rglob("*") if path.is_file()}
    trace = tmp_path / "git-trace"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    with pytest.raises(ContractError, match="missing local blobs"):
        adapter.check_repository(dependency, pin)
    assert " fetch " not in trace.read_text()
    assert before == {str(path.relative_to(objects)): path.read_bytes()
                      for path in objects.rglob("*") if path.is_file()}


def test_readonly_pin_view_ignores_original_replace_refs(fixture, tmp_path):
    _, _, _, dependency, pin = fixture
    original_tree = adapter.readonly_git(dependency, "rev-parse", pin + "^{tree}")
    (dependency / "replacement.txt").write_text("unrelated replacement tree\n")
    git(dependency, "add", ".")
    git(dependency, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "replacement")
    replacement = git(dependency, "rev-parse", "HEAD").decode().strip()
    git(dependency, "replace", pin, replacement)
    assert adapter.readonly_git(dependency, "rev-parse", pin + "^{tree}") == original_tree
