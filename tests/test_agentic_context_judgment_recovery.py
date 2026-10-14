"""Offline full-source and durable once-only controls; never use real pair paths."""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import recover_agentic_context_judgments as context
import recover_agentic_judgments as retained
from reviewbench_maintainability.agentic.contracts import PROTOCOL, canonical_hash, file_hash
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB


MODELS = ["gpt-4.1", "gpt-5.6-sol", "gemini-3.6-flash"]
IDENTITIES = {"gpt-4.1": ["gpt-4.1-2025-04-14"], "gpt-5.6-sol": ["gpt-5.6-sol-2026-07-09"],
              "gemini-3.6-flash": ["gemini-3.6-flash"]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline context fixture attempted real network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def vote(model, passed=True):
    verdict = {"addressed": "yes" if passed else "no", "quality_1_to_5": 5 if passed else 1,
               "introduces_new_debt": False, "focused": True, "reason": "retain this exact negative too"}
    raw = {"model": IDENTITIES[model][0], "choices": [
        {"finish_reason": "stop", "message": {"content": json.dumps(verdict)}}]}
    return {"judge_model": model, "http_status": 200, "raw_response": raw,
            "raw_response_text": json.dumps(raw), "passed": passed, "verdict": verdict}


def write_base(directory, tasks, rows):
    columns = [[row[key] for row in rows.values() if key in row] for key in ("generation", "correctness", "judgment")]
    table, report = retained.make_report(tasks, ["gpt-5.4-responses"], *columns)
    for name, values in zip(("generation_results", "correctness", "judgments", "results"), (*columns, table)):
        retained.write_jsonl(directory / (name + ".jsonl"), values)
    write(directory / "report.json", report)


def owner_request(f, root, selected=None):
    selected = selected or f.selected
    manifest, _, _ = context.controller_manifest(f.bundle, f.config_file, f.base, root, False)
    context.check_owner_manifest(root, manifest)
    pair = selected["id"], selected["model"]
    return {"input_bundle": str(f.input_dir), "config": str(f.config_file),
            "task": f.tasks[pair[0]], "row": f.rows[pair], "selection": selected,
            "recipe": {}, "output": str(context.output_directory(root, selected))}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    repo_cache = tmp_path / "repos"
    repository = repo_cache / "fixture__complete-project"
    repository.mkdir(parents=True)
    (repository / "large.py").write_text("VALUE = 1\n" + "# complete original production line\n" * 5200)
    (repository / "unrelated_helper.py").write_text("KEEP_THIS_COMPLETE_PRODUCTION_FILE = True\n")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                    "commit", "-qm", "original full fixture project"], cwd=repository, check=True)
    start = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    original_config = {"endpoint": "http://127.0.0.1:4000/v1/chat/completions", "models": MODELS,
                       "expected_response_models": IDENTITIES, "max_context_chars": 300000,
                       "max_output_tokens": 4096, "timeout_seconds": 180}
    configs = {"original": original_config, "admission": {**original_config, "max_context_chars": 1600000}}
    tasks, rows, selections, cases = {}, {}, [], []
    after = tmp_path / "expected-after"
    after.mkdir()
    (after / "large.py").write_text((repository / "large.py").read_text().replace("VALUE = 1", "VALUE = 2", 1))
    for i in range(13):
        ident = f"synthetic-context-{i}"
        task = {"id": ident, "repo": "fixture/complete-project", "start_sha": start,
                "locality": {"path": "large.py", "line": 1}, "task_instruction": "Keep the whole candidate"}
        episode = tmp_path / "original-pairs" / ident / "episode"
        episode.mkdir(parents=True)
        patch, trace = episode / "candidate.patch", episode / "trajectory.json"
        patch.write_text("--- a/large.py\n+++ b/large.py\n@@ -1,4 +1,4 @@\n-VALUE = 1\n+VALUE = 2\n"
                         + " # complete original production line\n" * 3)
        trace.write_text('{"original":"completed synthetic answer"}\n')
        generation = {"id": ident, "model": "gpt-5.4-responses", "protocol": PROTOCOL, "status": "generated",
                      "apply_ok": True, "engine_fingerprint": context.ENGINE, "task_fingerprint": canonical_hash(task),
                      "runtime_fingerprint": "b" * 64, "patch_file": str(patch), "patch_sha256": file_hash(patch),
                      "trace_file": str(trace), "trace_sha256": file_hash(trace)}
        judgment = {key: generation[key] for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256")}
        judgment.update(engine_fingerprint=context.ENGINE, judge_fingerprint=retained.CONTEXT_OLD_FINGERPRINT,
                        status="infra_error", panel=[], maintainability_pass=None, error=context.ORIGINAL_ERROR)
        correctness = {key: generation[key] for key in ("id", "model", "protocol", "task_fingerprint",
                                                       "patch_sha256", "runtime_fingerprint")}
        correctness.update(status="pass", correctness_ok=True, scope_ok=True, oracle_replayed=True)
        row = {"id": ident, "model": generation["model"], "generation": generation,
               "correctness": correctness, "judgment": judgment}
        prompt = context.judge_prompt(task, repository, after, patch.read_text(), ["large.py"], 1600000)
        selected = {"id": ident, "model": generation["model"], **{
            key + "_sha256": canonical_hash(row[key]) for key in ("generation", "correctness", "judgment")},
            "judge_engine": context.ENGINE, "judge_models": MODELS, "max_first_attempts_per_member": 1,
            "old_max_context_chars": 300000, "new_max_context_chars": 1600000,
            "old_judge_fingerprint": retained.CONTEXT_OLD_FINGERPRINT,
            "new_judge_fingerprint": retained.CONTEXT_NEW_FINGERPRINT,
            "full_evidence_characters": len(prompt), "full_evidence_sha256": canonical_hash(prompt)}
        tasks[ident], rows[(ident, generation["model"])] = task, row
        selections.append(selected)
        cases.append({"row": row, "selection": selected})
    for i in range(389):
        ident = f"other-{i}"
        tasks[ident] = {**tasks[selections[0]["id"]], "id": ident}
    tasks_file, scope_file, config_file = tmp_path / "tasks.jsonl", tmp_path / "scope.ids", tmp_path / "config.json"
    retained.write_jsonl(tasks_file, list(tasks.values()))
    scope_file.write_text("".join(ident + "\n" for ident in tasks))
    config = {"judge": original_config, "repo_cache": str(repo_cache), "asset_root": str(tmp_path),
              "tasks": str(tasks_file), "scope_ids": str(scope_file)}
    write(config_file, config)
    source, anchor, base = tmp_path / "source", tmp_path / "measurement", tmp_path / "v10"
    scope = {"task_ids": list(tasks), "tasks_fingerprint": canonical_hash(tasks), "models": ["gpt-5.4-responses"],
             "expected_pairs": 402}
    write(source / "experiment-manifest.json", {**scope, "schema_version": "reviewbench-progressive-experiment/v1",
                                               "protocol": PROTOCOL, "engine_fingerprint": context.ENGINE})
    write(source / "experiment-config.json", config)
    source_scope = {**scope, "source_run": str(source),
                    "source_manifest_sha256": file_hash(source / "experiment-manifest.json"),
                    "generation_engine_fingerprint": context.ENGINE}
    write(anchor / "experiment-manifest.json", {
        **source_scope, "schema_version": retained.MEASUREMENT_BASE_SCHEMA, "host_recovery_engine_fingerprint": "c" * 64})
    write(base / "experiment-manifest.json", {
        **source_scope, "schema_version": retained.SCHEMA, "base_run": str(anchor),
        "base_manifest_sha256": file_hash(anchor / "experiment-manifest.json"), "base_host_engine_fingerprint": "c" * 64})
    write_base(base, tasks, rows)
    base_digest = file_hash(base / "experiment-manifest.json")
    monkeypatch.setattr(retained, "CONTEXT_BASE_MANIFEST_SHA256", base_digest)
    input_dir = tmp_path / "synthetic-inputs"
    for name, value in (("selection.json", selections), ("original-case-bindings.json", cases),
                        ("judge-configs.json", configs)):
        write(input_dir / name, value)
    pins = {name: file_hash(input_dir / name) for name in retained.CONTEXT_INPUT_HASHES}
    write(input_dir / "paths.json", {"selection_sha256": pins["selection.json"], "source_run": str(source),
                                    "base_run": str(base), "base_manifest_sha256": base_digest,
                                    "engine_src": os.environ.get("REVIEWBENCH_ENGINE_SRC", str(tmp_path))})
    write(input_dir / "original-bindings-proof.json", {"synthetic_fixture": True})
    monkeypatch.setattr(retained, "CONTEXT_INPUT_HASHES", pins)
    monkeypatch.setattr(context, "ADDITIONAL_INPUT_HASHES",
                        {name: file_hash(input_dir / name) for name in context.ADDITIONAL_INPUT_HASHES})
    bundle = context.load_inputs(input_dir)
    selected, task = selections[0], tasks[selections[0]["id"]]
    row = rows[(selected["id"], selected["model"])]
    monkeypatch.setattr(retained, "load_config", lambda _: copy.deepcopy(config))
    monkeypatch.setattr(retained, "engine_identity", lambda: {"fingerprint": context.ENGINE})
    recipe = SimpleNamespace(fingerprint="b" * 64, submodules=[])
    monkeypatch.setattr(context, "RuntimeRecipe", SimpleNamespace(from_dict=lambda _: recipe))
    events = []
    @contextmanager
    def acquire(request, **kwargs):
        assert request.cpus == 1 and request.memory_bytes == 2 * GiB
        assert request.scratch_bytes >= GiB and kwargs["uses_containers"] is False
        events.append("admitted")
        token = CURRENT_JOB.set({"scratch_root": str(tmp_path), "lease": "synthetic-lease",
                                 "task": task, "source_catalog": None})
        try:
            yield {"lease": "synthetic-lease"}
        finally:
            CURRENT_JOB.reset(token)
            events.append("released")
    services = SimpleNamespace(config=config, cache=None, scratch=tmp_path, sources=None,
                               source_bytes=lambda *_: 500000, gate=SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(retained, "ExecutionServices", lambda _: services)
    f = SimpleNamespace(**locals())
    f.output_root = tmp_path / "owner"
    f.request = owner_request(f, f.output_root)
    return f


def test_default_cap_blocks_same_complete_prompt_but_new_cap_changes_no_evidence(fixture, monkeypatch):
    f = fixture
    with pytest.raises(retained.ContractError, match="judge_context_limit"):
        context.judge_prompt(f.task, f.repository, f.after, Path(f.row["generation"]["patch_file"]).read_text(),
                             ["large.py"], 300000)
    original_extract = context.extract_start
    def complete_extract(repo, sha, output):
        result = original_extract(repo, sha, output)
        assert (output / "unrelated_helper.py").read_text() == "KEEP_THIS_COMPLETE_PRODUCTION_FILE = True\n"
        return result
    monkeypatch.setattr(context, "extract_start", complete_extract)
    calls = []
    def invoke(model, prompt, config):
        assert f.events == ["admitted"] and canonical_hash(prompt) == f.selected["full_evidence_sha256"]
        assert len(prompt) == f.selected["full_evidence_characters"]
        assert config == f.configs["admission"]
        calls.append(model)
        return vote(model, model != "gpt-5.6-sol")
    before = copy.deepcopy(f.row)
    result = context.recover_one(f.request, invoke=invoke)
    assert calls == MODELS and f.events == ["admitted", "released"]
    assert result["row"]["generation"] == before["generation"]
    assert result["row"]["correctness"] == before["correctness"]
    assert result["original_row"] == before and f.row == before
    assert result["row"]["judgment"]["judge_fingerprint"] != before["judgment"]["judge_fingerprint"]
    assert result["row"]["judgment"]["maintainability_pass"] is False
    assert json.loads((Path(f.request["output"]) / "original-judgment.json").read_text()) == before["judgment"]


@pytest.mark.parametrize("field", ["generation_sha256", "judgment_sha256", "correctness_sha256",
                                  "full_evidence_sha256", "full_evidence_characters", "judge_models",
                                  "new_max_context_chars", "max_first_attempts_per_member"])
def test_changed_selection_is_rejected_without_claims(fixture, field):
    f = fixture
    request = copy.deepcopy(f.request)
    request["selection"][field] = ["gpt-4.1"] if field == "judge_models" else 2 if "chars" in field else "changed"
    with pytest.raises(retained.ContractError):
        context.recover_one(request, invoke=lambda *_: pytest.fail("changed pin invoked"))
    assert not Path(f.row["generation"]["trace_file"]).parent.joinpath(".judge-member-recovery-claims-v1").exists()


@pytest.mark.parametrize("change", ["accepted", "evidence", "error", "generation", "correctness", "task", "recipe"])
def test_original_objects_and_empty_panel_boundary_are_strict(fixture, monkeypatch, change):
    f = fixture
    request = copy.deepcopy(f.request)
    if change == "accepted":
        request["row"]["judgment"]["panel"] = [vote(MODELS[0], False)]
    elif change == "evidence":
        request["row"]["judgment"]["evidence_complete"] = False
    elif change == "error":
        request["row"]["judgment"]["error"] = "ContractError: another error"
    elif change in ("generation", "correctness"):
        request["row"][change]["changed"] = True
    elif change == "task":
        request["task"]["task_instruction"] += " modified"
    else:
        monkeypatch.setattr(context, "RuntimeRecipe", SimpleNamespace(
            from_dict=lambda _: SimpleNamespace(fingerprint="f" * 64)))
    with pytest.raises(retained.ContractError):
        context.recover_one(request, invoke=lambda *_: pytest.fail("changed source/config invoked"))


@pytest.mark.parametrize("field,value", [
    ("max_output_tokens", 8192), ("timeout_seconds", 181), ("endpoint", "http://different.invalid"),
    ("max_context_chars", 1700000), ("expected_response_models", {"gpt-5.6-sol": ["gpt-5.6-sol"]}),
])
def test_other_config_changes_are_not_cap_admission(fixture, field, value):
    bad = copy.deepcopy(fixture.bundle["override"])
    bad["admitted_judge_config"][field] = value
    with pytest.raises(retained.ContractError):
        retained.validate_context_admission_metadata(bad)


def test_changed_regenerated_prompt_rejected_before_any_invocation(fixture, monkeypatch):
    original = context.judge_prompt
    monkeypatch.setattr(context, "judge_prompt", lambda *a, **kw: original(*a, **kw) + " ")
    with pytest.raises(retained.ContractError, match="SHA/character"):
        context.recover_one(fixture.request, invoke=lambda *_: pytest.fail("changed evidence invoked"))
    episode = Path(fixture.row["generation"]["trace_file"]).parent
    assert not (episode / ".judge-member-recovery-claims-v1").exists()


def test_cached_resume_has_no_source_job_build_or_model_call(fixture, monkeypatch):
    f = fixture
    first = context.recover_one(f.request, invoke=lambda model, *_: vote(model, False))
    monkeypatch.setattr(retained, "ExecutionServices", lambda *_: pytest.fail("cached result constructed services"))
    monkeypatch.setattr(context, "extract_start", lambda *_: pytest.fail("cached result extracted source"))
    assert context.recover_one(f.request, invoke=lambda *_: pytest.fail("cached result rerolled")) == first
    assert [m["passed"] for m in first["row"]["judgment"]["panel"]] == [False, False, False]


def test_full_source_extraction_canonicalizes_a_symlinked_scratch_prefix(fixture, monkeypatch):
    f = fixture
    physical = f.tmp_path / "physical-scratch"
    physical.mkdir()
    alias = f.tmp_path / "scratch-alias"
    alias.symlink_to(physical, target_is_directory=True)
    @contextmanager
    def aliased_directory(**kwargs):
        with tempfile.TemporaryDirectory(dir=alias) as directory:
            yield directory
    monkeypatch.setattr(context, "job_directory", aliased_directory)
    result = context.recover_one(f.request, invoke=lambda model, *_: vote(model, False))
    assert result["row"]["judgment"]["status"] == "completed"


def test_failed_new_member_is_retained_not_repeated(fixture):
    f = fixture
    calls = []
    def failed(model, *_):
        calls.append(model)
        return {"judge_model": model, "http_status": 503, "error": "HTTPError: retained first failure"}
    first = context.recover_one(f.request, invoke=failed)
    assert calls == MODELS and first["row"]["judgment"]["status"] == "infra_error"
    assert context.recover_one(f.request, invoke=lambda *_: pytest.fail("failed member retried")) == first
    # Generic legacy503 policy is unchanged, but this marked new outcome must
    # not use it to obtain another first attempt.
    assert retained.retryable_member(first["row"]["judgment"]["panel"][0]) is True
    with pytest.raises(retained.ContractError, match="context first-attempt outcomes are consumed"):
        retained.validate_partial(first["row"]["judgment"], f.row["generation"],
                                  f.configs["admission"], context.ENGINE)


def test_cap_metadata_does_not_change_source_claim_key(fixture):
    f = fixture
    first = context.source_claim(f.bundle, f.selected, f.row, Path(f.request["output"]), MODELS[0])
    first.invoke_once(MODELS[0], f.prompt, f.configs["admission"], lambda model, *_: vote(model))
    changed = copy.deepcopy(f.bundle)
    changed["override"]["profile_version"] = "another-controller-version"
    changed["override"]["admitted_judge_config"]["max_context_chars"] += 1
    second = context.source_claim(changed, f.selected, f.row, f.tmp_path / "different", MODELS[0])
    assert second.root == first.root
    with pytest.raises(retained.ContractError, match="owner_output|profile binding"):
        second.invoke_once(MODELS[0], f.prompt, f.configs["admission"], lambda *_: pytest.fail("second allowance"))


def child(request, counter, barrier=None, crash=False):
    if barrier is not None:
        barrier.wait(timeout=30)
    def fake(model, *_):
        with retained.file_lock(Path(counter).with_suffix(".lock")):
            with Path(counter).open("a") as stream:
                stream.write(model + "\n");stream.flush();os.fsync(stream.fileno())
        if crash:
            os._exit(23)
        return vote(model, False)
    try:
        result = context.recover_one(request, invoke=fake)
        summary = {"status": result["row"]["judgment"]["status"]}
    except retained.ContractError as exc:
        summary = {"error": str(exc)}
    write(Path(request["output"]) / "fixture-child.json", summary)


def processes(requests, counter, crash=False):
    mp = multiprocessing.get_context("fork")
    barrier = mp.Barrier(len(requests)) if len(requests) > 1 else None
    children = [mp.Process(target=child, args=(r, str(counter), barrier, crash)) for r in requests]
    for p in children:p.start()
    for p in children:
        p.join(timeout=30)
        if p.is_alive():
            p.terminate();p.join()
            pytest.fail("offline claim fixture hung")
        assert p.exitcode == (23 if crash else 0)


def test_cross_output_concurrent_cases_issue_at_most_one_invoke_per_member(fixture):
    f = fixture
    other = owner_request(f, f.tmp_path / "other")
    counter = f.tmp_path / "invocations"
    processes([f.request, other], counter)
    assert counter.read_text().splitlines() == MODELS
    processes([f.request, other], counter)
    assert counter.read_text().splitlines() == MODELS


def test_real_interrupted_start_is_consumed_then_only_unstarted_members_run(fixture):
    f = fixture
    counter = f.tmp_path / "invocations"
    processes([f.request], counter, crash=True)
    other = owner_request(f, f.tmp_path / "other")
    processes([f.request, other], counter)
    assert counter.read_text().splitlines() == MODELS
    record = json.loads((Path(f.request["output"]) / "result.json").read_text())
    assert "no automatic repeat" in record["row"]["judgment"]["panel"][0]["error"]
    assert record["row"]["judgment"]["panel"][1]["passed"] is False


def test_frozen_invoke_http_payload_is_identical_for_old_and_new_caps(fixture, monkeypatch):
    f = fixture
    requests = []
    class Response:
        status, headers = 200, {}
        def __enter__(self):return self
        def __exit__(self, *args):return False
        def read(self):return vote(MODELS[0], False)["raw_response_text"].encode()
    def urlopen(request, timeout):
        requests.append((request.full_url, request.data, timeout))
        return Response()
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    for cfg in (f.configs["original"], f.configs["admission"]):
        assert context.invoke_judge(MODELS[0], f.prompt, cfg)["passed"] is False
    assert requests[0] == requests[1]
    payload = json.loads(requests[0][1])
    assert payload["max_tokens"] == 4096 and payload["messages"][1]["content"] == f.prompt


def test_full402_overlay_changes_only_selected_judgment_and_preserves_other389(fixture):
    f = fixture
    tasks, rows = copy.deepcopy(f.tasks), copy.deepcopy(f.rows)
    output = f.tmp_path / "full402"
    request = owner_request(f, output)
    record = context.recover_one(request, invoke=lambda model, *_: vote(model, False))
    before = copy.deepcopy(rows)
    manifest = json.loads((output / "experiment-manifest.json").read_text())
    report = context.cached_iteration(f.bundle, manifest, tasks, output)
    table = [json.loads(x) for x in (output / "results.jsonl").read_text().splitlines()]
    assert len(table) == report["expected_pairs"] == 402 and report["schema_version"] == context.SCHEMA
    after = retained.read_base_rows(output, tasks, ["gpt-5.4-responses"])
    for pair, row in before.items():
        if "generation" in row:
            assert after[pair]["generation"] == row["generation"]
            assert after[pair]["correctness"] == row["correctness"]
            if pair[0] != f.selected["id"]:assert after[pair]["judgment"] == row["judgment"]
    assert rows == before


def manifest_for(f):
    return json.loads((f.output_root / "experiment-manifest.json").read_text())


def forbid_execution(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("cache-only publication attempted an intent, job, child, model, recipe or build")
    monkeypatch.setattr(retained.SourceMemberClaim, "invoke_once", forbidden)
    monkeypatch.setattr(retained, "ExecutionServices", forbidden)
    monkeypatch.setattr(retained, "source_judge_job", forbidden)
    monkeypatch.setattr(retained, "load_recipes", forbidden)
    monkeypatch.setattr(context, "extract_start", forbidden)
    monkeypatch.setattr(context, "invoke_judge", forbidden)
    monkeypatch.setattr(context.subprocess, "Popen", forbidden)
    return forbidden


def source_journals(f):
    result = {}
    # Only the thirteen isolated fixture episodes, not any retained real source.
    for selected in f.selections:
        episode = Path(f.rows[(selected["id"], selected["model"])]["generation"]["trace_file"]).parent
        for path in episode.glob(".judge-member-recovery-claims-v1/*/*.json"):
            result[str(path)] = path.read_bytes()
    return result


def add_unselected(f, ident="other-0", *, note="new unrelated completed generation"):
    pair = ident, "gpt-5.4-responses"
    generation = {**f.row["generation"], "id": ident, "task_fingerprint": canonical_hash(f.tasks[ident]),
                  "fixture_unrelated_progress": note}
    f.rows[pair] = {"id": ident, "model": pair[1], "generation": generation}
    write_base(f.base, f.tasks, f.rows)
    return pair


def test_cached_dynamic_publication_propagates_unrelated_additions_and_changes_without_calls(fixture, monkeypatch):
    f = fixture
    negative = context.recover_one(f.request, invoke=lambda model, *_: vote(model, False))
    before_journals = source_journals(f)
    manifest = manifest_for(f)
    immutable_before = (f.output_root / "experiment-manifest.json").read_bytes()
    forbidden = forbid_execution(monkeypatch)
    monkeypatch.setattr(context, "admit_once", forbidden)
    first = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    first_key = first["publication_key"]
    for note in ("first new main episode", "later unrelated raw metadata"):
        pair = add_unselected(f, note=note)
        report = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
        output = retained.read_base_rows(f.output_root, f.tasks, ["gpt-5.4-responses"])
        assert output[pair] == f.rows[pair]
        assert output[(f.selected["id"], f.selected["model"])] == negative["row"]
        assert report["additional_judge_calls"] == 0
        assert report["activity"] == context.zero_activity("cached-only")
        assert report["historical_first_attempts_consumed"] == 3
        assert report["publication_key"] != first_key
        for other in f.selections[1:]:
            key = other["id"], other["model"]
            assert output[key] == f.rows[key]
    assert source_journals(f) == before_journals
    assert (f.output_root / "experiment-manifest.json").read_bytes() == immutable_before
    assert not set(str(f.base / name) for name in context.FILES).intersection(manifest["bound_inputs"])


def test_watch_is_cached_only_from_first_iteration_and_same_owner_reopen(fixture, monkeypatch):
    f = fixture
    forbidden = forbid_execution(monkeypatch)
    monkeypatch.setattr(context, "admit_once", forbidden)
    args = ["context", "--input-bundle", str(f.input_dir), "--config", str(f.config_file),
            "--base-run", str(f.base), "--out-dir", str(f.output_root), "--watch", "--poll-seconds", "1"]
    ticks = []
    def sleep(_):
        ticks.append(True)
        if len(ticks) == 1:
            add_unselected(f)
        else:
            raise KeyboardInterrupt("stop only this offline watcher fixture")
    monkeypatch.setattr(context.time, "sleep", sleep)
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(KeyboardInterrupt):
        context.main()
    assert len(ticks) == 2 and source_journals(f) == {}
    status = json.loads((f.output_root / "CURRENT.json").read_text())
    assert status["activity"] == context.zero_activity("cached-only")
    assert len(status["cache_pending"]) == 13
    epochs = set((f.output_root / "publications").iterdir())
    monkeypatch.setattr(sys, "argv", args[:-3] + ["--cached-only"])
    assert context.main() == 2  # Full402 is incomplete, not a fixture failure.
    assert set((f.output_root / "publications").iterdir()) == epochs
    assert source_journals(f) == {}


def test_cached_only_does_not_create_missing_claims_or_first_intents(fixture, monkeypatch):
    f = fixture
    claim = context.source_claim(f.bundle, f.selected, f.row, f.request["output"], MODELS[0])
    claim.invoke_once(MODELS[0], f.prompt, f.configs["admission"], lambda model, *_: vote(model, False))
    old = source_journals(f)
    forbid_execution(monkeypatch)
    for _ in range(3):
        report = context.cached_iteration(f.bundle, manifest_for(f), f.tasks, f.output_root)
        assert report["historical_first_attempts_consumed"] == 1
        assert report["additional_judge_calls"] == 0 and report["judgment_recovery_records"] == 0
    assert source_journals(f) == old
    assert not (Path(f.request["output"]) / "result.json").exists()


def test_cache_reconstructs_all_consumed_unknown_503_negative_without_changing_journals(fixture, monkeypatch):
    f = fixture
    counter = f.tmp_path / "invocations"
    processes([f.request], counter, crash=True)
    for model in MODELS[1:]:
        claim = context.source_claim(f.bundle, f.selected, f.row, f.request["output"], model)
        claim.invoke_once(model, f.prompt, f.configs["admission"],
                          lambda m, *_: vote(m, False) if m == MODELS[1]
                          else {"judge_model": m, "http_status": 503, "error": "HTTPError: frozen cached failure"})
    old = source_journals(f)
    forbid_execution(monkeypatch)
    for _ in range(2):
        report = context.cached_iteration(f.bundle, manifest_for(f), f.tasks, f.output_root)
        assert report["additional_judge_calls"] == 0 and report["historical_first_attempts_consumed"] == 3
    record = json.loads((Path(f.request["output"]) / "result.json").read_text())
    members = record["row"]["judgment"]["panel"]
    assert members[0]["error"] == context.UNKNOWN_OUTCOME
    assert members[1] == vote(MODELS[1], False) and members[2]["http_status"] == 503
    assert record["row"]["judgment"]["status"] == "infra_error"
    assert record["row"]["generation"] == f.row["generation"] and record["row"]["correctness"] == f.row["correctness"]
    assert source_journals(f) == old and counter.read_text().splitlines() == MODELS[:1]


def test_cached_lock_is_nonblocking_and_never_clears_inflight_journal(fixture, monkeypatch):
    f = fixture
    claim = context.source_claim(f.bundle, f.selected, f.row, f.request["output"], MODELS[0])
    claim.invoke_once(MODELS[0], f.prompt, f.configs["admission"], lambda model, *_: vote(model, False))
    old = source_journals(f)
    forbid_execution(monkeypatch)
    with (claim.root / "claim.lock").open("rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        start = time.monotonic()
        report = context.cached_iteration(f.bundle, manifest_for(f), f.tasks, f.output_root)
        assert time.monotonic() - start < 3
        assert report["cache_pending"][f.selected["id"] + "::" + f.selected["model"]]["state"] == "in-flight"
    assert source_journals(f) == old


@pytest.mark.parametrize("field", ["generation", "correctness", "judgment"])
def test_selected_object_change_fails_closed_and_keeps_last_complete_epoch(fixture, monkeypatch, field):
    f = fixture
    manifest = manifest_for(f)
    forbid_execution(monkeypatch)
    context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    previous = os.readlink(f.output_root / "current")
    pair = f.selected["id"], f.selected["model"]
    f.rows[pair][field]["fixture_mutation"] = True
    write_base(f.base, f.tasks, f.rows)
    with pytest.raises(retained.ContractError, match="object changed"):
        context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert os.readlink(f.output_root / "current") == previous
    assert not source_journals(f)


@pytest.mark.parametrize("path", ["source_manifest", "source_config", "base_manifest", "original_config",
                                 "scope_file", "tasks_file", "input"])
def test_immutable_source_lineage_and_input_changes_fail_closed(fixture, monkeypatch, path):
    f = fixture
    manifest = manifest_for(f)
    forbid_execution(monkeypatch)
    context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    previous = os.readlink(f.output_root / "current")
    target = {"source_manifest": f.source / "experiment-manifest.json",
              "source_config": f.source / "experiment-config.json",
              "base_manifest": f.base / "experiment-manifest.json", "original_config": f.config_file,
              "scope_file": f.scope_file, "tasks_file": f.tasks_file,
              "input": f.input_dir / "original-case-bindings.json"}[path]
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(retained.ContractError):
        context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert os.readlink(f.output_root / "current") == previous and source_journals(f) == {}


@pytest.mark.parametrize("field,value", [
    ("generation_engine_fingerprint", "0" * 64), ("judge_fingerprint", "0" * 64),
    ("validation_only", True), ("controller_sha256", "0" * 64), ("source_manifest_sha256", "0" * 64),
])
def test_existing_owner_manifest_cannot_be_rebound(fixture, monkeypatch, field, value):
    f = fixture
    manifest = manifest_for(f)
    forbid_execution(monkeypatch)
    changed = copy.deepcopy(manifest)
    changed[field] = value
    write(f.output_root / "experiment-manifest.json", changed)
    with pytest.raises(retained.ContractError):
        context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert not (f.output_root / "current").exists()


def test_cache_in_another_output_cannot_adopt_original_owner(fixture, monkeypatch):
    f = fixture
    context.recover_one(f.request, invoke=lambda model, *_: vote(model, False))
    other = f.tmp_path / "another-output"
    request = owner_request(f, other)
    write(Path(request["output"]) / "result.json", json.loads((Path(f.request["output"]) / "result.json").read_text()))
    old = source_journals(f)
    forbid_execution(monkeypatch)
    with pytest.raises(retained.ContractError, match="owner_output"):
        context.cached_iteration(f.bundle, json.loads((other / "experiment-manifest.json").read_text()), f.tasks, other)
    assert source_journals(f) == old


@pytest.mark.parametrize("field", ["owner", "config", "evidence", "verdict", "response"])
def test_cached_result_requires_original_provenance_and_literal_source_members(fixture, monkeypatch, field):
    f = fixture
    record = context.recover_one(f.request, invoke=lambda model, *_: vote(model, False))
    bad = copy.deepcopy(record)
    if field == "owner":
        bad["owner_output"] += "-different"
    elif field == "config":
        bad["context_admission"]["admitted_judge_config"]["max_output_tokens"] = 8192
    elif field == "evidence":
        bad["row"]["judgment"]["evidence_sha256"] = "0" * 64
    elif field == "verdict":
        bad["row"]["judgment"]["panel"][0] = vote(MODELS[0], True)
    else:
        bad["row"]["judgment"]["panel"][0]["raw_response_text"] += " "
    write(Path(f.request["output"]) / "result.json", bad)
    forbid_execution(monkeypatch)
    with pytest.raises(retained.ContractError):
        context.cached_iteration(f.bundle, manifest_for(f), f.tasks, f.output_root)


def test_admission_rechecks_live_selected_original_before_next_intent(fixture):
    f = fixture
    calls = []
    def invoke(model, *_):
        calls.append(model)
        f.rows[(f.selected["id"], f.selected["model"])]["correctness"]["changed_during_first_call"] = True
        write_base(f.base, f.tasks, f.rows)
        return vote(model, False)
    with pytest.raises(retained.ContractError, match="correctness object changed"):
        context.recover_one(f.request, invoke=invoke)
    assert calls == MODELS[:1]
    started = [path for path in source_journals(f) if path.endswith("/started.json")]
    assert len(started) == 1


def test_admission_does_not_lifetime_pin_unrelated_generation_files(fixture):
    f = fixture
    calls = []
    def invoke(model, *_):
        calls.append(model)
        add_unselected(f, note=model)
        return vote(model, False)
    record = context.recover_one(f.request, invoke=invoke)
    assert calls == MODELS and record["row"]["judgment"]["status"] == "completed"
    report = context.cached_iteration(f.bundle, manifest_for(f), f.tasks, f.output_root)
    assert report["measured_generations"] == 14 and report["additional_judge_calls"] == 0


def test_snapshot_retries_whole_read_when_base_updates_between_passes(fixture, monkeypatch):
    f = fixture
    read = context.read_snapshot_files
    calls = []
    def update_after_first(base):
        raw = read(base)
        calls.append(True)
        if len(calls) == 1:
            add_unselected(f)
        return raw
    monkeypatch.setattr(context, "read_snapshot_files", update_after_first)
    snapshot = context.coherent_snapshot(f.bundle, f.base, f.tasks, ["gpt-5.4-responses"])
    assert len(calls) == 4 and snapshot["rows"] == f.rows
    assert snapshot["receipt"]["file_sha256"] == {name: file_hash(f.base / name) for name in context.FILES}


def test_stable_torn_columns_are_not_published_until_report_and_table_agree(fixture, monkeypatch):
    f = fixture
    manifest = manifest_for(f)
    forbid_execution(monkeypatch)
    context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    previous = os.readlink(f.output_root / "current")
    old = {name: (f.base / name).read_bytes() for name in context.FILES}
    add_unselected(f)
    # Simulate the actual multi-file publisher paused before results/report writes.
    for name in ("results.jsonl", "report.json"):
        (f.base / name).write_bytes(old[name])
    with pytest.raises(context.SnapshotUnavailable):
        context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert os.readlink(f.output_root / "current") == previous
    write_base(f.base, f.tasks, f.rows)
    context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert os.readlink(f.output_root / "current") != previous


def test_repeated_equal_snapshots_do_not_duplicate_epochs(fixture, monkeypatch):
    f = fixture
    forbid_execution(monkeypatch)
    for _ in range(3):
        context.cached_iteration(f.bundle, manifest_for(f), f.tasks, f.output_root)
        report = json.loads((f.base / "report.json").read_text())
        report["fixture_heartbeat"] = time.time_ns()
        write(f.base / "report.json", report)
    assert len(list((f.output_root / "publications").iterdir())) == 1
    assert not source_journals(f)


def test_interrupted_publication_keeps_previous_epoch_and_reopens_without_calls(fixture, monkeypatch):
    f = fixture
    manifest = manifest_for(f)
    forbid_execution(monkeypatch)
    context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    previous = os.readlink(f.output_root / "current")
    add_unselected(f)
    switch = context.switch_publication
    def interrupted(*a):
        raise OSError("fixture interruption immediately before atomic pointer replace")
    monkeypatch.setattr(context, "switch_publication", interrupted)
    with pytest.raises(OSError, match="fixture interruption"):
        context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert os.readlink(f.output_root / "current") == previous
    assert len(retained.read_base_rows(f.output_root, f.tasks, ["gpt-5.4-responses"])) == 13
    monkeypatch.setattr(context, "switch_publication", switch)
    context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert len(retained.read_base_rows(f.output_root, f.tasks, ["gpt-5.4-responses"])) == 14
    assert len(list((f.output_root / "publications").iterdir())) == 2 and source_journals(f) == {}


def test_validate_only_full402_never_constructs_source_claims(fixture, monkeypatch):
    f = fixture
    forbid_execution(monkeypatch)
    monkeypatch.setattr(context, "source_claim", lambda *a: pytest.fail("validation inspected original claim roots"))
    output = f.tmp_path / "validation-not-authority"
    monkeypatch.setattr(sys, "argv", ["context", "--input-bundle", str(f.input_dir), "--config", str(f.config_file),
                                    "--base-run", str(f.base), "--out-dir", str(output), "--validate-only"])
    assert context.main() == 2
    original_table = (f.base / "results.jsonl").read_bytes()
    assert (output / "results.jsonl").read_bytes() == original_table
    assert len(original_table.splitlines()) == 402 and source_journals(f) == {}


def test_exact_thirteen_panels_consume_39_once_then_watch_is_zero_call(fixture, monkeypatch):
    f = fixture
    calls = []
    def invoke(model, *_):
        calls.append(model)
        return vote(model, False)
    for selected in f.selections:
        request = owner_request(f, f.output_root, selected)
        # The source gate fixture is sequential; every case still uses full source.
        f.events.clear()
        context.recover_one(request, invoke=invoke)
    assert calls == MODELS * 13
    before = source_journals(f)
    forbid_execution(monkeypatch)
    for _ in range(2):
        report = context.cached_iteration(f.bundle, manifest_for(f), f.tasks, f.output_root)
        assert report["historical_first_attempts_consumed"] == 39
        assert report["additional_judge_calls"] == 0
        assert report["judgment_recovery_records"] == 13 and report["cache_pending"] == {}
    assert source_journals(f) == before and len(calls) == 39
