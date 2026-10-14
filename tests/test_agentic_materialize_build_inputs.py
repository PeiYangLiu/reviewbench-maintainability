"""Checksum-bound missing build inputs do not replace candidate-owned source/data."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_overlay_api_entry as entry
from test_agentic_rust_image import ref


def fixture(tmp_path):
    source, mirror = tmp_path / "candidate", tmp_path / "mirror"
    source.mkdir()
    mirror.mkdir()
    name = "src/prof/src/http/static/js/vendor/example.js"
    asset = tmp_path / "original.js"
    asset.write_bytes(b"original fixed build bytes")
    spec = {"build_inputs": [{"path": name, "source": ref(asset), "mode": 0o644}]}
    return source, mirror, name, asset, spec


def test_missing_native_data_is_staged_only_in_private_mirror(tmp_path):
    source, mirror, name, asset, public = fixture(tmp_path)
    before, expected = entry.core.snapshot(source), {}
    staged = entry.stage_build_inputs(public, mirror, before, expected)
    assert staged == [name] and (mirror / name).read_bytes() == asset.read_bytes()
    assert entry.core.snapshot(source) == before == {}
    assert entry.core.snapshot(mirror) == expected
    assert expected[name] == ["file", ref(asset)["sha256"], False]


@pytest.mark.parametrize("kind", ["file", "link"])
def test_candidate_owned_input_is_never_overwritten(tmp_path, kind):
    source, mirror, name, asset, public = fixture(tmp_path)
    for root in (source, mirror):
        path = root / name
        path.parent.mkdir(parents=True)
        if kind == "file":
            path.write_bytes(b"candidate owns these data bytes")
        else:
            path.symlink_to("candidate.js")
    before = entry.core.snapshot(source)
    expected = dict(before)
    assert entry.stage_build_inputs(public, mirror, before, expected) == []
    assert entry.core.snapshot(mirror) == before and expected == before


def test_changed_original_asset_stops_before_copying(tmp_path):
    source, mirror, name, asset, public = fixture(tmp_path)
    asset.write_bytes(b"changed")
    with pytest.raises(entry.core.VariantError, match="asset changed"):
        entry.stage_build_inputs(public, mirror, {}, {})
    assert not (mirror / name).exists()


def test_redirected_parent_is_not_followed(tmp_path):
    source, mirror, name, asset, public = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (mirror / "src").symlink_to(outside, target_is_directory=True)
    with pytest.raises(entry.core.VariantError, match="destination redirected"):
        entry.stage_build_inputs(public, mirror, {}, {})
    assert list(outside.iterdir()) == []


def test_old_profiles_without_native_inputs_are_unchanged(tmp_path):
    source, mirror, *_ = fixture(tmp_path)
    assert entry.stage_build_inputs({}, mirror, {}, {}) == []
    assert list(mirror.iterdir()) == []


def validated_fixture(tmp_path, mode):
    source, mirror, name, asset, public = fixture(tmp_path)
    bound = ref(asset)
    public.update(
        schema_version=entry.SAME_TARGET_SCHEMA, task_id="static-input-control",
        selection=entry.core.SELECTION, package="original", package_manifest="src/prof/Cargo.toml",
        feature_args=[], locks=[bound], reference_seed_sha256=bound["sha256"],
        cargo=bound, rustc=bound, overlay=bound, source_config=bound, overlay_paths=[],
        private_spec_sha256=bound["sha256"], private_tool_sha256=bound["sha256"],
        public_command=["build", "--locked", "-p", "original", "--lib", "--message-format=json"],
        cpus=2, timeout_seconds=60, metadata_timeout_seconds=10, max_log_bytes=1024**2,
    )
    public["build_inputs"][0]["mode"] = mode
    return source, mirror, name, asset, public


@pytest.mark.parametrize("mode", [0o644, 0o664, 0o666, 0o755])
def test_original_static_data_modes_validate_then_stage_exactly(tmp_path, mode):
    source, mirror, name, asset, public = validated_fixture(tmp_path, mode)
    entry.validate(public)
    expected = {}
    assert entry.stage_build_inputs(public, mirror, {}, expected) == [name]
    assert (mirror / name).stat().st_mode & 0o7777 == mode
    assert ref(mirror / name)["sha256"] == ref(asset)["sha256"]
    assert entry.core.snapshot(source) == {}
    assert entry.core.snapshot(mirror) == expected


@pytest.mark.parametrize("mode", [0o600, 0o777, 0o1666, 0o4666, 0o2666, -1, 420.0, "0644", True])
def test_unbound_special_or_noninteger_static_modes_are_rejected(tmp_path, mode):
    source, mirror, name, asset, public = validated_fixture(tmp_path, mode)
    with pytest.raises(entry.core.VariantError, match="build input destination"):
        entry.validate(public)
    assert not (mirror / name).exists() and entry.core.snapshot(source) == {}
