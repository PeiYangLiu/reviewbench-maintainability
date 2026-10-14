from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.jvm_platforms import SCHEMA, SDK_TARGET, configuration, runtime_inputs, sdk_files
from reviewbench_maintainability.agentic.contracts import ContractError


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sdk(tmp_path):
    root = tmp_path / "sdk"
    platform = root / "platforms/android-35"
    platform.mkdir(parents=True)
    properties = platform / "source.properties"
    properties.write_text("AndroidVersion.ApiLevel=35\nPkg.Revision=2\n")
    with zipfile.ZipFile(platform / "android.jar", "w") as archive:
        archive.writestr("android/app/Activity.class", b"public API fixture")
        archive.writestr("android/os/Bundle.class", b"public API fixture")
    (root / "licenses").mkdir()
    (root / "licenses/android-sdk-license").write_text("retained license")
    # This link exists in the real retained extension root and must not be
    # imported when only genuine platform components are requested.
    (root / "cmdline-tools").symlink_to("/old/absent/sdk-tools")
    return {"root": str(root), "components": ["platforms/android-35", "licenses"],
            "properties_sha256": {"platforms/android-35/source.properties": sha(properties)}}


def context(tmp_path, rows):
    config = tmp_path / "platforms.json"
    config.write_text(json.dumps({"schema_version": SCHEMA, "tasks": rows}))
    calls = []
    def bundle(root, names, role):
        calls.append((root, names, role))
        descriptor = tmp_path / "descriptor.json"
        descriptor.write_text(json.dumps(names))
        return {"path": str(descriptor), "sha256": sha(descriptor)}
    ctx = SimpleNamespace(config={"migration": {"jvm_platforms": str(config)}},
                          tasks={"task": {"id": "task"}}, output=tmp_path / "output", bundle=bundle)
    ctx.image = lambda reference: (reference, {"path": "existing-audit", "sha256": "a" * 64})
    return ctx, config, calls


def test_only_explicit_real_sdk_components_are_packaged_and_the_source_is_unchanged(tmp_path):
    settings = sdk(tmp_path)
    ctx, _, calls = context(tmp_path, {"task": {"android_sdk": settings}})
    before = sha(Path(settings["root"]) / "platforms/android-35/android.jar")
    result = runtime_inputs(ctx, {"id": "task", "start_sha": "a" * 40})
    assert result["env"] == {"ANDROID_HOME": SDK_TARGET, "ANDROID_SDK_ROOT": SDK_TARGET}
    assert result["assets"][0]["visibility"] == "developer" and result["assets"][0]["kind"] == "archive"
    assert calls[0][2] == "toolchain" and not any("cmdline-tools" in name for name in calls[0][1])
    assert sha(Path(settings["root"]) / "platforms/android-35/android.jar") == before


def test_sdk_pin_and_api_identity_must_match(tmp_path):
    settings = sdk(tmp_path)
    properties = Path(settings["root"]) / "platforms/android-35/source.properties"
    properties.write_text("AndroidVersion.ApiLevel=34\n")
    with pytest.raises(ContractError, match="metadata differs"):
        sdk_files(settings)
    settings["properties_sha256"]["platforms/android-35/source.properties"] = sha(properties)
    with pytest.raises(ContractError, match="API metadata differ"):
        sdk_files(settings)


@pytest.mark.parametrize("component", ["..", "/tmp", "workspace", "cmdline-tools", "platforms/../secrets", "platforms/android-*"])
def test_arbitrary_directories_and_unresolved_component_patterns_are_not_sdk_inputs(tmp_path, component):
    settings = sdk(tmp_path)
    settings["components"] = [component]
    with pytest.raises(ContractError, match="component selection"):
        sdk_files(settings)


def test_sdk_selected_closure_rejects_linked_files_and_directories(tmp_path):
    settings = sdk(tmp_path)
    root = Path(settings["root"])
    link = root / "platforms/android-35/secret"
    link.symlink_to(tmp_path / "other")
    with pytest.raises(ContractError, match="linked or special"):
        sdk_files(settings)
    link.unlink()
    (tmp_path / "other").mkdir()
    link.symlink_to(tmp_path / "other", target_is_directory=True)
    with pytest.raises(ContractError, match="linked directory"):
        sdk_files(settings)


def test_directory_or_empty_jar_is_not_a_real_sdk_platform(tmp_path):
    settings = sdk(tmp_path)
    jar = Path(settings["root"]) / "platforms/android-35/android.jar"
    jar.write_bytes(b"not an Android SDK")
    with pytest.raises(ContractError, match="JAR is invalid"):
        sdk_files(settings)
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("readme.txt", "empty marker SDK")
    with pytest.raises(ContractError, match="public Android API"):
        sdk_files(settings)


def test_internal_network_uses_pinned_anchor_and_normal_resource_accounting(tmp_path):
    image = "sha256:" + "b" * 64
    ctx, _, _ = context(tmp_path, {"task": {"internal_network": {"anchor_image": image}}})
    result = runtime_inputs(ctx, {"id": "task", "start_sha": "a" * 40})
    assert result["sidecars"] == [{"name": "network-anchor", "image": image, "command": ["sleep", "7200"]}]
    assert result["assets"] == [] and result["env"] == {}
    assert "docker.sock" not in json.dumps(result) and "host" not in json.dumps(result)


@pytest.mark.parametrize("network", [True, {"anchor_image": "ubuntu:latest"},
                                    {"anchor_image": "sha256:" + "b" * 64, "network": "host"}])
def test_unpinned_or_host_network_inputs_are_rejected(tmp_path, network):
    ctx, _, _ = context(tmp_path, {"task": {"internal_network": network}})
    with pytest.raises(ContractError, match="immutable anchor"):
        runtime_inputs(ctx, {"id": "task", "start_sha": "a" * 40})


def test_platform_configuration_is_task_bound_and_cannot_change_mid_batch(tmp_path):
    ctx, config, _ = context(tmp_path, {"task": {}})
    configuration(ctx)
    config.write_text(json.dumps({"schema_version": SCHEMA, "tasks": {"another": {}}}))
    with pytest.raises(ContractError, match="changed during"):
        configuration(ctx)
    del ctx._jvm_platform_inputs
    with pytest.raises(ContractError, match="task scope"):
        configuration(ctx)


def test_absent_platform_inputs_leave_existing_recipes_unchanged():
    ctx = SimpleNamespace(config={})
    assert runtime_inputs(ctx, {"id": "task"}) == {"assets": [], "env": {}, "sidecars": []}


@pytest.mark.parametrize("value", ["true", 1, None])
def test_build_cache_choice_is_not_an_implicit_truthy_value(tmp_path, value):
    ctx, _, _ = context(tmp_path, {"task": {"build_cache": value}})
    with pytest.raises(ContractError, match="explicit boolean"):
        configuration(ctx)


def test_calibration_rejects_a_resource_driver_engine_before_any_work(monkeypatch):
    import migrate_agentic_runtimes as migration
    monkeypatch.setattr(migration, "engine_identity", lambda: {"fingerprint": "frozen"})
    assert migration.verify_engine_binding({"migration": {"verification_engine_fingerprint": "frozen"}}) == "frozen"
    with pytest.raises(ValueError, match="configured frozen"):
        migration.verify_engine_binding({"migration": {"verification_engine_fingerprint": "resource-driver"}})
