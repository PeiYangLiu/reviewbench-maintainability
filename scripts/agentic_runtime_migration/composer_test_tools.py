"""Preparation-only Kirby PHPUnit overlay; no subprocesses or network operations.

Parent hooks, in order:
  plan_test_tools -> stage_test_tools -> original_install -> record_original_install
  -> resolve_tools -> record_resolution -> derived_install -> seal_prepared_tools.
The parent executes the three commands under its pinned-image/shared resource
gate and supplies literal execution/resolver evidence. This module executes none.

``require --no-install`` resolves once, followed by a complete locked install.
RequireCommand has no --no-autoloader option; both install steps use it because
Kirby's optimized root classmap needs CURRENT candidate source, not old source
copied into a dependency stage. Scripts/plugins stay disabled in all steps.
The original setup string and the actually executed adapted argv remain distinct.

At runtime load the sealed record by its externally bound SHA256. Project ONLY
composer.json/composer.lock into the current-candidate BUILD MIRROR, never the
candidate. Preserve current non-dependency manifest fields/autoload definitions;
use the exact prepared derived lock and dependencies, with no new resolution.

Two explicit composer_candidate integration hooks remain parent-owned:
  * validate_export_package admits only the unchanged, inert original Kirby
    installer plugin. Do not remove that package or relabel it as a library.
  * Validate candidate originals before applying candidate_build_inputs, then
    validate the projected graph against composer_candidate_contract. The old
    PrivateBin entry is not a drop-in derived-input overlay consumer.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import sys
from typing import Mapping

if __package__:
    from . import composer_candidate as cc
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agentic_runtime_migration import composer_candidate as cc


TASK_ID = "github__getkirby__kirby__pull_7745__comment_2596448494__testability"
START_SHA = "628b8d4d0442a1d47782eea034c43738091ef0ed"
SCHEMA = "composer-derived-test-tools-v1"
RECORD = "composer-test-tools.json"
REQUIREMENT = "phpunit/phpunit:^11.5"
PHPUNIT = "phpunit/phpunit"
CONSTRAINT = "^11.5"
INERT_PLUGIN = "getkirby/composer-installer"
ORIGINAL_SETUP = (
    "composer install --no-interaction --prefer-dist --ignore-platform-req=ext-gd"
    " && composer require --dev phpunit/phpunit:^11.5 --no-interaction"
    " --with-all-dependencies --ignore-platform-req=ext-gd"
)
ENVIRONMENT = {
    "COMPOSER": "composer.json", "COMPOSER_NO_DEV": "0",
    "COMPOSER_VENDOR_DIR": "vendor", "COMPOSER_BIN_DIR": "vendor/bin",
}
INSTALL = [
    "composer", "install", "--no-interaction", "--prefer-dist",
    "--ignore-platform-req=ext-gd", "--no-scripts", "--no-plugins", "--no-autoloader",
]
RESOLVE = [
    "composer", "require", "--dev", REQUIREMENT, "--no-interaction",
    "--with-all-dependencies", "--ignore-platform-req=ext-gd",
    "--no-scripts", "--no-plugins", "--no-install",
]
COMMANDS = {"original_install": INSTALL, "resolve_tools": RESOLVE, "derived_install": INSTALL}
PACKAGE_FIELDS = (
    "name", "version", "type", "source", "dist", "require", "conflict",
    "provide", "replace", "autoload", "bin", "extra", "target-dir",
)


class TestToolsError(ValueError):
    pass


def _need(ok, message):
    if not ok:
        raise TestToolsError(message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _fingerprint(value) -> str:
    return _sha(_encoded(value))


def _object(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, "duplicate JSON key: " + key)
            result[key] = value
        return result
    try:
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda value: _need(False, "nonfinite JSON"))
    except (ValueError, UnicodeError) as exc:
        raise TestToolsError("invalid Composer JSON") from exc
    _need(isinstance(result, dict), "Composer JSON must be an object")
    return result


def _identity(package: Mapping) -> dict:
    return {key: package.get(key, "library" if key == "type" else None) for key in PACKAGE_FIELDS}


def _packages(lock: Mapping) -> dict[str, dict]:
    result = {}
    for group in ("packages", "packages-dev"):
        _need(isinstance(lock.get(group), list), "lock must retain production and dev inventories")
        for package in lock[group]:
            _need(isinstance(package, dict), "invalid package record")
            name = package.get("name")
            _need(isinstance(name, str) and re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", name),
                  "invalid package name")
            cc._relative(name)
            _need(name not in result and isinstance(package.get("version"), str), "duplicate/unversioned package")
            kind = package.get("type", "library")
            _need(kind == "library" or (
                name == INERT_PLUGIN and kind == "composer-plugin" and package["version"] == "1.2.1"
            ), "package needs a separate installer adapter: " + name)
            _need(not package.get("target-dir") and package.get("dist", {}).get("type") != "path",
                  "path/custom-target dependencies are not a test-tool overlay")
            result[name] = package
    return result


def phpunit_compatible(version: str) -> bool:
    match = re.fullmatch(r"v?11\.(\d+)\.(\d+)", version)
    return bool(match and int(match[1]) >= 5)


def composer_content_hash(manifest: Mapping) -> str:
    """Composer Locker::getContentHash for these ordinary JSON input values.

    https://github.com/composer/composer/blob/2.10.3/src/Composer/Package/Locker.php
    Only top-level relevant keys are sorted; nested object order is retained.
    """
    keys = (*cc.DEPENDENCY_KEYS, "extra")
    relevant = {key: manifest[key] for key in keys if key in manifest}
    if "platform" in manifest.get("config", {}):
        relevant["config"] = {"platform": manifest["config"]["platform"]}
    relevant = dict(sorted(relevant.items()))
    encoded = json.dumps(relevant, separators=(",", ":"), ensure_ascii=True, allow_nan=False).replace("/", "\\/")
    return hashlib.md5(encoded.encode()).hexdigest()


def applies_to(task: Mapping, setup: str = ORIGINAL_SETUP) -> bool:
    return (task.get("id") == TASK_ID and task.get("start_sha") == START_SHA
            and setup == ORIGINAL_SETUP)


def plan_test_tools(task: Mapping, manifest: bytes, lock: bytes, setup: str = ORIGINAL_SETUP) -> dict:
    """Bind caller's original Git bytes; the parent still verifies them at start_sha."""
    _need(applies_to(task, setup), "test-tool plan is only the exact frozen Kirby setup")
    original_manifest, original_lock = _object(manifest), _object(lock)
    _need(original_manifest.get("name") == "getkirby/cms", "not the Kirby root manifest")
    cc._manifest_contract(original_manifest)
    _packages(original_lock)
    _need(PHPUNIT not in original_manifest.get("require", {})
          and PHPUNIT not in _packages(original_lock), "original PHPUnit requires a separate migration")
    return {
        "schema": SCHEMA, "status": "planned", "task_id": TASK_ID, "start_sha": START_SHA,
        "original_setup": setup, "requirement": REQUIREMENT,
        "adapter": "resolve-no-install-then-locked-install", "workspace": ".",
        "commands": copy.deepcopy(COMMANDS), "environment": dict(ENVIRONMENT),
        "original": {
            "manifest_sha256": _sha(manifest), "lock_sha256": _sha(lock),
            "manifest": original_manifest, "lock": original_lock,
        },
    }


def _plan(plan):
    _need(plan.get("schema") == SCHEMA and plan.get("status") == "planned", "expected an unsealed preparation plan")
    _need(plan.get("task_id") == TASK_ID and plan.get("start_sha") == START_SHA
          and plan.get("original_setup") == ORIGINAL_SETUP and plan.get("requirement") == REQUIREMENT,
          "plan differs from the frozen setup")
    _need(plan.get("commands") == COMMANDS and plan.get("environment") == ENVIRONMENT,
          "plan changes the literal requirement/install policy")


def preparation_commands(plan: Mapping) -> list[str]:
    """Preparation only: runtime_contract never returns these resolver commands."""
    _plan(plan)
    return [shlex.join(["env", *[key + "=" + value for key, value in ENVIRONMENT.items()],
                       *plan["commands"][phase]]) for phase in COMMANDS]


def _originals(plan, manifest, lock):
    _plan(plan)
    _need(_sha(manifest) == plan["original"]["manifest_sha256"]
          and _sha(lock) == plan["original"]["lock_sha256"], "original manifest/lock bytes changed")
    rebuilt = plan_test_tools({"id": TASK_ID, "start_sha": START_SHA}, manifest, lock)
    _need(_fingerprint(rebuilt) == _fingerprint(plan), "original inputs do not match the plan")


def stage_test_tools(destination: Path, plan: Mapping, manifest: bytes, lock: bytes) -> Path:
    """Create only mutable COPIES in a new dependency stage, never a checkout."""
    _originals(plan, manifest, lock)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        (destination / "composer.json").write_bytes(manifest)
        (destination / "composer.lock").write_bytes(lock)
    except Exception:
        shutil.rmtree(destination)
        raise
    return destination


def _read(root: Path, relative: str) -> bytes:
    cc._relative(relative)
    path = root / relative
    _need(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()),
          "missing/escaping regular input: " + relative)
    for parent in path.relative_to(root).parents:
        _need(not (root / parent).is_symlink(), "symlinked input parent")
    return path.read_bytes()


def _stage(stage: Path):
    _need(not stage.is_symlink() and stage.is_dir()
          and {p.name for p in stage.iterdir()} == {"composer.json", "composer.lock", "vendor"}
          and (stage / "vendor").is_dir() and not (stage / "vendor").is_symlink(),
          "expected a manifest-only dependency stage, not candidate source")


def _vendor_hash(stage: Path) -> str:
    _stage(stage)
    return _fingerprint({name: cc._file_record(path, stage / "vendor")
                         for name, path in cc._tree(stage / "vendor").items()})


def _inventory(stage: Path, lock: dict) -> str:
    _stage(stage)
    packages = _packages(lock)
    raw = _read(stage, "vendor/composer/installed.json")
    installed = _object(raw)
    actual = installed.get("packages")
    _need(isinstance(actual, list) and all(isinstance(p, dict) for p in actual), "invalid installed inventory")
    _need(installed.get("dev") is True and len(actual) == len(packages)
          and {p.get("name") for p in actual} == packages.keys()
          and sorted(installed.get("dev-package-names", []))
          == sorted(p["name"] for p in lock["packages-dev"]), "not all locked prod/dev dependencies were installed")
    for package in actual:
        name = package["name"]
        _need(_fingerprint(_identity(package)) == _fingerprint(_identity(packages[name])),
              "installed package differs from the lock: " + name)
        relative = package.get("install-path")
        # installed.json itself is in vendor/composer. A composer/* package
        # therefore legitimately uses "semver", not "../composer/semver".
        # Accept only paths normalizing to this exact package, never redirects.
        package_dir = stage / "vendor" / name
        _need(isinstance(relative, str) and not relative.startswith("/")
              and not any(char in relative for char in ("\\", "\0", ":"))
              and posixpath.normpath(posixpath.join("composer", relative)) == name
              and package_dir.is_dir() and not package_dir.is_symlink(),
              "unexpected installed package path: " + name + " " + repr(relative))
        for binary in package.get("bin", []):
            _need(bool(_read(stage, "vendor/" + name + "/" + cc._relative(binary))), "empty dependency executable")
    for name in cc.SEEDS:
        _need(bool(_read(stage, name)), "missing installed-version metadata")
    return _sha(raw)


def _resolver(resolver: Mapping):
    _need(re.fullmatch(r"sha256:[0-9a-f]{64}", str(resolver.get("image", ""))), "resolver image must be a digest")
    path = resolver.get("composer_path", "")
    _need(isinstance(path, str) and path.startswith("/") and "\0" not in path,
          "record the actual Composer executable path")
    _need(re.fullmatch(r"[0-9a-f]{64}", str(resolver.get("composer_sha256", ""))),
          "record the actual Composer executable SHA256")
    _need(resolver.get("version_argv") == [path, "--version"]
          and type(resolver.get("version_exit_status")) is int and resolver["version_exit_status"] == 0,
          "record a successful actual Composer version probe")
    _need(isinstance(resolver.get("version_stdout"), str) and isinstance(resolver.get("version_stderr"), str),
          "record literal Composer version output")
    _need(re.search(r"(?m)^Composer version 2\.\d+\.\d+(?:\s|$)",
                    resolver["version_stdout"] + "\n" + resolver["version_stderr"]), "unrecognized Composer 2 resolver")


def _execution(plan, phase, resolver, evidence):
    _plan(plan)
    _resolver(resolver)
    expected = list(COMMANDS[phase])
    argv = evidence.get("argv", [])
    _need(isinstance(argv, list) and len(argv) == len(expected)
          and argv[0] in {"composer", resolver["composer_path"]} and argv[1:] == expected[1:],
          "actual execution differs from the declared " + phase)
    _need(type(evidence.get("exit_status")) is int and evidence["exit_status"] == 0,
          phase + " did not exit zero")
    _need(isinstance(evidence.get("stdout"), str) and isinstance(evidence.get("stderr"), str),
          "literal command output is required")
    _need(isinstance(evidence.get("workdir"), str) and evidence["workdir"].startswith("/"),
          "actual container workdir is required")
    environment = evidence.get("environment", {})
    _need(all(environment.get(k) == v for k, v in ENVIRONMENT.items()), "execution omits the all-dev/default-path environment")
    _need(not environment.get("COMPOSER_IGNORE_PLATFORM_REQS")
          and environment.get("COMPOSER_IGNORE_PLATFORM_REQ", "") in {"", "ext-gd"},
          "execution broadens the original platform exception")


def _checkpoint(plan, phase, resolver, evidence, **state):
    _execution(plan, phase, resolver, evidence)
    result = {
        "phase": phase, "plan_sha256": _fingerprint(plan), "resolver_sha256": _fingerprint(resolver),
        "execution": copy.deepcopy(evidence), **state,
    }
    return {**result, "checkpoint_sha256": _fingerprint(result)}


def _check_checkpoint(plan, phase, resolver, checkpoint):
    data = {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
    _need(checkpoint.get("checkpoint_sha256") == _fingerprint(data)
          and checkpoint.get("phase") == phase and checkpoint.get("plan_sha256") == _fingerprint(plan)
          and checkpoint.get("resolver_sha256") == _fingerprint(resolver), "checkpoint/resolver binding changed")
    _execution(plan, phase, resolver, checkpoint["execution"])


def record_original_install(plan: Mapping, stage: Path, *, resolver: Mapping, execution: Mapping) -> dict:
    """Call immediately AFTER the first install and BEFORE require mutates copies."""
    stage = Path(stage)
    _originals(plan, _read(stage, "composer.json"), _read(stage, "composer.lock"))
    inventory = _inventory(stage, plan["original"]["lock"])
    return _checkpoint(plan, "original_install", resolver, execution,
                       installed_json_sha256=inventory, vendor_tree_sha256=_vendor_hash(stage))


def _derived(plan, manifest: bytes, lock: bytes) -> dict:
    original_manifest, original_lock = plan["original"]["manifest"], plan["original"]["lock"]
    derived_manifest, derived_lock = _object(manifest), _object(lock)
    expected = copy.deepcopy(original_manifest)
    expected.setdefault("require-dev", {})[PHPUNIT] = CONSTRAINT
    _need(_fingerprint(derived_manifest) == _fingerprint(expected),
          "require changed manifest fields beyond require-dev.phpunit/phpunit")
    original, derived = _packages(original_lock), _packages(derived_lock)
    _need({p["name"] for p in derived_lock["packages"]} == {p["name"] for p in original_lock["packages"]},
          "derived resolution changed the production package set")
    original_dev = {p["name"] for p in original_lock["packages-dev"]}
    derived_dev = {p["name"] for p in derived_lock["packages-dev"]}
    _need(original_dev <= derived_dev, "derived resolution removed/demoted original dev packages")
    for name, package in original.items():
        _need(name in derived and _fingerprint(_identity(package)) == _fingerprint(_identity(derived[name])),
              "original package identity drift: " + name)
    for key in ("aliases", "minimum-stability", "stability-flags", "prefer-stable", "prefer-lowest",
                "platform", "platform-dev", "platform-overrides"):
        _need(_fingerprint(derived_lock.get(key)) == _fingerprint(original_lock.get(key)),
              "derived lock changes the original platform/stability contract: " + key)
    _need(PHPUNIT in derived_dev and phpunit_compatible(derived[PHPUNIT]["version"])
          and "phpunit" in derived[PHPUNIT].get("bin", []), "derived graph lacks real PHPUnit ^11.5 executable metadata")
    _need(derived_lock.get("content-hash") == composer_content_hash(derived_manifest),
          "derived lock content-hash does not match the resolved manifest")
    return {
        "manifest_sha256": _sha(manifest), "lock_sha256": _sha(lock),
        "original_package_identities_preserved": True,
        "added_dev_packages": sorted(derived_dev - original_dev),
        "phpunit_version": derived[PHPUNIT]["version"], "historical_lock": False,
    }


def record_resolution(plan: Mapping, stage: Path, *, original_install: Mapping,
                      resolver: Mapping, execution: Mapping) -> dict:
    """Record NEW resolution immediately after require --no-install."""
    _check_checkpoint(plan, "original_install", resolver, original_install)
    stage = Path(stage)
    _need(_vendor_hash(stage) == original_install["vendor_tree_sha256"],
          "resolve-only step modified installed dependencies")
    derived = _derived(plan, _read(stage, "composer.json"), _read(stage, "composer.lock"))
    _need(execution.get("workdir") == original_install["execution"]["workdir"], "setup steps changed workspace")
    return _checkpoint(plan, "resolve_tools", resolver, execution, **derived)


def seal_prepared_tools(destination: Path, plan: Mapping, stage: Path, *,
                        original_manifest: bytes, original_lock: bytes,
                        original_install: Mapping, resolution: Mapping,
                        resolver: Mapping, execution: Mapping) -> dict:
    """After derived locked install: seal immutable build inputs and evidence.

    Return the record's SHA256 for the parent's externally trusted preparation
    manifest. Do not relabel these newly resolved bytes as a historical lock.
    """
    _originals(plan, original_manifest, original_lock)
    _check_checkpoint(plan, "original_install", resolver, original_install)
    _check_checkpoint(plan, "resolve_tools", resolver, resolution)
    _execution(plan, "derived_install", resolver, execution)
    _need(execution.get("workdir") == resolution["execution"]["workdir"]
          == original_install["execution"]["workdir"], "setup steps changed workspace")
    stage, destination = Path(stage), Path(destination)
    manifest, lock = _read(stage, "composer.json"), _read(stage, "composer.lock")
    derived = _derived(plan, manifest, lock)
    _need(all(derived[key] == resolution[key] for key in derived), "derived install re-resolved or changed build inputs")
    inventory = _inventory(stage, _object(lock))
    _need(bool(_read(stage, "vendor/phpunit/phpunit/phpunit")), "installed PHPUnit executable is missing")
    vendor_hash = _vendor_hash(stage)
    record = {
        "schema": SCHEMA, "status": "prepared", "task_id": TASK_ID, "start_sha": START_SHA,
        "requirement": REQUIREMENT, "original_setup": ORIGINAL_SETUP, "historical_lock": False,
        "plan": copy.deepcopy(plan), "resolver": copy.deepcopy(resolver), "derived": derived,
        "original_install": copy.deepcopy(original_install), "resolution": copy.deepcopy(resolution),
        "derived_install": _checkpoint(plan, "derived_install", resolver, execution,
                                       installed_json_sha256=inventory, vendor_tree_sha256=vendor_hash),
        "files": {},
    }
    files = {
        "original/composer.json": original_manifest, "original/composer.lock": original_lock,
        "derived/composer.json": manifest, "derived/composer.lock": lock,
    }
    record["files"] = {name: _sha(data) for name, data in files.items()}
    _need(not destination.resolve().is_relative_to(stage.resolve()), "sealed inputs must be outside the mutable install stage")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for name, data in files.items():
            path = destination / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o444)
        data = _encoded(record)
        (destination / RECORD).write_bytes(data)
        (destination / RECORD).chmod(0o444)
    except Exception:
        shutil.rmtree(destination)
        raise
    return {
        "schema": SCHEMA, "record_sha256": _sha(data),
        "original_manifest_sha256": _sha(original_manifest), "original_lock_sha256": _sha(original_lock),
        "derived_manifest_sha256": _sha(manifest), "derived_lock_sha256": _sha(lock),
        "resolver_sha256": _fingerprint(resolver), "image": resolver["image"], "historical_lock": False,
    }


def load_prepared_tools(directory: Path, *, expected_sha256: str, image: str) -> dict:
    """Authenticate against the parent's external digest, not a self-declared hash."""
    directory = Path(directory)
    raw = _read(directory, RECORD)
    _need(re.fullmatch(r"[0-9a-f]{64}", expected_sha256) and _sha(raw) == expected_sha256,
          "sealed preparation record changed")
    record = _object(raw)
    _need(record.get("schema") == SCHEMA and record.get("status") == "prepared"
          and record.get("historical_lock") is False and record.get("requirement") == REQUIREMENT
          and record.get("original_setup") == ORIGINAL_SETUP
          and record.get("task_id") == TASK_ID and record.get("start_sha") == START_SHA,
          "not a sealed derived Kirby test-tool preparation")
    _resolver(record["resolver"])
    _need(record["resolver"]["image"] == image, "runtime image differs from actual resolver image")
    names = {f"{kind}/composer.{suffix}" for kind in ("original", "derived") for suffix in ("json", "lock")}
    _need(set(record["files"]) == names, "invalid sealed build input set")
    files = {name: _read(directory, name) for name in names}
    _need(all(_sha(data) == record["files"][name] for name, data in files.items()), "sealed build input bytes changed")
    plan = record["plan"]
    _originals(plan, files["original/composer.json"], files["original/composer.lock"])
    derived = _derived(plan, files["derived/composer.json"], files["derived/composer.lock"])
    _need(derived == record["derived"], "derived input binding changed")
    for phase, field in (("original_install", "original_install"), ("resolve_tools", "resolution"),
                         ("derived_install", "derived_install")):
        _check_checkpoint(plan, phase, record["resolver"], record[field])
    _need(all(derived[key] == record["resolution"][key] for key in derived), "resolution evidence changed")
    return record


def verify_sealed_install(directory: Path, stage: Path, *, expected_sha256: str, image: str) -> dict:
    """Parent calls this BEFORE extracting a candidate-preserving vendor payload."""
    record = load_prepared_tools(directory, expected_sha256=expected_sha256, image=image)
    stage = Path(stage)
    for name, field in (("composer.json", "manifest_sha256"), ("composer.lock", "lock_sha256")):
        _need(_sha(_read(stage, name)) == record["derived"][field], "derived install inputs changed after sealing")
    _need(_vendor_hash(stage) == record["derived_install"]["vendor_tree_sha256"],
          "derived installed dependencies changed after sealing")
    return record


def validate_export_package(package: Mapping, prepared: Mapping) -> None:
    """Exact inert-plugin export hook; no blanket composer-plugin allowance."""
    derived = _packages(prepared["plan"]["original"]["lock"])
    kind, name = package.get("type", "library"), package.get("name")
    _need(kind == "library" or (
        kind == "composer-plugin" and name == INERT_PLUGIN and name in derived
        and _fingerprint(_identity(package)) == _fingerprint(_identity(derived[name]))
    ), "package is not a library or the unchanged inert Kirby installer")
    _need(not package.get("target-dir") and package.get("dist", {}).get("type") != "path",
          "custom installation layout is not supported")


def composer_candidate_contract(prepared: Mapping, derived_manifest: bytes, derived_lock: bytes) -> dict:
    """Payload guard for the derived graph; do not use it to validate originals."""
    derived = _derived(prepared["plan"], derived_manifest, derived_lock)
    _need(derived == prepared["derived"], "wrong prepared derived inputs")
    return {"dependency_manifest": cc._manifest_contract(_object(derived_manifest)),
            "locked": cc._lock_contract(_object(derived_lock))}


def candidate_build_inputs(directory: Path, *, expected_sha256: str, image: str,
                           candidate_manifest: bytes, candidate_lock: bytes) -> dict:
    """Pure projection: return two authorized MIRROR replacements, write nothing.

    Parent's runtime hook performs this validation/projection against the CURRENT
    candidate before dump-autoload, and keeps the normal source/coverage mapping.
    No dependency resolver or post-update hook belongs in that runtime hook.
    """
    directory = Path(directory)
    record = load_prepared_tools(directory, expected_sha256=expected_sha256, image=image)
    original = record["plan"]["original"]
    cc.validate_candidate_inputs(candidate_manifest, candidate_lock, {
        "dependency_manifest": cc._manifest_contract(original["manifest"]),
        "locked": cc._lock_contract(original["lock"]),
    })
    current = _object(candidate_manifest)
    # Locker includes extra in its resolution inputs; do not silently change it.
    _need(_fingerprint(current.get("extra")) == _fingerprint(original["manifest"].get("extra")),
          "candidate changed extra resolution inputs")
    current.setdefault("require-dev", {})[PHPUNIT] = CONSTRAINT
    build_manifest = (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode()
    build_lock = _read(directory, "derived/composer.lock")
    contract = composer_candidate_contract(record, _read(directory, "derived/composer.json"), build_lock)
    cc.validate_candidate_inputs(build_manifest, build_lock, contract)
    return {
        "files": {"composer.json": build_manifest, "composer.lock": build_lock},
        "source_manifest_sha256": _sha(candidate_manifest), "source_lock_sha256": _sha(candidate_lock),
        "build_manifest_sha256": _sha(build_manifest), "build_lock_sha256": _sha(build_lock),
        "prepared_record_sha256": expected_sha256, "historical_lock": False,
        "changed_manifest_fields": [["require-dev", PHPUNIT]],
    }


def runtime_contract() -> dict:
    """Instructions/data for the parent-owned PHP mirror entry; never resolve."""
    return {
        "overlay_paths": ["composer.json", "composer.lock"],
        "allow_resolution": False, "environment": {"COMPOSER_DISABLE_NETWORK": "1", "COMPOSER_NO_DEV": "0"},
        "composer_argv": ["composer", "dump-autoload", "--dev", "--no-interaction", "--no-scripts", "--no-plugins"],
        "phpunit_entry": "vendor/phpunit/phpunit/phpunit",
        "phpunit_version_probe_argv": ["php", "vendor/phpunit/phpunit/phpunit", "--version"],
        "phpunit_version_pattern": r"^PHPUnit 11\.(?:[5-9]|[1-9][0-9]+)\.\d+(?:\s|$)",
        "fixed_selector": "tests/Cms/ResponderTest.php::testFromArray",
        "coverage_target": "src/Cms/Responder.php",
        "coverage_normalization": "execute mirror target; restore canonical candidate target field only",
        "inert_plugin": INERT_PLUGIN,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "stage"):
        command = actions.add_parser(action)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--lock", type=Path, required=True)
        command.add_argument("--task-id", required=True)
        command.add_argument("--start-sha", required=True)
        if action == "stage":
            command.add_argument("--out", type=Path, required=True)
    actions.add_parser("runtime-contract")
    args = parser.parse_args(argv)
    try:
        if args.action == "runtime-contract":
            result = runtime_contract()
        else:
            manifest, lock = args.manifest.read_bytes(), args.lock.read_bytes()
            result = plan_test_tools({"id": args.task_id, "start_sha": args.start_sha}, manifest, lock)
            if args.action == "stage":
                stage_test_tools(args.out, result, manifest, lock)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (ValueError, OSError) as error:
        parser.exit(2, "TestToolsError: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
