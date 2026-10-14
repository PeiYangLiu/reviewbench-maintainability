"""Real Git/blob controls; no benchmark compilation or calibration."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from agentic_runtime_migration import native_prepared_sources as reuse
from agentic_runtime_migration import native_submodules as adapter
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.snapshots import start_archive, extract_tar, tree_hash, tree_files
from test_agentic_native_submodules import fixture


def ref(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return ref(path)


@pytest.fixture
def prepared(fixture, tmp_path):
    ctx, task, plan, _, _ = fixture
    roots = adapter.readonly_links(ctx.repo(task), task["start_sha"])
    paths = adapter.selected_paths(task, plan, roots)
    pins = {row["path"]: row["commit"] for row in roots}
    records, _ = adapter.closure(ctx.repo(task), task["start_sha"], paths,
        ctx.config["migration"]["native_submodule_repositories"][task["repo"]])
    original = {"status": "exact_recursive_original_git_inputs_prepared",
                "id": task["id"], "full_scope": 402, "closure": records,
                "new_project_build_test_model_judge_calls": 0}
    source_record = tmp_path / "source-preparation.json"
    source_ref = write(source_record, original)
    part = {"sha256": hashlib.sha256(b"x").hexdigest(), "size_bytes": 1,
            "md5_base64": base64.b64encode(hashlib.md5(b"x").digest()).decode()}
    published = {}
    for name in paths:
        destination = tmp_path / "physical" / name
        for row in records:
            if row["path"] != name and not row["path"].startswith(name + "/"):
                continue
            target = destination if row["path"] == name else destination / row["path"][len(name)+1:]
            if target.exists():
                target.rmdir()
            with start_archive(Path(row["repository"]), row["commit"]) as stream:
                extract_tar(stream, target)
        descriptor = {
            "schema_version": "reviewbench-runtime-asset-tree/v1",
            "source_kind": "submodule", "git_commit": pins[name],
            "tree_sha256": tree_hash(destination), "files": len(tree_files(destination)),
            "archive": {"schema_version": "reviewbench-chunked-archive/v1", "codec": "gzip",
                        "input_bytes": 100, "compressed_bytes": 1, "parts": [part]},
        }
        asset = write(tmp_path / "assets" / (name.replace("/", "_") + ".json"), descriptor)
        published[name] = {"asset": asset, "commit": pins[name], "tree_sha256": descriptor["tree_sha256"],
                           "files": descriptor["files"], "physical_restoration": str(destination)}
    publication = {
        "status": "all_original_recursive_source_roots_archived_and_independently_restored",
        "id": task["id"], "full_scope": 402, "new_project_build_test_model_judge_calls": 0,
        "source_preparation": source_ref, "roots": published, "modules": len(records),
        "original_files": sum(row["files"] for row in records), "independent_get_receipts": [part],
    }
    file = tmp_path / "publication.json"
    pointer = write(file, publication)
    ctx.config["migration"][reuse.OPTION] = {task["repo"]: pointer}
    return ctx, task, plan, paths, pins, records, publication, file


def update(prepared):
    ctx, task, _, _, _, _, publication, file = prepared
    ctx.config["migration"][reuse.OPTION][task["repo"]] = write(file, publication)


def test_original_archived_roots_are_reused_without_any_export(prepared):
    ctx, task, plan, paths, _, _, publication, _ = prepared
    old = json.loads(json.dumps(plan))
    changed, modules = adapter.adapt(ctx, task, plan)
    assert plan == old
    assert not ctx.bundles
    assert {row["path"]: row["asset"] for row in modules} == {
        name: publication["roots"][name]["asset"] for name in paths}
    assert changed["test_command"] == plan["test_command"]
    assert changed["build_command"] == plan["build_command"]
    assert "git submodule update" not in changed["configure_command"]
    assert changed["configure_command"].endswith("; cmake -S . -B build-oracle -DTESTS=OFF -DTOOLS=OFF")
    record = json.loads(next((ctx.output / "native-source-reuse").glob("*.json")).read_text())
    assert record["status"] == "exact_archived_sources_reused_without_export"
    assert record["new_fetch_export_upload_build_test_model_judge_calls"] == 0
    assert all(row["actual_Git_blobs_compared"] for row in record["roots"].values())


def test_no_prepared_option_keeps_the_existing_export_path(fixture):
    ctx, task, plan, _, _ = fixture
    adapter.adapt(ctx, task, plan)
    assert len(ctx.bundles) == 3


@pytest.mark.parametrize("change", ["content", "missing", "extra", "mode", "linked-file", "linked-parent"])
def test_restored_tree_is_checked_against_actual_git_not_metadata_alone(prepared, change):
    ctx, task, plan, paths, _, _, publication, _ = prepared
    root = Path(publication["roots"][paths[0]]["physical_restoration"])
    if change == "content":
        (root / "public.hpp").write_text("// different source\n")
    elif change == "missing":
        (root / "tests/public.cpp").unlink()
    elif change == "extra":
        (root / "untracked.cpp").write_text("// injected source\n")
    elif change == "mode":
        (root / "public.hpp").chmod(0o755)
    elif change == "linked-file":
        (root / "public.hpp").unlink()
        (root / "public.hpp").symlink_to("/etc/hostname")
    else:
        (root / "nested/tests/nested.cpp").unlink()
        (root / "nested/tests").rmdir()
        (root / "nested/tests").symlink_to(root / "tests")
    with pytest.raises(ContractError, match="source|blob|permissions|linked"):
        adapter.adapt(ctx, task, plan)
    assert not ctx.bundles


@pytest.mark.parametrize("field,value", [
    ("full_scope", 401), ("id", "another-task"), ("modules", 1), ("original_files", 1),
    ("new_project_build_test_model_judge_calls", 1),
])
def test_preparation_scope_and_identity_are_not_relabelled(prepared, field, value):
    ctx, task, plan, _, _, _, publication, _ = prepared
    publication[field] = value
    update(prepared)
    with pytest.raises(ContractError):
        adapter.adapt(ctx, task, plan)


def test_changed_recursive_git_identity_is_not_accepted(prepared):
    ctx, task, plan, _, _, _, publication, _ = prepared
    pointer = publication["source_preparation"]
    p = Path(pointer["path"])
    data = json.loads(p.read_text())
    data["closure"][0]["commit"] = "f" * 40
    publication["source_preparation"] = write(p, data)
    update(prepared)
    with pytest.raises(ContractError, match="recursive Git closure"):
        adapter.adapt(ctx, task, plan)


def test_independent_get_receipt_is_required_for_every_part(prepared):
    ctx, task, plan, _, _, _, publication, _ = prepared
    publication["independent_get_receipts"] = []
    update(prepared)
    with pytest.raises(ContractError, match="GET receipt"):
        adapter.adapt(ctx, task, plan)


@pytest.mark.parametrize("field,value", [("source_kind", "grader"), ("git_commit", "f" * 40),
                                       ("tree_sha256", "f" * 64), ("files", 0)])
def test_archive_roles_and_identities_are_bound(prepared, field, value):
    ctx, task, plan, paths, _, _, publication, _ = prepared
    row = publication["roots"][paths[0]]
    p = Path(row["asset"]["path"])
    d = json.loads(p.read_text())
    d[field] = value
    row["asset"] = write(p, d)
    update(prepared)
    with pytest.raises(ContractError):
        adapter.adapt(ctx, task, plan)


def test_pointer_content_change_is_rejected(prepared):
    ctx, task, plan, _, _, _, _, file = prepared
    file.write_text(file.read_text() + " ")
    with pytest.raises(ContractError, match="record path/hash"):
        adapter.adapt(ctx, task, plan)


def test_qlever_recognizes_only_its_original_recursive_preparation_prefix():
    task = {"repo": "ad-freiburg/qlever"}
    profile = adapter.PROFILES[(task["repo"], 30)]
    links = [{"path": "third_party/a", "commit": "a" * 40},
             {"path": "third_party/b", "commit": "b" * 40}]
    plan = {"input_index": 30, "configure_command": profile.prefix + profile.operation
            + "; cmake -S . -B build-oracle -DCMAKE_CXX_COMPILER=g++-11", "build_command": "cmake --build build-oracle"}
    assert adapter.selected_paths(task, plan, links) == ("third_party/a", "third_party/b")
    plan["configure_command"] = plan["configure_command"].replace("--recursive", "--remote --recursive")
    with pytest.raises(ContractError):
        adapter.selected_paths(task, plan, links)
