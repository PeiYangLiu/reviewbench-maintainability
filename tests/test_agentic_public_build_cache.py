import copy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import cache_seed_entry as entry, public_build_cache as host
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.contracts import RuntimeRecipe


def fixture(tmp_path):
    target = tmp_path / "original"
    target.mkdir()
    files = {
        "debug/deps/libpublic_dep-0123456789abcdef.rlib": b"immutable compiled dependency",
        "debug/deps/public_dep-0123456789abcdef.d": b"dependency paths",
        "debug/build/public-dep-fedcba9876543210/out/libnative.a": b"native object archive",
        "debug/.fingerprint/public-dep-0123456789abcdef/lib-public_dep": b"external fingerprint",
        "debug/deps/libworkspace-123456789abcdef0.rlib": b"NEVER CACHE local implementation",
        "debug/.fingerprint/workspace-123456789abcdef0/lib-workspace": b"local fingerprint",
        "debug/original_private_test": b"NEVER CACHE test executable",
        "debug/incremental/private/test": b"NEVER CACHE incremental state",
    }
    for name, data in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, ns=(1710000000123456789, 1710000000123456789))
    metadata = {"packages": [{"name": "public-dep", "source": "registry+https://example.invalid/index"},
                              {"name": "workspace", "source": None}]}
    output = tmp_path / "seed"
    value = host.capture(target, metadata, output, "a" * 64, producer_receipt={
        "producer": "clean-start-public-check", "grader_assets_visible": False, "public_check_exit_status": 0})
    return target, output, value, metadata


def test_only_external_dependencies_are_kept(tmp_path):
    target, seed, manifest, _ = fixture(tmp_path)
    assert len(manifest["files"]) == 4
    assert not any("workspace" in row["path"] or "private" in row["path"] for row in manifest["files"])
    restored = tmp_path / "private-cache"
    assert entry.restore(seed, restored, "a" * 64)["status"] == "restored"
    for row in manifest["files"]:
        assert (restored / row["path"]).read_bytes() == (target / row["path"]).read_bytes()
        assert (restored / row["path"]).stat().st_mtime_ns == row["mtime_ns"]
        assert (restored / row["path"]).stat().st_ino != (seed / "payload" / row["path"]).stat().st_ino


def test_writable_jobs_do_not_share_inodes_or_poison_the_seed(tmp_path):
    _, seed, manifest, _ = fixture(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    entry.restore(seed, first, "a" * 64)
    entry.restore(seed, second, "a" * 64)
    name = manifest["files"][0]["path"]
    before = (seed / "payload" / name).read_bytes()
    (first / name).write_bytes(b"agent mutation")
    assert (seed / "payload" / name).read_bytes() == (second / name).read_bytes() == before
    assert entry.restore(seed, first, "a" * 64)["status"] == "already_initialized"
    assert (first / name).read_bytes() == b"agent mutation"


@pytest.mark.parametrize("mutation", [
    "bytes", "mode", "extra", "missing", "binding", "role", "producer", "grader",
    "traversal", "absolute", "duplicate", "total", "time",
])
def test_changed_or_unbound_seed_is_rejected_before_any_copy(tmp_path, mutation):
    _, seed, value, _ = fixture(tmp_path)
    row = value["files"][0]
    path = seed / "payload" / row["path"]
    if mutation == "bytes":
        path.write_bytes(b"tampered")
    elif mutation == "mode":
        path.chmod(0o700)
    elif mutation == "extra":
        (seed / "payload/unlisted").write_text("unknown")
    elif mutation == "missing":
        path.unlink()
    elif mutation == "binding":
        value["binding_sha256"] = "b" * 64
    elif mutation == "role":
        value["role"] = "grader"
    elif mutation == "producer":
        value["producer"] = "candidate-after-generation"
    elif mutation == "grader":
        value["grader_assets_visible"] = True
    elif mutation in {"traversal", "absolute"}:
        row["path"] = "../escape" if mutation == "traversal" else "/tmp/escape"
    elif mutation == "duplicate":
        value["files"].append(copy.deepcopy(row))
    elif mutation == "total":
        value["bytes"] += 1
    else:
        row["mtime_ns"] = -1
    (seed / entry.MANIFEST).write_text(json.dumps(value))
    output = tmp_path / "rejected"
    with pytest.raises((ValueError, OSError, KeyError)):
        entry.restore(seed, output, "a" * 64)
    assert not output.exists()


def test_existing_unmarked_cache_is_not_overwritten(tmp_path):
    _, seed, _, _ = fixture(tmp_path)
    output = tmp_path / "existing"
    output.mkdir()
    (output / "user-data").write_text("keep")
    with pytest.raises(ValueError, match="not empty"):
        entry.restore(seed, output, "a" * 64)
    assert (output / "user-data").read_text() == "keep"


def test_grader_produced_seed_is_not_accepted(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ContractError, match="public checks"):
        host.capture(target, {}, tmp_path / "seed", "a" * 64, producer_receipt={
            "producer": "clean-start-public-check", "grader_assets_visible": True,
            "public_check_exit_status": 0})


def test_lib_prefix_and_workspace_name_collisions_are_conservative(tmp_path):
    target = tmp_path / "target"
    (target / "debug/deps").mkdir(parents=True)
    for name in ("libc", "liblibc", "libcollision", "collision"):
        (target / ("debug/deps/" + name + "-0123456789abcdef.d")).write_text("data")
    metadata = {"packages": [{"name": "libc", "source": "registry+fixed"},
                              {"name": "collision", "source": "registry+fixed"},
                              {"name": "libcollision", "source": None}]}
    paths, _, _ = host.external_artifacts(target, metadata)
    assert "debug/deps/libc-0123456789abcdef.d" in paths
    assert "debug/deps/liblibc-0123456789abcdef.d" in paths
    assert "debug/deps/libcollision-0123456789abcdef.d" not in paths


def test_same_named_local_and_registry_crates_are_not_cached(tmp_path):
    target, _, _, metadata = fixture(tmp_path)
    metadata["packages"].append({"name": "public-dep", "source": None})
    with pytest.raises(ContractError, match="external"):
        host.external_artifacts(target, metadata)


def recipe():
    return RuntimeRecipe(
        task_id="fixture", repo="fixture/repo", start_sha="a" * 40, lane="rust",
        image="sha256:" + "b" * 64, image_audit={"path": "/audit.json", "sha256": "c" * 64},
        assets=[
            {"kind": "archive", "path": "/public.json", "sha256": "d" * 64,
             "target": "/opt/reviewbench-assets/vendor", "visibility": "developer"},
            {"kind": "file", "path": "/private.rs", "sha256": "e" * 64,
             "target": "/opt/reviewbench-assets/test.rs", "visibility": "grader"}],
        cache_mounts={"target": "/opt/reviewbench-cache/target"}, dev_checks=["cargo check"],
        grading={"multifile_build": True, "immutable_paths": ["tests/original.rs"],
                 "commands": [{"command": "original-test", "parser": "rust-test", "expected_tests": ["fixed"]}]})


def test_binding_excludes_grader_inputs_and_physical_pointer_locations():
    before = recipe()
    after = copy.deepcopy(before)
    after.assets[0]["path"] = "/restored/other-location.json"
    after.assets[1]["sha256"] = "f" * 64
    after.grading["commands"][0]["command"] = "another-private-test"
    assert host.public_binding(before) == host.public_binding(after)


def test_seed_attachment_preserves_every_original_test_and_command():
    base = recipe()
    descriptor = {"public_runtime_binding": host.public_binding(base),
                  "asset": {"path": "/seed.json", "sha256": "f" * 64}}
    warmed = host.bind(base, descriptor, {"path": "/import.py", "sha256": "1" * 64})
    assert warmed.grading == base.grading and warmed.dev_checks == base.dev_checks
    assert len(warmed.bootstrap) == 1 and warmed.fingerprint != base.fingerprint
    assert base.bootstrap == []
    with pytest.raises(ContractError):
        host.bind(warmed, descriptor, {"path": "/import.py", "sha256": "1" * 64})


def test_wrong_source_or_compiler_image_binding_is_rejected():
    base = recipe()
    descriptor = {"public_runtime_binding": "0" * 64,
                  "asset": {"path": "/seed.json", "sha256": "f" * 64}}
    with pytest.raises(ContractError, match="different"):
        host.bind(base, descriptor, {"path": "/import.py", "sha256": "1" * 64})


def test_fixed_timestamp_same_size_payloads_have_distinct_context_paths():
    first, second = b"compiler1", b"compiler2"
    assert len(first) == len(second)
    a = {"sha256": hashlib.sha256(first).hexdigest()}
    b = {"sha256": hashlib.sha256(second).hexdigest()}
    assert host.image_input_name(0, a) != host.image_input_name(0, b)
    assert host.image_input_name(0, a) == host.image_input_name(0, dict(a))
    assert a["sha256"] in host.image_input_name(0, a)


@pytest.mark.parametrize("index,digest", [(-1, "a" * 64), (True, "a" * 64), (0, "../asset"), (0, "x" * 64)])
def test_public_image_context_names_reject_unbound_identities(index, digest):
    with pytest.raises(ContractError):
        host.image_input_name(index, {"sha256": digest})


def recapture(target, metadata, output, **kwargs):
    return host.capture(target, metadata, output, "a" * 64, producer_receipt={
        "producer": "clean-start-public-check", "grader_assets_visible": False,
        "public_check_exit_status": 0}, **kwargs)


def test_absolute_link_inside_declared_cache_becomes_equivalent_relative_link(tmp_path):
    target, _, _, metadata = fixture(tmp_path)
    name = "debug/build/public-dep-fedcba9876543210/out/alias.a"
    link = target / name
    original = "/opt/reviewbench-cache/target/debug/build/public-dep-fedcba9876543210/out/libnative.a"
    link.symlink_to(original)
    seed = tmp_path / "portable"
    manifest = recapture(target, metadata, seed, runtime_target="/opt/reviewbench-cache/target")
    assert manifest["omitted_nonportable_packages"] == []
    assert manifest["normalized_cache_links"] == [
        {"path": name, "original_target": original, "target": "libnative.a"}]
    assert os.readlink(link) == original
    restored = tmp_path / "restored"
    entry.restore(seed, restored, "a" * 64)
    assert os.readlink(restored / name) == "libnative.a"
    assert (restored / name).read_bytes() == b"native object archive"


@pytest.mark.parametrize("link_target", [
    "/outside/private-data",
    "../../../../../../outside/private-data",
    "/opt/reviewbench-cache/target/debug/deps/libworkspace-123456789abcdef0.rlib",
    "missing.a",
])
def test_nonportable_package_is_omitted_without_discarding_other_public_dependencies(tmp_path, link_target):
    target, _, _, metadata = fixture(tmp_path)
    other = target / "debug/deps/libother_dep-1123456789abcdef.rlib"
    other.write_bytes(b"keep this independent dependency")
    metadata["packages"].append({"name": "other-dep", "source": "registry+fixed"})
    (target / "debug/build/public-dep-fedcba9876543210/out/link.a").symlink_to(link_target)
    seed = tmp_path / "portable"
    manifest = recapture(target, metadata, seed, runtime_target="/opt/reviewbench-cache/target")
    assert manifest["omitted_nonportable_packages"] == ["public-dep"]
    assert [row["path"] for row in manifest["files"]] == [other.relative_to(target).as_posix()]
    assert len(list(target.rglob("libnative.a"))) == 1
    entry.restore(seed, tmp_path / "restored", "a" * 64)


def test_link_closure_omits_an_alias_to_an_omitted_package(tmp_path):
    target, _, _, metadata = fixture(tmp_path)
    metadata["packages"] += [{"name": "alias-dep", "source": "registry+fixed"},
                             {"name": "unrelated", "source": "registry+fixed"}]
    native = target / "debug/build/public-dep-fedcba9876543210/out"
    (native / "external.a").symlink_to("/outside/image-specific-library")
    alias = target / "debug/deps/libalias_dep-2123456789abcdef.rlib"
    alias.symlink_to("../build/public-dep-fedcba9876543210/out/libnative.a")
    (target / "debug/deps/libunrelated-3123456789abcdef.rlib").write_bytes(b"independent")
    manifest = recapture(target, metadata, tmp_path / "portable")
    assert manifest["omitted_nonportable_packages"] == ["alias-dep", "public-dep"]
    assert len(manifest["files"]) == 1 and "unrelated" in manifest["files"][0]["path"]


def test_closed_relative_link_stays_relative_and_cycles_are_not_exported(tmp_path):
    target, _, _, metadata = fixture(tmp_path)
    native = target / "debug/build/public-dep-fedcba9876543210/out"
    (native / "alias.a").symlink_to("libnative.a")
    good = recapture(target, metadata, tmp_path / "good")
    assert good["normalized_cache_links"] == []
    (native / "alias.a").unlink()
    (native / "alias.a").symlink_to("cycle.a")
    (native / "cycle.a").symlink_to("alias.a")
    with pytest.raises(ContractError, match="no portable"):
        recapture(target, metadata, tmp_path / "cycle")


@pytest.mark.parametrize("root", ["relative/root", "/", "/opt/../escape"])
def test_unbound_runtime_cache_mount_is_rejected(tmp_path, root):
    target, _, _, metadata = fixture(tmp_path)
    with pytest.raises(ContractError, match="cache mount"):
        recapture(target, metadata, tmp_path / "rejected", runtime_target=root)
