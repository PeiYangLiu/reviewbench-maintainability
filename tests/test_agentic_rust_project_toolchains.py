"""Only public toolchain declarations select original compilers; graph selection stays independent."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_project_toolchain_entry as entry, rust_project_toolchains as host
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_cargo_contract import nested_fixture, runner
from test_agentic_rust_image import ref


def fixture(tmp_path, dependency="0.1.0"):
    source, core, private = nested_fixture(tmp_path, dependency)
    tools = {}
    for version in entry.VERSIONS:
        folder = tmp_path / version / "bin"
        folder.mkdir(parents=True)
        for name in ("cargo", "rustc"):
            (folder / name).write_text("fixed original " + version + " " + name)
        tools[version] = {n: ref(folder / n) for n in ("cargo", "rustc")}
    core = {k: v for k, v in core.items() if k not in ("cargo", "rustc")}
    spec = {"schema_version": entry.SCHEMA, "task_id": core["task_id"], "default_toolchain": "1.68.2",
            "toolchains": tools, "cargo_contract": core}
    return source, spec, private


def test_original_default_when_project_has_no_declaration(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    version, evidence = entry.project_toolchain(root, "1.68.2")
    assert version == "1.68.2" and evidence["path"] is None


@pytest.mark.parametrize("name,content,version", [
    ("rust-toolchain.toml", '[toolchain]\nchannel="nightly-2023-05-04"\n', "nightly-2023-05-04"),
    ("rust-toolchain", "1.68.2\n", "1.68.2"),
    ("rust-toolchain", '[toolchain]\nchannel="nightly-2023-05-04"\n', "nightly-2023-05-04"),
])
def test_public_declaration_selects_only_original_versions_and_keeps_exact_hash(tmp_path, name, content, version):
    (tmp_path / name).write_text(content)
    chosen, evidence = entry.project_toolchain(tmp_path, "1.68.2")
    assert chosen == version and evidence["sha256"] == ref(tmp_path / name)["sha256"]


def test_nearest_public_file_and_legacy_precedence(tmp_path):
    nested = tmp_path / "member"
    nested.mkdir()
    (tmp_path / "rust-toolchain.toml").write_text('[toolchain]\nchannel="nightly-2023-05-04"\n')
    assert entry.project_toolchain(tmp_path, "1.68.2", cwd=nested)[0] == "nightly-2023-05-04"
    (nested / "rust-toolchain").write_text("1.68.2\n")
    (nested / "rust-toolchain.toml").write_text('[toolchain]\nchannel="nightly-2023-05-04"\n')
    assert entry.project_toolchain(tmp_path, "1.68.2", cwd=nested)[0] == "1.68.2"


@pytest.mark.parametrize("content", [
    '[toolchain]\nchannel="nightly"\n', '[toolchain]\nchannel="1.93.0"\n',
    '[toolchain]\npath="/foreign/compiler"\nchannel="1.68.2"\n',
])
def test_unknown_or_external_compiler_is_not_substituted(tmp_path, content):
    (tmp_path / "rust-toolchain.toml").write_text(content)
    with pytest.raises(entry.core.VariantError):
        entry.project_toolchain(tmp_path, "1.68.2")


def test_declaration_link_inside_candidate_is_supported_but_escape_is_rejected(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "decl").write_text('[toolchain]\nchannel="nightly-2023-05-04"\n')
    link = root / "rust-toolchain.toml"
    link.symlink_to("decl")
    assert entry.project_toolchain(root, "1.68.2")[0] == "nightly-2023-05-04"
    link.unlink()
    (tmp_path / "outside").write_text("1.68.2")
    link.symlink_to("../outside")
    with pytest.raises(entry.core.VariantError):
        entry.project_toolchain(root, "1.68.2")


@pytest.mark.parametrize("dependency", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("version", entry.VERSIONS)
def test_compiler_choice_is_independent_of_full_graph_and_test_outcomes(tmp_path, dependency, version):
    source, spec, private = fixture(tmp_path, dependency)
    if version != "1.68.2":
        (source / "rust-toolchain.toml").write_text('[toolchain]\nchannel="' + version + '"\n')
    chosen, declaration, public = entry.selected_spec(spec, source)
    assert chosen == version
    before, calls = entry.core.snapshot(source), []
    result = entry.core.run_candidate(public, source, tmp_path / "work", private, run=runner(calls, passed=False))
    assert result["test_results"] == {"original_behavior": False}
    assert result["source_before"] == result["source_after"] == before
    assert len(calls) == 3 and all(command[0] == spec["toolchains"][version]["cargo"]["path"] for command in calls)
    assert "--locked" not in calls[-1]


def test_explicit_compiler_override_precedes_project_declaration(tmp_path):
    source, spec, _ = fixture(tmp_path)
    (source / "rust-toolchain.toml").write_text('[toolchain]\nchannel="unprovided"\n')
    chosen, evidence, _ = entry.selected_spec(spec, source, override="1.68.2")
    assert chosen == "1.68.2" and evidence["explicit_override"]
    with pytest.raises(entry.core.VariantError):
        entry.selected_spec(spec, source, override="nightly")


@pytest.mark.parametrize("binary", ["cargo", "rustc"])
def test_selected_compiler_bytes_must_match_original_pins(tmp_path, binary):
    source, spec, _ = fixture(tmp_path)
    Path(spec["toolchains"]["1.68.2"][binary]["path"]).write_text("changed")
    with pytest.raises(entry.core.VariantError, match="binary changed"):
        entry.selected_spec(spec, source)


def test_other_task_routes_and_frozen_runtime_entry_remain_unchanged(monkeypatch):
    from agentic_runtime_migration import rust_lane, rust_vcx
    assert not host.requested(SimpleNamespace(), {"id": host.ID})
    configured = SimpleNamespace(config={"migration": {host.OPTION: {host.ID: {}}}})
    assert host.requested(configured, {"id": host.ID})
    assert not host.requested(configured, {"id": "other"})
    configured.config["migration"][host.OPTION]["other"] = {}
    with pytest.raises(ContractError):
        host.requested(configured, {"id": "other"})
    marker = object()
    monkeypatch.setattr(rust_vcx, "requested", lambda *_: True)
    monkeypatch.setattr(rust_vcx, "migrate", lambda *_: marker)
    assert rust_lane.migrate(SimpleNamespace(), {"id": "existing"}, {}) is marker
