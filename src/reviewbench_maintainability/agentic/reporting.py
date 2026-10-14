"""Fixed-denominator four-gate results: model failure != missing measurement."""
from __future__ import annotations

from . import PROTOCOL
from .contracts import ContractError, canonical_hash
from .judge_policy import policy_from_judgment, validate_completed_panel


def make_report(tasks: dict[str, dict], models: list[str], generations: list[dict],
                correctness: list[dict], judgments: list[dict], *,
                judge_policy: dict | None = None) -> tuple[list[dict], dict]:
    if not models or len(models) != len(set(models)):
        raise ContractError("models must be nonempty and unique")
    expected = {(ident, model) for ident in tasks for model in models}
    policies = [policy_from_judgment(j) for j in judgments]
    policy = judge_policy if judge_policy is not None else (policies[0] if policies else None)
    if judge_policy is not None:
        policy_from_judgment({"score_version": judge_policy.get("version"), "judge_policy": judge_policy})
    if any(p != policy for p in policies):
        raise ContractError("judgments mix scoring versions or rosters; publish separate reports")

    def index(rows, label):
        result = {}
        for row in rows:
            key = row.get("id"), row.get("model")
            if key not in expected or key in result or row.get("protocol") != PROTOCOL:
                raise ContractError(f"foreign/duplicate/mixed-protocol {label}: {key}")
            if row.get("task_fingerprint") != canonical_hash(tasks[key[0]]):
                raise ContractError(f"stale task fingerprint in {label}")
            result[key] = row
        return result

    gs, cs, js = (index(rows, label) for rows, label in [
        (generations, "generation"), (correctness, "correctness"), (judgments, "judgment")
    ])
    engines = {row["engine_fingerprint"] for row in generations if row.get("engine_fingerprint")}
    if len(engines) > 1:
        raise ContractError("generation rows mix different execution engines/dependency versions")
    rows = []
    for ident, model in sorted(expected):
        key = ident, model
        g, c, j = gs.get(key), cs.get(key), js.get(key)
        row = {"id": ident, "model": model, "protocol": PROTOCOL, "resolved": None,
               "generation_apply": None, "scope_ok": None, "correctness_ok": None,
               "maintainability_pass": None, "votes": None, "status": "incomplete"}
        if not g or g["status"] == "infra_error":
            row["missing_reason"] = "generation_not_measured" if not g else "generation_infrastructure"
        elif g["status"] == "generation_failed":
            if c or j:
                raise ContractError("no-patch generation unexpectedly has downstream measurements")
            row.update(resolved=False, generation_apply=False, scope_ok=False,
                       correctness_ok=False, maintainability_pass=False, votes=0,
                       status="model_failed", missing_reason=None)
        elif g["status"] == "generated" and g.get("apply_ok"):
            row["generation_apply"] = True
            for downstream in (c, j):
                if downstream and downstream.get("patch_sha256") != g.get("patch_sha256"):
                    raise ContractError("downstream evidence does not match candidate patch")
            if c and c.get("status") in {"pass", "fail", "apply_error", "scope_error"}:
                row["scope_ok"] = c.get("scope_ok") is True
                row["correctness_ok"] = c.get("correctness_ok") is True and c.get("oracle_replayed") is True
                if not row["scope_ok"] or not row["correctness_ok"]:
                    row.update(resolved=False, status="model_failed", missing_reason=None)
            if j and j.get("status") == "completed":
                panel = j.get("panel", [])
                required_votes = validate_completed_panel(j)
                votes = sum(p["passed"] for p in panel)
                row.update(votes=votes, maintainability_pass=(votes == required_votes))
                if j.get("maintainability_pass") is not (votes == required_votes):
                    raise ContractError("stored maintainability verdict disagrees with panel")
            if row["scope_ok"] is not None and row["correctness_ok"] is not None and row["maintainability_pass"] is not None:
                row.update(resolved=bool(row["scope_ok"] and row["correctness_ok"]
                                         and row["maintainability_pass"]), status="completed", missing_reason=None)
            elif row["resolved"] is None:
                row["missing_reason"] = "correctness_or_judge_incomplete"
        else:
            raise ContractError(f"unknown/inconsistent generation status: {g.get('status')}")
        if policy:
            row.update(score_version=policy["version"], judge_policy=policy)
        rows.append(row)
    board = {}
    for model in models:
        selected = [r for r in rows if r["model"] == model]
        unresolved_measurements = sum(r["resolved"] is None for r in selected)
        resolved = sum(r["resolved"] is True for r in selected)
        # Even where a false correctness gate decides FINAL, incomplete panels
        # remain visible rather than being published as a complete four-gate board.
        missing_gates = sum(
            r["generation_apply"] is not False
            and any(r[k] is None for k in ("scope_ok", "correctness_ok", "maintainability_pass"))
            for r in selected
        )
        complete = not unresolved_measurements and not missing_gates
        board[model] = {
            "tasks": len(tasks), "resolved": resolved, "complete": complete,
            "resolved_rate": resolved / len(tasks) if complete else None,
            "resolved_lower_bound": resolved / len(tasks),
            "missing_measurements": unresolved_measurements, "incomplete_gate_rows": missing_gates,
            "generation_failed": sum(r["generation_apply"] is False for r in selected),
            "votes": sum(r["votes"] or 0 for r in selected),
        }
    report = {"protocol": PROTOCOL, "task_count": len(tasks),
              "complete": all(r["complete"] for r in board.values()), "leaderboard": board}
    if policy:
        report.update(score_version=policy["version"], judge_policy=policy)
        for value in board.values():
            value["score_version"] = policy["version"]
    return rows, report
