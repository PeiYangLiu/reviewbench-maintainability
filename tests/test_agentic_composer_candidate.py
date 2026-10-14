"""Bounded unit tests plus two tiny offline ZIP dependencies, never PrivateBin replay.

Set COMPOSER_CANDIDATE_IMAGE to an ALREADY PRESENT PHP/Composer image to run
the real fixtures without host PHP. Containers use no network, .5 CPU and 256 MiB.
Otherwise a local PHP/Composer pair is used, or only the bounded unit tests run.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import composer_candidate as cc


def _bytes(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def _put(root, name, contents):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents.encode() if isinstance(contents, str) else contents)
    return target


def _inputs():
    manifest = {
        "name": "fixture/root", "version": "1.0.0", "type": "project",
        "require": {"fixture/prod": "1.0.0"}, "require-dev": {"fixture/dev": "1.0.0"},
        "autoload": {"psr-4": {"Candidate\\": "lib/"}},
        "repositories": {"packagist.org": False},
        "config": {"autoloader-suffix": "DontChange"},
        "scripts": {"post-autoload-dump": "php -r 'file_put_contents(\"SCRIPT_RAN\", \"bad\");'"},
    }
    prod = {"name": "fixture/prod", "version": "1.0.0", "type": "library",
            "autoload": {"psr-4": {"Locked\\": "src/"}}}
    dev = {"name": "fixture/dev", "version": "1.0.0", "type": "library",
           "autoload": {"psr-4": {"Dev\\": "src/"}}, "bin": ["bin/devtool"]}
    lock = {"packages": [prod], "packages-dev": [dev], "aliases": [],
            "minimum-stability": "stable", "stability-flags": {},
            "prefer-stable": False, "prefer-lowest": False, "platform": {}, "platform-dev": {},
            "plugin-api-version": "2.2.0"}
    return manifest, lock


PROTECTED = [
    "vendor/.htaccess", "vendor/fixture/prod/src/Value.php",
    "vendor/fixture/prod/src/Deleted.php",
]
TRACKED = [*PROTECTED, *sorted(cc.METADATA), "lib/Value.php", "composer.json", "composer.lock"]


def _fake_install(root):
    manifest, lock = _inputs()
    manifest_bytes, lock_bytes = _bytes(manifest), _bytes(lock)
    cc.stage_locked_install(root, manifest_bytes, lock_bytes)
    packages = [*lock["packages"], *lock["packages-dev"]]
    for package in packages:
        _put(root, "vendor/" + package["name"] + "/src/Value.php", "<?php // locked source\n")
    _put(root, "vendor/fixture/prod/src/Deleted.php", "<?php // do not resurrect\n")
    _put(root, "vendor/fixture/prod/LICENSE", "upstream license\n")
    _put(root, "vendor/fixture/dev/bin/devtool", "#!/usr/bin/env php\n<?php echo 'dev';\n").chmod(0o755)
    _put(root, "vendor/bin/devtool", "<?php // generated proxy\n").chmod(0o755)
    inventory = {"dev": True, "dev-package-names": ["fixture/dev"],
                 "packages": [{**p, "install-path": "../" + p["name"]} for p in packages]}
    _put(root, "vendor/composer/installed.json", _bytes(inventory))
    _put(root, "vendor/composer/installed.php", "<?php return [];\n")
    _put(root, "vendor/composer/InstalledVersions.php", "<?php // tool runtime\n")
    _put(root, "vendor/autoload.php", "<?php // generated against install stage, never export\n")
    return manifest_bytes, lock_bytes


def _export(root, payload, inputs):
    return cc.export_dependency_payload(root, payload, TRACKED, manifest=inputs[0], lock=inputs[1])


def _snapshot(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git" and not (Path(directory) == root and d == ".reviewbench")]
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_file():
                result[relative] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
            else:
                result[relative] = ("dir", path.stat().st_mode & 0o777)
    return result


def test_opt_in_is_only_the_exact_frozen_privatebin_prod_and_dev_task():
    task = {"id": cc.TASK_ID, "start_sha": cc.START_SHA}
    plan = {"kind": "composer", "workspace": ".", "no_dev": False, "platform_flags": [], "root_version": None}
    assert cc.applies_to(task, plan)
    for changed in ({"no_dev": True}, {"workspace": "nested"}, {"kind": "bundler"},
                    {"platform_flags": ["--ignore-platform-reqs"]}, {"root_version": "2.0.0"}):
        assert not cc.applies_to(task, {**plan, **changed})
    assert not cc.applies_to({**task, "start_sha": "other"}, plan)
    assert not cc.applies_to({**task, "id": "other"}, plan)
    assert cc.requires_candidate_mirror(TRACKED)
    assert not cc.requires_candidate_mirror(["vendor/library/source.php"])


@pytest.mark.parametrize("name", [
    "vendor/composer/custom.php", "vendor/composer/semver/src/VersionParser.php",
    "vendor/composer/autoload_fake.php", "vendor/bin/phpunit", "vendor/.htaccess",
    "lib/Data/Filesystem.php", "../vendor/autoload.php",
])
def test_metadata_is_an_exact_filename_set_not_a_vendor_whitelist(name):
    assert not cc.is_composer_metadata(name)
    assert cc.is_composer_metadata("vendor/autoload.php")
    assert cc.is_composer_metadata("vendor/composer/InstalledVersions.php")


def test_stage_copies_only_original_manifest_and_committed_lock(tmp_path):
    manifest, lock = map(_bytes, _inputs())
    root = tmp_path / "install"
    result = cc.stage_locked_install(root, manifest, lock)
    assert sorted(p.name for p in root.iterdir()) == ["composer.json", "composer.lock"]
    assert (root / "composer.json").read_bytes() == manifest
    assert (root / "composer.lock").read_bytes() == lock
    assert result["manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
    command = shlex.split(result["command"])
    assert "install" in command and "COMPOSER_NO_DEV=0" in command
    assert {"--no-autoloader", "--no-scripts", "--no-plugins"} <= set(command)
    assert not {"update", "require", "--no-dev", "--ignore-platform-reqs"} & set(command)
    with pytest.raises(FileExistsError):
        cc.stage_locked_install(root, manifest, lock)


@pytest.mark.parametrize("mutation", ["no-dev-list", "path", "plugin", "traversal", "vendor-dir", "autoload-escape"])
def test_stage_rejects_unhandled_install_graphs_without_creating_output(tmp_path, mutation):
    manifest, lock = _inputs()
    if mutation == "no-dev-list":
        del lock["packages-dev"]
    elif mutation == "path":
        lock["packages"][0]["dist"] = {"type": "path", "url": "../history"}
    elif mutation == "plugin":
        lock["packages"][0]["type"] = "composer-plugin"
    elif mutation == "traversal":
        lock["packages"][0]["name"] = "../source"
    elif mutation == "vendor-dir":
        manifest["config"]["vendor-dir"] = "lib"
    else:
        manifest["autoload"]["files"] = ["../history.php"]
    with pytest.raises(cc.ComposerCandidateError):
        cc.stage_locked_install(tmp_path / "install", _bytes(manifest), _bytes(lock))
    assert not (tmp_path / "install").exists()


def test_export_drops_all_historical_tracked_source_but_keeps_both_dependency_sets(tmp_path):
    install, payload = tmp_path / "install", tmp_path / "payload"
    inputs = _fake_install(install)
    before = _snapshot(install)
    record = _export(install, payload, inputs)
    assert _snapshot(install) == before
    assert record == json.loads((payload / cc.RECORD).read_text())
    assert record["scratch_paths"] == []
    assert record["production_packages"] == ["fixture/prod"]
    assert record["dev_packages"] == ["fixture/dev"]
    assert record["protected_vendor_paths"] == sorted(PROTECTED)
    assert not set(PROTECTED) & record["files"].keys()
    assert not (payload / "vendor/autoload.php").exists()
    assert all((payload / name).is_file() for name in cc.SEEDS)
    assert (payload / "vendor/fixture/dev/src/Value.php").is_file()
    assert (payload / "vendor/fixture/prod/LICENSE").is_file()
    assert (payload / "vendor/bin/devtool").stat().st_mode & 0o111
    assert not (payload / "composer.json").exists() and not (payload / "lib").exists()


@pytest.mark.parametrize("mutation,match", [
    ("source", "manifest-only"), ("manifest", "changed the original"), ("lock", "changed the original"),
    ("dev", "all locked prod\\+dev"), ("version", "differs from lock"), ("install-path", "install path"),
    ("arbitrary", "unexpected file"), ("history", "relative"), ("link", "link escapes"),
    ("missing-seed", "seed metadata"), ("missing-bin", "binary proxy"),
])
def test_export_fails_closed_on_unproven_or_incomplete_installs(tmp_path, mutation, match):
    install, payload = tmp_path / "install", tmp_path / "payload"
    inputs = _fake_install(install)
    inventory = install / "vendor/composer/installed.json"
    if mutation == "source":
        _put(install, "lib/OldCandidate.php", "<?php // historical code\n")
    elif mutation in {"manifest", "lock"}:
        with (install / ("composer.json" if mutation == "manifest" else "composer.lock")).open("ab") as output:
            output.write(b"\n")
    elif mutation in {"dev", "version", "install-path"}:
        data = json.loads(inventory.read_text())
        if mutation == "dev":
            data["packages"] = data["packages"][:1]
            data["dev"] = False
        elif mutation == "version":
            data["packages"][0]["version"] = "99.0.0"
        else:
            data["packages"][0]["install-path"] = "/historical/source"
        inventory.write_bytes(_bytes(data))
    elif mutation == "arbitrary":
        _put(install, "vendor/composer/custom.php", "<?php // not generated metadata\n")
    elif mutation == "history":
        _put(install, "vendor/fixture/prod/.git/config", "history")
    elif mutation == "link":
        (install / "vendor/fixture/dev/src/escape").symlink_to("/etc/passwd")
    elif mutation == "missing-seed":
        (install / "vendor/composer/InstalledVersions.php").unlink()
    else:
        (install / "vendor/bin/devtool").unlink()
    with pytest.raises(cc.ComposerCandidateError, match=match):
        _export(install, payload, inputs)
    assert not payload.exists()


def test_current_root_autoload_edits_and_formatted_lock_survive_but_new_graph_does_not(tmp_path):
    install = tmp_path / "install"
    inputs = _fake_install(install)
    record = _export(install, tmp_path / "payload", inputs)
    manifest, lock = map(json.loads, inputs)
    manifest["autoload"]["classmap"] = ["new-helpers/"]
    manifest["autoload-dev"] = {"psr-4": {"Tests\\": "new-tests/"}}
    manifest["description"] = "current candidate"
    lock["content-hash"] = "format/autoload change, not a new graph"
    cc.validate_candidate_inputs(_bytes(manifest), _bytes(lock), record)
    changed = copy.deepcopy(manifest)
    changed["require-dev"] = {}
    with pytest.raises(cc.ComposerCandidateError, match="dependency graph"):
        cc.validate_candidate_inputs(_bytes(changed), _bytes(lock), record)
    lock["packages-dev"][0]["version"] = "2.0.0"
    with pytest.raises(cc.ComposerCandidateError, match="dependency graph"):
        cc.validate_candidate_inputs(_bytes(manifest), _bytes(lock), record)


def test_shell_hook_maps_only_checkout_prefix_and_preserves_grader_artifacts():
    original = ("php /workspace/test.php; echo '/workspace' '/workspace/.reviewbench' "
                "/workspace/.reviewbench/artifacts/cov.json /workspace-other/a /other/workspace/a")
    result = cc.relocate_candidate_paths(original, "/workspace", "/build/current")
    assert result == ("php /build/current/test.php; echo '/build/current' '/workspace/.reviewbench' "
                      "/workspace/.reviewbench/artifacts/cov.json /workspace-other/a /other/workspace/a")
    command = shlex.split(cc.runtime_command(
        "/tools/entry.php", "/deps", "/build/current", original,
        env={"ORACLE_TARGET": "/workspace/lib/Value.php", "XDEBUG_MODE": "coverage"},
    ))
    assert command[:3] == ["env", "ORACLE_TARGET=/workspace/lib/Value.php", "XDEBUG_MODE=coverage"]
    assert command[3:9] == ["php", "/tools/entry.php", "/workspace", "/deps", "/build/current", "--"]
    assert command[-3:] == ["bash", "-c", result]
    with pytest.raises(cc.ComposerCandidateError):
        cc.runtime_command("/entry", "/deps", "/mirror", env={"BAD;KEY": "value"})


def test_python_cli_and_emitted_php_are_self_contained(tmp_path):
    manifest, lock = map(_bytes, _inputs())
    _put(tmp_path, "original.json", manifest)
    _put(tmp_path, "original.lock", lock)
    module = str(Path(cc.__file__).resolve())
    stage = subprocess.run(
        [sys.executable, "-B", module, "stage", "--manifest", str(tmp_path / "original.json"),
         "--lock", str(tmp_path / "original.lock"), "--out", str(tmp_path / "install")],
        capture_output=True, text=True, timeout=10,
    )
    assert stage.returncode == 0, stage.stderr
    assert json.loads(stage.stdout)["lock_sha256"] == hashlib.sha256(lock).hexdigest()
    entry = cc.write_php_runtime(tmp_path / "entry.php")
    assert entry.read_text() == cc.php_runtime_source()
    assert "__METADATA__" not in entry.read_text()
    assert "dump-autoload" in entry.read_text() and "'--dev'" in entry.read_text()


class _Runtime:
    def __init__(self, root, image=None):
        self.root, self.image = root, image

    def run(self, command, cwd=None, env=None):
        cwd = cwd or self.root
        environment = {"HOME": "/tmp", "COMPOSER_DISABLE_NETWORK": "1",
                       "COMPOSER_CACHE_DIR": "/tmp/composer-fixture-cache", **(env or {})}
        if self.image:
            argv = [
                "docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                "--cpus=.5", "--memory=256m", "--pids-limit=64", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
                "--mount", f"type=bind,source={self.root},target={self.root}", "-w", str(cwd),
            ]
            for key, value in environment.items():
                argv += ["-e", key + "=" + str(value)]
            argv += ["--entrypoint", command[0], self.image, *map(str, command[1:])]
            result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
        else:
            argv = list(map(str, command))
            result = subprocess.run(argv, cwd=cwd, env={**os.environ, **environment},
                                    capture_output=True, text=True, timeout=45)
        if os.environ.get("COMPOSER_CANDIDATE_LOG"):
            with open(os.environ["COMPOSER_CANDIDATE_LOG"], "a") as log:
                log.write(json.dumps({"command": argv, "cwd": str(cwd), "input_env": environment,
                                      "stdout": result.stdout, "stderr": result.stderr,
                                      "exit_status": result.returncode}) + "\n")
        return result


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    image = os.environ.get("COMPOSER_CANDIDATE_IMAGE")
    if not image and not (shutil.which("php") and shutil.which("composer")):
        pytest.skip("real fixture needs local PHP/Composer or COMPOSER_CANDIDATE_IMAGE")
    if image:
        assert shutil.which("docker"), "COMPOSER_CANDIDATE_IMAGE requires Docker"
    return _Runtime(tmp_path_factory.getbasetemp().resolve(), image)


def _real_install(root, runtime):
    root.mkdir(parents=True)
    manifest, lock = _inputs()
    contents = {
        "fixture/prod": {
            "src/Value.php": "<?php namespace Locked; class Value { static function get() { return 'old'; } }\n",
            "src/Deleted.php": "<?php namespace Locked; class Deleted {}\n",
            "src/Added.php": "<?php namespace Locked; class Added { static function get() { return 'locked-added'; } }\n",
            "src/OnlyLocked.php": "<?php namespace Locked; class OnlyLocked { static function get() { return 'locked-prod'; } }\n",
            "LICENSE": "tiny local production fixture\n",
        },
        "fixture/dev": {
            "src/Value.php": "<?php namespace Dev; class Value { static function get() { return 'dev'; } }\n",
            "bin/devtool": "#!/usr/bin/env php\n<?php echo \"DEV_TOOL_OK\\n\";\n",
        },
    }
    for package in [*lock["packages"], *lock["packages-dev"]]:
        archive = root / (package["name"].split("/")[1] + ".zip")
        with zipfile.ZipFile(archive, "w") as output:
            for name, value in contents[package["name"]].items():
                output.writestr(name, value)
            output.writestr("composer.json", _bytes(package))
        # PHP's local file transport opens the literal path; percent-escaping
        # a pytest username containing '@' produces a different filename.
        package["dist"] = {"type": "zip", "url": "file://" + str(archive), "reference": "fixture-1.0.0",
                           "shasum": hashlib.sha1(archive.read_bytes()).hexdigest()}
    # Composer Locker::getContentHash sorts top-level dependency keys and uses
    # json_encode(..., 0), including escaped slashes. No fixture 'update' runs.
    relevant = {key: manifest[key] for key in sorted(cc.DEPENDENCY_KEYS) if key in manifest}
    encoded = json.dumps(relevant, separators=(",", ":")).replace("/", "\\/")
    lock["content-hash"] = hashlib.md5(encoded.encode()).hexdigest()
    inputs = _bytes(manifest), _bytes(lock)
    install, payload = root / "install", root / "payload"
    cc.stage_locked_install(install, *inputs)
    result = runtime.run(["sh", "-c", cc.locked_install_command()], cwd=install)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "including require-dev" in result.stderr + result.stdout
    assert "fixture/prod" in result.stderr + result.stdout and "fixture/dev" in result.stderr + result.stdout
    assert "lock file is not up to date" not in result.stderr + result.stdout
    record = _export(install, payload, inputs)
    assert not (install / "SCRIPT_RAN").exists()
    entry = cc.write_php_runtime(root / "entry.php")
    lint = runtime.run(["php", "-l", str(entry)])
    assert lint.returncode == 0, lint.stdout + lint.stderr
    return inputs, payload, entry, record


@pytest.fixture(scope="module")
def real_payload(runtime):
    return _real_install(runtime.root / "real-composer", runtime)


def _candidate(root, inputs):
    manifest = json.loads(inputs[0])
    manifest["autoload"]["classmap"] = ["helpers/"]  # A CURRENT candidate manifest edit.
    manifest["autoload-dev"] = {"psr-4": {"CurrentDev\\": "dev-helpers/"}}
    _put(root, "composer.json", _bytes(manifest))
    _put(root, "composer.lock", inputs[1] + b"\n")  # Formatting survives byte-for-byte.
    _put(root, "lib/Value.php", "<?php\nnamespace Candidate;\nclass Value {\n"
         " static function get() {\n  return 'current';\n }\n}\n").chmod(0o640)
    _put(root, "helpers/Extracted.php", "<?php class Extracted { static function get() { return 'helper'; } }\n")
    _put(root, "dev-helpers/Value.php", "<?php namespace CurrentDev; class Value { static function get() { return 'current-dev'; } }\n")
    _put(root, "vendor/fixture/prod/src/Value.php",
         "<?php namespace Locked; class Value { static function get() { return 'current-vendor'; } }\n")
    _put(root, "vendor/fixture/prod/src/Added.php",
         "<?php namespace Locked; class Added { static function get() { return 'current-added'; } }\n")
    _put(root, "vendor/.htaccess", "candidate vendor policy\n")
    _put(root, "vendor/composer/custom.php", "<?php // current non-tool source\n")
    _put(root, "vendor/autoload.php", "<?php // tracked ComposerAutoloaderInitDontChange placeholder\n")
    _put(root, "vendor/composer/ClassLoader.php", "<?php // tracked tool metadata\n")
    _put(root, ".git/config", "history must not reach mirror\n")
    (root / "empty-directory").mkdir()
    _put(root, "fixed-test.php", r'''<?php
require __DIR__ . '/vendor/autoload.php';
echo json_encode([
    'app' => Candidate\Value::get(), 'prod' => Locked\Value::get(),
    'added' => Locked\Added::get(), 'dev' => Dev\Value::get(),
    'locked' => Locked\OnlyLocked::get(), 'root_dev' => CurrentDev\Value::get(),
    'helper' => Extracted::get(), 'deleted' => class_exists('Locked\Deleted'),
    'dev_installed' => Composer\InstalledVersions::isInstalled('fixture/dev'),
    'root_path' => (new ReflectionClass('Candidate\Value'))->getFileName(),
    'vendor_path' => (new ReflectionClass('Locked\Value'))->getFileName(),
    'cwd' => getcwd(), 'home' => getenv('HOME')
], JSON_UNESCAPED_SLASHES), "\n";
''')
    return root


def _invoke(runtime, entry, source, payload, mirror, command=None, env=None):
    return runtime.run(["sh", "-c", cc.runtime_command(
        str(entry), str(payload), str(mirror), command, source=str(source), env=env,
    )])


def test_real_current_candidate_root_vendor_dev_autoload_and_refresh(runtime, real_payload, tmp_path):
    inputs, payload, entry, record = real_payload
    source = _candidate(tmp_path.resolve() / "candidate", inputs)
    mirror = source / ".reviewbench/build/current"
    before = _snapshot(source)
    result = _invoke(runtime, entry, source, payload, mirror, "php fixed-test.php", {"HOME": str(source)})
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout.splitlines()[-1])
    assert {k: data[k] for k in ("app", "prod", "added", "dev", "locked", "root_dev", "helper", "deleted", "dev_installed")} == {
        "app": "current", "prod": "current-vendor", "added": "current-added", "dev": "dev",
        "locked": "locked-prod", "root_dev": "current-dev",
        "helper": "helper", "deleted": False, "dev_installed": True,
    }
    assert data["root_path"] == str(mirror / "lib/Value.php")
    assert data["vendor_path"] == str(mirror / "vendor/fixture/prod/src/Value.php")
    assert data["cwd"] == data["home"] == str(mirror)
    assert _snapshot(source) == before
    assert (mirror / "composer.json").read_bytes() == (source / "composer.json").read_bytes()
    assert (mirror / "composer.lock").read_bytes() == (source / "composer.lock").read_bytes()
    assert (mirror / "empty-directory").is_dir()
    assert (mirror / "lib/Value.php").stat().st_mode & 0o777 == 0o640
    assert (mirror / "vendor/composer/custom.php").read_bytes() == (source / "vendor/composer/custom.php").read_bytes()
    assert not (mirror / "SCRIPT_RAN").exists() and not (mirror / ".git").exists()
    assert "ComposerAutoloaderInitDontChange" in (mirror / "vendor/composer/autoload_real.php").read_text()
    assert not (mirror / PROTECTED[-1]).exists()
    assert record["production_packages"] and record["dev_packages"]
    # A second invocation must remove stale build files/classmap entries, not
    # reuse old vendored code or the previous candidate's extracted helper.
    (source / "helpers/Extracted.php").unlink()
    _put(source, "helpers/Replacement.php", "<?php class Replacement {}\n")
    _put(source, "new-file.php", "<?php // current added file\n")
    _put(mirror, "stale-build.php", "<?php // prior build\n")
    _put(source, "refresh.php", "<?php require __DIR__.'/vendor/autoload.php'; "
         "echo json_encode([class_exists('Extracted'),class_exists('Replacement')]),\"\\n\";\n")
    refreshed_before = _snapshot(source)
    refreshed = _invoke(runtime, entry, source, payload, mirror, "php refresh.php && php vendor/bin/devtool")
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    assert refreshed.stdout.endswith("[false,true]\nDEV_TOOL_OK\n")
    assert not (mirror / "stale-build.php").exists()
    assert not (mirror / "helpers/Extracted.php").exists()
    assert (mirror / "new-file.php").is_file()
    assert _snapshot(source) == refreshed_before


def test_real_coverage_executes_mirror_then_normalizes_only_target_path(runtime, real_payload, tmp_path):
    inputs, payload, entry, _ = real_payload
    source = _candidate(tmp_path.resolve() / "candidate", inputs)
    mirror = tmp_path.resolve() / "mirror"
    coverage = tmp_path.resolve() / "coverage.json"
    coverage.write_text('{"target":"stale","covered_lines":[999]}')
    helper = _put(tmp_path.resolve(), "coverage.php", r'''<?php
if (!function_exists('xdebug_start_code_coverage')) { fwrite(STDERR, "NO_XDEBUG\n"); exit(77); }
$target = realpath(getenv('ORACLE_TARGET'));
$output = getenv('ORACLE_COVERAGE_JSON');
xdebug_start_code_coverage(XDEBUG_CC_UNUSED | XDEBUG_CC_DEAD_CODE);
register_shutdown_function(function () use ($target, $output) {
    $lines = [];
    foreach ((xdebug_get_code_coverage()[$target] ?? []) as $line => $hits) {
        if ($hits > 0) { $lines[] = (int) $line; }
    }
    sort($lines);
    file_put_contents($output, json_encode(['target' => $target, 'covered_lines' => $lines,
        'coverage_backend' => 'xdebug', 'unchanged_extra_field' => ['fixture', 17]]));
});
''')
    before = _snapshot(source)
    result = _invoke(
        runtime, entry, source, payload, mirror,
        "php -d auto_prepend_file=" + shlex.quote(str(helper)) + " fixed-test.php",
        {"XDEBUG_MODE": "coverage", "ORACLE_TARGET": str(source / "lib/Value.php"),
         "ORACLE_COVERAGE_JSON": str(coverage)},
    )
    if result.returncode == 77 and not runtime.image:
        pytest.skip("local PHP has no Xdebug; other real PHP fixtures still ran")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(coverage.read_text())
    assert data["target"] == str(source / "lib/Value.php")
    assert data["covered_lines"] and 5 in data["covered_lines"] and 999 not in data["covered_lines"]
    assert data["coverage_backend"] == "xdebug"
    assert data["unchanged_extra_field"] == ["fixture", 17]
    assert _snapshot(source) == before


def test_real_internal_links_are_mirrored_without_aliasing_source(runtime, real_payload, tmp_path):
    inputs, payload, entry, _ = real_payload
    source = _candidate(tmp_path.resolve() / "candidate", inputs)
    mirror = tmp_path.resolve() / "mirror"
    (source / "absolute-link").symlink_to(source / "lib/Value.php")
    (source / "relative-link").symlink_to("lib/Value.php")
    before = _snapshot(source)
    result = _invoke(runtime, entry, source, payload, mirror)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["source_unchanged"] is True
    assert (mirror / "absolute-link").resolve() == mirror / "lib/Value.php"
    assert os.readlink(mirror / "relative-link") == "lib/Value.php"
    assert (mirror / "lib/Value.php").stat().st_ino != (source / "lib/Value.php").stat().st_ino
    assert _snapshot(source) == before


@pytest.mark.parametrize("mutation,expected", [
    ("unowned", "not adapter-owned"), ("overlap", "overlap"),
    ("payload", "dependency bytes/modes changed"), ("manifest", "dependency graph"),
    ("lock", "dependency graph"), ("metadata-link", "symlink in build/payload"),
    ("composer-failure", "dump-autoload failed with exit 17"),
    ("composer-source-write", "changed non-metadata source"),
])
def test_real_failures_never_run_tests_or_modify_original_source(runtime, real_payload, tmp_path, mutation, expected):
    inputs, original_payload, entry, _ = real_payload
    source = _candidate(tmp_path.resolve() / "candidate", inputs)
    payload = tmp_path.resolve() / "payload"
    shutil.copytree(original_payload, payload, symlinks=True)
    mirror = tmp_path.resolve() / "mirror"
    env = {}
    if mutation == "unowned":
        _put(mirror, "keep", "not adapter scratch")
    elif mutation == "overlap":
        mirror = source / "lib"
    elif mutation == "payload":
        _put(payload, "vendor/fixture/dev/src/Value.php", "<?php // tampered dependency\n")
    elif mutation == "manifest":
        data = json.loads((source / "composer.json").read_text())
        data["require-dev"] = {}
        _put(source, "composer.json", _bytes(data))
    elif mutation == "lock":
        data = json.loads((source / "composer.lock").read_text())
        data["packages-dev"][0]["version"] = "2.0.0"
        _put(source, "composer.lock", _bytes(data))
    elif mutation == "metadata-link":
        shutil.rmtree(source / "vendor/composer")
        (source / "vendor/composer").symlink_to("../lib")
    elif mutation.startswith("composer-"):
        body = "exit 17" if mutation == "composer-failure" else "printf bad > lib/Value.php\nexit 0"
        script = _put(tmp_path.resolve(), "bin/composer", "#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
        env["PATH"] = str(script.parent) + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    before = _snapshot(source)
    result = _invoke(runtime, entry, source, payload, mirror, "touch TEST_RAN", env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert expected in result.stderr
    assert _snapshot(source) == before
    assert not (source / "TEST_RAN").exists() and not (mirror / "TEST_RAN").exists()
    if mutation.startswith("composer-"):
        assert not mirror.exists()
    if mutation == "unowned":
        assert (mirror / "keep").read_text() == "not adapter scratch"


def test_real_test_exit_status_is_not_converted_to_success(runtime, real_payload, tmp_path):
    inputs, payload, entry, _ = real_payload
    source = _candidate(tmp_path.resolve() / "candidate", inputs)
    before = _snapshot(source)
    result = _invoke(runtime, entry, source, payload, tmp_path.resolve() / "mirror",
                     "printf 'FIXED_TEST_FAILURE\\n'; exit 23")
    assert result.returncode == 23, result.stdout + result.stderr
    assert result.stdout.endswith("FIXED_TEST_FAILURE\n")
    assert _snapshot(source) == before
