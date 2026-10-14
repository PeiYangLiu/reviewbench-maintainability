"""Versioned judge rosters; historical records never silently adopt a new panel."""
from .contracts import ContractError

LEGACY_SCORE_VERSION = "reviewbench-judge-panel/three-judge-v1"
NO_GEMINI_SCORE_VERSION = "reviewbench-judge-panel/no-gemini-3.6-20261011-v1"
ASTRA_ONLY_SCORE_VERSION = "reviewbench-judge-panel/astra-only-20261011-v1"
ASTRA_MODEL = "gpt-6-astra"
EXCLUDED_MODEL = "gemini-3.6-flash"


def policy_from_config(config: dict) -> dict | None:
    models = config.get("models", [])
    if (not isinstance(models, list) or any(not isinstance(m, str) or not m for m in models)
            or len(set(models)) != len(models)):
        raise ContractError("judge models must be nonempty distinct names")
    version = config.get("score_version", LEGACY_SCORE_VERSION)
    if version == LEGACY_SCORE_VERSION:
        if len(models) != 3:
            raise ContractError("the primary protocol requires three distinct judges")
        if config.get("judge_policy") is not None:
            raise ContractError("legacy judge policy must not override the primary roster")
        return None
    if version == ASTRA_ONLY_SCORE_VERSION:
        if models != [ASTRA_MODEL]:
            raise ContractError("the Astra-only policy requires exactly gpt-6-astra")
        policy = {"version": version, "models": [ASTRA_MODEL],
                  "excluded_models": ["gpt-4.1", "gpt-5.6-sol", EXCLUDED_MODEL],
                  "aggregation": "single_judge"}
        if config.get("judge_policy", policy) != policy:
            raise ContractError("judge policy differs from the configured roster")
        return policy
    if version != NO_GEMINI_SCORE_VERSION:
        raise ContractError("unknown judge scoring version")
    if len(models) != 2 or EXCLUDED_MODEL in models:
        raise ContractError("the no-gemini policy requires two distinct remaining judges")
    policy = {"version": version, "models": list(models),
              "excluded_models": [EXCLUDED_MODEL], "aggregation": "unanimous"}
    if config.get("judge_policy", policy) != policy:
        raise ContractError("judge policy differs from the configured roster")
    return policy


def policy_from_judgment(judgment: dict) -> dict | None:
    version = judgment.get("score_version", LEGACY_SCORE_VERSION)
    policy = judgment.get("judge_policy")
    if version == LEGACY_SCORE_VERSION:
        if policy is not None:
            raise ContractError("unversioned judgment cannot supply an alternate judge policy")
        return None
    if not isinstance(policy, dict) or set(policy) != {"version", "models", "excluded_models", "aggregation"}:
        raise ContractError("versioned judgment requires an exact judge policy")
    expected = policy_from_config({"models": policy["models"], "score_version": version})
    if expected != policy:
        raise ContractError("judgment scoring policy mismatch")
    return policy


def validate_completed_panel(judgment: dict) -> int:
    policy = policy_from_judgment(judgment)
    panel = judgment.get("panel", [])
    required = len(policy["models"]) if policy else 3
    if (not isinstance(panel, list) or len(panel) != required
            or any(not isinstance(p, dict) or not isinstance(p.get("judge_model"), str)
                   or not p["judge_model"] or type(p.get("passed")) is not bool for p in panel)
            or len({p["judge_model"] for p in panel}) != required):
        raise ContractError("invalid judge panel")
    if policy and [p["judge_model"] for p in panel] != policy["models"]:
        raise ContractError("completed panel differs from its versioned roster")
    return required
