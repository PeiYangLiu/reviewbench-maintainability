import copy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from recover_agentic_progressive import recovered_generation, requires_recovery


def test_host_repair_preserves_agent_engine_usage_and_terminal_model_failure():
    original = {"id": "task", "status": "infra_error", "engine_fingerprint": "original-agent-engine",
                "usage": {"model_calls": 23, "input_tokens": 537129, "output_tokens": 12224},
                "stop_reason": "infrastructure_error", "error": "host collector failed"}
    untouched = copy.deepcopy(original)
    trace = {"messages": [{"role": "exit", "extra": {"exit_status": "RepeatedFormatError"}}]}
    collected = {"apply_ok": True, "patch_file": "/new/candidate.patch", "patch_sha256": "digest"}
    result = recovered_generation(original, collected, trace, "fixed-host-engine", {"workspace_sha256": "same"})
    assert original == untouched
    assert result["usage"] == original["usage"]
    assert result["engine_fingerprint"] == "original-agent-engine"
    assert result["serialization_engine_fingerprint"] == "fixed-host-engine"
    assert result["additional_generation_calls"] == 0
    assert result["stop_reason"] == "format_error_limit"
    assert result["status"] == "generated"


def test_host_recovery_does_not_regrade_completed_negative_verdicts():
    generation = {"status": "generated"}
    for status in ("pass", "fail", "apply_error", "scope_error"):
        assert not requires_recovery({"generation": generation, "correctness": {"status": status}})
    assert requires_recovery({"status": "processed", "generation": generation, "correctness": {
        "status": "infra_error", "error": "ContractError: immutable test missing at start: tests/link"}})
    assert not requires_recovery({"generation": generation, "correctness": {
        "status": "infra_error", "error": "an unrelated environment failure"}})


def test_pre_agent_failure_does_not_abort_host_recovery_scan(tmp_path):
    assert not requires_recovery({"generation": {
        "status": "infra_error", "trace_file": str(tmp_path/"never-created-trajectory.json"),
        "usage": {}, "error": "container bootstrap failed"}})
