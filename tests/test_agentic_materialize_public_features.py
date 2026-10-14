"""Public library builds reproduce current, explicitly bound test feature unions."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_overlay_api_entry as entry
from test_agentic_materialize_build_inputs import validated_fixture
from test_agentic_rust_image import output, ref
from test_agentic_rust_overlay_api import fixture as overlay_fixture, runner


def fixture(tmp_path, normal=None, development=None):
    manifest = tmp_path / "Cargo.toml"
    normal = normal if normal is not None else {"version": "3.2.24", "features": ["derive"]}
    development = development if development is not None else {
        "version": "3.2.24", "features": ["derive", "env"]}
    value = {"package": {"name": "original", "version": "0.1.0"},
             "dependencies": {"clap": normal}, "dev-dependencies": {"clap": development}}
    manifest.write_text(entry.tomli_w.dumps(value))
    public = {"schema_version": entry.SAME_TARGET_SCHEMA, "package_manifest": "Cargo.toml",
              "public_test_feature_dependencies": ["clap"]}
    return manifest, value, public


def test_exact_current_development_feature_delta_leaves_manifest_unchanged(tmp_path):
    path, value, public = fixture(tmp_path)
    before = path.read_bytes()
    assert entry.public_test_feature_args(public, tmp_path) == ["--features", "clap/env"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["missing", "removed_feature", "version", "path", "package",
                                   "optional", "default_features"])
def test_candidate_dependency_changes_are_not_repaired(tmp_path, change):
    path, value, public = fixture(tmp_path)
    if change == "missing":
        value.pop("dev-dependencies")
    elif change == "removed_feature":
        value["dev-dependencies"]["clap"]["features"] = ["derive"]
    elif change == "default_features":
        value["dependencies"]["clap"]["default-features"] = False
    elif change == "optional":
        value["dependencies"]["clap"]["optional"] = True
    else:
        value["dev-dependencies"]["clap"][change] = "different"
    path.write_text(entry.tomli_w.dumps(value))
    before = path.read_bytes()
    assert entry.public_test_feature_args(public, tmp_path) == []
    assert path.read_bytes() == before


def test_normal_features_and_simple_version_declarations_are_respected(tmp_path):
    path, value, public = fixture(tmp_path, normal="3.2.24")
    assert entry.public_test_feature_args(public, tmp_path) == ["--features", "clap/derive,clap/env"]
    value["dependencies"]["clap"] = deepcopy(value["dev-dependencies"]["clap"])
    path.write_text(entry.tomli_w.dumps(value))
    assert entry.public_test_feature_args(public, tmp_path) == []


@pytest.mark.parametrize("names", [None, "clap", ["clap", "clap"], ["../clap"], [True], ["a"] * 9])
def test_public_dependency_inventory_is_bounded_and_typed(tmp_path, names):
    *_, public = validated_fixture(tmp_path, 0o644)
    public["public_test_feature_dependencies"] = names
    with pytest.raises(entry.core.VariantError, match="feature inventory"):
        entry.validate(public)


def test_old_and_nonopted_profiles_do_not_read_any_manifest(tmp_path):
    assert entry.public_test_feature_args({}, tmp_path / "absent") == []
    assert entry.public_test_workspace_args({}, tmp_path / "absent") == []
    assert entry.public_test_feature_args({
        "schema_version": entry.SCHEMA, "public_test_feature_dependencies": ["clap"]
    }, tmp_path / "absent") == []


@pytest.mark.parametrize("private", [False, True])
def test_existing_execution_branch_keeps_private_command_exact(tmp_path, private):
    source, public, private_spec = overlay_fixture(tmp_path)
    path = source / public["package_manifest"]
    manifest = entry.tomllib.loads(path.read_text())
    manifest["dev-dependencies"] = {"dep": {"version": "0.1.0", "features": ["env"]}}
    path.write_text(entry.tomli_w.dumps(manifest))
    overlay_path = tmp_path / "overlay.json"
    overlay_path.write_text(json.dumps({
        "schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": public["task_id"],
        "manifests": [], "locks": [], "private_members": []}))
    public.update(schema_version=entry.SAME_TARGET_SCHEMA, overlay=ref(overlay_path),
                  feature_args=["--features", "askar_wallet"], overlay_paths=[],
                  locks=[public["locks"][0]], private_spec_sha256="0" * 64,
                  private_tool_sha256="0" * 64, public_test_feature_dependencies=["dep"],
                  public_command=["build", "--locked", "-p", public["package"],
                                  "--features", "askar_wallet", "--lib", "--message-format=json"])
    calls = []
    metadata_runner = runner(calls)

    def run(command, cwd, env, folder, label, deadline, limit):
        if "metadata" in command:
            return metadata_runner(command, cwd, env, folder, label, deadline, limit)
        calls.append(command)
        if private:
            assert command == [sys.executable, "/private-api.py", "--spec", "/private-api.json"]
            return output(command, cwd, "test original ... ok\n")
        assert command == [public["cargo"]["path"], "--offline", *public["public_command"],
                           "--features", "dep/env"]
        return output(command, cwd)

    before = entry.core.snapshot(source)
    result = entry.run_candidate(public, source, tmp_path / "work", private_spec if private else None,
                                 private_tool="/private-api.py", private_path="/private-api.json", run=run)
    assert result["exit_status"] == 0 and result["command_executions"] == 1 and len(calls) == 2
    assert entry.core.snapshot(source) == before
    assert ("public_test_dependency_feature_args" in result) == (not private)


def workspace_fixture(tmp_path):
    source = tmp_path / "workspace"
    (source / "app").mkdir(parents=True)
    (source / "addon").mkdir()
    path = source / "app/Cargo.toml"
    value = {"package": {"name": "app", "version": "0.1.0"},
             "dev-dependencies": {"addon": {"path": "../addon"}}}
    path.write_text(entry.tomli_w.dumps(value))
    (source / "addon/Cargo.toml").write_text('[package]\nname="addon"\nversion="0.1.0"\n')
    public = {"schema_version": entry.SAME_TARGET_SCHEMA, "package_manifest": "app/Cargo.toml",
              "public_test_workspace_dependencies": ["addon"]}
    return source, path, value, public


def test_current_path_test_dependency_becomes_library_peer_only(tmp_path):
    source, path, value, public = workspace_fixture(tmp_path)
    before = entry.core.snapshot(source)
    assert entry.public_test_workspace_args(public, source) == ["-p", "addon"]
    assert entry.core.snapshot(source) == before
    value["dev-dependencies"]["addon"]["features"] = ["second", "first"]
    path.write_text(entry.tomli_w.dumps(value))
    assert entry.public_test_workspace_args(public, source) == [
        "-p", "addon", "--features", "addon/first,addon/second"]


@pytest.mark.parametrize("change", ["absent", "registry", "outside", "package", "optional", "default_features"])
def test_changed_path_test_dependency_is_not_replaced_by_original(tmp_path, change):
    source, path, value, public = workspace_fixture(tmp_path)
    if change == "absent":
        value.pop("dev-dependencies")
    elif change == "registry":
        value["dev-dependencies"]["addon"] = {"version": "0.1.0"}
    elif change == "outside":
        value["dev-dependencies"]["addon"]["path"] = "../../outside"
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside/Cargo.toml").write_text('[package]\nname="addon"\nversion="0.1.0"\n')
    elif change == "package":
        value["dev-dependencies"]["addon"]["package"] = "candidate-changed"
    elif change == "optional":
        value["dev-dependencies"]["addon"]["optional"] = True
    else:
        value["dev-dependencies"]["addon"]["default-features"] = False
    path.write_text(entry.tomli_w.dumps(value))
    before = entry.core.snapshot(source)
    assert entry.public_test_workspace_args(public, source) == []
    assert entry.core.snapshot(source) == before
