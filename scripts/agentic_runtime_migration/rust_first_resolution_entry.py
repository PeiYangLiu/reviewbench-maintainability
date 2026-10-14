#!/usr/bin/env python3
"""Task-bound NEW Cargo defaults; validate and build the complete current tree.

The host authenticates original references and first-resolution receipts.
Missing-lock selection inspects current public local package identities:
Prom path-dependency names, Font workspace read-fonts name/version, or the
Scylla workspace package name. Candidate locks bypass selection. No resolver,
reference label or test outcome chooses a default here.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

if __package__:
    from . import rust_test_compat_entry as guard
    from .rust_build_entry import OverlayError, candidate_files, synchronize_candidate, tomllib
else:
    import rust_test_compat_entry as guard
    from build import OverlayError, candidate_files, synchronize_candidate, tomllib

TASK = "github__prometheus__client_rust__pull_82__comment_956892586__complexity"
SCHEMA = "reviewbench-cargo-first-new-resolution/v1"
PROFILE = "prometheus82-first-new-locks-v1"
PRIVATE_SCHEMA = "reviewbench-cargo-first-new-resolution-private/v1"
SELECTORS = ("prometheus-client-derive-text-encode", "prometheus-client-derive-encode")
REAL_CARGO = "/opt/reviewbench-assets/rust/bin/cargo"
REAL_RUSTC = "/opt/reviewbench-assets/rust/bin/rustc"
VENDOR = "/opt/reviewbench-assets/vendor"
PREFIX = ["--offline", "--config", 'source.crates-io.replace-with="reviewbench-vendor"',
          "--config", 'source.reviewbench-vendor.directory="/opt/reviewbench-assets/vendor"']
PRIVATE_COMMAND = ["test", "--test", "oracle_session", "--", "--nocapture"]
HARNESS = {"path": "tests/oracle_session.rs",
           "sha256": "b7cbe2ba8083d0e6c932328d64a9b28691cb04f5387e0ff0786281983a11e421"}
EXPECTED = ["registered_counter_is_encoded_through_the_public_registry"]
MAX_BYTES = 33554432
COMMAND_SECONDS = 1800
EVIDENCE_PREFIX = "ReviewBench first-new Cargo lock evidence: "
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
METADATA_STDERR = re.compile(
    r"Blocking waiting for file lock on (?:package cache|build directory)|"
    r"warning: unused manifest key: [A-Za-z0-9_.-]+")

FONT_TASK = "github__googlefonts__fontations__pull_191__comment_1055650458__complexity"
FONT_PROFILE = "fontations45-first-new-locks-v1"
FONT_SOURCE_CONFIG = "/opt/reviewbench-assets/cargo-sources.toml"
FONT_COMMAND = ["test", "-p", "punchcut", "--test", "oracle_session", "--", "--nocapture"]
FONT_HARNESS = {"path": "punchcut/tests/oracle_session.rs",
                "sha256": "4bd6d4e8638af5071a2d6b553c7e17f414bd1e8d398b7a04fff932b3ec887ccc"}
FONT_EXPECTED = ["public_scaler_recognizes_real_truetype_outlines"]
FONT_RESOLVER_WARNING = (
    'warning: virtual workspace defaulting to `resolver = "1"` despite one or more workspace members '
    'being on edition 2021 which implies `resolver = "2"`\n'
    '  |\n'
    '  = note: to keep the current resolver, specify `workspace.resolver = "1"` in the workspace root\'s manifest\n'
    '  = note: to use the edition 2021 resolver, specify `workspace.resolver = "2"` in the workspace root\'s manifest\n'
    '  = note: for more details see https://doc.rust-lang.org/cargo/reference/resolver.html#resolver-versions\n'
)

SCYLLA_TASK = "github__scylladb__scylla-rust-driver__pull_509__comment_962978126__api_design"
SCYLLA_PROFILE = "scylla96-original-prepare-lock-v1"
SCYLLA_COMMAND = ["test", "-p", "scylla", "--test", "oracle_session", "--", "--nocapture"]
SCYLLA_HARNESS = {"path": "scylla/tests/oracle_session.rs",
                  "sha256": "fc3d82be0af9a66de1493be8e49335dc792ff910b24a260181aa86920d990308"}
SCYLLA_EXPECTED = ["dummy_metadata_assigns_each_initial_peer_a_distinct_token"]

ZEROCOPY_TASK = "github__google__zerocopy__pull_1752__comment_1777397723__complexity"
ZEROCOPY_PROFILE = "zerocopy44-first-new-shared-lock-v1"
ZEROCOPY_COMMAND = ['test', '-p', 'zerocopy-derive', '--test', 'oracle_session', '--', '--nocapture']
ZEROCOPY_HARNESS = {'path': 'zerocopy-derive/tests/oracle_session.rs', 'sha256': '62148172bb3106665225704232de814c2ec64dddf91840f0d5b4016753191494'}
ZEROCOPY_EXPECTED = ['public_repr_derive_accepts_a_c_layout_header']

LIBSBP_TASK = 'github__swift-nav__libsbp__pull_1218__comment_970220101__naming'
LIBSBP_PROFILE = 'libsbp104-original-prepare-library-v1'
LIBSBP_COMMAND = ['test', '-p', 'sbp', '--lib', '--', '--nocapture']
LIBSBP_EXPECTED = ['de::tests::test_decode_sbp', 'de::tests::test_invalid_utf8', 'de::tests::test_iter_messages', 'de::tests::test_iter_messages_with_timeout', 'de::tests::test_parse_bad_message', 'de::tests::test_parse_crc_error', 'de::tests::test_parse_frame_incomplete', 'de::tests::test_parse_nothing', 'sbp_iter_ext::tests::test_handle_errors', 'sbp_iter_ext::tests::test_stop_on_errors', 'sbp_string::tests::test_parse_sbp_string_array', 'sbp_string::tests::test_parse_sbp_string_invalid_utf8', 'sbp_string::tests::test_parse_sbp_string_vec', 'ser::tests::test_to_vec', 'ser::tests::test_to_writer', 'wire_format::tests::test_parse_u16_array', 'wire_format::tests::test_parse_u16_vec']
LIBSBP_TEST_BLOCKS = [{'bytes': 6292, 'marker': '#[cfg(test)]\nmod tests {', 'sha256': '66a436d264234bc4b522c1bb44ae0d4fc58f4840c9f518467ac34967d2697ece', 'source_path': 'rust/sbp/src/de.rs'}, {'bytes': 866, 'marker': '#[cfg(test)]\nmod tests {', 'sha256': '73ed8a0feb26127543fa4d43ac859e4f25123cbde090e5bb188f4934dc75cb8c', 'source_path': 'rust/sbp/src/ser.rs'}, {'bytes': 4077, 'marker': '#[cfg(test)]\nmod tests {', 'sha256': '12fd5e7a8499e5629063cc4b77a9ec6df3e355d7758575973e6d9da45ec4ef50', 'source_path': 'rust/sbp/src/sbp_iter_ext.rs'}, {'bytes': 1899, 'marker': '#[cfg(test)]\nmod tests {', 'sha256': 'e9c83d278406f2e6a8df9f3577a658b540b21f7adb123e44b121c8342def4e6b', 'source_path': 'rust/sbp/src/sbp_string.rs'}, {'bytes': 715, 'marker': '#[cfg(test)]\nmod tests {', 'sha256': 'd1517fa9bc090bc2102df40cb5a58500b4222529c27c9679ed772d10d3252b79', 'source_path': 'rust/sbp/src/wire_format.rs'}]




def require(condition, message):
    if not condition:
        raise OverlayError("first-new Cargo lock: " + message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024**2), b""):
            value.update(data)
    return value.hexdigest()


def strict_json(data):
    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON field")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def input_bytes(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BYTES,
            "missing/non-regular/oversize input: " + str(path))
    return path.read_bytes()


def profile_for(spec=None, private=None):
    """Explicit dispatch; keep all exported Prom defaults and old call shapes."""
    if spec is None:
        font = isinstance(private, dict) and private.get("task_id") == FONT_TASK
        scylla = isinstance(private, dict) and private.get("task_id") == SCYLLA_TASK
        zerocopy = isinstance(private, dict) and private.get("task_id") == ZEROCOPY_TASK
        libsbp = isinstance(private, dict) and private.get("task_id") == LIBSBP_TASK
    else:
        require(isinstance(spec, dict), "invalid profile specification")
        pair = spec.get("task_id"), spec.get("profile")
        require(pair in {(TASK, PROFILE), (FONT_TASK, FONT_PROFILE), (SCYLLA_TASK, SCYLLA_PROFILE),
                         (ZEROCOPY_TASK, ZEROCOPY_PROFILE), (LIBSBP_TASK, LIBSBP_PROFILE)},
                "unknown task/profile")
        font = pair == (FONT_TASK, FONT_PROFILE)
        scylla = pair == (SCYLLA_TASK, SCYLLA_PROFILE)
        zerocopy = pair == (ZEROCOPY_TASK, ZEROCOPY_PROFILE)
        libsbp = pair == (LIBSBP_TASK, LIBSBP_PROFILE)
    if libsbp:
        return {"font": False, "libsbp": True, "task_id": LIBSBP_TASK, "profile": LIBSBP_PROFILE,
                "prefix": list(PREFIX), "public": ["check", "-p", "sbp"],
                "command": list(LIBSBP_COMMAND), "harness": None, "command_cwd": "rust",
                "expected_tests": list(LIBSBP_EXPECTED), "repository_test_blocks": LIBSBP_TEST_BLOCKS}
    if zerocopy:
        return {"font": False, "zerocopy": True, "task_id": ZEROCOPY_TASK, "profile": ZEROCOPY_PROFILE,
                "prefix": list(PREFIX), "public": ["check", "-p", "zerocopy-derive"],
                "command": list(ZEROCOPY_COMMAND), "harness": dict(ZEROCOPY_HARNESS),
                "expected_tests": list(ZEROCOPY_EXPECTED)}
    if scylla:
        return {"font": False, "scylla": True, "task_id": SCYLLA_TASK, "profile": SCYLLA_PROFILE,
                "prefix": list(PREFIX), "public": ["check", "-p", "scylla"],
                "command": list(SCYLLA_COMMAND), "harness": dict(SCYLLA_HARNESS),
                "expected_tests": list(SCYLLA_EXPECTED)}
    if font:
        return {"font": True, "task_id": FONT_TASK, "profile": FONT_PROFILE,
                "prefix": ["--offline", "--config", FONT_SOURCE_CONFIG],
                "public": ["check", "-p", "punchcut"], "command": list(FONT_COMMAND),
                "harness": dict(FONT_HARNESS), "expected_tests": list(FONT_EXPECTED)}
    return {"font": False, "task_id": TASK, "profile": PROFILE, "prefix": list(PREFIX),
            "public": ["check"], "command": list(PRIVATE_COMMAND), "harness": dict(HARNESS),
            "expected_tests": list(EXPECTED)}


def git_source(source):
    require(isinstance(source, str) and source.startswith("git+"), "invalid Git source")
    url = urlsplit(source[4:])
    require(re.fullmatch(r"[0-9a-f]{40}", url.fragment) is not None, "Git source lacks a full pinned revision")
    require(url.path and url.scheme in {"https", "http", "ssh", "git", "file"}
            and (url.scheme == "file" or url.netloc), "invalid Git source URL")
    query = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True) if url.query else []
    require(len(query) <= 1 and all(k in {"branch", "tag", "rev"} and v for k, v in query),
            "ambiguous/unsupported Git selector")
    return urlunsplit((url.scheme, url.netloc, url.path, "", "")), dict(query), url.fragment


def validate_source_mapping(spec):
    mapping = spec.get("vendor_sources")
    require(isinstance(mapping, dict), "missing source-aware vendor mapping")
    for source, directory in mapping.items():
        require(isinstance(source, str) and directory == "source-" + digest(source.encode()),
                "vendor source directory differs from complete source identity")
        if source != REGISTRY:
            git_source(source)
    return mapping


def verify_source_configuration(spec, vendor, path=None):
    """Verify the existing rust_vendor.source_configuration wire format."""
    mapping = validate_source_mapping(spec)
    path = Path(FONT_SOURCE_CONFIG) if path is None else Path(path)
    data = input_bytes(path)
    require(digest(data) == spec["source_config_sha256"], "source configuration hash changed")
    expected, selectors = {}, {}
    for source, directory in sorted(mapping.items()):
        key = digest(source.encode())
        replacement = "reviewbench-" + directory
        if source == REGISTRY:
            name, definition = "crates-io", {}
        else:
            base, query, _ = git_source(source)
            selector = base, tuple(query.items())
            require(selector not in selectors or selectors[selector] == source,
                    "one Git selector maps to different pinned sources")
            selectors[selector] = source
            name, definition = "reviewbench-original-" + key, {"git": base, **query}
        expected[name] = {**definition, "replace-with": replacement}
        expected[replacement] = {"directory": str(vendor / directory)}
    document = tomllib.loads(data.decode())
    require(document == {"source": expected, "net": {"offline": True}}
            and document.get("net", {}).get("offline") is True,
            "source configuration differs from bound source mapping/offline policy")
    return {"path": str(path), "sha256": digest(data), "vendor_sources": mapping, "document": document}


def validate(spec, private=None):
    profile = profile_for(spec)
    extra = {"source_config_sha256", "vendor_sources"} if profile["font"] else set()
    if profile.get("scylla") or profile.get("zerocopy"):
        extra = {"command_timeout_seconds"}
    require(isinstance(spec, dict) and set(spec) == {
        "schema_version", "task_id", "profile", "toolchain", "cargo_sha256", "rustc_sha256",
        "cpus", "max_log_bytes", "metadata_timeout_seconds", "receipt_sha256", "defaults"
    } | extra, "unknown public specification fields")
    require(spec["schema_version"] == SCHEMA and spec["toolchain"] == (
        "1.64.0" if profile.get("scylla") or profile.get("libsbp") else "1.81.0" if profile.get("zerocopy") else "1.93.0"),
            "unknown task/profile/toolchain")
    for key, expected in (("cpus", 4), ("max_log_bytes", MAX_BYTES), ("metadata_timeout_seconds", 120)):
        require(type(spec[key]) is int and spec[key] == expected, "changed resource bound: " + key)
    if profile.get("scylla") or profile.get("zerocopy"):
        require(type(spec["command_timeout_seconds"]) is int and spec["command_timeout_seconds"] == 2400,
                "changed original command timeout")
    require(all(isinstance(spec[k], str) and re.fullmatch(r"[0-9a-f]{64}", spec[k])
                for k in ("cargo_sha256", "rustc_sha256", "receipt_sha256")), "unbound receipt/toolchain")
    selectors = (("sbp",) if profile.get("libsbp") else ("zerocopy-derive",) if profile.get("zerocopy") else ("scylla",) if profile.get("scylla")
                 else ("read-fonts", "read-fonts") if profile["font"] else SELECTORS)
    require(isinstance(spec["defaults"], list) and len(spec["defaults"]) == len(selectors),
            "one shared default is required" if profile.get("scylla") or profile.get("zerocopy") or profile.get("libsbp") else "two defaults are required")
    for index, (row, selector) in enumerate(zip(spec["defaults"], selectors)):
        keys = {"selector_package", "asset_target", "sha256"} | ({"selector_version"} if profile["font"] else set())
        require(isinstance(row, dict) and set(row) == keys
                and row["selector_package"] == selector
                and row["asset_target"] == "/opt/reviewbench-assets/rust-new-lock-" + str(index)
                and isinstance(row["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
                "unbound/ambiguous default")
        if profile["font"]:
            require(row["selector_version"] == ("0.0.4", "0.0.5")[index], "unbound read-fonts selector version")
    if profile["font"]:
        require(isinstance(spec["source_config_sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", spec["source_config_sha256"]), "unbound source configuration")
        validate_source_mapping(spec)
    if private is not None:
        extra_private = {"repository_test_blocks"} if profile.get("libsbp") else set()
        require(isinstance(private, dict) and set(private) == {
            "schema_version", "task_id", "command", "harness", "expected_tests"
        } | extra_private and private["schema_version"] == PRIVATE_SCHEMA and private["task_id"] == profile["task_id"]
            and private["command"] == profile["command"] and private["harness"] == profile["harness"]
            and private["expected_tests"] == profile["expected_tests"], "changed private command/harness/test identities")
        if profile.get("libsbp"):
            require(private["repository_test_blocks"] == LIBSBP_TEST_BLOCKS, "original repository test item bindings changed")


def inside(root, path, *, regular=True):
    """Resolve only paths wholly contained in this complete candidate."""
    root = root.resolve()
    require(path.is_absolute(), "non-absolute resolved input path")
    require(path.resolve().is_relative_to(root), "local input escapes complete candidate")
    require(not any(p.is_symlink() for p in (path, *path.parents)
                    if p == root or p.is_relative_to(root)), "symlinked local manifest/lock")
    if regular:
        require(path.is_file(), "missing local manifest/target")
    return path.resolve()


def snapshot(root):
    files = candidate_files(root)
    require("Cargo.toml" in files, "missing current root Cargo.toml")
    require(not any("target" in Path(name).parts for name in files), "old target output in current source")
    for name, path in files.items():
        if Path(name).name in {"Cargo.toml", "Cargo.lock"}:
            inside(root, path)
            tomllib.loads(input_bytes(path).decode())
    return guard.fingerprint(root)


def authenticate_defaults(spec, lock_sources=None):
    paths = {}
    for row in spec["defaults"]:
        path = Path((lock_sources or {}).get(row["asset_target"], row["asset_target"]))
        data = input_bytes(path)
        require(digest(data) == row["sha256"], "default asset hash changed")
        # Structural validation is not a selector and never compares the
        # candidate's lock hash/versions against either default.
        parse_lock(data, spec)
        paths[row["asset_target"]] = path
    return paths


def select_default(source, spec):
    profile = profile_for(spec)
    if profile["font"] or profile.get("scylla") or profile.get("libsbp"):
        return select_workspace_default(source, spec)
    manifest = tomllib.loads(input_bytes(source / "Cargo.toml").decode())
    dependencies = manifest.get("dependencies", {})
    require(isinstance(dependencies, dict), "invalid current root dependencies")
    inherited = manifest.get("workspace", {}).get("dependencies", {})
    require(isinstance(inherited, dict), "invalid workspace dependency table")
    selectors = ("zerocopy-derive",) if profile.get("zerocopy") else SELECTORS
    matches = []
    for alias, value in dependencies.items():
        if not isinstance(value, dict):
            continue
        if value.get("workspace") is True:
            require(alias in inherited and isinstance(inherited[alias], dict),
                    "unbound workspace-inherited dependency")
            # Cargo resolves workspace dependency paths relative to the
            # workspace root, not the derive package directory.
            require(not ({"path", "package", "version", "git"} & set(value)),
                    "conflicting workspace dependency override")
            value = inherited[alias]
        if "path" not in value:
            continue
        relative = value["path"]
        require(isinstance(relative, str) and relative and not Path(relative).is_absolute()
                and "\0" not in relative and "\\" not in relative, "invalid public path dependency")
        local = inside(source, source / relative / "Cargo.toml")
        package = tomllib.loads(input_bytes(local).decode()).get("package", {})
        name = package.get("name")
        require(isinstance(name, str) and name, "local dependency has no package.name")
        require(value.get("package", alias) == name, "dependency alias/package differs from actual local package")
        if name in selectors:
            matches.append({"selector_package": name, "dependency_alias": alias,
                            "manifest": local.relative_to(source).as_posix()})
    require(len(matches) == 1, "both/neither public local derive selectors match")
    selected = next(row for row in spec["defaults"] if row["selector_package"] == matches[0]["selector_package"])
    return selected, matches[0]


def workspace_manifests(source):
    document = tomllib.loads(input_bytes(source / "Cargo.toml").decode())
    workspace = document.get("workspace")
    require(isinstance(workspace, dict), "missing-lock default requires a current workspace")
    members, excluded = workspace.get("members"), workspace.get("exclude", [])
    require(isinstance(members, list) and members and isinstance(excluded, list),
            "missing/invalid current workspace members")
    def expand(patterns, required):
        paths = set()
        for pattern in patterns:
            require(isinstance(pattern, str) and pattern and not Path(pattern).is_absolute()
                    and "\\" not in pattern and "\0" not in pattern
                    and (source / pattern).resolve().is_relative_to(source), "workspace member path escapes candidate")
            matches = list(source.glob(pattern))
            require(matches or not required, "current workspace member is missing")
            for folder in matches:
                inside(source, folder, regular=False)
                manifest = folder / "Cargo.toml"
                if required:
                    inside(source, manifest)
                paths.add(manifest.resolve())
        return paths
    selected = expand(members, True) - expand(excluded, False)
    if "package" in document:
        selected.add(source / "Cargo.toml")
    require(selected, "empty current workspace")
    return [(path, tomllib.loads(input_bytes(path).decode())) for path in sorted(selected)], workspace


def select_workspace_default(source, spec):
    manifests, workspace = workspace_manifests(source)
    profile = profile_for(spec)
    if profile.get("scylla") or profile.get("libsbp"):
        selected_name = "sbp" if profile.get("libsbp") else "scylla"
        matches = [path for path, document in manifests
                   if document.get("package", {}).get("name") == selected_name]
        require(len(matches) == 1, "both/neither current " + selected_name + " workspace members match")
        # One host-authenticated final original-prepare default serves both
        # references. Runtime selection uses no version, phase or source hash.
        return spec["defaults"][0], {
            "kind": "current-public-workspace-package", "selector_package": selected_name,
            "manifest": matches[0].relative_to(source).as_posix()}
    matches = []
    for path, document in manifests:
        package = document.get("package", {})
        if package.get("name") == "read-fonts":
            version = package.get("version")
            if version == {"workspace": True}:
                version = workspace.get("package", {}).get("version")
            matches.append((path, version))
    require(len(matches) == 1, "both/neither current read-fonts workspace members match")
    path, version = matches[0]
    rows = [row for row in spec["defaults"] if row["selector_package"] == "read-fonts"
            and row["selector_version"] == version]
    require(len(rows) == 1, "current read-fonts version has no authenticated missing-lock default")
    return rows[0], {"kind": "current-public-workspace-package", "selector_package": "read-fonts",
                     "selector_version": version, "manifest": path.relative_to(source).as_posix()}


def prepare_mirror(source, mirror, spec, private=None, *, lock_sources=None):
    validate(spec, private)
    source = source.resolve()
    before = snapshot(source)
    assets = authenticate_defaults(spec, lock_sources)
    current = source / "Cargo.lock"
    selected, basis, locks = None, {"kind": "candidate-authored-lock; default selection bypassed"}, []
    if not current.exists():
        selected, selection = select_default(source, spec)
        basis = {"kind": "current-public-local-path-package", **selection}
        locks = [{"path": "Cargo.lock", "asset_target": selected["asset_target"],
                  "sha256": selected["sha256"], "allow_missing": True, "replace_if_sha256": []}]
    else:
        inside(source, current)
        parse_lock(input_bytes(current), spec)
    if private is not None:
        verify_harness(source, private)
    report = synchronize_candidate(source, mirror, {
        "manifests": [], "private_members": [], "locks": locks}, assets)
    mirror = mirror.resolve()
    require(snapshot(source) == before, "mirror preparation changed original candidate")
    mirrored = snapshot(mirror)
    expected = dict(before)
    if selected is not None:
        expected["Cargo.lock"] = ("file", selected["sha256"], False)
    require(mirrored == expected, "mirror differs beyond the missing-only lock")
    return {"selection": basis, "selected_default": selected, "synchronization": report,
            "source_before": before, "mirror_before": mirrored,
            "candidate_lock_sha256": file_hash(mirror / "Cargo.lock")}


def parse_lock(data, spec=None):
    profile = profile_for(spec)
    require(len(data) <= MAX_BYTES, "current lock exceeds input bound")
    value = tomllib.loads(data.decode())
    rows = value.get("package")
    require(isinstance(rows, list) and 1 <= len(rows) <= 8192, "missing/oversize current lock package graph")
    by_key = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("name"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]+", row["name"])
                and isinstance(row.get("version"), str)
                and re.fullmatch(r"[A-Za-z0-9_.+-]+", row["version"]), "invalid lock package")
        source = row.get("source")
        git = profile["font"] and isinstance(source, str) and source.startswith("git+")
        require(source is None or source == REGISTRY or git, "current external dependency is not provided registry source")
        if git:
            git_source(source)
            require("checksum" not in row, "unexpected checksum on a pinned Git lock package")
        if profile["font"] and source is not None:
            require(source in spec["vendor_sources"], "current external source is not provided by the bound vendor")
        if source is not None and not git:
            require(isinstance(row.get("checksum"), str) and re.fullmatch(r"[0-9a-f]{64}", row["checksum"]),
                    "current registry checksum missing")
        key = row["name"], row["version"], source
        require(key not in by_key, "duplicate current lock package")
        by_key[key] = row
    return by_key


def lock_edges(packages, row):
    values = row.get("dependencies", [])
    require(isinstance(values, list) and all(isinstance(v, str) for v in values), "invalid current lock edges")
    edges = set()
    for value in values:
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?: ([A-Za-z0-9_.+-]+))?(?: \(([^()]+)\))?", value)
        require(match is not None, "unparsed current lock edge")
        name, version, source = match.groups()
        keys = [k for k in packages if k[0] == name and (version is None or k[1] == version)
                and (source is None or k[2] == source)]
        require(len(keys) == 1 and keys[0] not in edges, "missing/ambiguous/duplicate current lock edge")
        edges.add(keys[0])
    return edges


def vendor_file_hashes(folder, declared):
    require(isinstance(declared, dict) and all(isinstance(n, str) and isinstance(h, str)
            and re.fullmatch(r"[0-9a-f]{64}", h) for n, h in declared.items()),
            "invalid source-aware vendor file checksum map")
    actual = {}
    for directory, dirs, files in os.walk(folder, followlinks=False):
        links = [d for d in dirs if (Path(directory) / d).is_symlink()]
        for name in [*files, *links]:
            path = Path(directory) / name
            relative = path.relative_to(folder).as_posix()
            if relative == ".cargo-checksum.json":
                continue
            require(path.is_file() and path.resolve().is_relative_to(folder.resolve()),
                    "vendor file/link escapes its self-contained package")
            actual[relative] = file_hash(path)
    require(actual == declared, "source-aware vendor file hashes changed or files are missing/extra")


def provided_vendor(packages, vendor, spec=None):
    profile = profile_for(spec)
    vendor = vendor.resolve()
    result = {}
    for key, package in packages.items():
        if key[2] is None:
            continue
        parent = vendor
        if profile["font"]:
            mapping = validate_source_mapping(spec)
            require(key[2] in mapping, "current source absent from provided vendor")
            parent = vendor / mapping[key[2]]
        folder = parent / (key[0] + "-" + key[1])
        manifest, checksum = folder / "Cargo.toml", folder / ".cargo-checksum.json"
        require(folder.is_dir() and not folder.is_symlink(), "current package absent from provided vendor")
        inside(vendor, manifest)
        inside(vendor, checksum)
        check = strict_json(input_bytes(checksum))
        expected_checksum = None if profile["font"] and key[2].startswith("git+") else package["checksum"]
        require(isinstance(check, dict) and "package" in check and check.get("package") == expected_checksum
                and isinstance(check.get("files"), dict), "vendor checksum differs from current lock")
        if profile["font"]:
            vendor_file_hashes(folder, check["files"])
        require(check["files"].get("Cargo.toml") == file_hash(manifest), "vendor manifest checksum changed")
        document = tomllib.loads(input_bytes(manifest).decode())
        data = document.get("package", {})
        version = data.get("version")
        if profile["font"] and version == {"workspace": True}:
            version = document.get("workspace", {}).get("package", {}).get("version")
        require((data.get("name"), version) == key[:2], "vendor manifest package identity changed")
        result[key] = str(manifest)
    return result


def verify_harness(root, private):
    if private.get("task_id") == LIBSBP_TASK:
        if __package__:
            from .rust_libsbp_entry import verify_blocks
        else:
            from rust_libsbp_entry import verify_blocks
        return verify_blocks(root, private["repository_test_blocks"])
    path = inside(root, root / private["harness"]["path"])
    require(file_hash(path) == private["harness"]["sha256"], "original private harness changed")


def verify_graph(graph, root, packages, vendor, private=None, spec=None):
    profile = profile_for(spec, private)
    require(isinstance(graph, dict) and type(graph.get("version")) is int and graph["version"] == 1
            and graph.get("workspace_root") == str(root), "wrong complete metadata root/version")
    rows, resolve = graph.get("packages"), graph.get("resolve")
    require(isinstance(rows, list) and isinstance(resolve, dict), "metadata lacks packages/resolve")
    nodes = resolve.get("nodes")
    require(isinstance(nodes, list) and len(rows) == len(nodes) == len(packages), "incomplete package/node graph")
    require(all(isinstance(p, dict) and isinstance(p.get("id"), str) for p in rows + nodes),
            "invalid metadata package/node IDs")
    by_id = {p["id"]: (p["name"], p["version"], p.get("source")) for p in rows}
    require(len(by_id) == len(rows) == len({n["id"] for n in nodes})
            and set(by_id) == {n["id"] for n in nodes} and set(by_id.values()) == set(packages),
            "metadata package identities differ from entire current lock")
    local_ids = {p["id"] for p in rows if p.get("source") is None}
    members = graph.get("workspace_members")
    require(isinstance(members, list) and members and len(set(members)) == len(members)
            and set(members) <= local_ids, "missing/foreign workspace member")
    require(resolve.get("root") is None or resolve["root"] in local_ids, "foreign resolved root")
    package_rows = {p["id"]: p for p in rows}
    for package in rows:
        key = by_id[package["id"]]
        targets, features = package.get("targets"), package.get("features")
        require(isinstance(features, dict) and all(isinstance(k, str) and isinstance(v, list)
                and all(isinstance(i, str) for i in v) for k, v in features.items()),
                "invalid metadata feature declarations")
        require(isinstance(targets, list) and targets, "package source targets missing")
        if key[2] is None:
            path = inside(root, Path(package.get("manifest_path", "")))
            actual = tomllib.loads(input_bytes(path).decode()).get("package", {})
            version = actual.get("version")
            if version == {"workspace": True}:
                version = tomllib.loads(input_bytes(root / "Cargo.toml").decode()).get(
                    "workspace", {}).get("package", {}).get("version")
            require((actual.get("name"), version) == key[:2], "local package is not current manifest")
            for target in targets:
                require(isinstance(target, dict) and isinstance(target.get("src_path"), str)
                        and isinstance(target.get("kind"), list), "invalid local metadata target")
                # The original golden tree declares a missing proto bench.
                # Neither plain check nor the fixed --test command selects it.
                # This is a target-kind rule, never a replay/reference selector.
                unselected_bench = target["kind"] == ["bench"] and target.get("test") is False
                inside(root, Path(target["src_path"]), regular=not unselected_bench)
        else:
            require(package.get("manifest_path") == vendor.get(key), "external package redirects outside provided vendor")
    normalized = []
    for node in nodes:
        dependencies, deps, features = node.get("dependencies"), node.get("deps"), node.get("features")
        require(isinstance(dependencies, list) and len(set(dependencies)) == len(dependencies)
                and set(dependencies) <= set(by_id) and isinstance(deps, list)
                and all(isinstance(d, dict) and d.get("pkg") in by_id for d in deps)
                and {d["pkg"] for d in deps} == set(dependencies), "metadata edge closure changed")
        key = by_id[node["id"]]
        require({by_id[i] for i in dependencies} == lock_edges(packages, packages[key]),
                "metadata edges differ from complete current lock")
        require(isinstance(features, list) and all(isinstance(f, str) for f in features)
                and len(set(features)) == len(features)
                and set(features) <= set(package_rows[node["id"]]["features"]),
                "metadata node features are absent/duplicate/undeclared")
        for dep in deps:
            require(isinstance(dep.get("name"), str) and isinstance(dep.get("dep_kinds"), list)
                    and dep["dep_kinds"] and all(isinstance(k, dict) and set(k) == {"kind", "target"}
                    and k["kind"] in {None, "dev", "build"} and (k["target"] is None or isinstance(k["target"], str))
                    for k in dep["dep_kinds"]), "invalid dependency kind/target")
        normalized.append({"package": key, "features": sorted(features),
                           "dependencies": sorted(dependencies), "deps": deps})
    if private is not None:
        verify_harness(root, private)
        if profile["font"] or profile.get("scylla") or profile.get("zerocopy") or profile.get("libsbp"):
            selected = ("sbp" if profile.get("libsbp") else "zerocopy-derive" if profile.get("zerocopy")
                        else "scylla" if profile.get("scylla") else "punchcut")
            defaults = [p["id"] for p in rows if p["name"] == selected
                        and p.get("source") is None and p["id"] in members]
            require(len(defaults) == 1, "explicit -p " + selected + " has no unique current workspace package")
        else:
            defaults = graph.get("workspace_default_members")
            require(isinstance(defaults, list) and defaults and set(defaults) <= set(members),
                    "private command has no bound default workspace selection")
        if profile.get("libsbp"):
            targets = [(p, t) for p in rows if p["id"] in defaults for t in p["targets"]
                       if t.get("kind") == ["lib"]]
            require(len(targets) == 1 and targets[0][1].get("test") is True,
                    "original sbp --lib target is disabled, missing or ambiguous")
            inside(root, Path(targets[0][1]["src_path"]))
        else:
            targets = [(p, t) for p in rows if p["id"] in defaults for t in p["targets"]
                       if t.get("name") == "oracle_session" and t.get("kind") == ["test"]]
            path_matches = len(targets) == 1 and targets[0][1].get("src_path") == str(root / private["harness"]["path"])
            if (profile.get("scylla") or profile.get("zerocopy")) and len(targets) == 1:
                # Cargo1.64 retains ../ components for a moved member's explicit
                # test path. Resolve only inside the complete current candidate;
                # the raw graph, original harness hash and all symlink guards stay.
                path_matches = inside(root, Path(targets[0][1]["src_path"])) == inside(
                    root, root / private["harness"]["path"])
            require(len(targets) == 1 and path_matches
                    and targets[0][1].get("test") is True,
                    "metadata redirects/disables/skips the original private target")
    return {"packages": len(rows), "nodes": len(nodes), "workspace_members": members,
            "graph_sha256": digest(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode())}


def transport(command, private=None, spec=None):
    profile = profile_for(spec, private)
    prefix = profile["prefix"]
    require(isinstance(command, list) and command[:1] == ["cargo"]
            and command[1:1 + len(prefix)] == prefix, "unbound Cargo transport prefix")
    tail = command[1 + len(prefix):]
    require(tail == (profile["command"] if private is not None else profile["public"]),
            "changed original check/test command or missing private contract")
    index = command.index("--") if "--" in command else len(command)
    return [REAL_CARGO, *command[1:index], "--locked", "--offline", *command[index:]]


def metadata_stderr_allowed(text, spec=None, root=None):
    remaining = text
    profile = profile_for(spec)
    if profile.get("libsbp") and remaining.strip():
        require(root is not None, "libsbp metadata warning needs its current root")
        if __package__:
            from .rust_libsbp_entry import metadata_warnings
        else:
            from rust_libsbp_entry import metadata_warnings
        remaining = metadata_warnings(remaining, root)
    if (profile["font"] or profile.get("scylla")) and FONT_RESOLVER_WARNING in remaining:
        # Remove only for recognition. The original record is never modified.
        require(remaining.count(FONT_RESOLVER_WARNING) == 1, "duplicate resolver-warning block")
        remaining = remaining.replace(FONT_RESOLVER_WARNING, "", 1)
    return all(not line.strip() or METADATA_STDERR.fullmatch(line.strip()) for line in remaining.splitlines())


def write_stream(name, data):
    stream = getattr(sys, name)
    if hasattr(stream, "buffer"):
        stream.buffer.write(data)
        stream.buffer.flush()
    else:
        stream.write(data.decode("utf-8", errors="replace"))
        stream.flush()


def run_logged(command, root, environment, logs, name, deadline, limit, *, forward=False):
    paths = {kind: logs / (name + "." + kind) for kind in ("stdout", "stderr")}
    limited = None
    started = time.monotonic()
    with paths["stdout"].open("xb") as stdout, paths["stderr"].open("xb") as stderr:
        process = subprocess.Popen(command, cwd=root, env=environment, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    limited = "deadline"
                    break
                if sum(p.stat().st_size for p in paths.values()) > limit:
                    limited = "output_limit"
                    break
                time.sleep(.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    if sum(p.stat().st_size for p in paths.values()) > limit:
        limited = limited or "output_limit"
    record = {"command": command, "cwd": str(root), "pid": process.pid,
              "returncode": process.returncode, "limit": limited, "seconds": time.monotonic() - started}
    for kind, path in paths.items():
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        try:
            text, utf8 = data.decode("utf-8"), True
        except UnicodeDecodeError:
            text, utf8 = data.decode("utf-8", errors="replace"), False
        record.update({kind: text, kind + "_utf8_valid": utf8, kind + "_path": str(path),
                       kind + "_sha256": digest(data), kind + "_total_bytes": path.stat().st_size,
                       kind + "_captured_bytes": len(data)})
        if forward:
            write_stream(kind, data)
    return record


def execute(spec, source, build_root, command, private=None, *, run=run_logged,
            lock_sources=None, vendor=Path(VENDOR), source_config=None):
    report = {"task_id": spec.get("task_id") if isinstance(spec, dict) else None,
              "status": "checking", "historical_restoration": False,
              "selection_uses_test_results": False, "default_resolution_commands": 0}
    code = 2
    logs = None
    try:
        validate(spec, private)
        profile = profile_for(spec)
        report["binding"] = {
            "spec_sha256": digest(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()),
            "receipt_sha256": spec["receipt_sha256"], "toolchain": spec["toolchain"],
            "cargo_sha256": spec["cargo_sha256"], "rustc_sha256": spec["rustc_sha256"],
        }
        command_seconds = spec.get("command_timeout_seconds", COMMAND_SECONDS)
        if profile.get("scylla") or profile.get("zerocopy"):
            report["binding"]["command_timeout_seconds"] = command_seconds
        effective = transport(command, private, spec)
        require(file_hash(REAL_CARGO) == spec["cargo_sha256"]
                and file_hash(REAL_RUSTC) == spec["rustc_sha256"], "pinned Cargo/rustc bytes changed")
        source, build_root = source.resolve(), build_root.absolute()
        require(not build_root.is_symlink() and not build_root.resolve().is_relative_to(source)
                and not source.is_relative_to(build_root.resolve()), "source/build cache collision")
        build_root.mkdir(parents=True, exist_ok=True)
        with (build_root / "build.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            logs = build_root / ("first-new-" + str(time.time_ns()))
            logs.mkdir()
            deadline = time.monotonic() + command_seconds
            mirror = build_root / "source"
            if profile["font"]:
                report["source_configuration"] = verify_source_configuration(spec, vendor, source_config)
            report["mirror"] = prepare_mirror(source, mirror, spec, private, lock_sources=lock_sources)
            before, mirror_before = snapshot(source), snapshot(mirror)
            environment = {**os.environ, "SOURCE_ROOT": str(mirror), "CARGO_NET_OFFLINE": "true",
                           "RUSTC": REAL_RUSTC, "CARGO_BUILD_JOBS": str(spec["cpus"]),
                           "CARGO_TARGET_DIR": str(build_root / "target"), "RUSTUP_AUTO_INSTALL": "0"}
            report["environment"] = {k: environment[k] for k in (
                "SOURCE_ROOT", "CARGO_NET_OFFLINE", "RUSTC", "CARGO_BUILD_JOBS",
                "CARGO_TARGET_DIR", "RUSTUP_AUTO_INSTALL")}
            def unchanged():
                require(snapshot(source) == before and snapshot(mirror) == mirror_before,
                        "command changed current source/manifest/lock/harness")
            packages = parse_lock(input_bytes(mirror / "Cargo.lock"), spec)
            provided = provided_vendor(packages, vendor, spec)
            child_root = mirror
            if profile.get("libsbp"):
                child_root = inside(mirror, mirror / profile["command_cwd"], regular=False)
                require(child_root.is_dir(), "original libsbp command workdir is missing")
            report["command_cwd"] = str(child_root)
            metadata = [REAL_CARGO, *profile["prefix"], "metadata", "--format-version", "1",
                        "--locked", "--offline", "--all-features"]
            record = run(metadata, child_root, environment, logs, "metadata",
                         min(deadline, time.monotonic() + spec["metadata_timeout_seconds"]),
                         spec["max_log_bytes"])
            report["metadata"] = record
            unchanged()
            guard.infrastructure({**record, "stdout": ""}, report)
            require(record.get("stdout_utf8_valid", True) and record.get("stderr_utf8_valid", True)
                    and record["returncode"] == 0 and metadata_stderr_allowed(
                        record.get("stderr", ""), spec, root=mirror), "complete offline metadata failed")
            report["graph"] = verify_graph(strict_json(record["stdout"]), mirror, packages, provided, private, spec)
            unchanged()
            report["transport"] = {"original_argv": command, "effective_argv": effective}
            record = run(effective, child_root, environment, logs, "cargo", deadline,
                         spec["max_log_bytes"], forward=True)
            report["execution"] = record
            unchanged()
            code = record["returncode"] if record["returncode"] >= 0 else 128 - record["returncode"]
            try:
                guard.infrastructure(record, report)
            except guard.SelectionError as exc:
                report["infrastructure_error"] = str(exc)
                code = code or 2  # Never turn a failed child into success.
            if not record.get("stdout_utf8_valid", True) or not record.get("stderr_utf8_valid", True):
                report["infrastructure_error"] = "non-UTF8 Cargo stream; original bytes retained"
                code = code or 2
            if private is not None:
                report["test_results"] = guard.api.test_results(record, private["expected_tests"])
                if not all(report["test_results"].values()):
                    code = code or 2
            report.update(status="completed" if code == 0 else "failed", exit_status=code,
                          source_unchanged=True, mirror_unchanged=True)
    except (OverlayError, guard.SelectionError, OSError, ValueError, KeyError, TypeError) as exc:
        report.update(status="rejected", error=type(exc).__name__ + ": " + str(exc), exit_status=2)
        code = 2
    finally:
        if logs is not None:
            (logs / "record.json").write_text(json.dumps(report, sort_keys=True) + "\n")
    return code, report


def publish(report, private):
    # Separate the wrapper's evidence protocol from actual Cargo bytes.
    if report.get("execution", {}).get("stdout") and not report["execution"]["stdout"].endswith("\n"):
        write_stream("stdout", b"\n")
    if private is not None:
        print(EVIDENCE_PREFIX + json.dumps(report, sort_keys=True, separators=(",", ":")), flush=True)
    else:
        compact = {"status": report["status"], "exit_status": report.get("exit_status", 2),
                   "spec_sha256": report.get("binding", {}).get("spec_sha256"),
                   "packages": report.get("graph", {}).get("packages"),
                   "candidate_lock_sha256": report.get("mirror", {}).get("candidate_lock_sha256")}
        print("ReviewBench first-new Cargo lock: " + json.dumps(compact, sort_keys=True), flush=True)
    if report.get("error"):
        print(report["error"], file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    parser.add_argument("--build-root", type=Path, default=Path("/opt/reviewbench-cache/rust-build"))
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--cwd", choices=["."], default=".")
    parser.add_argument("--private-spec", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    private = strict_json(input_bytes(args.private_spec)) if args.private_spec else None
    spec = strict_json(input_bytes(args.spec))
    validate(spec, private)
    if hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, set(sorted(os.sched_getaffinity(0))[:spec["cpus"]]))
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    code, report = execute(spec, args.source, args.build_root, command, private)
    publish(report, private)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OverlayError, OSError, ValueError, KeyError, TypeError) as exc:
        print("first-new Cargo lock infrastructure: " + str(exc), file=sys.stderr, flush=True)
        raise SystemExit(2)
