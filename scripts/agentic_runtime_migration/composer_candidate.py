"""Build-only Composer metadata for the frozen PrivateBin scripted task.

Integration (the caller still owns image pinning, leases and frozen assertions):

* Stage ONLY the start's composer.json/composer.lock with stage_locked_install,
  then run locked_install_command in that directory using the pinned image.
* export_dependency_payload packages the verified locked prod+dev install,
  minus EVERY tracked vendor source path. Pass the complete start-tree names,
  including deleted candidate paths, not a list of files currently present.
* Bundle write_php_runtime's output alongside this payload. runtime_command
  rebuilds a whole CURRENT candidate mirror before each development/grading
  invocation. Restore the fixed harness in the candidate before invoking it.
  No vendor path in the actual candidate becomes scratch or is overwritten.

The emitted entry point uses PHP 7.4, not Python, because the original PHP image
has no Python runtime. It retains Composer's installed-version seed files from
the manifest-only install: dump-autoload does not regenerate those files.
Only the explicit metadata filenames below can be replaced in the mirror.
Dependency graph changes fail explicitly; root autoload edits, source additions,
edits and deletions, and non-dependency manifest/lock formatting edits survive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
from typing import Iterable, Mapping


TASK_ID = "github__PrivateBin__PrivateBin__pull_1014__comment_1018461878__modularity"
START_SHA = "0288d94a68c23b940a057f60b073017aa45babb8"
SCHEMA = "composer-current-candidate-v1"
RECORD = "composer-candidate.json"
# NOT vendor/composer/**: composer/semver and custom files are source, too.
METADATA = frozenset({"vendor/autoload.php"} | {
    "vendor/composer/" + name for name in (
        "autoload_classmap.php", "autoload_files.php", "autoload_namespaces.php",
        "autoload_psr4.php", "autoload_real.php", "autoload_static.php",
        "include_paths.php", "platform_check.php", "ClassLoader.php", "LICENSE",
        "InstalledVersions.php", "installed.php", "installed.json",
    )
})
SEEDS = frozenset("vendor/composer/" + name for name in (
    "installed.json", "installed.php", "InstalledVersions.php",
))
DEPENDENCY_KEYS = (
    "name", "version", "require", "require-dev", "conflict", "replace", "provide",
    "repositories", "minimum-stability", "prefer-stable",
)


class ComposerCandidateError(ValueError):
    pass


def applies_to(task: Mapping, plan: Mapping) -> bool:
    """Deliberately opt in just this frozen root-workspace, prod+dev task."""
    return (
        task.get("id") == TASK_ID and task.get("start_sha") == START_SHA
        and plan.get("kind") == "composer" and plan.get("workspace") == "."
        and plan.get("no_dev") is False and not plan.get("platform_flags")
        and not plan.get("root_version")
    )


def _relative(value: str) -> str:
    if (not isinstance(value, str) or not value or "\0" in value or "\\" in value
            or ":" in value or value.startswith("/")
            or any(p in {"", ".", "..", ".git", ".hg", ".svn", ".reviewbench"}
                   for p in value.split("/"))):
        raise ComposerCandidateError("invalid relative payload/source path: " + repr(value))
    return value


def is_composer_metadata(name: str) -> bool:
    return name in METADATA


def requires_candidate_mirror(tracked: Iterable[str]) -> bool:
    return any(is_composer_metadata(name) for name in tracked)


def _json(data: bytes) -> dict:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ComposerCandidateError("invalid Composer JSON input") from exc
    if not isinstance(value, dict):
        raise ComposerCandidateError("Composer input must be a JSON object")
    return value


def _manifest_contract(manifest: dict) -> dict:
    config = manifest.get("config", {})
    if not isinstance(config, dict):
        raise ComposerCandidateError("Composer config must be an object")
    if config.get("vendor-dir", "vendor") != "vendor" or config.get("bin-dir", "vendor/bin") != "vendor/bin":
        raise ComposerCandidateError("adapter requires default vendor and bin directories")
    if manifest.get("type") == "composer-plugin" or manifest.get("target-dir"):
        raise ComposerCandidateError("adapter requires a root project without a custom installer")
    for field in ("autoload", "autoload-dev"):
        autoload = manifest.get(field, {})
        if not isinstance(autoload, dict):
            raise ComposerCandidateError("invalid root autoload definition")
        paths = list(autoload.get("files", [])) + list(autoload.get("classmap", []))
        for kind in ("psr-0", "psr-4"):
            for entries in autoload.get(kind, {}).values():
                paths.extend([entries] if isinstance(entries, str) else entries)
        for path in paths:
            if not isinstance(path, str):
                raise ComposerCandidateError("invalid root autoload path")
            if path not in {"", ".", "./"}:
                _relative(path.removeprefix("./").rstrip("/"))
    return {
        "fields": {key: manifest[key] for key in DEPENDENCY_KEYS if key in manifest},
        "platform": config.get("platform", {}),
    }


def _lock_contract(lock: dict) -> dict:
    # Autoload edits/formatting do not authorize a different locked graph.
    return {key: value for key, value in lock.items() if key not in {"_readme", "content-hash"}}


def _packages(lock: dict) -> dict[str, dict]:
    result = {}
    for field in ("packages", "packages-dev"):
        if not isinstance(lock.get(field), list):
            raise ComposerCandidateError("lock must enumerate production AND dev packages")
        for package in lock[field]:
            if not isinstance(package, dict):
                raise ComposerCandidateError("invalid locked package entry")
            name = package.get("name", "")
            if (not isinstance(name, str)
                    or not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", name) or name in result):
                raise ComposerCandidateError("invalid or duplicate locked package")
            _relative(name)
            if (package.get("type", "library") != "library" or package.get("target-dir")
                    or package.get("dist", {}).get("type") == "path"):
                raise ComposerCandidateError("locked package needs a separate installer/path adapter: " + name)
            if not isinstance(package.get("version"), str):
                raise ComposerCandidateError("locked package lacks an exact version")
            result[name] = package
    return result


def locked_install_command() -> str:
    """No update, scripts, plugins, dev omission, or start-source autoload scan."""
    return shlex.join([
        "env", "COMPOSER=composer.json", "COMPOSER_NO_DEV=0",
        "COMPOSER_VENDOR_DIR=vendor", "COMPOSER_BIN_DIR=vendor/bin",
        "composer", "install", "--no-interaction", "--prefer-dist", "--no-progress",
        "--no-scripts", "--no-plugins", "--no-autoloader",
    ])


def stage_locked_install(destination: Path, manifest: bytes, lock: bytes) -> dict:
    """Create a NEW manifest-only install root; never install over a checkout."""
    _manifest_contract(_json(manifest))
    _packages(_json(lock))
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "composer.json").write_bytes(manifest)
    (destination / "composer.lock").write_bytes(lock)
    return {
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
        "command": locked_install_command(),
    }


def _tree(root: Path) -> dict[str, Path]:
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = _relative(path.relative_to(root).as_posix())
            if path.is_symlink() or not path.is_dir():
                result[relative] = path
    return dict(sorted(result.items()))


def _file_record(path: Path, root: Path) -> list:
    if path.is_symlink():
        target = os.readlink(path)
        if (os.path.isabs(target) or not path.resolve().is_relative_to(root.resolve())
                or any(part in {".git", ".hg", ".svn"} for part in Path(target).parts)):
            raise ComposerCandidateError("dependency link escapes its history-free vendor tree")
        return ["link", target]
    if not stat.S_ISREG(path.stat().st_mode):
        raise ComposerCandidateError("dependency payload contains a non-regular file")
    return ["file", hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(path.stat().st_mode)]


def export_dependency_payload(
    installed: Path, destination: Path, tracked: Iterable[str], *,
    manifest: bytes, lock: bytes,
) -> dict:
    """Export locked additions and Composer seed metadata, never start source.

    ``installed`` MUST be stage_locked_install's manifest-only root after a
    successful pinned install. The caller retains the install exit/log and
    binds the resulting payload fingerprint to its task/image preparation.
    """
    installed, destination = Path(installed), Path(destination)
    manifest_data, lock_data = _json(manifest), _json(lock)
    contract, packages = _manifest_contract(manifest_data), _packages(lock_data)
    protected = sorted({_relative(name) for name in tracked
                        if name.startswith("vendor/") and name not in METADATA})
    if installed.is_symlink() or (installed / "vendor").is_symlink():
        raise ComposerCandidateError("install root/vendor must be real directories")
    if {path.name for path in installed.iterdir()} != {"composer.json", "composer.lock", "vendor"}:
        raise ComposerCandidateError("export requires a manifest-only install, not a historical checkout")
    for name, original in (("composer.json", manifest), ("composer.lock", lock)):
        path = installed / name
        if path.is_symlink() or path.read_bytes() != original:
            raise ComposerCandidateError("install changed the original " + name)
    files = _tree(installed / "vendor")
    if any(not (installed / name).is_file() or (installed / name).is_symlink() for name in SEEDS):
        raise ComposerCandidateError("Composer installed-version seed metadata is missing")
    inventory = _json((installed / "vendor/composer/installed.json").read_bytes())
    actual = inventory.get("packages", [])
    if (inventory.get("dev") is not True or not isinstance(actual, list)
            or not all(isinstance(package, dict) for package in actual)
            or len(actual) != len(packages)
            or {p.get("name") for p in actual} != packages.keys()
            or sorted(inventory.get("dev-package-names", []))
            != sorted(p["name"] for p in lock_data["packages-dev"])):
        raise ComposerCandidateError("install does not contain exactly all locked prod+dev packages")
    for package in actual:
        original = packages[package["name"]]
        for key in ("name", "version", "source", "dist", "autoload", "bin", "require", "provide", "replace"):
            if package.get(key) != original.get(key):
                raise ComposerCandidateError("installed package differs from lock: " + package["name"] + ":" + key)
        if (package.get("type", "library") != "library"
                or package.get("install-path") != "../" + package["name"]
                or not (installed / "vendor" / package["name"]).is_dir()):
            raise ComposerCandidateError("installed package uses an unexpected install path/type")
    binaries = {"vendor/bin/" + PurePosixPath(_relative(name)).name
                for package in packages.values() for name in package.get("bin", [])}
    if any(name.removeprefix("vendor/") not in files for name in binaries):
        raise ComposerCandidateError("locked dependency binary proxy is missing")
    selected = {}
    for relative, path in files.items():
        name = "vendor/" + relative
        _file_record(path, installed / "vendor")
        if name in METADATA:
            if name in SEEDS:
                selected[name] = path
        elif name in protected:
            # This exclusion is also a deletion tombstone: never rehydrate it.
            continue
        elif name in binaries or any(name.startswith("vendor/" + p + "/") for p in packages):
            selected[name] = path
        else:
            raise ComposerCandidateError("unexpected file in locked install: " + name)
    if destination.exists() or destination.is_symlink():
        raise ComposerCandidateError("payload destination must be new")
    if destination.resolve().is_relative_to(installed.resolve()):
        raise ComposerCandidateError("payload must be outside the install root")
    destination.mkdir(parents=True)
    try:
        for name, path in selected.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                target.symlink_to(os.readlink(path))
            else:
                shutil.copy2(path, target)
        record = {
            "schema": SCHEMA, "workspace": ".", "scratch_paths": [],
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "lock_sha256": hashlib.sha256(lock).hexdigest(),
            "dependency_manifest": contract, "locked": _lock_contract(lock_data),
            "protected_vendor_paths": protected,
            "production_packages": sorted(p["name"] for p in lock_data["packages"]),
            "dev_packages": sorted(p["name"] for p in lock_data["packages-dev"]),
            "files": {name: _file_record(path, installed / "vendor") for name, path in selected.items()},
        }
        (destination / RECORD).write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    except Exception:
        shutil.rmtree(destination)
        raise
    return record


def validate_candidate_inputs(manifest: bytes, lock: bytes, record: Mapping) -> None:
    """Accept current autoload/formatting, reject an unprepared dependency graph."""
    if (_manifest_contract(_json(manifest)) != record["dependency_manifest"]
            or _lock_contract(_json(lock)) != record["locked"]):
        raise ComposerCandidateError("candidate dependency graph differs from the prepared lock")


def relocate_candidate_paths(text: str, source: str, mirror: str) -> str:
    """Relocate checkout references, not sibling paths or grader scratch paths."""
    source, mirror = source.rstrip("/"), mirror.rstrip("/")
    return re.sub(
        r"(?<![A-Za-z_0-9/.-])" + re.escape(source)
        + r"(?!/\.reviewbench(?=/|$|[\s'\";]))(?=/|$|[\s'\";])",
        lambda match: mirror, text,
    )


def runtime_command(
    entry: str, payload: str, mirror: str, command: str | None = None, *,
    source: str = "/workspace", env: Mapping[str, str] | None = None,
) -> str:
    """Wrap the unchanged normalized PHP test payload; selectors stay external."""
    environment = []
    for key, value in sorted((env or {}).items()):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ComposerCandidateError("invalid runtime environment name")
        environment.append(key + "=" + value)
    argv = ["env", *environment, "php", entry, source, payload, mirror]
    if command is not None:
        argv += ["--", "bash", "-c", relocate_candidate_paths(command, source, mirror)]
    return shlex.join(argv)


# This is emitted verbatim as a developer tool asset, never into candidate code.
# Composer's CLI and FilesystemRepository are the only metadata generators:
# https://getcomposer.org/doc/03-cli.md#dump-autoload-dumpautoload
# https://github.com/composer/composer/blob/2.10.3/src/Composer/Repository/FilesystemRepository.php
_PHP_RUNTIME = r'''<?php
declare(strict_types=1);

const CC_SCHEMA = 'composer-current-candidate-v1';
const CC_RECORD = 'composer-candidate.json';
const CC_METADATA = __METADATA__;
const CC_DEPENDENCY_KEYS = __DEPENDENCY_KEYS__;

function cc_need($ok, string $message): void {
    if (!$ok) { throw new RuntimeException($message); }
}
function cc_json(string $file): array {
    $value = json_decode((string) file_get_contents($file), true, 512, JSON_THROW_ON_ERROR);
    cc_need(is_array($value), 'invalid JSON object: ' . $file);
    return $value;
}
function cc_within(string $path, string $root): bool {
    return $path === $root || strpos($path, $root . '/') === 0;
}
function cc_relative(string $path): string {
    cc_need($path !== '' && !preg_match('~[\\\\:\x00]|^/|(^|/)(\.{1,2}|\.git|\.hg|\.svn|\.reviewbench)?(/|$)~', $path),
        'invalid relative path: ' . $path);
    return $path;
}
function cc_normal(string $path): string {
    cc_need(isset($path[0]) && $path[0] === '/', 'absolute path required');
    $parts = [];
    foreach (explode('/', $path) as $part) {
        if ($part === '' || $part === '.') { continue; }
        if ($part === '..') { array_pop($parts); } else { $parts[] = $part; }
    }
    return '/' . implode('/', $parts);
}
function cc_real_parents(string $path): void {
    for ($p = $path; $p !== '/'; $p = dirname($p)) {
        cc_need(!is_link($p), 'symlink in build/payload path: ' . $p);
    }
}
function cc_equal($left, $right): bool {
    if (gettype($left) !== gettype($right)) { return false; }
    if (!is_array($left)) { return $left === $right; }
    if (count($left) !== count($right)) { return false; }
    foreach ($left as $key => $value) {
        if (!array_key_exists($key, $right) || !cc_equal($value, $right[$key])) { return false; }
    }
    return true;
}
function cc_contract(array $manifest): array {
    $config = $manifest['config'] ?? [];
    cc_need(($config['vendor-dir'] ?? 'vendor') === 'vendor'
        && ($config['bin-dir'] ?? 'vendor/bin') === 'vendor/bin', 'non-default vendor/bin directory');
    cc_need(($manifest['type'] ?? '') !== 'composer-plugin' && empty($manifest['target-dir']),
        'custom root installer');
    foreach (['autoload', 'autoload-dev'] as $field) {
        $autoload = $manifest[$field] ?? [];
        $paths = array_merge($autoload['files'] ?? [], $autoload['classmap'] ?? []);
        foreach (['psr-0', 'psr-4'] as $kind) {
            foreach (($autoload[$kind] ?? []) as $entries) {
                $paths = array_merge($paths, is_array($entries) ? $entries : [$entries]);
            }
        }
        foreach ($paths as $path) {
            cc_need(is_string($path), 'invalid root autoload path');
            if (!in_array($path, ['', '.', './'], true)) {
                cc_relative(rtrim(strpos($path, './') === 0 ? substr($path, 2) : $path, '/'));
            }
        }
    }
    $fields = [];
    foreach (CC_DEPENDENCY_KEYS as $key) {
        if (array_key_exists($key, $manifest)) { $fields[$key] = $manifest[$key]; }
    }
    return ['fields' => $fields, 'platform' => $config['platform'] ?? []];
}
function cc_tree(string $root, bool $candidate = false, string $prefix = '', bool $directories = false): array {
    $out = [];
    foreach (scandir($root . ($prefix === '' ? '' : '/' . $prefix)) as $name) {
        if ($name === '.' || $name === '..') { continue; }
        if ($candidate && ($name === '.git' || ($prefix === '' && $name === '.reviewbench'))) { continue; }
        $relative = $prefix === '' ? $name : $prefix . '/' . $name;
        cc_relative($relative);
        $path = $root . '/' . $relative;
        if (is_link($path)) {
            $target = readlink($path);
            $resolved = cc_normal($target[0] === '/' ? $target : dirname($path) . '/' . $target);
            cc_need(cc_within($resolved, $root) && !cc_within($resolved, $root . '/.reviewbench')
                && !preg_match('~/(?:\.git|\.hg|\.svn)(?:/|$)~', $resolved), 'link escapes tree: ' . $relative);
            $real = realpath($path);
            cc_need($real === false || cc_within($real, $root), 'indirect link escapes tree');
            $out[$relative] = ['link', $target];
        } elseif (is_dir($path)) {
            if ($candidate || $directories) { $out[$relative] = ['dir', fileperms($path) & 0777]; }
            $out += cc_tree($root, $candidate, $relative, $directories);
        } else {
            cc_need(is_file($path), 'non-regular tree entry: ' . $relative);
            $out[$relative] = ['file', hash_file('sha256', $path), fileperms($path) & 0777];
        }
    }
    ksort($out);
    return $out;
}
function cc_remove(string $path): void {
    if (is_link($path) || !is_dir($path)) {
        if (file_exists($path) || is_link($path)) { cc_need(unlink($path), 'unlink failed'); }
        return;
    }
    foreach (scandir($path) as $name) {
        if ($name !== '.' && $name !== '..') { cc_remove($path . '/' . $name); }
    }
    cc_need(rmdir($path), 'rmdir failed');
}
function cc_mkdir(string $path): void {
    cc_real_parents($path);
    cc_need(is_dir($path) || mkdir($path, 0777, true), 'mkdir failed: ' . $path);
}
function cc_copy(string $from, string $to, string $source, string $mirror): void {
    cc_mkdir(dirname($to));
    if (is_dir($from) && !is_link($from)) {
        cc_mkdir($to);
        cc_need(chmod($to, fileperms($from) & 0777), 'directory mode copy failed');
    } elseif (is_link($from)) {
        $link = readlink($from);
        // Relative links retain their topology. Absolute in-candidate links
        // must resolve inside the mirror, not back into the writable source.
        if ($link[0] === '/') { $link = $mirror . substr(cc_normal($link), strlen($source)); }
        cc_need(symlink($link, $to), 'symlink copy failed');
    } else {
        cc_need(copy($from, $to) && chmod($to, fileperms($from) & 0777), 'file copy failed');
    }
}
function cc_nonmetadata(array $tree): array {
    return array_diff_key($tree, array_fill_keys(CC_METADATA, true));
}
function cc_run(array $command, string $cwd, array $environment): int {
    $process = proc_open($command, [0 => STDIN, 1 => STDOUT, 2 => STDERR], $pipes, $cwd, $environment);
    cc_need(is_resource($process), 'process creation failed');
    return proc_close($process);
}
function cc_build(string $source, string $payload, string $mirror): array {
    $record = cc_json($payload . '/' . CC_RECORD);
    cc_need(($record['schema'] ?? '') === CC_SCHEMA && ($record['workspace'] ?? '') === '.',
        'invalid Composer candidate payload schema');
    $payloadTree = cc_tree($payload);
    unset($payloadTree[CC_RECORD]);
    cc_need(cc_equal($payloadTree, $record['files']), 'prepared dependency bytes/modes changed');
    foreach ($record['protected_vendor_paths'] as $path) {
        cc_relative($path);
        cc_need(!isset($payloadTree[$path]), 'payload contains historical candidate source');
    }
    $before = cc_tree($source, true);
    cc_need(isset($before['composer.json'], $before['composer.lock'])
        && $before['composer.json'][0] === 'file' && $before['composer.lock'][0] === 'file',
        'candidate manifests must be regular files');
    $manifest = cc_json($source . '/composer.json');
    $lock = cc_json($source . '/composer.lock');
    unset($lock['_readme'], $lock['content-hash']);
    cc_need(cc_equal(cc_contract($manifest), $record['dependency_manifest'])
        && cc_equal($lock, $record['locked']), 'candidate dependency graph differs from the prepared lock');
    $owner = $mirror . '.owner';
    cc_real_parents($owner);
    $ownership = CC_SCHEMA . "\n" . $source . "\n";
    if (file_exists($mirror) || is_link($mirror)) {
        cc_need(!is_link($mirror) && is_file($owner)
            && file_get_contents($owner) === $ownership, 'existing mirror is not adapter-owned');
        cc_remove($mirror);
    }
    cc_mkdir($mirror);
    file_put_contents($owner, $ownership);
    try {
        foreach ($before as $name => $state) {
            if (!in_array($name, CC_METADATA, true)) {
                cc_copy($source . '/' . $name, $mirror . '/' . $name, $source, $mirror);
            }
        }
        foreach ($payloadTree as $name => $state) {
            cc_need(strpos($name, 'vendor/') === 0, 'non-vendor dependency payload');
            $to = $mirror . '/' . $name;
            if (file_exists($to) || is_link($to)) { continue; } // CURRENT candidate wins.
            cc_copy($payload . '/' . $name, $to, $payload, $mirror);
        }
        $expected = cc_nonmetadata(cc_tree($mirror, false, '', true));
        $home = $mirror . '.composer-home';
        cc_real_parents($home);
        cc_need(!file_exists($home), 'stale Composer home');
        cc_mkdir($home);
        try {
            $env = getenv();
            foreach (array_keys($env) as $key) {
                if (strpos($key, 'COMPOSER_') === 0 || $key === 'COMPOSER') { unset($env[$key]); }
            }
            $env = array_merge($env, [
                'HOME' => $home, 'COMPOSER_HOME' => $home, 'COMPOSER_CACHE_DIR' => '/dev/null',
                'COMPOSER' => 'composer.json', 'COMPOSER_NO_DEV' => '0',
                'COMPOSER_VENDOR_DIR' => 'vendor', 'COMPOSER_BIN_DIR' => 'vendor/bin',
                'COMPOSER_DISABLE_NETWORK' => '1',
            ]);
            $status = cc_run(['composer', 'dump-autoload', '--dev', '--no-interaction',
                '--no-scripts', '--no-plugins'], $mirror, $env);
        } finally { cc_remove($home); }
        cc_need($status === 0, 'Composer dump-autoload failed with exit ' . $status);
        $after = cc_tree($mirror, false, '', true);
        cc_need(cc_equal($expected, cc_nonmetadata($after)), 'Composer changed non-metadata source');
        cc_need(isset($after['vendor/autoload.php']), 'Composer did not generate an autoloader');
        cc_need(cc_equal($before, cc_tree($source, true)), 'candidate changed while building mirror');
        return ['mirror' => $mirror, 'source_unchanged' => true,
            'metadata_paths' => array_values(array_intersect(array_keys($after), CC_METADATA))];
    } catch (Throwable $error) {
        cc_remove($mirror);
        cc_remove($owner);
        throw $error;
    }
}
function cc_main(array $argv): int {
    cc_need(count($argv) >= 4, 'usage: entry.php SOURCE PAYLOAD MIRROR [-- COMMAND ...]');
    [$entry, $sourceArg, $payloadArg, $mirrorArg] = $argv;
    cc_real_parents(cc_normal($sourceArg));
    cc_real_parents(cc_normal($payloadArg));
    $source = realpath($sourceArg);
    $payload = realpath($payloadArg);
    cc_need($source !== false && $payload !== false && is_dir($source) && is_dir($payload),
        'source and payload directories must exist');
    $mirror = cc_normal($mirrorArg);
    cc_real_parents($mirror);
    cc_need(!cc_within($source, $mirror) && !cc_within($payload, $mirror)
        && !cc_within($mirror, $payload) && !cc_within($payload, $source)
        && (!cc_within($mirror, $source) || cc_within($mirror, $source . '/.reviewbench')),
        'source, dependency payload and build mirror overlap');
    if (count($argv) > 4) {
        cc_need($argv[4] === '--' && count($argv) > 5, 'command must follow --');
    }
    $built = cc_build($source, $payload, $mirror);
    if (count($argv) === 4) {
        echo json_encode($built, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR), "\n";
        return 0;
    }
    $before = cc_tree($source, true);
    $environment = getenv();
    $target = $environment['ORACLE_TARGET'] ?? '';
    $coverage = $environment['ORACLE_COVERAGE_JSON'] ?? '';
    $canonicalTarget = null;
    $mappedTarget = null;
    if ($target !== '') {
        $canonicalTarget = realpath($target[0] === '/' ? $target : $source . '/' . $target);
        cc_need($canonicalTarget !== false && cc_within($canonicalTarget, $source)
            && !cc_within($canonicalTarget, $source . '/.reviewbench'), 'coverage target is not candidate source');
        $mappedTarget = realpath($mirror . substr($canonicalTarget, strlen($source)));
        cc_need($mappedTarget !== false && cc_within($mappedTarget, $mirror), 'coverage target missing in mirror');
        $environment['ORACLE_TARGET'] = $mappedTarget;
    }
    if ($coverage !== '') {
        $coverage = cc_normal($coverage);
        cc_need(!cc_within($coverage, $source) || cc_within($coverage, $source . '/.reviewbench'),
            'coverage output would overwrite candidate source');
        cc_real_parents($coverage);
        if (is_file($coverage)) { cc_need(unlink($coverage), 'stale coverage removal failed'); }
        $environment['ORACLE_COVERAGE_JSON'] = $coverage;
    }
    if (isset($environment['HOME']) && cc_within($environment['HOME'], $source)
        && !cc_within($environment['HOME'], $source . '/.reviewbench')) {
        $environment['HOME'] = $mirror . substr($environment['HOME'], strlen($source));
    }
    $status = cc_run(array_slice($argv, 5), $mirror, $environment);
    cc_need(cc_equal($before, cc_tree($source, true)), 'test command changed original candidate source');
    if ($coverage !== '' && is_file($coverage) && $mappedTarget !== null) {
        $data = cc_json($coverage);
        cc_need(($data['target'] ?? null) === $mappedTarget, 'coverage target does not match executed mirror');
        // Relocate the path only. Lines/backend and the test's exit stay intact.
        $data['target'] = $canonicalTarget;
        cc_need(file_put_contents($coverage, json_encode($data, JSON_THROW_ON_ERROR)) !== false,
            'coverage path normalization failed');
    }
    return $status;
}
try { exit(cc_main($argv)); }
catch (Throwable $error) {
    fwrite(STDERR, 'ComposerCandidateError: ' . $error->getMessage() . "\n");
    exit(2);
}
'''


def php_runtime_source() -> str:
    return (_PHP_RUNTIME
            .replace("__METADATA__", json.dumps(sorted(METADATA)))
            .replace("__DEPENDENCY_KEYS__", json.dumps(DEPENDENCY_KEYS)))


def write_php_runtime(destination: Path) -> Path:
    destination = Path(destination)
    destination.write_text(php_runtime_source())
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("stage", "export"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--lock", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "export":
            command.add_argument("--installed", type=Path, required=True)
            command.add_argument("--tracked", type=Path, required=True,
                                 help="JSON array of complete start-tree tracked paths")
    commands.add_parser("emit-runtime").add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "emit-runtime":
            print(write_php_runtime(args.out))
        elif args.action == "stage":
            print(json.dumps(stage_locked_install(args.out, args.manifest.read_bytes(), args.lock.read_bytes()),
                             sort_keys=True))
        else:
            record = export_dependency_payload(
                args.installed, args.out, json.loads(args.tracked.read_text()),
                manifest=args.manifest.read_bytes(), lock=args.lock.read_bytes(),
            )
            print(json.dumps(record, sort_keys=True))
    except (ComposerCandidateError, OSError, ValueError) as exc:
        parser.exit(2, "ComposerCandidateError: " + str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
