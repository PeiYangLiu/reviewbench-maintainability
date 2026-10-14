"""Explicit v9 -> v8 -> v7 -> v6 -> v5 -> v4, without another member call."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_agentic_judge_recovery_bootstrap import lineage, write, ENGINE, cached_full402_fixture
import recover_agentic_judgments as repair
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash


ORDER = ("v4", "v5", "v6", "v7", "v8", "v9")


def extended_lineage(tmp_path, graph=None):
    graph = graph or lineage(tmp_path)
    paths = {name: tmp_path / name for name in (*ORDER, "source")}
    common = {key: copy.deepcopy(graph.bootstrap[key])
              for key in ("task_ids", "tasks_fingerprint", "models", "expected_pairs", "source_run")}
    objects = {"source": graph.original, "v4": graph.predecessor, "v5": graph.previous,
               "v6": graph.bootstrap}
    objects["v7"] = {
        **common, "schema_version": repair.SCHEMA, "base_run": str(paths["v6"]),
        "generation_engine_fingerprint": ENGINE, "base_host_engine_fingerprint": "c" * 64,
        "bootstrap_base_schema": repair.BOOTSTRAP_BASE_SCHEMA,
    }
    objects["v8"] = {
        **common, "schema_version": repair.SCHEMA, "base_run": str(paths["v7"]),
        "generation_engine_fingerprint": ENGINE, "base_host_engine_fingerprint": "c" * 64,
    }
    objects["v9"] = {
        **common, "schema_version": repair.BOOTSTRAP_BASE_SCHEMA, "base_run": str(paths["v8"]),
        "engine_fingerprint": ENGINE, "behavior_fingerprint": graph.original["behavior_fingerprint"],
    }
    # The older host fingerprint is deliberately different from the v4 anchor.
    objects["v4"]["base_host_engine_fingerprint"] = "d" * 64
    g = SimpleNamespace(paths=paths, objects=copy.deepcopy(objects), tasks=graph.tasks, models=["solver"])
    persist(g)
    return g


def persist(g, changed=None, mutate=None):
    """Recompute ancestor hashes so negatives actually reach the changed field."""
    original = copy.deepcopy(g.objects["source"])
    if changed == "source":
        mutate(original)
    write(g.paths["source"] / "experiment-manifest.json", original)
    source_hash = file_hash(g.paths["source"] / "experiment-manifest.json")
    for name in ORDER:
        value = copy.deepcopy(g.objects[name])
        value["source_manifest_sha256"] = source_hash
        if name in {"v5", "v7", "v8"}:
            value["base_manifest_sha256"] = file_hash(Path(value["base_run"]) / "experiment-manifest.json")
        if name == "v7":
            value["bootstrap_lineage"] = [
                {"manifest": str(g.paths[parent] / "experiment-manifest.json"),
                 "sha256": file_hash(g.paths[parent] / "experiment-manifest.json"),
                 "schema_version": g.objects[parent]["schema_version"]} for parent in ("v5", "v4")]
        if name == changed:
            mutate(value)
        write(g.paths[name] / "experiment-manifest.json", value)


def bind(g, name="v9"):
    path = g.paths[name]
    return repair.base_engine_binding(path, json.loads((path / "experiment-manifest.json").read_text()),
                                      g.tasks, g.models)


def test_v9_binds_all_five_ancestors_and_correct_host_without_rewriting(tmp_path):
    g = extended_lineage(tmp_path)
    before = {name: (path / "experiment-manifest.json").read_bytes() for name, path in g.paths.items()}
    result = bind(g)
    assert result["generation_engine_fingerprint"] == ENGINE
    assert result["base_host_engine_fingerprint"] == "c" * 64
    assert [Path(x["manifest"]).parent for x in result["bootstrap_lineage"]] == [
        g.paths[name] for name in ("v8", "v7", "v6", "v5", "v4")]
    assert {name: (path / "experiment-manifest.json").read_bytes() for name, path in g.paths.items()} == before


@pytest.mark.parametrize("name", ["v8", "v7", "v5", "v4"])
def test_explicit_full402_judge_and_measurement_bases_use_same_bounded_contract(tmp_path, name):
    g = extended_lineage(tmp_path)
    bound = bind(g, name)
    assert "bootstrap_lineage" not in bound
    assert len(bound["recovery_lineage"]) == ORDER.index(name)
    assert bound["base_host_engine_fingerprint"] == "c" * 64


@pytest.mark.parametrize("name", ORDER)
@pytest.mark.parametrize("field", [
    "schema", "engine", "task_ids", "tasks_fingerprint", "models", "expected_pairs",
    "source_run", "source_manifest_sha256",
])
def test_every_overlay_must_keep_original_schema_scope_source_model_engine(tmp_path, name, field):
    g = extended_lineage(tmp_path)

    def mutate(value):
        if field == "schema":
            value["schema_version"] = "unknown-recovery/v1"
        elif field == "engine":
            key = "engine_fingerprint" if name in {"v6", "v9"} else "generation_engine_fingerprint"
            value[key] = "f" * 64
        elif field == "task_ids":
            value[field] = list(reversed(value[field]))
        elif field == "tasks_fingerprint":
            value[field] = "f" * 64
        elif field == "models":
            value[field] = ["another-solver"]
        elif field == "expected_pairs":
            value[field] = 402.0
        elif field == "source_run":
            value[field] = str(tmp_path / "different-source")
        else:
            value[field] = "0" * 64

    persist(g, name, mutate)
    with pytest.raises(ContractError):
        bind(g)


@pytest.mark.parametrize("field", ["schema_version", "protocol", "engine_fingerprint", "models", "tasks_fingerprint"])
def test_original_progressive_anchor_is_not_an_engine_alias(tmp_path, field):
    g = extended_lineage(tmp_path)
    persist(g, "source", lambda value: value.update({field: ["other"] if field == "models" else "unknown"}))
    with pytest.raises(ContractError):
        bind(g)


@pytest.mark.parametrize("name", ["v4", "v5", "v7", "v8"])
def test_every_judge_host_must_equal_measurement_current_host_not_older_host(tmp_path, name):
    g = extended_lineage(tmp_path)
    key = "host_recovery_engine_fingerprint" if name == "v4" else "base_host_engine_fingerprint"
    persist(g, name, lambda value: value.update({key: "d" * 64}))
    with pytest.raises(ContractError, match="host"):
        bind(g)


@pytest.mark.parametrize("name", ["v5", "v7", "v8"])
@pytest.mark.parametrize("pin", ["missing", "invalid", "wrong"])
def test_each_judge_parent_requires_matching_hash(tmp_path, name, pin):
    g = extended_lineage(tmp_path)
    def mutate(value):
        if pin == "missing":
            value.pop("base_manifest_sha256")
        else:
            value["base_manifest_sha256"] = "invalid" if pin == "invalid" else "0" * 64
    persist(g, name, mutate)
    with pytest.raises(ContractError, match="parent manifest hash|base_manifest_sha256"):
        bind(g)


@pytest.mark.parametrize("name", ["v6", "v9"])
def test_optional_bootstrap_parent_hash_is_checked_when_present(tmp_path, name):
    g = extended_lineage(tmp_path)
    persist(g, name, lambda value: value.update(base_manifest_sha256="0" * 64))
    with pytest.raises(ContractError, match="base_manifest_sha256"):
        bind(g)


@pytest.mark.parametrize("name", ["v5", "v6", "v8", "v9"])
def test_parent_must_be_absolute_and_resolved_cycles_are_rejected(tmp_path, name):
    g = extended_lineage(tmp_path)
    persist(g, name, lambda value: value.update(base_run="relative-parent"))
    with pytest.raises(ContractError, match="absolute"):
        bind(g)
    alias = tmp_path / "cycle-alias"
    alias.symlink_to(g.paths["v9"], target_is_directory=True)
    persist(g, name, lambda value: value.update(base_run=str(alias)))
    with pytest.raises(ContractError, match="cycle"):
        bind(g)


@pytest.mark.parametrize("name", ["v6", "v9"])
def test_bootstrap_does_not_accept_alias_fields_or_changed_behavior(tmp_path, name):
    g = extended_lineage(tmp_path)
    for value in ({"generation_engine_fingerprint": ENGINE}, {"behavior_fingerprint": "f" * 64}):
        persist(g, name, lambda row: row.update(value))
        with pytest.raises(ContractError, match="engine/behavior"):
            bind(g)


@pytest.mark.parametrize("change", ["digest", "schema", "foreign-path", "relative-path", "missing",
                                  "extra", "duplicate", "base-schema", "without-claims"])
def test_v7_embedded_claims_are_checked_without_reading_extra_manifests(tmp_path, monkeypatch, change):
    g = extended_lineage(tmp_path)
    foreign = tmp_path / "do-not-read/experiment-manifest.json"
    def mutate(value):
        claims = value["bootstrap_lineage"]
        if change == "digest":
            claims[0]["sha256"] = "0" * 64
        elif change == "schema":
            claims[0]["schema_version"] = repair.MEASUREMENT_BASE_SCHEMA
        elif change == "foreign-path":
            claims[0]["manifest"] = str(foreign)
        elif change == "relative-path":
            claims[0]["manifest"] = "relative-manifest.json"
        elif change == "missing":
            claims.pop()
        elif change == "extra":
            claims[0]["extra"] = "unrecognized"
        elif change == "duplicate":
            claims[1] = claims[0]
        elif change == "base-schema":
            value["bootstrap_base_schema"] = "unknown/v1"
        else:
            value.pop("bootstrap_lineage")
    persist(g, "v7", mutate)
    original = Path.read_bytes
    def read(path):
        assert path != foreign, "embedded claim caused an unbounded external read"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(ContractError, match="embedded|absolute"):
        bind(g)


@pytest.mark.parametrize("depth,accepted", [(16, True), (17, False)])
def test_lineage_has_a_hard_sixteen_overlay_limit(tmp_path, depth, accepted):
    g = extended_lineage(tmp_path)
    previous = g.paths["v9"]
    base = json.loads((previous / "experiment-manifest.json").read_text())
    for index in range(6, depth):
        directory = tmp_path / f"extra-{index}"
        extra = {key: copy.deepcopy(base[key]) for key in (
            "task_ids", "tasks_fingerprint", "models", "expected_pairs", "source_run", "source_manifest_sha256")}
        extra.update(schema_version=repair.SCHEMA, base_run=str(previous),
                     base_manifest_sha256=file_hash(previous / "experiment-manifest.json"),
                     generation_engine_fingerprint=ENGINE, base_host_engine_fingerprint="c" * 64)
        write(directory / "experiment-manifest.json", extra)
        previous, base = directory, extra
    if accepted:
        assert len(repair.base_engine_binding(previous, base, g.tasks, g.models)["recovery_lineage"]) == 15
    else:
        with pytest.raises(ContractError, match="sixteen"):
            repair.base_engine_binding(previous, base, g.tasks, g.models)


def current_cached_fixture(tmp_path, monkeypatch):
    (tmp_path / "answer").mkdir()
    f = cached_full402_fixture(tmp_path, monkeypatch)
    g = extended_lineage(tmp_path / "lineage", f.graph)
    # Only the newest v9 rows have these markers. An ancestor fallback loses them.
    for index, name in enumerate(("Bigtable", "Hyrise", "Rippled5860", "NumeRe149")):
        f.rows[(f"other-{index:03d}", "solver")]["generation"]["newest_completed_binding"] = name
    for key, name in (("generation", "generation_results"), ("correctness", "correctness"), ("judgment", "judgments")):
        repair.write_jsonl(g.paths["v9"] / (name + ".jsonl"), [row[key] for row in f.rows.values()])
    argv = list(sys.argv)
    argv[argv.index("--base-run") + 1] = str(g.paths["v9"])
    monkeypatch.setattr(sys, "argv", argv)
    return f, g


def test_v9_parent_uses_newest_full402_rows_and_cached_member_only(tmp_path, monkeypatch):
    f, g = current_cached_fixture(tmp_path, monkeypatch)
    before = copy.deepcopy(f.rows)
    assert repair.main() == 0
    after = repair.read_base_rows(f.output, g.tasks, ["solver"])
    assert len(after) == 402
    for pair, row in before.items():
        assert after[pair]["generation"] == row["generation"]
        assert after[pair]["correctness"] == row["correctness"]
        if pair[0] != "task":
            assert after[pair]["judgment"] == row["judgment"]
    assert after[("task", "solver")]["judgment"]["panel"][0] == before[("task", "solver")]["judgment"]["panel"][0]
    assert after[("task", "solver")]["judgment"]["panel"][0]["passed"] is False
    manifest = json.loads((f.output / "experiment-manifest.json").read_text())
    assert len(manifest["bootstrap_lineage"]) == 5
    assert Path(manifest["base_run"]) == g.paths["v9"]


@pytest.mark.parametrize("name", ("source", *ORDER))
@pytest.mark.parametrize("read_number", [1, 2])
def test_all_read_manifest_hashes_are_rechecked_before_publication(tmp_path, monkeypatch, name, read_number):
    f, g = current_cached_fixture(tmp_path, monkeypatch)
    original, reads = repair.read_base_rows, []
    def changed(*args):
        values = original(*args)
        reads.append(True)
        if len(reads) == read_number:
            path = g.paths[name] / "experiment-manifest.json"
            path.write_bytes(path.read_bytes() + b" ")
        return values
    monkeypatch.setattr(repair, "read_base_rows", changed)
    with pytest.raises(ContractError, match="bindings changed"):
        repair.main()
    assert not (f.output / "report.json").exists()


@pytest.mark.parametrize("base", ["v8", "v9"])
def test_source_hash_is_not_rebound_if_it_changes_after_lineage_validation(tmp_path, monkeypatch, base):
    f, g = current_cached_fixture(tmp_path, monkeypatch)
    if base == "v8":
        for key, name in (("generation", "generation_results"), ("correctness", "correctness"), ("judgment", "judgments")):
            repair.write_jsonl(g.paths["v8"] / (name + ".jsonl"), [row[key] for row in f.rows.values()])
    argv = list(sys.argv)
    argv[argv.index("--base-run") + 1] = str(g.paths[base])
    monkeypatch.setattr(sys, "argv", argv)
    original = repair.base_engine_binding
    def changed(*args):
        result = original(*args)
        path = g.paths["source"] / "experiment-manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
        return result
    monkeypatch.setattr(repair, "base_engine_binding", changed)
    with pytest.raises(ContractError, match="bindings changed"):
        repair.main()
    assert not (f.output / "report.json").exists()
