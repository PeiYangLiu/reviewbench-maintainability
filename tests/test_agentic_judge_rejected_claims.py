"""Real local processes prove the two explicit profiles are source-once-only."""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import time

import pytest

from test_agentic_judge_rejected_profiles import profiled, vote, configure
from test_agentic_judge_member_recovery import fixture, member
import recover_agentic_judgments as repair
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, component


def callback(f, output, invoke):
    return repair.JournaledJudge(
        f.previous, f.selection["judge_models"], output / "members", invoke=invoke,
        selection=f.selection, generation=f.generation)


def packet(f):
    return {key: getattr(f, key) for key in ("previous", "selection", "generation", "prompt", "config")}


def process_call(data, output, counter, barrier=None, crash=False):
    """No model/socket/subprocess: invoke is a single fsynced local ledger append."""
    output, counter = Path(output), Path(counter)
    output.mkdir(parents=True, exist_ok=True)
    if barrier is not None:
        barrier.wait(timeout=30)

    def fake(model, prompt, config):
        with repair.file_lock(counter.with_suffix(".lock")):
            with counter.open("a") as stream:
                stream.write(json.dumps({"pid": os.getpid(), "output": str(output), "model": model}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        if crash:
            os._exit(23)
        time.sleep(.05)
        return vote(model, False)

    cb = repair.JournaledJudge(
        data["previous"], data["selection"]["judge_models"], output / "members", invoke=fake,
        selection=data["selection"], generation=data["generation"])
    try:
        response = cb("gemini-3.6-flash", data["prompt"], data["config"])
        result = {"status": "returned", "member": response}
    except ContractError as exc:
        result = {"status": "rejected", "error": str(exc)}
    repair.atomic_json(output / ("fixture-process-" + str(os.getpid()) + ".json"),
                       {**result, "pid": os.getpid()})


def start_processes(f, outputs, counter, *, crash=False):
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(len(outputs)) if len(outputs) > 1 else None
    processes = [context.Process(target=process_call, args=(packet(f), str(out), str(counter), barrier, crash))
                 for out in outputs]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
            pytest.fail("local claim fixture child did not finish")
        assert process.exitcode == (23 if crash else 0)
    return processes


def count(counter):
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def test_source_claim_is_not_derived_from_output_base_or_profile_name(profiled):
    f = profiled
    output = f.tmp_path / "one"
    cb = callback(f, output, lambda model, *_: vote(model, True))
    claim = cb.profile_claim(f.config)
    assert not claim.root.exists()  # deriving/checking a location is not an intent
    expected_key = canonical_hash({
        "generation_sha256": canonical_hash(f.generation), "judgment_sha256": canonical_hash(f.previous),
        "original_member_sha256": canonical_hash(f.failed), "judge_model": "gemini-3.6-flash",
        "model": f.generation["model"],
    })
    assert claim.root == Path(f.generation["trace_file"]).parent / ".judge-member-recovery-claims-v1" / expected_key
    cb("gemini-3.6-flash", f.prompt, f.config)
    ownership = json.loads((claim.root / "claim.json").read_text())
    assert ownership["owner_output"] == str(output.resolve())
    assert ownership["key"] == expected_key
    assert ownership["binding"]["rejected_member_profile"] == f.selection["rejected_member_profile"]


def test_two_concurrent_outputs_and_fresh_process_restart_make_only_one_invoke(profiled):
    f = profiled
    outputs, counter = [f.tmp_path / name for name in ("out-a", "out-b")], f.tmp_path / "invocations.jsonl"
    before = copy.deepcopy(f.row)
    processes = start_processes(f, outputs, counter)
    records = [json.loads((out / f"fixture-process-{process.pid}.json").read_text())
               for out, process in zip(outputs, processes)]
    assert sorted(x["status"] for x in records) == ["rejected", "returned"]
    assert count(counter) == 1
    owner = outputs[next(i for i, value in enumerate(records) if value["status"] == "returned")]
    loser = outputs[1 - outputs.index(owner)]
    restarted = start_processes(f, [owner, loser], counter)
    returned = json.loads((owner / f"fixture-process-{restarted[0].pid}.json").read_text())
    assert returned["member"]["passed"] is False
    assert returned["member"] == records[outputs.index(owner)]["member"]
    assert count(counter) == 1 and f.row == before
    assert f.previous["panel"][:2] == before["judgment"]["panel"][:2]


def test_two_concurrent_processes_in_same_output_reuse_one_response(profiled):
    f = profiled
    output, counter = f.tmp_path / "same", f.tmp_path / "invocations.jsonl"
    processes = start_processes(f, [output, output], counter)
    results = [json.loads((output / f"fixture-process-{p.pid}.json").read_text()) for p in processes]
    assert all(x["status"] == "returned" for x in results)
    assert results[0]["member"] == results[1]["member"] and count(counter) == 1


def test_real_crash_after_started_never_reissues_in_same_or_other_output(profiled):
    f = profiled
    owner, other, counter = f.tmp_path / "owner", f.tmp_path / "other", f.tmp_path / "invocations.jsonl"
    start_processes(f, [owner], counter, crash=True)
    claim = callback(f, owner, None).profile_claim(f.config)
    original_start = (claim.root / "started.json").read_bytes()
    assert not (claim.root / "response.json").exists()
    processes = start_processes(f, [owner, other], counter)
    cached = json.loads((owner / f"fixture-process-{processes[0].pid}.json").read_text())
    rejected = json.loads((other / f"fixture-process-{processes[1].pid}.json").read_text())
    assert cached["status"] == "returned" and "no automatic repeat" in cached["member"]["error"]
    assert rejected["status"] == "rejected" and "owner_output" in rejected["error"]
    assert count(counter) == 1 and (claim.root / "started.json").read_bytes() == original_start
    assert not (claim.root / "response.json").exists()


def test_crash_between_source_and_local_started_is_already_consumed(profiled, monkeypatch):
    f = profiled
    output = f.tmp_path / "owner"
    cb = callback(f, output, lambda *_: pytest.fail("an invocation escaped before local intent"))
    claim = cb.profile_claim(f.config)
    write = repair.SourceMemberClaim._write

    def interrupted(path, value):
        if path == claim.local / "started.json":
            raise KeyboardInterrupt
        write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(repair.SourceMemberClaim, "_write", staticmethod(interrupted))
        with pytest.raises(KeyboardInterrupt):
            cb("gemini-3.6-flash", f.prompt, f.config)
    assert (claim.root / "started.json").exists()
    assert not (claim.local / "started.json").exists()
    response = cb("gemini-3.6-flash", f.prompt, f.config)
    assert "no automatic repeat" in response["error"]
    assert (claim.local / "started.json").exists()


def test_source_response_survives_local_journal_loss_without_a_repeat(profiled):
    f = profiled
    output = f.tmp_path / "owner"
    cb = callback(f, output, lambda model, *_: vote(model, False))
    response = cb("gemini-3.6-flash", f.prompt, f.config)
    claim = cb.profile_claim(f.config)
    retained = (claim.root / "response.json").read_bytes()
    shutil.rmtree(output / "members")
    second = callback(f, output, lambda *_: pytest.fail("response loss repeated invocation"))
    assert second("gemini-3.6-flash", f.prompt, f.config) == response
    assert (claim.root / "response.json").read_bytes() == retained
    assert json.loads((claim.local / "response.json").read_text())["member"] == response
    with pytest.raises(ContractError, match="owner_output"):
        callback(f, f.tmp_path / "other", lambda *_: pytest.fail("new output repeated invocation"))(
            "gemini-3.6-flash", f.prompt, f.config)


def test_changed_valid_profile_name_uses_same_key_but_cannot_steal_claim(profiled, monkeypatch):
    f = profiled
    output = f.tmp_path / "owner"
    first = callback(f, output, lambda model, *_: vote(model, True))
    first("gemini-3.6-flash", f.prompt, f.config)
    old_claim = first.profile_claim(f.config)
    # A future controller alias is not a second allowance for the same response.
    monkeypatch.setitem(repair.REJECTED_MEMBER_PROFILES, "different-explicit-profile", copy.deepcopy(f.profile))
    f.selection = {**f.selection, "rejected_member_profile": repair.rejected_profile_descriptor(
        "different-explicit-profile", f.profile)}
    second = callback(f, output, lambda *_: pytest.fail("changed profile obtained a second call"))
    assert second.profile_claim(f.config).root == old_claim.root
    with pytest.raises(ContractError, match="profile binding"):
        second("gemini-3.6-flash", f.prompt, f.config)


def test_caller_claim_directory_fields_cannot_redirect_the_source_claim(profiled):
    f = profiled
    first = callback(f, f.tmp_path / "one", lambda model, *_: vote(model, True))
    first("gemini-3.6-flash", f.prompt, f.config)
    f.selection.update(claim_dir=str(f.tmp_path / "caller-chosen"), source_claim_dir="/unused/override")
    second = callback(f, f.tmp_path / "two", lambda *_: pytest.fail("caller changed the claim directory"))
    assert second.profile_claim(f.config).root == first.profile_claim(f.config).root
    with pytest.raises(ContractError, match="owner_output"):
        second("gemini-3.6-flash", f.prompt, f.config)
    assert not (f.tmp_path / "caller-chosen").exists()


@pytest.mark.parametrize("which", ["source-root", "source-key", "journal", "journal-member", "trace"])
def test_symlink_claim_or_journal_redirection_is_rejected(profiled, which):
    f = profiled
    output = f.tmp_path / "owner"
    cb = callback(f, output, lambda *_: pytest.fail("symlink redirection invoked a member"))
    claim = cb.profile_claim(f.config)
    target = f.tmp_path / "redirected"
    target.mkdir()
    link = {"source-root": claim.root.parent, "source-key": claim.root, "journal": claim.local.parent,
            "journal-member": claim.local, "trace": Path(f.generation["trace_file"])}[which]
    link.parent.mkdir(parents=True, exist_ok=True)
    if which == "trace":
        copied = target / "trajectory.json"
        copied.write_bytes(link.read_bytes())
        link.unlink()
        link.symlink_to(copied)
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ContractError, match="symlink|co-located"):
        cb("gemini-3.6-flash", f.prompt, f.config)


def test_copied_result_cannot_bypass_profile_owner_on_child_fast_path(profiled, monkeypatch):
    f = profiled
    request, _ = configure(f, monkeypatch)
    saved = repair.recover_one(request, invoke=lambda model, *_: vote(model, True))
    other = f.tmp_path / "other-output"
    other.mkdir()
    repair.atomic_json(other / "result.json", saved)
    with pytest.raises(ContractError, match="owner_output"):
        repair.recover_one({**request, "output": str(other)},
                           invoke=lambda *_: pytest.fail("copied cached result allowed a repeat"))


def test_profile_does_not_adopt_an_unowned_cached_result(profiled, monkeypatch):
    f = profiled
    request, _ = configure(f, monkeypatch)
    Path(request["output"]).mkdir()
    repair.atomic_json(Path(request["output"]) / "result.json", {"selection": request["selection"]})
    with pytest.raises(ContractError, match="no retained source claim"):
        repair.recover_one(request, invoke=lambda *_: pytest.fail("unowned cache invoked"))


@pytest.mark.parametrize("has_response", [False, True])
def test_retained_v1_local_started_is_consumed_not_reinvoked(profiled, has_response):
    f = profiled
    output = f.tmp_path / "owner"
    cb = callback(f, output, lambda *_: pytest.fail("V1 consumed intent was repeated"))
    claim = cb.profile_claim(f.config)
    repair.atomic_json(claim.local / "started.json", claim.binding)
    if has_response:
        repair.atomic_json(claim.local / "response.json",
                           {"binding": claim.binding, "member": vote("gemini-3.6-flash", False)})
    result = cb("gemini-3.6-flash", f.prompt, f.config)
    if has_response:
        assert result["passed"] is False and (claim.root / "response.json").exists()
    else:
        assert "no automatic repeat" in result["error"] and not (claim.root / "response.json").exists()
    assert (claim.root / "started.json").exists()


def test_no_profile_keeps_original_per_output_legacy_journal_behavior(fixture):
    f = fixture
    calls = []

    def fake(model, *_):
        calls.append(model)
        return member(model, False)

    for name in ("first", "second"):
        cb = repair.JournaledJudge(f.previous, ["one"], f.tmp_path / name, invoke=fake)
        assert cb("one", f.prompt, f.config)["passed"] is False
        assert cb("one", f.prompt, f.config)["passed"] is False
    assert calls == ["one", "one"]
    assert not (f.tmp_path / ".judge-member-recovery-claims-v1").exists()
