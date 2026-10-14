import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.scripted_lane import dependency_plan, dependency_tree_fingerprint, bundler_environment
from prepare_agentic_scripted_dependencies import bundler_version, composer_payload, install_command
from prepare_agentic_managed_toolchains import pin_debian_snapshot
from agentic_runtime_migration.common import bounded_cpu_set, retain_preparation_attempt
from reviewbench_maintainability.agentic.contracts import ContractError


def _case(command, image="rb-scripted-php74-cov", cwd="/work"):
    import shlex
    return {"environment": {"docker": image},
            "setup_host_command": "mkdir -p {task_dir}/artifacts && docker run --rm -w " +
                                  cwd + " " + image + " bash -lc " + shlex.quote(command)}


def test_scripted_dependency_plans_preserve_frozen_install_flags_and_workspace():
    plan = dependency_plan(_case("composer install --prefer-dist --no-dev --ignore-platform-req=php",
                                 cwd="/work/Spanner"))
    assert plan["workspace"] == "Spanner" and plan["no_dev"] is True
    command = install_command(plan, "{}")
    assert command == "composer install --prefer-dist --no-dev --ignore-platform-req=php"
    assert "--no-dev" in command and "--ignore-platform-req=php" in command
    assert "update" not in command
    assert dependency_plan({"setup_host_command": "mkdir -p {task_dir}/artifacts"}) is None


@pytest.mark.parametrize("command", [
    "composer update", "composer install && composer require library:^2",
    "bundle add redis --skip-install && bundle install", "composer install --unknown",
])
def test_scripted_dependency_plans_reject_unpinned_resolution_and_setup_sequences(command):
    with pytest.raises(ContractError):
        dependency_plan(_case(command))


def test_scripted_bundler_pins_exact_bundler_and_caps_build_jobs():
    plan = dependency_plan(_case(
        "bundle config set path /work/.bundle && bundle install --jobs 8", image="ruby:3.2-bookworm"))
    lock = "PATH\n  remote: .\n  specs:\n    sample (1.0)\n\nBUNDLED WITH\n   2.3.26\n"
    assert bundler_version(lock) == "2.3.26"
    command = install_command(plan, lock)
    assert "--version 2.3.26" in command and "_2.3.26_ install --jobs 2 --retry 1" in command
    assert "--jobs 8" not in command
    with pytest.raises(ContractError, match="exact Bundler"):
        bundler_version("BUNDLED WITH\n  latest\n")
    with pytest.raises(ContractError, match="Git source"):
        bundler_version("GIT\n  remote: repo\n\n" + lock)
    with pytest.raises(ContractError, match="external path"):
        bundler_version(lock.replace("remote: .", "remote: ../frozen-source"))


def test_bundler_uses_lease_dependencies_and_does_not_write_image_default_config():
    env = bundler_environment("/workspace/.reviewbench/scripted/bundle-config")
    assert env["BUNDLE_FROZEN"] == "true"
    assert env["BUNDLE_PATH"] == "/opt/reviewbench-cache/scripted-dependencies/gems"
    assert env["BUNDLE_APP_CONFIG"] != "/usr/local/bundle"
    assert env["GEM_HOME"] == env["GEM_PATH"] == "/opt/reviewbench-cache/scripted-dependencies/bootstrap"


def test_composer_payload_preserves_tracked_vendor_placeholder_and_excludes_only_new_dependencies(tmp_path):
    source, payload = tmp_path / "source", tmp_path / "payload"
    (source / "vendor/library").mkdir(parents=True)
    (source / "vendor/.htaccess").write_text("tracked")
    (source / "vendor/library/api.php").write_text("dependency")
    (source / "vendor/autoload.php").write_text("generated")
    paths = composer_payload(source, payload, {"vendor/.htaccess": b"tracked"})
    assert paths == ["vendor/autoload.php", "vendor/library"]
    assert not (payload / "vendor/.htaccess").exists()
    assert (payload / "vendor/library/api.php").read_text() == "dependency"


def test_composer_payload_rejects_overwriting_tracked_source_or_hiding_its_sibling(tmp_path):
    source = tmp_path / "source"
    (source / "vendor/library").mkdir(parents=True)
    (source / "vendor/library/api.php").write_text("changed")
    with pytest.raises(ContractError, match="changed a tracked"):
        composer_payload(source, tmp_path / "first", {"vendor/library/api.php": b"original"})
    (source / "vendor/library/api.php").write_text("original")
    (source / "vendor/library/helper.php").write_text("new")
    with pytest.raises(ContractError, match="overlaps tracked"):
        composer_payload(source, tmp_path / "second", {"vendor/library/api.php": b"original"})


def test_scripted_dependency_fingerprint_binds_bytes_and_executable_modes(tmp_path):
    path = tmp_path / "library.php"
    path.write_text("original")
    baseline = dependency_tree_fingerprint(tmp_path)
    path.write_text("modified")
    assert dependency_tree_fingerprint(tmp_path) != baseline
    path.write_text("original")
    path.chmod(0o755)
    assert dependency_tree_fingerprint(tmp_path) != baseline


def test_migration_reuses_explicit_toolchain_registry_without_rebuilding_images(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from agentic_runtime_migration import common
    registry = tmp_path / "shared.json"
    image = "sha256:" + "a" * 64
    registry.write_text(json.dumps({"original-alias": {"status": "ready", "image": image}}))
    called = []
    def docker(*args, **kwargs):
        called.append(args)
        return json.dumps([{"Id": image}]) if args[0] == "image" else ""
    monkeypatch.setattr(common, "docker", docker)
    ctx = SimpleNamespace(config={"migration": {"toolchain_registry": str(registry)}}, output=tmp_path / "out",
                          _images={}, _image_lock=threading.Lock())
    observed, audit = common.MigrationContext.image(ctx, "original-alias")
    assert observed == image and Path(audit["path"]).is_file()
    assert called[0] == ("image", "inspect", image)


def test_legacy_debian_package_sources_are_timestamped_without_disabling_signature_checks():
    text = "FROM php:7.4-cli\nRUN apt-get update && apt-get install -y git\n"
    result = pin_debian_snapshot(text, "20260804T000000Z")
    assert result.count("20260804T000000Z") == 2
    assert "[check-valid-until=no]" in result and "snapshot.debian.org" in result
    assert "trusted=yes" not in result and "AllowUnauthenticated" not in result
    assert result.endswith("RUN apt-get update && apt-get install -y git\n")
    for value in ("latest", "20260999T000000Z", "20260804; shell"):
        with pytest.raises(ValueError):
            pin_debian_snapshot(text, value)


def test_nested_dependency_workers_see_only_their_bounded_cpu_set():
    result = bounded_cpu_set(2, "lease-a", available={4, 7, 9, 12})
    assert len(result.split(",")) == 2
    assert set(map(int, result.split(","))) <= {4, 7, 9, 12}
    assert bounded_cpu_set(2, "lease-a", available={4, 7, 9, 12}) == result
    assert bounded_cpu_set(2, "one", available={7}) == "7"
    assert len({bounded_cpu_set(2, str(i), available=range(16)) for i in range(10)}) > 1
    for value in (0, -1, True):
        with pytest.raises(ContractError):
            bounded_cpu_set(value, "lease", available={0, 1})
    with pytest.raises(ContractError):
        bounded_cpu_set(2, "lease", available=set())


def test_dependency_preparation_retry_retains_literal_command_evidence(tmp_path):
    (tmp_path / "preparation.json").write_text('{"status":"pending","exit_status":17}')
    (tmp_path / "install.log").write_text("original literal failure\n")
    retain_preparation_attempt(tmp_path)
    retain_preparation_attempt(tmp_path)
    attempts = list((tmp_path / "attempts").iterdir())
    assert len(attempts) == 1
    (tmp_path / "install.log").write_text("next attempt")
    assert (attempts[0] / "install.log").read_text() == "original literal failure\n"
    assert json.loads((attempts[0] / "preparation.json").read_text())["exit_status"] == 17
