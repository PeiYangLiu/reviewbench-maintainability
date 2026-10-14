"""Kirby-only dependency exporter and independent PHP current-candidate runtime.

This module never changes composer_candidate or its emitted PrivateBin PHP.
The exported vendor additions and sealed derived inputs are one immutable asset.
Only explicit Composer metadata and two declared mirror build inputs can differ
from the complete current candidate. No resolver runs at development/grading.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
from typing import Iterable

from . import composer_candidate as cc
from . import composer_test_tools as tools

SCHEMA = "composer-kirby-current-candidate-v1"
RECORD = "composer-kirby-payload.json"
BUILD_INPUTS = "build-inputs"


def tree_fingerprint(root: Path) -> str:
    """Bind bytes, links and modes independently of any mutable start checkout."""
    root = Path(root)
    return tools._fingerprint({name: cc._file_record(path, root)
                               for name, path in cc._tree(root).items()})


def export_payload(installed: Path, destination: Path, tracked: Iterable[str], *,
                   sealed: Path, expected_sha256: str, image: str) -> dict:
    """History-free, candidate-preserving exporter with the exact inert plugin."""
    installed, destination, sealed = map(Path, (installed, destination, sealed))
    prepared = tools.verify_sealed_install(sealed, installed, expected_sha256=expected_sha256, image=image)
    derived_manifest = tools._read(sealed, "derived/composer.json")
    derived_lock = tools._read(sealed, "derived/composer.lock")
    packages = tools._packages(tools._object(derived_lock))
    for package in packages.values():
        tools.validate_export_package(package, prepared)
    protected = sorted({cc._relative(name) for name in tracked
                        if name.startswith("vendor/") and name not in cc.METADATA})
    binaries = {"vendor/bin/" + PurePosixPath(cc._relative(binary)).name
                for package in packages.values() for binary in package.get("bin", [])}
    selected = {}
    vendor = installed / "vendor"
    for relative, path in cc._tree(vendor).items():
        name = "vendor/" + relative
        cc._file_record(path, vendor)
        if name in cc.METADATA:
            if name in cc.SEEDS:
                selected[name] = path
        elif name in protected:
            continue
        elif name in binaries or any(name.startswith("vendor/" + p + "/") for p in packages):
            selected[name] = path
        else:
            raise tools.TestToolsError("unexpected installed vendor file: " + name)
    tools._need(all((installed / name).exists() for name in binaries), "missing dependency bin proxy")
    tools._need(not destination.exists() and not destination.is_symlink(), "payload destination must be new")
    tools._need(not destination.resolve().is_relative_to(installed.resolve())
                and not destination.resolve().is_relative_to(sealed.resolve()), "payload overlaps build inputs")
    destination.mkdir(parents=True)
    try:
        for name, path in selected.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                target.symlink_to(os.readlink(path))
            else:
                shutil.copy2(path, target)
        for name in [tools.RECORD, *sorted(prepared["files"])]:
            target = destination / BUILD_INPUTS / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sealed / name, target)
        contract = tools.composer_candidate_contract(prepared, derived_manifest, derived_lock)
        record = {
            "schema": SCHEMA, "task_id": tools.TASK_ID, "start_sha": tools.START_SHA,
            "image": image, "sealed_sha256": expected_sha256, "historical_lock": False,
            "protected_vendor_paths": protected, "scratch_paths": [], **contract,
            "files": {name: cc._file_record(path, destination)
                      for name, path in cc._tree(destination).items()},
        }
        (destination / RECORD).write_bytes(tools._encoded(record))
    except Exception:
        shutil.rmtree(destination)
        raise
    validate_payload(destination, expected_sha256=expected_sha256, image=image)
    return record


def validate_payload(payload: Path, *, expected_sha256: str, image: str) -> dict:
    payload = Path(payload)
    record = tools._object(tools._read(payload, RECORD))
    tools._need(record.get("schema") == SCHEMA and record.get("task_id") == tools.TASK_ID
                and record.get("start_sha") == tools.START_SHA and record.get("image") == image
                and record.get("sealed_sha256") == expected_sha256 and record.get("historical_lock") is False,
                "Kirby payload identity changed")
    actual = {name: cc._file_record(path, payload) for name, path in cc._tree(payload).items() if name != RECORD}
    tools._need(actual == record.get("files"), "Kirby payload bytes/modes changed")
    tools._need(not set(record["protected_vendor_paths"]) & actual.keys(),
                "payload contains historical candidate vendor source")
    prepared = tools.load_prepared_tools(payload / BUILD_INPUTS, expected_sha256=expected_sha256, image=image)
    contract = tools.composer_candidate_contract(
        prepared, tools._read(payload / BUILD_INPUTS, "derived/composer.json"),
        tools._read(payload / BUILD_INPUTS, "derived/composer.lock"))
    tools._need(all(record[key] == value for key, value in contract.items()), "payload graph differs from sealed inputs")
    return record


def runtime_command(entry: str, payload: str, mirror: str, image: str, sealed_sha256: str,
                    command: str | None = None, *, source="/workspace", env=None) -> str:
    environment = []
    for key, value in sorted((env or {}).items()):
        tools._need(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key) and isinstance(value, str),
                    "invalid Kirby runtime environment")
        environment.append(key + "=" + value)
    argv = ["env", *environment, "php", entry, source, payload, mirror, image, sealed_sha256]
    if command is not None:
        argv += ["--", "bash", "-c", cc.relocate_candidate_paths(command, source, mirror)]
    return shlex.join(argv)


_PHP = r'''<?php
declare(strict_types=1);
const KT_SCHEMA = 'composer-kirby-current-candidate-v1';
const KT_TASK = __TASK__;
const KT_START = __START__;
const KT_METADATA = __METADATA__;
const KT_KEYS = __KEYS__;

function kt_need($ok, string $message): void {
    if (!$ok) { throw new RuntimeException($message); }
}
function kt_json(string $path): array {
    $data = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
    kt_need(is_array($data), 'invalid JSON input: ' . $path);
    return $data;
}
function kt_within(string $path, string $root): bool {
    return $path === $root || strpos($path, $root . '/') === 0;
}
function kt_normal(string $path): string {
    kt_need(isset($path[0]) && $path[0] === '/', 'absolute path required');
    $parts = [];
    foreach (explode('/', $path) as $part) {
        if ($part === '' || $part === '.') { continue; }
        if ($part === '..') { array_pop($parts); } else { $parts[] = $part; }
    }
    return '/' . implode('/', $parts);
}
function kt_relative(string $path): string {
    kt_need($path !== '' && !preg_match('~[\\\\:\x00]|^/|(^|/)(\.{1,2}|\.git|\.hg|\.svn|\.reviewbench)?(/|$)~', $path),
        'invalid relative path: ' . $path);
    return $path;
}
function kt_real_parents(string $path): void {
    for ($p = $path; $p !== '/'; $p = dirname($p)) {
        kt_need(!is_link($p), 'symlink in build/payload path: ' . $p);
    }
}
function kt_equal($a, $b): bool {
    if (gettype($a) !== gettype($b)) { return false; }
    if (!is_array($a)) { return $a === $b; }
    if (count($a) !== count($b)) { return false; }
    foreach ($a as $key => $value) {
        if (!array_key_exists($key, $b) || !kt_equal($value, $b[$key])) { return false; }
    }
    return true;
}
function kt_contract(array $manifest): array {
    $config = $manifest['config'] ?? [];
    kt_need(($config['vendor-dir'] ?? 'vendor') === 'vendor'
        && ($config['bin-dir'] ?? 'vendor/bin') === 'vendor/bin', 'non-default vendor/bin directory');
    kt_need(($manifest['type'] ?? '') !== 'composer-plugin' && empty($manifest['target-dir']), 'custom root installer');
    foreach (['autoload', 'autoload-dev'] as $field) {
        $autoload = $manifest[$field] ?? [];
        $paths = array_merge($autoload['files'] ?? [], $autoload['classmap'] ?? []);
        foreach (['psr-0', 'psr-4'] as $kind) {
            foreach (($autoload[$kind] ?? []) as $values) {
                $paths = array_merge($paths, is_array($values) ? $values : [$values]);
            }
        }
        foreach ($paths as $path) {
            kt_need(is_string($path), 'invalid root autoload path');
            if (!in_array($path, ['', '.', './'], true)) {
                kt_relative(rtrim(strpos($path, './') === 0 ? substr($path, 2) : $path, '/'));
            }
        }
    }
    $fields = [];
    foreach (KT_KEYS as $key) {
        if (array_key_exists($key, $manifest)) { $fields[$key] = $manifest[$key]; }
    }
    return ['fields' => $fields, 'platform' => $config['platform'] ?? []];
}
function kt_lock(array $lock): array {
    unset($lock['_readme'], $lock['content-hash']);
    return $lock;
}
function kt_tree(string $root, bool $candidate = false, string $prefix = '', bool $dirs = false): array {
    $out = [];
    foreach (scandir($root . ($prefix === '' ? '' : '/' . $prefix)) as $name) {
        if ($name === '.' || $name === '..') { continue; }
        if ($candidate && ($name === '.git' || ($prefix === '' && $name === '.reviewbench'))) { continue; }
        $relative = $prefix === '' ? $name : $prefix . '/' . $name;
        kt_relative($relative);
        $path = $root . '/' . $relative;
        if (is_link($path)) {
            $target = readlink($path);
            $resolved = kt_normal($target[0] === '/' ? $target : dirname($path) . '/' . $target);
            kt_need(kt_within($resolved, $root) && !kt_within($resolved, $root . '/.reviewbench')
                && !preg_match('~/(?:\.git|\.hg|\.svn)(?:/|$)~', $resolved), 'link escapes tree: ' . $relative);
            $real = realpath($path);
            kt_need($real === false || kt_within($real, $root), 'indirect link escapes tree');
            $out[$relative] = ['link', $target];
        } elseif (is_dir($path)) {
            if ($candidate || $dirs) { $out[$relative] = ['dir', fileperms($path) & 0777]; }
            $out += kt_tree($root, $candidate, $relative, $dirs);
        } else {
            kt_need(is_file($path), 'non-regular tree entry');
            $out[$relative] = ['file', hash_file('sha256', $path), fileperms($path) & 0777];
        }
    }
    ksort($out);
    return $out;
}
function kt_remove(string $path): void {
    if (is_link($path) || !is_dir($path)) {
        if (file_exists($path) || is_link($path)) { kt_need(unlink($path), 'unlink failed'); }
        return;
    }
    foreach (scandir($path) as $name) {
        if ($name !== '.' && $name !== '..') { kt_remove($path . '/' . $name); }
    }
    kt_need(rmdir($path), 'rmdir failed');
}
function kt_mkdir(string $path): void {
    kt_real_parents($path);
    kt_need(is_dir($path) || mkdir($path, 0777, true), 'mkdir failed: ' . $path);
}
function kt_copy(string $from, string $to, string $source, string $mirror): void {
    kt_mkdir(dirname($to));
    if (is_dir($from) && !is_link($from)) {
        kt_mkdir($to);
        kt_need(chmod($to, fileperms($from) & 0777), 'directory mode copy failed');
    } elseif (is_link($from)) {
        $target = readlink($from);
        if ($target[0] === '/') { $target = $mirror . substr(kt_normal($target), strlen($source)); }
        kt_need(symlink($target, $to), 'symlink copy failed');
    } else {
        kt_need(copy($from, $to) && chmod($to, fileperms($from) & 0777), 'file copy failed');
    }
}
function kt_nonmetadata(array $tree): array {
    return array_diff_key($tree, array_fill_keys(KT_METADATA, true));
}
function kt_archive_tree(array $tree): array {
    // The frozen engine's extract_tar preserves bytes/links/executable status,
    // normalizing regular-file permissions to 0644 or 0755. Source and mirror
    // snapshots elsewhere still compare their complete original modes.
    foreach ($tree as $name => $state) {
        if ($state[0] === 'file') {
            kt_need(count($state) === 3 && is_int($state[2]), 'invalid payload file mode');
            $state[2] = ($state[2] & 0111) !== 0 ? 0755 : 0644;
            $tree[$name] = $state;
        }
    }
    return $tree;
}
function kt_run(array $argv, string $cwd, array $environment): int {
    $process = proc_open($argv, [0 => STDIN, 1 => STDOUT, 2 => STDERR], $pipes, $cwd, $environment);
    kt_need(is_resource($process), 'process creation failed');
    return proc_close($process);
}
function kt_capture(array $argv, string $cwd, array $environment): array {
    $out = tmpfile(); $err = tmpfile();
    try {
        $process = proc_open($argv, [0 => STDIN, 1 => $out, 2 => $err], $pipes, $cwd, $environment);
        kt_need(is_resource($process), 'probe creation failed');
        $status = proc_close($process);
        rewind($out); rewind($err);
        return [$status, stream_get_contents($out), stream_get_contents($err)];
    } finally { fclose($out); fclose($err); }
}
function kt_inputs(string $payload, string $image, string $seal): array {
    $record = kt_json($payload . '/composer-kirby-payload.json');
    kt_need(($record['schema'] ?? '') === KT_SCHEMA && ($record['task_id'] ?? '') === KT_TASK
        && ($record['start_sha'] ?? '') === KT_START && ($record['image'] ?? '') === $image
        && ($record['sealed_sha256'] ?? '') === $seal && ($record['historical_lock'] ?? null) === false,
        'Kirby payload identity changed');
    $tree = kt_tree($payload);
    unset($tree['composer-kirby-payload.json']);
    kt_need(kt_equal(kt_archive_tree($tree), kt_archive_tree($record['files'])),
        'Kirby payload bytes/links/executable modes changed');
    foreach ($record['protected_vendor_paths'] as $path) {
        kt_relative($path);
        kt_need(!isset($tree[$path]), 'payload contains historical candidate vendor source');
    }
    $base = $payload . '/build-inputs';
    kt_need(hash_file('sha256', $base . '/composer-test-tools.json') === $seal, 'sealed record changed');
    $prepared = kt_json($base . '/composer-test-tools.json');
    kt_need(($prepared['schema'] ?? '') === 'composer-derived-test-tools-v1'
        && ($prepared['status'] ?? '') === 'prepared' && ($prepared['task_id'] ?? '') === KT_TASK
        && ($prepared['start_sha'] ?? '') === KT_START && ($prepared['requirement'] ?? '') === 'phpunit/phpunit:^11.5'
        && ($prepared['historical_lock'] ?? null) === false && $prepared['resolver']['image'] === $image,
        'invalid sealed derived graph');
    foreach ($prepared['files'] as $path => $hash) {
        kt_relative($path);
        kt_need(hash_file('sha256', $base . '/' . $path) === $hash, 'sealed build input changed');
    }
    $derived = kt_json($base . '/derived/composer.json');
    $lock = kt_json($base . '/derived/composer.lock');
    kt_need(kt_equal(kt_contract($derived), $record['dependency_manifest'])
        && kt_equal(kt_lock($lock), $record['locked']), 'derived metadata guard changed');
    $composer = $prepared['resolver']['composer_path'];
    kt_need(is_file($composer) && hash_file('sha256', $composer) === $prepared['resolver']['composer_sha256'],
        'runtime Composer differs from actual prepared resolver');
    return [$record, $prepared, $tree];
}
function kt_build(string $source, string $payload, string $mirror, string $image, string $seal): array {
    [$record, $prepared, $payloadTree] = kt_inputs($payload, $image, $seal);
    $before = kt_tree($source, true);
    kt_need(isset($before['composer.json'], $before['composer.lock'])
        && $before['composer.json'][0] === 'file' && $before['composer.lock'][0] === 'file',
        'candidate manifests must be regular files');
    $current = kt_json($source . '/composer.json');
    $original = $prepared['plan']['original'];
    kt_need(kt_equal(kt_contract($current), kt_contract($original['manifest']))
        && kt_equal(kt_lock(kt_json($source . '/composer.lock')), kt_lock($original['lock']))
        && kt_equal($current['extra'] ?? null, $original['manifest']['extra'] ?? null),
        'candidate original dependency graph changed');
    // Retain JSON object/list types and all CURRENT non-dependency fields.
    $object = json_decode((string) file_get_contents($source . '/composer.json'), false, 512, JSON_THROW_ON_ERROR);
    if (!isset($object->{'require-dev'})) { $object->{'require-dev'} = new stdClass(); }
    $object->{'require-dev'}->{'phpunit/phpunit'} = '^11.5';
    $buildManifest = json_encode($object, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
    $buildLock = (string) file_get_contents($payload . '/build-inputs/derived/composer.lock');
    kt_need(kt_equal(kt_contract(json_decode($buildManifest, true, 512, JSON_THROW_ON_ERROR)),
        $record['dependency_manifest']), 'projected manifest differs from prepared graph');
    $owner = $mirror . '.owner';
    kt_real_parents($owner);
    $ownership = KT_SCHEMA . "\n" . $source . "\n";
    if (file_exists($mirror) || is_link($mirror)) {
        kt_need(!is_link($mirror) && is_file($owner) && file_get_contents($owner) === $ownership,
            'existing mirror is not Kirby-adapter-owned');
        kt_remove($mirror);
    }
    kt_mkdir($mirror);
    kt_need(file_put_contents($owner, $ownership) !== false, 'owner marker write failed');
    try {
        foreach ($before as $name => $state) {
            if (!in_array($name, KT_METADATA, true)) {
                kt_copy($source . '/' . $name, $mirror . '/' . $name, $source, $mirror);
            }
        }
        foreach ($payloadTree as $name => $state) {
            if (strpos($name, 'build-inputs/') === 0) { continue; }
            kt_need(strpos($name, 'vendor/') === 0, 'non-vendor dependency payload');
            $target = $mirror . '/' . $name;
            if (file_exists($target) || is_link($target)) { continue; }
            kt_copy($payload . '/' . $name, $target, $payload, $mirror);
        }
        foreach (['composer.json' => $buildManifest, 'composer.lock' => $buildLock] as $name => $data) {
            kt_remove($mirror . '/' . $name);
            kt_need(file_put_contents($mirror . '/' . $name, $data) !== false, 'mirror build input write failed');
        }
        $expected = kt_nonmetadata(kt_tree($mirror, false, '', true));
        $home = $mirror . '.composer-home';
        kt_real_parents($home);
        kt_need(!file_exists($home), 'stale Composer home');
        kt_mkdir($home);
        try {
            $env = getenv();
            foreach (array_keys($env) as $key) {
                if (strpos($key, 'COMPOSER_') === 0 || $key === 'COMPOSER') { unset($env[$key]); }
            }
            $env = array_merge($env, ['HOME' => $home, 'COMPOSER_HOME' => $home,
                'COMPOSER_CACHE_DIR' => '/dev/null', 'COMPOSER' => 'composer.json',
                'COMPOSER_NO_DEV' => '0', 'COMPOSER_VENDOR_DIR' => 'vendor',
                'COMPOSER_BIN_DIR' => 'vendor/bin', 'COMPOSER_DISABLE_NETWORK' => '1']);
            $status = kt_run([$prepared['resolver']['composer_path'], 'dump-autoload', '--dev',
                '--no-interaction', '--no-scripts', '--no-plugins'], $mirror, $env);
        } finally { kt_remove($home); }
        kt_need($status === 0, 'Composer dump-autoload failed with exit ' . $status);
        $after = kt_tree($mirror, false, '', true);
        kt_need(kt_equal($expected, kt_nonmetadata($after)), 'Composer changed non-metadata mirror source/build inputs');
        kt_need(isset($after['vendor/autoload.php']), 'Composer did not generate an autoloader');
        $probeEnv = getenv();
        unset($probeEnv['ORACLE_TARGET'], $probeEnv['ORACLE_COVERAGE_JSON']);
        [$code, $stdout, $stderr] = kt_capture([PHP_BINARY, 'vendor/phpunit/phpunit/phpunit', '--version'], $mirror, $probeEnv);
        kt_need($code === 0 && preg_match('/^PHPUnit (11\.(?:[5-9]|[1-9][0-9]+)\.\d+)(?:\s|$)/', $stdout, $match)
            && $match[1] === ltrim($prepared['derived']['phpunit_version'], 'v'),
            'prepared PHPUnit version probe failed: ' . $stdout . $stderr);
        kt_need(kt_equal($after, kt_tree($mirror, false, '', true)), 'version probe changed mirror inputs');
        kt_need(kt_equal($before, kt_tree($source, true)), 'original candidate changed during build');
        return ['mirror' => $mirror, 'source_unchanged' => true, 'historical_lock' => false,
            'build_manifest_sha256' => hash('sha256', $buildManifest), 'build_lock_sha256' => hash('sha256', $buildLock),
            'phpunit_version' => $match[1], 'phpunit_version_output' => $stdout];
    } catch (Throwable $error) {
        kt_remove($mirror); kt_remove($owner);
        throw $error;
    }
}
function kt_main(array $argv): int {
    kt_need(count($argv) >= 6, 'usage: entry.php SOURCE PAYLOAD MIRROR IMAGE SEALED_SHA [-- COMMAND ...]');
    [$entry, $sourceArg, $payloadArg, $mirrorArg, $image, $seal] = $argv;
    kt_real_parents(kt_normal($sourceArg)); kt_real_parents(kt_normal($payloadArg));
    $source = realpath($sourceArg); $payload = realpath($payloadArg); $mirror = kt_normal($mirrorArg);
    kt_need($source !== false && $payload !== false && is_dir($source) && is_dir($payload), 'input directories missing');
    kt_real_parents($mirror);
    kt_need(!kt_within($source, $mirror) && !kt_within($payload, $mirror) && !kt_within($mirror, $payload)
        && !kt_within($payload, $source)
        && (!kt_within($mirror, $source) || kt_within($mirror, $source . '/.reviewbench')),
        'candidate, payload and mirror overlap');
    kt_need(preg_match('/^sha256:[a-f0-9]{64}$/', $image) && preg_match('/^[a-f0-9]{64}$/', $seal), 'unbound runtime identity');
    if (count($argv) > 6) { kt_need($argv[6] === '--' && count($argv) > 7, 'command must follow --'); }
    $built = kt_build($source, $payload, $mirror, $image, $seal);
    if (count($argv) === 6) {
        echo json_encode($built, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR), "\n";
        return 0;
    }
    $before = kt_tree($source, true);
    $env = getenv(); $target = $env['ORACLE_TARGET'] ?? ''; $coverage = $env['ORACLE_COVERAGE_JSON'] ?? '';
    $canonical = null; $mapped = null;
    if ($target !== '') {
        $canonical = realpath($target[0] === '/' ? $target : $source . '/' . $target);
        kt_need($canonical !== false && kt_within($canonical, $source)
            && !kt_within($canonical, $source . '/.reviewbench'), 'coverage target is not current candidate source');
        $mapped = realpath($mirror . substr($canonical, strlen($source)));
        kt_need($mapped !== false && kt_within($mapped, $mirror), 'coverage target missing in mirror');
        $env['ORACLE_TARGET'] = $mapped;
    }
    if ($coverage !== '') {
        $coverage = kt_normal($coverage);
        kt_need(!kt_within($coverage, $source) || kt_within($coverage, $source . '/.reviewbench'),
            'coverage output would overwrite candidate source');
        kt_real_parents($coverage);
        if (is_file($coverage)) { kt_need(unlink($coverage), 'stale coverage removal failed'); }
        $env['ORACLE_COVERAGE_JSON'] = $coverage;
    }
    if (isset($env['HOME']) && kt_within($env['HOME'], $source)
        && !kt_within($env['HOME'], $source . '/.reviewbench')) {
        $env['HOME'] = $mirror . substr($env['HOME'], strlen($source));
    }
    $status = kt_run(array_slice($argv, 7), $mirror, $env);
    kt_need(kt_equal($before, kt_tree($source, true)), 'test changed original candidate source');
    if ($coverage !== '' && is_file($coverage) && $mapped !== null) {
        $data = kt_json($coverage);
        kt_need(($data['target'] ?? null) === $mapped, 'coverage target differs from executed mirror');
        $data['target'] = $canonical;
        kt_need(file_put_contents($coverage, json_encode($data, JSON_THROW_ON_ERROR)) !== false, 'coverage normalization failed');
    }
    return $status;
}
try { exit(kt_main($argv)); }
catch (Throwable $error) {
    fwrite(STDERR, 'KirbyComposerError: ' . $error->getMessage() . "\n");
    exit(2);
}
'''


def php_runtime_source() -> str:
    return (_PHP.replace("__TASK__", json.dumps(tools.TASK_ID))
            .replace("__START__", json.dumps(tools.START_SHA))
            .replace("__METADATA__", json.dumps(sorted(cc.METADATA)))
            .replace("__KEYS__", json.dumps(cc.DEPENDENCY_KEYS)))


def write_php_runtime(destination: Path) -> Path:
    destination = Path(destination)
    destination.write_text(php_runtime_source())
    return destination
