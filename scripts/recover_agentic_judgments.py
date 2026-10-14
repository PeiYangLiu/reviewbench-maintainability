#!/usr/bin/env python3
"""Retry explicitly bound failed judge members without resampling accepted votes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.assets import AssetPool, asset_descriptor
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, PROTOCOL, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash,
    selected_tasks, write_jsonl,
)
from reviewbench_maintainability.agentic.evaluation import (
    RUBRIC, RUBRIC_VERSION, invoke_judge, judge_candidate, parse_judge_verdict, verdict_passes,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity, sanitize_config
from reviewbench_maintainability.agentic.preparation import load_recipes
from reviewbench_maintainability.agentic.reporting import make_report
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB
from reviewbench_maintainability.agentic.resources import ResourceRequest, file_lock
from reviewbench_maintainability.agentic.scheduling import output_lock

SCHEMA = "reviewbench-judge-member-recovery/v1"
HEX256 = re.compile(r"[0-9a-f]{64}")
TERMINAL_CORRECTNESS = {"pass", "fail", "scope_error", "apply_error"}
BOOTSTRAP_BASE_SCHEMA = "reviewbench-zero-call-bootstrap-recovery/v1"
BOOTSTRAP_GENERATION_ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"
MEASUREMENT_BASE_SCHEMA = "reviewbench-measurement-recovery/v1"
CONTEXT_ADMISSION_SCHEMA = "reviewbench-judge-context-admission-recovery/v1"
CONTEXT_OVERRIDE_SCHEMA = "reviewbench-exact-context-cap-override/v1"
CONTEXT_INPUT_HASHES = {
    "selection.json": "0f4d8964aba4a55cc1eb1c5d5547a69753c560703665613700c74df14e363c17",
    "original-case-bindings.json": "1977ce54913c8e91e0387fc0658e070fe22d1433b48f2ebe3f9a7595df0f1a15",
    "judge-configs.json": "4515fe2a6c567c0581357a610dc1b8b92bf5d7c67d802b1f1e1123068900efce",
}
CONTEXT_OLD_FINGERPRINT = "1068d00f36c9d239732ee3dfe093227f703ef90a9c17e0e2626e21c77a871016"
CONTEXT_NEW_FINGERPRINT = "cc0b73d887a3981a70a837107563ae6d1d8b9ff56e28b5c647f52eac6357e65a"
# Preserve all existing 1.6M admissions. Only the separately sealed original
# SqlClient empty panel has this larger cap and corresponding rubric/config hash.
CONTEXT_PROFILE_SETTINGS = {
    "sqlclient16-original-wide-empty-panel-v1": (
        3200000, "24798f40019fd31888e4d50dcb63ad069278af973bbb0821ef9d2e66b5735056",
    ),
}
CONTEXT_BASE_MANIFEST_SHA256 = "86ef4b380e6ed86de788b8e2ff79ea8f710852c21db5dc12965170b699e867cb"
CONTEXT_PUBLICATION_POLICY = "reviewbench-context-cached-publication/v2"
CONTEXT_PUBLICATION_RECEIPT = "reviewbench-context-publication-snapshot/v2"
CONTEXT_PUBLICATION_FILES = (
    "generation_results.jsonl", "correctness.jsonl", "judgments.jsonl", "results.jsonl", "report.json",
)
ADDITIONAL_CONTEXT_PROFILES = {
    'sqlclient16-original-wide-empty-panel-v1': {'additional_input_hashes': {'original-bindings-proof.json': '8f264c14966a0c90963c1af1b32c5a6a480a1aa2fcc5b6cb20753742e2d3df67',
                             'paths.json': '217037d9647a34ded0d8a53430b7454ac1329072a51b4c4254c9f8fc55b19de8'},
 'base_manifest_sha256': 'cf7171a9c30cbb8de9f97691df56eb7ce0705e9d08d3db94ab2bc0d71235b893',
 'inherited_context_first_attempts': 42,
 'input_hashes': {'judge-configs.json': '6599789097b0ba288bc6a563d797258b8e117fde71415473afc1f8afa9f557f3',
                  'original-case-bindings.json': '90496f8925c230bb42041253b48c6c445c58c8591f6b151dcd4c8e85725cca74',
                  'selection.json': '25b9b555d62fb8914fc158e7d86096bfe636c2fb3649fc6cb983c73927c7fbda'},
 'name': 'sqlclient16-original-wide-empty-panel-v1',
 'selected_originals': 1},
    'zcash114-original-empty-panel-v1': {'additional_input_hashes': {'original-bindings-proof.json': '73721f00efcda953fcc7838bcb352de7401b4854a8202ec9ba58194bc67bb15a',
                             'paths.json': 'e5b5a1d4e7e3e429c49ea498fd8be2fc53b04ce6848d779ee2e1d0b467378414'},
 'base_manifest_sha256': 'cf7171a9c30cbb8de9f97691df56eb7ce0705e9d08d3db94ab2bc0d71235b893',
 'inherited_context_first_attempts': 42,
 'input_hashes': {'judge-configs.json': '4515fe2a6c567c0581357a610dc1b8b92bf5d7c67d802b1f1e1123068900efce',
                  'original-case-bindings.json': '0b3797c61fa53cc97024c5e4438c34315cd7a4c2e7688edec63c93f714a75b2d',
                  'selection.json': '5ee7aa7af626e731ff2b983a470ece72927301f6d4183548a04e1878a74e3309'},
 'name': 'zcash114-original-empty-panel-v1',
 'selected_originals': 1},
    'libp2p58-original-empty-panel-v1': {'additional_input_hashes': {'original-bindings-proof.json': 'd7c8dd7eea9b578eb935cde1942dd10702e63f5a8113460e9805c43a9ce41218',
                                 'paths.json': '5cce41fee5822432a36a54c44c4bd6b2cf7d29b16df7e2e53c83ee9e81816fe6'},
     'base_manifest_sha256': 'cf7171a9c30cbb8de9f97691df56eb7ce0705e9d08d3db94ab2bc0d71235b893',
     'inherited_context_first_attempts': 42,
     'input_hashes': {'judge-configs.json': '4515fe2a6c567c0581357a610dc1b8b92bf5d7c67d802b1f1e1123068900efce',
                      'original-case-bindings.json': '24cb37030bae9faaf2d63abb58a702808494345d9acddeb572e31a8ac0377880',
                      'selection.json': '06429cb78d51b17d2578d73b16df3b777914c5948c16832a258b4897c5c76f77'},
     'name': 'libp2p58-original-empty-panel-v1',
     'selected_originals': 1},
    'tantivy84-original-empty-panel-v1': {'additional_input_hashes': {'original-bindings-proof.json': 'ea37b22763c2df45bc42f15f14ef2841209095d59f85dadc268c13c3045c7847',
                                 'paths.json': 'ea8d92e85ec8d89e19e33ac6853220d961b9c986c6b5db483ce93e68517ee2f0'},
     'base_manifest_sha256': 'cf7171a9c30cbb8de9f97691df56eb7ce0705e9d08d3db94ab2bc0d71235b893',
     'inherited_context_first_attempts': 42,
     'input_hashes': {'judge-configs.json': '4515fe2a6c567c0581357a610dc1b8b92bf5d7c67d802b1f1e1123068900efce',
                      'original-case-bindings.json': 'e5d44e8862db543358147e837506f45df65751d5bfb31942d22cfea299e2f75b',
                      'selection.json': 'a39a24db899997c86969e6690aebc81fbdc5e9503ef9b5daf7479f22ca3e06dc'},
     'name': 'tantivy84-original-empty-panel-v1',
     'selected_originals': 1},
    'libbpf57-original-empty-panel-v1': {'additional_input_hashes': {'original-bindings-proof.json': '0beaf2a5f9c875e1a71e1967fb78634fafc6b59af6228e528a7a6f8be72b5e84',
                                 'paths.json': '3fc37b7053ab3cfebf08937cb63d20d703ef2aa5797fcc7308847b15c2c9a562'},
     'base_manifest_sha256': 'cf7171a9c30cbb8de9f97691df56eb7ce0705e9d08d3db94ab2bc0d71235b893',
     'inherited_context_first_attempts': 42,
     'input_hashes': {'judge-configs.json': '4515fe2a6c567c0581357a610dc1b8b92bf5d7c67d802b1f1e1123068900efce',
                      'original-case-bindings.json': 'd39489b8a176ca0e4ebb5be130ea975ba5abf054fae9d659b7d9068d04eb244d',
                      'selection.json': '349aeb8d29ee4d7c43ff4b643198f95e9fe051087b5fb2b2df6c8adfee1a8641'},
     'name': 'libbpf57-original-empty-panel-v1',
     'selected_originals': 1},
    "cargo-contract108109-original-empty-panel-v1": {
        "name": "cargo-contract108109-original-empty-panel-v1",
        "input_hashes": {
            "selection.json": "9c3117ba2f6e24487cc20f0a684c8597699982bfcf31357bf4b0a3bdcd102087",
            "original-case-bindings.json": "69f37cf09031f7741bb82cd6c9a1e8104542e9bb04fc07f391a162c551a2d843",
            "judge-configs.json": "4515fe2a6c567c0581357a610dc1b8b92bf5d7c67d802b1f1e1123068900efce",
        },
        "additional_input_hashes": {
            "paths.json": "099a1fc2e268ec8bb25b70e8d5b3130326707ee30b5d3403e1d11d447db8ecbb",
            "original-bindings-proof.json": "5417712b824acb60c484108df90c258332ec53e329968b2b434bdc4dfdf66516",
        },
        "base_manifest_sha256": "cf7171a9c30cbb8de9f97691df56eb7ce0705e9d08d3db94ab2bc0d71235b893",
        "selected_originals": 2,
        "inherited_context_first_attempts": 42,
    },
    "clarity103-original-empty-panel-v1": {
        "name": "clarity103-original-empty-panel-v1",
        "input_hashes": {
            "selection.json": "c120028d68eeed8b602db6c1d3e470c13e925936815cc202c27ad6952c01a83f",
            "original-case-bindings.json": "91fd928c4c33089d99077c561eb3254913f52bda3a62b1f5160e41ddff29e867",
            "judge-configs.json": "4515fe2a6c567c0581357a610dc1b8b92bf5d7c67d802b1f1e1123068900efce",
        },
        "additional_input_hashes": {
            "paths.json": "a341ffbf4df6daa0823dc763cab66c50c1671996ee179176743707b1004b9d91",
            "original-bindings-proof.json": "aed48c81561a588c63982661b4fe46495818f215aff53d1d3bd3d88d59168fd8",
        },
        "base_manifest_sha256": "cf7171a9c30cbb8de9f97691df56eb7ce0705e9d08d3db94ab2bc0d71235b893",
        "selected_originals": 1,
        "inherited_context_first_attempts": 42,
    },
    "scylla96-original-empty-panel-v1": {
        "name": "scylla96-original-empty-panel-v1",
        "input_hashes": {
            "selection.json": "25f7ff0b0e95ff4a2994646b55cdb229a416ea30989b22dfd96912d42537ea3f",
            "original-case-bindings.json": "1c099935a40b0759c62c484cbc8842a3646797f501003db6e77adce5064094c2",
            "judge-configs.json": "4515fe2a6c567c0581357a610dc1b8b92bf5d7c67d802b1f1e1123068900efce",
        },
        "additional_input_hashes": {
            "paths.json": "b67cb9a77f2483fa33577fbbc4cc8836cb00b95ea0cad94ff1e2d2220d5b4f01",
            "original-bindings-proof.json": "22940867ebb1f02b0b54a7109155148b5ecd1a80a9d223bd18c43a56858ee746",
        },
        "base_manifest_sha256": "e7b8c6d72cd4f382581543eaf2614d954f7f7cbb919ad0b7a2d1a9c36ecc4fed",
        "selected_originals": 1,
        "inherited_context_first_attempts": 39,
    },
}
MAX_RECOVERY_LINEAGE = 16
REJECTED_MEMBER_PROFILE_SCHEMA = "reviewbench-explicit-rejected-judge-member/v1"
REJECTED_MEMBER_CLAIM_SCHEMA = "reviewbench-source-judge-member-claim/v1"
# These are authorizations for two original, unaccepted responses, not error classes.
# Every binding is checked again before writing the one-shot request intent.
REJECTED_MEMBER_PROFILES = {
    "rippled5996-gemini-length-v1": {
        "id": "github__XRPLF__rippled__pull_5996__comment_2635153432__dead_code",
        "model": "gpt-5.4-responses",
        "generation_sha256": "0d5532fa7b5138ba2b13d25d5a5e41b1c6e4a8ebc57fe6ad0eeb2c1e3d9d70c4",
        "judgment_sha256": "133e1152a5b4365c39290bed7c7a9c248de1d69afb1e1f803e38b1cee28a7165",
        "correctness_sha256": "7dccb70fb754783aebc12fc6ee6f5f8c75b777e661d4b2076ce576553786a0da",
        "judge_engine": BOOTSTRAP_GENERATION_ENGINE,
        "judge_models": ["gemini-3.6-flash"],
        "error": "InfrastructureError: judge response did not complete",
        "member_sha256": "440c165a8259420d565747a7e46aa66c0ab8d0c9d24d07c7e900d9ef16694eb1",
        "raw_response_text_sha256": "84f8ee5b5fa1bcf795191338d1559eba71c3ff532b8232e1514de02ad42c8cce",
        "finish_reason": "length",
    },
    "openthread8966-gemini-control-character-v1": {
        "id": "github__openthread__openthread__pull_8966__comment_1171970977__modularity",
        "model": "gpt-5.4-responses",
        "generation_sha256": "201acaa191d6bf383d06e9d07126b874a492f61ead3da23b36c2d1101cb82886",
        "judgment_sha256": "e39696123fab2626121b880d427397909fc8eda47bd04f09c43fda94f006808c",
        "correctness_sha256": "b41ed339ea2fbd9b389daea2dd36df73ed822604fc58f4af15de24a55ffdfefc",
        "judge_engine": BOOTSTRAP_GENERATION_ENGINE,
        "judge_models": ["gemini-3.6-flash"],
        "error": "JSONDecodeError: Invalid control character at: line 6 column 161 (char 256)",
        "member_sha256": "9dab649f413c737943ec0f2b668b34063e7f093c08e96a4d7dadbe2606d6aa91",
        "raw_response_text_sha256": "438747ef675fd07532b998e453e08c3748fbfcd8cfadc721c26b451af3a17f80",
        "finish_reason": "stop",
    },
}


def rejected_profile_descriptor(name, profile):
    return {
        "schema_version": REJECTED_MEMBER_PROFILE_SCHEMA,
        "name": name, "judge_model": profile["judge_models"][0],
        "error": profile["error"], "original_member_sha256": profile["member_sha256"],
        "max_new_attempts": 1,
    }


def selected_rejected_profile(selection):
    if "rejected_member_profile" not in selection:
        return None
    descriptor = selection["rejected_member_profile"]
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("name"), str):
        raise ContractError("rejected member recovery requires an exact named profile descriptor")
    profile = REJECTED_MEMBER_PROFILES.get(descriptor["name"])
    if (profile is None or type(descriptor.get("max_new_attempts")) is not int
            or descriptor != rejected_profile_descriptor(descriptor["name"], profile)):
        raise ContractError("unknown or changed rejected member profile")
    for key in ("id", "model", "generation_sha256", "judgment_sha256", "correctness_sha256",
                "judge_engine", "judge_models"):
        if selection.get(key) != profile[key]:
            raise ContractError("rejected member profile differs from selected " + key)
    return profile


def validate_rejected_member_profile(selection, generation, judgment, config):
    """Prove a pinned original response failed before any accepted verdict existed."""
    profile = selected_rejected_profile(selection)
    if profile is None:
        return None
    if (canonical_hash(generation) != profile["generation_sha256"]
            or canonical_hash(judgment) != profile["judgment_sha256"]):
        raise ContractError("rejected member profile original generation/judgment changed")
    validate_partial(judgment, generation, config, profile["judge_engine"])
    member = next(row for row in judgment["panel"] if row["judge_model"] == profile["judge_models"][0])
    if ("passed" in member or "verdict" in member or member.get("http_status") != 200
            or member.get("error") != profile["error"]
            or canonical_hash(member) != profile["member_sha256"]):
        raise ContractError("rejected member profile requires the exact unaccepted original member")
    text, raw = member.get("raw_response_text"), member.get("raw_response")
    if (not isinstance(text, str) or not isinstance(raw, dict)
            or hashlib.sha256(text.encode()).hexdigest() != profile["raw_response_text_sha256"]
            or json.loads(text) != raw
            or raw.get("model") not in config.get("expected_response_models", {}).get(member["judge_model"], [])):
        raise ContractError("rejected member profile raw response/identity binding differs")
    choice = raw["choices"][0]
    if choice.get("finish_reason") != profile["finish_reason"]:
        raise ContractError("rejected member profile completion boundary changed")
    if profile["finish_reason"] == "length":
        # Frozen invoke_judge rejects this before calling the strict verdict parser.
        if member["error"] != "InfrastructureError: judge response did not complete":
            raise ContractError("rejected member profile is not the frozen incomplete-response boundary")
    else:
        # Verification must fail strictly. Never repair/escape the retained content.
        try:
            parse_judge_verdict(choice["message"]["content"])
        except json.JSONDecodeError as exc:
            if f"JSONDecodeError: {exc}" != profile["error"]:
                raise ContractError("rejected member profile strict JSON failure changed") from exc
        else:
            raise ContractError("rejected member profile unexpectedly contains a parseable verdict")
    return profile


def context_profile(input_hashes=None):
    """Select a sealed original cohort, never a general failed-member retry."""
    if input_hashes is None or input_hashes == CONTEXT_INPUT_HASHES:
        return {
            "name": "original-thirteen",
            "input_hashes": dict(CONTEXT_INPUT_HASHES),
            "base_manifest_sha256": CONTEXT_BASE_MANIFEST_SHA256,
            "selected_originals": 13,
        }
    for profile in ADDITIONAL_CONTEXT_PROFILES.values():
        if input_hashes == profile["input_hashes"]:
            return copy.deepcopy(profile)
    raise ContractError("unknown or changed context-admission metadata")


def context_profile_for_selection(digest):
    profiles = [context_profile(), *ADDITIONAL_CONTEXT_PROFILES.values()]
    for profile in profiles:
        if digest == profile["input_hashes"]["selection.json"]:
            return copy.deepcopy(profile)
    raise ContractError("sealed context-admission input changed: selection.json")


def context_admitted_settings(profile):
    """Return a code-bound cap/fingerprint, not a caller-supplied override."""
    return CONTEXT_PROFILE_SETTINGS.get(profile["name"], (1600000, CONTEXT_NEW_FINGERPRINT))


def validate_context_admission_metadata(value):
    """A specific cap-only admission, not a generic grading-config override."""
    fields = {
        "schema_version", "input_hashes", "original_judge_config", "admitted_judge_config",
        "original_judge_fingerprint", "admitted_judge_fingerprint", "changed_fields",
        "max_first_attempts_per_member",
    }
    if (not isinstance(value, dict) or set(value) != fields
            or value["schema_version"] != CONTEXT_OVERRIDE_SCHEMA
            or not isinstance(value["input_hashes"], dict)
            or value["changed_fields"] != ["max_context_chars"]
            or type(value["max_first_attempts_per_member"]) is not int
            or value["max_first_attempts_per_member"] != 1):
        raise ContractError("unknown or changed context-admission metadata")
    profile = context_profile(value["input_hashes"])
    maximum, admitted_fingerprint = context_admitted_settings(profile)
    original, admitted = value["original_judge_config"], value["admitted_judge_config"]
    if (not isinstance(original, dict) or not isinstance(admitted, dict)
            or type(original.get("max_context_chars")) is not int
            or original["max_context_chars"] != 300000
            or type(admitted.get("max_context_chars")) is not int
            or admitted["max_context_chars"] != maximum
            or admitted != {**original, "max_context_chars": maximum}):
        raise ContractError(f"context admission must change only the exact 300000-to-{maximum} cap")
    for config, key, fingerprint in (
        (original, "original_judge_fingerprint", CONTEXT_OLD_FINGERPRINT),
        (admitted, "admitted_judge_fingerprint", admitted_fingerprint),
    ):
        if (value[key] != fingerprint
                or canonical_hash({"rubric": RUBRIC, "version": RUBRIC_VERSION, "config": config}) != fingerprint):
            raise ContractError("context admission changes original identities/rubric/endpoint/output/timeout")


def context_publication_policy(output, metadata=None):
    profile = context_profile(None if metadata is None else metadata["input_hashes"])
    return {
        "schema_version": CONTEXT_PUBLICATION_POLICY, "owner_output": str(Path(output).resolve()),
        "watch_mode": "cached-only", "snapshot_reader": "double-stat-sha256-and-derived-report",
        "selected_originals": profile["selected_originals"],
        "max_first_attempts": 3 * profile["selected_originals"],
        "claim_owner": "original-source-episode", "pointer": "current",
        "epochs": "publications", "unrelated_rows": "unchanged-as-of-snapshot",
    }


def validate_context_publication_policy(manifest, output):
    """V2 is explicit; old V1 manifests retain their original lineage contract."""
    validate_context_admission_metadata(manifest.get("context_admission"))
    metadata = manifest["context_admission"]
    profile = context_profile(metadata["input_hashes"])
    if manifest.get("publication_policy") != context_publication_policy(output, metadata):
        raise ContractError("unknown or changed context cached-publication policy/owner_output")
    if (manifest.get("schema_version") != CONTEXT_ADMISSION_SCHEMA
            or manifest.get("base_manifest_sha256") != profile["base_manifest_sha256"]
            or manifest.get("selection_sha256") != profile["input_hashes"]["selection.json"]
            or manifest.get("judge_fingerprint") != metadata["admitted_judge_fingerprint"]
            or manifest.get("generation_engine_fingerprint") != BOOTSTRAP_GENERATION_ENGINE
            or type(manifest.get("validation_only")) is not bool):
        raise ContractError("context cached-publication manifest/config binding differs")
    bound = manifest.get("bound_inputs")
    if not isinstance(bound, dict) or not bound:
        raise ContractError("context cached-publication immutable input bindings are missing")
    required = {
        str(Path(manifest["base_run"]) / "experiment-manifest.json"): manifest["base_manifest_sha256"],
        str(Path(manifest["source_run"]) / "experiment-manifest.json"): manifest["source_manifest_sha256"],
    }
    for field in ("original_config", "tasks_file", "scope_ids_file", "controller_file", "retained_helper_file",
                  "input_bundle"):
        if not isinstance(manifest.get(field), str) or not Path(manifest[field]).is_absolute():
            raise ContractError("context cached-publication requires absolute " + field)
    for field, digest in (("controller_file", "controller_sha256"), ("retained_helper_file", "retained_helper_sha256")):
        required[manifest[field]] = manifest.get(digest)
    for name, digest in profile["input_hashes"].items():
        required[str(Path(manifest["input_bundle"]) / name)] = digest
    for name, digest in profile.get("additional_input_hashes", {}).items():
        required[str(Path(manifest["input_bundle"]) / name)] = digest
    for name in ("paths.json", "original-bindings-proof.json"):
        if str(Path(manifest["input_bundle"]) / name) not in bound:
            raise ContractError("context auxiliary sealed input binding is missing")
    for field in ("bootstrap_lineage", "recovery_lineage"):
        for row in manifest.get(field, []):
            required[row["manifest"]] = row["sha256"]
    for field in ("original_config", "tasks_file", "scope_ids_file"):
        if manifest[field] not in bound:
            raise ContractError("context immutable configuration/task binding is missing")
    if str(Path(manifest["source_run"]) / "experiment-config.json") not in bound:
        raise ContractError("context original source configuration is not bound")
    if any(bound.get(path) != digest for path, digest in required.items()):
        raise ContractError("context immutable source/controller bindings differ")
    volatile = {str(Path(manifest["base_run"]) / name) for name in CONTEXT_PUBLICATION_FILES}
    if volatile.intersection(bound):
        raise ContractError("context publication cannot lifetime-pin mutable measurement files")
    for path, digest in bound.items():
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or not isinstance(digest, str) or not HEX256.fullmatch(digest)
                or file_hash(Path(path)) != digest):
            raise ContractError("context immutable input/lineage changed: " + str(path))


def measurement_rows(raw, tasks, models):
    """Parse one already captured set of columns, without reopening live paths."""
    rows = {}
    for key, name in (("generation", "generation_results"), ("correctness", "correctness"), ("judgment", "judgments")):
        seen = set()
        for line in raw[name + ".jsonl"].decode("utf-8").splitlines():
            value = json.loads(line)
            pair = value["id"], value["model"]
            if pair in seen or pair[0] not in tasks or pair[1] not in models:
                raise ContractError("base report has duplicate/foreign task-model measurements")
            seen.add(pair)
            rows.setdefault(pair, {"id": pair[0], "model": pair[1]})[key] = value
    return rows


def checked_measurement_report(raw, tasks, models):
    rows = measurement_rows(raw, tasks, models)
    columns = [[row[key] for row in rows.values() if key in row] for key in ("generation", "correctness", "judgment")]
    table, expected = make_report(tasks, models, *columns)
    actual = [json.loads(line) for line in raw["results.jsonl"].decode("utf-8").splitlines()]
    report = json.loads(raw["report.json"])
    if (actual != table or any(canonical_hash(report.get(key)) != canonical_hash(value)
                               for key, value in expected.items())):
        raise ContractError("measurement columns/results/report are not a coherent snapshot")
    return rows, report


def read_context_publication(base, manifest, tasks, models, *, epoch=None):
    """Pin current once. A concurrent pointer swap never mixes published columns."""
    base = Path(base).resolve()
    validate_context_publication_policy(manifest, base)
    manifest_bytes = (base / "experiment-manifest.json").read_bytes()
    if json.loads(manifest_bytes) != manifest:
        raise ContractError("context publication manifest changed while reading")
    for name in CONTEXT_PUBLICATION_FILES:
        path = base / name
        if not path.is_symlink() or os.readlink(path) != "current/" + name:
            raise ContractError("context publication root must use fixed current aliases")
    pointer = base / "current"
    if epoch is None:
        if not pointer.is_symlink():
            raise ContractError("context publication has no committed current pointer")
        target = os.readlink(pointer)
    else:
        target = "publications/" + epoch
    parts = Path(target).parts
    if (len(parts) != 2 or parts[0] != "publications" or not HEX256.fullmatch(parts[1])
            or (base / "publications").is_symlink()):
        raise ContractError("context publication pointer is outside immutable epochs")
    directory = base / target
    if directory.is_symlink() or directory.resolve().parent != (base / "publications").resolve():
        raise ContractError("context publication epoch is not an owned directory")
    receipt_file = directory / "snapshot.json"
    if receipt_file.is_symlink():
        raise ContractError("context publication receipt must not be a symlink")
    receipt_raw = receipt_file.read_bytes()
    receipt = json.loads(receipt_raw)
    if (set(receipt) != {"schema_version", "publication_key", "owner_output", "manifest_sha256",
                        "identity", "base_snapshot", "output_hashes"}
            or receipt["schema_version"] != CONTEXT_PUBLICATION_RECEIPT
            or receipt["publication_key"] != parts[1]
            or canonical_hash(receipt["identity"]) != parts[1]
            or receipt["owner_output"] != str(base)
            or receipt["manifest_sha256"] != hashlib.sha256(manifest_bytes).hexdigest()
            or set(receipt["identity"]) != {"inputs", "base_snapshot", "output_hashes"}
            or receipt["identity"]["inputs"].get("manifest_sha256") != receipt["manifest_sha256"]
            or receipt["identity"]["output_hashes"] != receipt["output_hashes"]
            or receipt["identity"]["base_snapshot"] != {
                k: v for k, v in receipt["base_snapshot"].items() if k != "captured_at_utc"}
            or receipt["base_snapshot"].get("base_run") != manifest["base_run"]
            or receipt["base_snapshot"].get("rows_sha256") != receipt["identity"]["inputs"].get("base_rows_sha256")
            or set(receipt["base_snapshot"].get("file_sha256", {})) != set(CONTEXT_PUBLICATION_FILES)
            or set(receipt["output_hashes"]) != set(CONTEXT_PUBLICATION_FILES)):
        raise ContractError("context immutable snapshot receipt/owner/manifest differs")
    raw = {}
    for name in CONTEXT_PUBLICATION_FILES:
        path = directory / name
        if path.is_symlink():
            raise ContractError("context epoch data must not be a symlink")
        raw[name] = path.read_bytes()
        if hashlib.sha256(raw[name]).hexdigest() != receipt["output_hashes"][name]:
            raise ContractError("context publication file hash differs: " + name)
    rows, report = checked_measurement_report(raw, tasks, models)
    if (report.get("publication_inputs_sha256") != canonical_hash(receipt["identity"]["inputs"])
            or report.get("publication_policy") != manifest["publication_policy"]
            or report.get("context_admission") != manifest["context_admission"]
            or report.get("validation_only") != manifest["validation_only"]):
        raise ContractError("context report publication provenance differs")
    if (receipt_file.read_bytes() != receipt_raw or (base / "experiment-manifest.json").read_bytes() != manifest_bytes):
        raise ContractError("context immutable epoch/manifest changed during read")
    return rows, {**report, "publication_key": receipt["publication_key"]}, receipt


def base_engine_binding(base, base_manifest, tasks, models):
    """Bind at most sixteen explicit overlays to the original full402 source."""
    schemas = {BOOTSTRAP_BASE_SCHEMA, SCHEMA, MEASUREMENT_BASE_SCHEMA, CONTEXT_ADMISSION_SCHEMA}
    schema = base_manifest.get("schema_version")
    if schema in {BOOTSTRAP_BASE_SCHEMA, CONTEXT_ADMISSION_SCHEMA} or len(tasks) == 402:
        if schema not in schemas:
            raise ContractError("unknown recovery base schema")
        return full402_engine_binding(base, base_manifest, tasks, models)
    if schema != BOOTSTRAP_BASE_SCHEMA:
        # Keep the original non-full402 legacy contract, never alias engine fields.
        engine = base_manifest["generation_engine_fingerprint"]
        if schema not in schemas:
            raise ContractError("unknown recovery base schema")
        return {
            "generation_engine_fingerprint": engine,
            "base_host_engine_fingerprint": (base_manifest.get("host_recovery_engine_fingerprint")
                                             or base_manifest.get("base_host_engine_fingerprint")),
        }


def full402_engine_binding(base, base_manifest, tasks, models):
    """Only bootstrap/judge-member/measurement schemas; no field fallback."""
    if (len(tasks) != 402 or type(models) is not list or len(models) != 1
            or not isinstance(models[0], str) or not models[0]):
        raise ContractError("recovery lineage requires the original full402 single-model contract")

    def directory(value, label):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ContractError("recovery lineage " + label + " must be an absolute retained path")
        return Path(value).resolve()

    def read_manifest(path):
        try:
            data = path.read_bytes()
            value = json.loads(data)
        except (OSError, ValueError) as exc:
            raise ContractError("retained recovery lineage manifest is unreadable: " + str(path)) from exc
        if not isinstance(value, dict):
            raise ContractError("recovery lineage manifest must be an object")
        return value, hashlib.sha256(data).hexdigest()

    def full_scope(value):
        if (value.get("task_ids") != list(tasks)
                or value.get("tasks_fingerprint") != canonical_hash(tasks)
                or value.get("models") != models
                or type(value.get("expected_pairs")) is not int or value["expected_pairs"] != 402):
            raise ContractError("recovery lineage full402 task/model binding differs")

    source = directory(base_manifest.get("source_run"), "source_run")
    original, original_sha = read_manifest(source / "experiment-manifest.json")
    full_scope(original)
    if (original.get("schema_version") != "reviewbench-progressive-experiment/v1"
            or original.get("protocol") != PROTOCOL
            or original.get("engine_fingerprint") != BOOTSTRAP_GENERATION_ENGINE
            or "generation_engine_fingerprint" in original):
        raise ContractError("recovery lineage original source/engine binding differs")

    current = directory(str(base), "base_run")
    seen, layers, values = {source}, [], []
    parent_digest = None
    for depth in range(MAX_RECOVERY_LINEAGE):
        if current in seen:
            raise ContractError("recovery lineage contains a resolved-path cycle")
        seen.add(current)
        path = current / "experiment-manifest.json"
        value, digest = read_manifest(path)
        schema = value.get("schema_version")
        if depth == 0 and value != base_manifest:
            raise ContractError("recovery base manifest changed during lineage binding")
        if parent_digest is not None and parent_digest != digest:
            raise ContractError("recovery lineage base_manifest_sha256 differs")
        if schema not in {BOOTSTRAP_BASE_SCHEMA, SCHEMA, MEASUREMENT_BASE_SCHEMA, CONTEXT_ADMISSION_SCHEMA}:
            raise ContractError("unknown recovery lineage schema")
        full_scope(value)
        if (value.get("source_manifest_sha256") != original_sha
                or directory(value.get("source_run"), "layer source_run") != source):
            raise ContractError("recovery lineage source manifest binding differs")
        if schema == BOOTSTRAP_BASE_SCHEMA:
            if ("generation_engine_fingerprint" in value
                    or value.get("engine_fingerprint") != BOOTSTRAP_GENERATION_ENGINE
                    or value.get("behavior_fingerprint") != original.get("behavior_fingerprint")):
                raise ContractError("bootstrap lineage engine/behavior binding differs")
        elif ("engine_fingerprint" in value
              or value.get("generation_engine_fingerprint") != BOOTSTRAP_GENERATION_ENGINE):
            raise ContractError("recovery lineage generation engine binding differs")
        if schema == CONTEXT_ADMISSION_SCHEMA:
            validate_context_admission_metadata(value.get("context_admission"))
            profile = context_profile(value["context_admission"]["input_hashes"])
            if (value.get("selection_sha256") != profile["input_hashes"]["selection.json"]
                    or value.get("judge_fingerprint") != value["context_admission"]["admitted_judge_fingerprint"]
                    or value.get("base_manifest_sha256") != profile["base_manifest_sha256"]):
                raise ContractError("context overlay selection/judge fingerprint differs")
            if "publication_policy" in value:
                validate_context_publication_policy(value, current)
        layers.append({"manifest": str(path), "sha256": digest, "schema_version": schema})
        values.append(value)
        if schema == MEASUREMENT_BASE_SCHEMA:
            # The retained measurement schema is the host-engine anchor, not its
            # older base_recovery_run. Do not traverse another unknown host schema.
            if "base_run" in value or "base_manifest_sha256" in value:
                raise ContractError("measurement lineage anchor has an unexpected overlay parent")
            host = value.get("host_recovery_engine_fingerprint")
            if not isinstance(host, str) or not HEX256.fullmatch(host):
                raise ContractError("measurement lineage host-engine anchor is missing")
            break
        parent_digest = value.get("base_manifest_sha256")
        if ("base_manifest_sha256" in value or schema in {SCHEMA, CONTEXT_ADMISSION_SCHEMA}) and (
                not isinstance(parent_digest, str) or not HEX256.fullmatch(parent_digest)):
            raise ContractError("recovery lineage requires an exact parent manifest hash")
        current = directory(value.get("base_run"), "layer base_run")
    else:
        raise ContractError("recovery lineage exceeds sixteen overlay manifests")

    for index, value in enumerate(values):
        if (value["schema_version"] in {SCHEMA, CONTEXT_ADMISSION_SCHEMA}
                and value.get("base_host_engine_fingerprint") != host):
            raise ContractError("judge lineage host engine differs from the measurement anchor")
        # Old v7 embeds v5/v4 claims. New outputs pin the same explicit list for
        # their immediate base. Validate against this walk; never open extra paths.
        for field in ("bootstrap_lineage", "recovery_lineage"):
            if field not in value:
                continue
            claims = value[field]
            if value["schema_version"] not in {SCHEMA, CONTEXT_ADMISSION_SCHEMA} or index + 1 >= len(values):
                raise ContractError("embedded recovery lineage has no judge-overlay base")
            if field == "bootstrap_lineage":
                if (value.get("bootstrap_base_schema") != BOOTSTRAP_BASE_SCHEMA
                        or values[index + 1]["schema_version"] != BOOTSTRAP_BASE_SCHEMA):
                    raise ContractError("embedded bootstrap lineage base schema differs")
            elif (values[index + 1]["schema_version"] == BOOTSTRAP_BASE_SCHEMA
                  or "bootstrap_lineage" in value):
                raise ContractError("embedded recovery lineage uses the wrong schema field")
            if not isinstance(claims, list) or len(claims) != len(layers[index + 2:]):
                raise ContractError("embedded recovery lineage is incomplete")
            for claim, expected in zip(claims, layers[index + 2:]):
                if (not isinstance(claim, dict)
                        or set(claim) != {"manifest", "sha256", "schema_version"}
                        or directory(claim.get("manifest"), "embedded manifest") != Path(expected["manifest"])
                        or claim["sha256"] != expected["sha256"]
                        or claim["schema_version"] != expected["schema_version"]):
                    raise ContractError("embedded recovery lineage claim differs from the walked manifest")
        if "bootstrap_base_schema" in value and "bootstrap_lineage" not in value:
            raise ContractError("embedded bootstrap lineage claims are missing")

    binding = {
        "generation_engine_fingerprint": BOOTSTRAP_GENERATION_ENGINE,
        "base_host_engine_fingerprint": host,
    }
    if base_manifest["schema_version"] == BOOTSTRAP_BASE_SCHEMA:
        binding.update(bootstrap_base_schema=BOOTSTRAP_BASE_SCHEMA, bootstrap_lineage=layers[1:])
    else:
        binding["recovery_lineage"] = layers[1:]
    return binding


def validate_answer(generation):
    if generation.get("status") != "generated" or generation.get("protocol") != PROTOCOL:
        raise ContractError("judge recovery requires an already generated answer")
    for key in ("patch", "trace"):
        path = Path(generation[key + "_file"])
        checksum = generation.get(key + "_sha256", "")
        if not HEX256.fullmatch(checksum) or not path.is_file() or file_hash(path) != checksum:
            raise ContractError("original answer " + key + " bytes changed")


def validate_member(member, config):
    model = member["judge_model"]
    if model not in config["models"] or type(member.get("passed")) is not bool:
        raise ContractError("cached accepted judge member is not a fixed boolean vote")
    if verdict_passes(member.get("verdict", {})) != member["passed"]:
        raise ContractError("cached accepted vote disagrees with its verdict")
    if type(member.get("http_status")) is not int or not 200 <= member["http_status"] < 300:
        raise ContractError("cached accepted judge did not return HTTP success")
    raw = member.get("raw_response")
    if (not isinstance(raw, dict) or raw.get("model") not in
            config.get("expected_response_models", {}).get(model, [model])):
        raise ContractError("cached accepted response lacks its declared model identity")
    if json.loads(member["raw_response_text"]) != raw:
        raise ContractError("cached accepted response text/JSON representations differ")
    choice = raw["choices"][0]
    if choice.get("finish_reason") not in {"stop", "end_turn"}:
        raise ContractError("cached accepted response is incomplete")
    if parse_judge_verdict(choice["message"]["content"]) != member["verdict"]:
        raise ContractError("cached accepted raw response differs from the retained verdict")


def retryable_member(member):
    """A retained malformed substantive verdict is not automatically retried."""
    if "passed" in member:
        return False
    if member.get("http_status") in {408, 409, 429, 500, 502, 503, 504}:
        return True
    error = member.get("error", "")
    if not isinstance(error, str):
        return False
    return (error == "InfrastructureError: judge model identity mismatch"
            or error.startswith(("TimeoutError:", "URLError:", "ConnectionError:")))


def validate_partial(judgment, generation, config, engine):
    validate_answer(generation)
    if "context_admission" in judgment:
        raise ContractError("context first-attempt outcomes are consumed; reuse their source-scoped context journal")
    if judgment.get("status") != "infra_error":
        raise ContractError("completed judgments are never resampled")
    for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256"):
        if judgment.get(key) != generation.get(key):
            raise ContractError("partial judgment differs from the original answer in " + key)
    if judgment.get("engine_fingerprint") != engine:
        raise ContractError("judge recovery must use the original judgment engine")
    expected = canonical_hash({"rubric": RUBRIC, "version": RUBRIC_VERSION, "config": config})
    if judgment.get("judge_fingerprint") != expected:
        raise ContractError("judge recovery changes the fixed prompt/rubric/model configuration")
    if (judgment.get("evidence_complete") is not True
            or not HEX256.fullmatch(judgment.get("evidence_sha256", ""))):
        raise ContractError("context-cap or missing-evidence errors require a separate protocol decision")
    members = judgment.get("panel", [])
    if (len(config["models"]) != 3 or len(set(config["models"])) != 3
            or [row.get("judge_model") for row in members] != config["models"]):
        raise ContractError("partial judgment is not the exact fixed three-member panel")
    for member in members:
        if "passed" in member:
            validate_member(member, config)


class SourceMemberClaim:
    """One profile attempt at the hash-bound original answer, not per output."""
    def __init__(self, generation, previous, directory, binding):
        # No CLI/config/selection claim-directory override exists. The original
        # generation hash pins both absolute file names before reaching this code.
        trace, patch = (Path(generation[key + "_file"]) for key in ("trace", "patch"))
        if (not trace.is_absolute() or not patch.is_absolute()
                or trace.is_symlink() or patch.is_symlink()
                or trace.parent.resolve() != patch.parent.resolve()):
            raise ContractError("profile source claim requires co-located absolute original answer files")
        source = trace.parent.resolve(strict=True)
        directory = Path(directory)
        if directory.name != "members":
            raise ContractError("profile source claim requires the fixed output/members journal")
        output = directory.parent.resolve()
        self.local = output / "members" / component(binding["judge_model"])
        key = canonical_hash({
            "generation_sha256": canonical_hash(generation),
            "judgment_sha256": canonical_hash(previous),
            "original_member_sha256": binding["original_member_sha256"],
            "judge_model": binding["judge_model"], "model": generation["model"],
        })
        self.root = source / ".judge-member-recovery-claims-v1" / key
        self.binding = binding
        self.owner = {
            "schema_version": REJECTED_MEMBER_CLAIM_SCHEMA, "key": key,
            "source_answer_directory": str(source), "owner_output": str(output),
            "binding": copy.deepcopy(binding),
        }
        self._check_paths()

    def _check_paths(self):
        for path in (
            self.root.parent, self.root, self.root / "claim.lock",
            self.root / "claim.json", self.root / "started.json", self.root / "response.json",
            self.local.parent, self.local, self.local / "request.lock",
            self.local / "started.json", self.local / "response.json",
        ):
            if path.is_symlink():
                raise ContractError("profile source claim/journal path must not be a symlink")

    @staticmethod
    def _write(path, value):
        atomic_json(path, value)
        # atomic_json fsyncs the file; also persist the metadata directory entry
        # before any invocation can leave the process.
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _check_owner(self, *, required):
        path = self.root / "claim.json"
        if path.exists():
            if json.loads(path.read_text()) != self.owner:
                raise ContractError("profile source claim belongs to another owner_output or profile binding")
        elif required:
            raise ContractError("profile cached result has no retained source claim")

    def check_owner(self, *, required=False):
        """Read-only preflight; the invocation path repeats this under the lock."""
        self._check_paths()
        if not self.root.exists():
            if required:
                raise ContractError("profile cached result has no retained source claim")
            return
        with file_lock(self.root / "claim.lock"):
            self._check_paths()
            self._check_owner(required=required)

    def invoke_once(self, model, prompt, config, invoke):
        self.root.mkdir(parents=True, exist_ok=True)
        # Persist new directories as well as files. This claim has no timeout,
        # stale-PID cleanup, replacement owner or automatic reset.
        for directory in (self.root.parent, self.root.parent.parent):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._check_paths()
        with file_lock(self.root / "claim.lock"):
            self._check_paths()
            self._check_owner(required=False)
            claim = self.root / "claim.json"
            shared_start, shared_response = self.root / "started.json", self.root / "response.json"
            if not claim.exists():
                if shared_start.exists() or shared_response.exists():
                    raise ContractError("profile source claim has unowned invocation records")
                self._write(claim, self.owner)
            self.local.mkdir(parents=True, exist_ok=True)
            with file_lock(self.local / "request.lock"):
                self._check_paths()
                local_start, local_response = self.local / "started.json", self.local / "response.json"
                if shared_start.exists() and json.loads(shared_start.read_text()) != self.owner:
                    raise ContractError("profile source started record has a different binding")
                if local_start.exists() and json.loads(local_start.read_text()) != self.binding:
                    raise ContractError("unfinished judge request has a different binding")
                responses = []
                for path, started in ((shared_response, shared_start), (local_response, local_start)):
                    if not path.exists():
                        continue
                    cached = json.loads(path.read_text())
                    if (not started.exists() or not isinstance(cached, dict)
                            or cached.get("binding") != self.binding or "member" not in cached):
                        raise ContractError("journaled profile response has a different or missing request binding")
                    responses.append(cached)
                if len(responses) == 2 and responses[0] != responses[1]:
                    raise ContractError("profile source/local recorded responses differ")
                already_started = shared_start.exists() or local_start.exists()
                if not shared_start.exists():
                    self._write(shared_start, self.owner)
                if not local_start.exists():
                    self._write(local_start, self.binding)
                if responses:
                    # A V1 local response or a V2 source response is a consumed
                    # attempt. Copy its exact bytes-as-JSON, never re-invoke.
                    cached = responses[0]
                    if not shared_response.exists():
                        self._write(shared_response, cached)
                    if not local_response.exists():
                        self._write(local_response, cached)
                    return cached["member"]
                if already_started:
                    return {"judge_model": model, "error": "request outcome not recorded; no automatic repeat"}
                try:
                    member = invoke(model, prompt, config)
                except Exception as exc:
                    member = {"judge_model": model, "error": type(exc).__name__ + ": " + str(exc)}
                cached = {"binding": self.binding, "member": member}
                self._write(shared_response, cached)
                self._write(local_response, cached)
                return member


class JournaledJudge:
    """Persist each invocation intent/result so a restart cannot silently reroll."""
    def __init__(self, previous, selected, directory, *, invoke=invoke_judge,
                 selection=None, generation=None):
        self.previous = previous
        self.selected = set(selected)
        self.directory = Path(directory)
        self.invoke = invoke
        self.members = {member["judge_model"]: member for member in previous["panel"]}
        self.selection, self.generation = selection, generation

    def profile_claim(self, config):
        if self.selection is None:
            return None
        profile = validate_rejected_member_profile(self.selection, self.generation, self.previous, config)
        if profile is None:
            return None
        model = profile["judge_models"][0]
        binding = {
            "judge_model": model, "evidence_sha256": self.previous["evidence_sha256"],
            "judge_fingerprint": self.previous["judge_fingerprint"],
            "original_member_sha256": canonical_hash(self.members[model]),
            "rejected_member_profile": copy.deepcopy(self.selection["rejected_member_profile"]),
            "generation_sha256": canonical_hash(self.generation),
            "judgment_sha256": canonical_hash(self.previous),
        }
        return SourceMemberClaim(self.generation, self.previous, self.directory, binding)

    def __call__(self, model, prompt, config):
        if canonical_hash(prompt) != self.previous["evidence_sha256"]:
            raise ContractError("regenerated complete evidence differs before any new judge request")
        expected = canonical_hash({"rubric": RUBRIC, "version": RUBRIC_VERSION, "config": config})
        if expected != self.previous["judge_fingerprint"]:
            raise ContractError("fixed judge request configuration changed")
        original = self.members[model]
        if "passed" in original:
            raise ContractError("accepted judge was unexpectedly scheduled for another invocation")
        if model not in self.selected:
            return copy.deepcopy(original)
        profile = (validate_rejected_member_profile(self.selection, self.generation, self.previous, config)
                   if self.selection is not None else None)
        if not retryable_member(original) and not (profile and profile["judge_models"] == [model]):
            raise ContractError("only explicit transport/identity failures may be retried")
        if profile is not None:
            return self.profile_claim(config).invoke_once(model, prompt, config, self.invoke)
        root = self.directory / component(model)
        root.mkdir(parents=True, exist_ok=True)
        binding = {
            "judge_model": model, "evidence_sha256": canonical_hash(prompt),
            "judge_fingerprint": expected, "original_member_sha256": canonical_hash(original),
        }
        with file_lock(root / "request.lock"):
            result_file, started = root / "response.json", root / "started.json"
            if result_file.is_file():
                cached = json.loads(result_file.read_text())
                if cached["binding"] != binding:
                    raise ContractError("journaled judge member has a different request binding")
                return cached["member"]
            if started.exists():
                if json.loads(started.read_text()) != binding:
                    raise ContractError("unfinished judge request has a different binding")
                return {"judge_model": model, "error": "request outcome not recorded; no automatic repeat"}
            atomic_json(started, binding)
            try:
                member = self.invoke(model, prompt, config)
            except Exception as exc:
                member = {"judge_model": model, "error": type(exc).__name__ + ": " + str(exc)}
            atomic_json(result_file, {"binding": binding, "member": member})
            return member


@contextmanager
def source_judge_job(services, task, recipe, repository, name):
    """No runtime image/build/dependency restore for a source-only review."""
    source_size = services.source_bytes(task, repository)
    module_size = 0
    seen = set()
    for item in recipe.submodules:
        if not item.get("omit") and item["asset"]["sha256"] not in seen:
            module_size += asset_descriptor(item["asset"], Path(services.config["asset_root"]))["archive"]["input_bytes"]
            seen.add(item["asset"]["sha256"])
    request = ResourceRequest(name=name + ":" + task["id"], cpus=1, memory_bytes=2 * GiB,
                              scratch_bytes=6 * (source_size + module_size) + GiB,
                              heavy=source_size + module_size > GiB)
    with services.gate.acquire(
            request, uses_containers=False, source_catalog=services.sources, task=task,
            timeout=services.config.get("execution", {}).get("admission_timeout_seconds", 600)) as lease:
        pool = AssetPool(services.cache, services.scratch, lease["lease"], Path(services.config["asset_root"]))
        CURRENT_JOB.get().update(asset_pool=pool, submodules=recipe.submodules,
                                 scratch_paths=[], workspace_exclusions={})
        try:
            yield lease
        finally:
            pool.close()


def recover_one(request, *, invoke=invoke_judge):
    config = load_config(Path(request["config"]))
    task, row, selection = request["task"], copy.deepcopy(request["row"]), request["selection"]
    generation, previous = row["generation"], row["judgment"]
    engine = engine_identity()["fingerprint"]
    validate_partial(previous, generation, config["judge"], engine)
    if (selection["id"] != task["id"] or selection["model"] != generation["model"]
            or row["id"] != task["id"] or row["model"] != generation["model"]):
        raise ContractError("selected task-model identity changed")
    if canonical_hash(task) != generation["task_fingerprint"]:
        raise ContractError("recovery task changed")
    if canonical_hash(generation) != selection["generation_sha256"]:
        raise ContractError("selected generation object changed")
    if canonical_hash(previous) != selection["judgment_sha256"]:
        raise ContractError("selected original partial judgment changed")
    if canonical_hash(row["correctness"]) != selection["correctness_sha256"]:
        raise ContractError("completed correctness measurement changed")
    if row["correctness"].get("status") not in TERMINAL_CORRECTNESS:
        raise ContractError("judge-only recovery must preserve a completed correctness result")
    for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256", "runtime_fingerprint"):
        if row["correctness"].get(key) != generation.get(key):
            raise ContractError("completed correctness differs from the answer in " + key)
    rejected_profile = validate_rejected_member_profile(selection, generation, previous, config["judge"])
    if (not selection["judge_models"] or len(set(selection["judge_models"])) != len(selection["judge_models"])
            or not set(selection["judge_models"]) <= {member["judge_model"] for member in previous["panel"]}
            or any(not retryable_member(next(member for member in previous["panel"]
                                            if member["judge_model"] == model))
                   and not (rejected_profile and rejected_profile["judge_models"] == [model])
                   for model in selection["judge_models"])):
        raise ContractError("selection includes an accepted/unretryable judge")
    recipe = RuntimeRecipe.from_dict(request["recipe"])
    if recipe.fingerprint != generation["runtime_fingerprint"]:
        raise ContractError("source submodule/runtime inputs differ from the original generation")
    output = Path(request["output"])
    output.mkdir(parents=True, exist_ok=True)
    result_file = output / "result.json"
    callback = JournaledJudge(previous, selection["judge_models"], output / "members", invoke=invoke,
                             selection=selection, generation=generation)
    if rejected_profile is not None:
        callback.profile_claim(config["judge"]).check_owner(required=result_file.is_file())
    if result_file.is_file():
        saved = json.loads(result_file.read_text())
        if saved["selection"] != selection:
            raise ContractError("saved judge repair has a different selection")
        return saved
    previous_file = output / "judgment.json"
    if not previous_file.exists():
        atomic_json(previous_file, previous)
    services = ExecutionServices({**config, "out_dir": str(output)})
    if any(task["id"] in value.get("request", {}).get("name", "")
           for value in services.gate.status().get("leases", {}).values()):
        raise ContractError("the selected task still has an active execution lease")
    repository = Path(config["repo_cache"]) / task["repo"].replace("/", "__")
    before_generation, before_correctness = canonical_hash(generation), canonical_hash(row["correctness"])
    with source_judge_job(services, task, recipe, repository, "judge-member-recovery"):
        judgment = judge_candidate(task, generation, repository, config["judge"], previous_file,
                                   invoke=callback, retry_infra=True)
    if judgment.get("evidence_sha256") != previous["evidence_sha256"]:
        raise ContractError("judge recovery did not preserve the complete evidence")
    incoming = {member["judge_model"]: member for member in judgment["panel"]}
    for original in previous["panel"]:
        if "passed" in original and incoming.get(original["judge_model"]) != original:
            raise ContractError("judge recovery changed an already accepted member")
    for member in judgment["panel"]:
        if "passed" in member:
            validate_member(member, config["judge"])
    if canonical_hash(generation) != before_generation or canonical_hash(row["correctness"]) != before_correctness:
        raise ContractError("judge recovery altered the answer or completed correctness")
    validate_answer(generation)
    row["judgment"] = judgment
    record = {
        "selection": selection, "row": row, "original_judgment_engine_fingerprint": engine,
        "additional_generation_calls": 0, "additional_correctness_calls": 0,
        "judge_request_attempts": len(list((output / "members").glob("*/started.json"))),
        "preserved_accepted_members": [member["judge_model"] for member in previous["panel"] if "passed" in member],
        "original_judgment": previous, "original_judgment_sha256": canonical_hash(previous),
    }
    atomic_json(result_file, record)
    return record


def read_base_rows(base, tasks, models):
    base = Path(base)
    manifest_file = base / "experiment-manifest.json"
    if manifest_file.is_file():
        manifest = json.loads(manifest_file.read_text())
        if manifest.get("schema_version") == CONTEXT_ADMISSION_SCHEMA:
            if "publication_policy" in manifest:
                base_engine_binding(base, manifest, tasks, models)
                return read_context_publication(base, manifest, tasks, models)[0]
            if (base / "current").exists() or (base / "current").is_symlink():
                raise ContractError("context atomic publication cannot omit its explicit V2 policy")
    return measurement_rows({name + ".jsonl": (base / (name + ".jsonl")).read_bytes()
                             for name in ("generation_results", "correctness", "judgments")}, tasks, models)


def validate_selections(selections, tasks, models):
    if not isinstance(selections, list) or not selections:
        raise ContractError("judge recovery requires an explicit nonempty selection")
    keys = []
    for row in selections:
        selected_rejected_profile(row)
        if not isinstance(row, dict) or row.get("id") not in tasks or row.get("model") not in models:
            raise ContractError("judge selection has a foreign task-model identity")
        if any(not isinstance(row.get(name), str) or not HEX256.fullmatch(row[name])
               for name in ("generation_sha256", "judgment_sha256", "correctness_sha256", "judge_engine")):
            raise ContractError("judge selection requires complete SHA256 object bindings")
        members = row.get("judge_models")
        if (not isinstance(members, list) or not members or not all(isinstance(name, str) for name in members)
                or len(set(members)) != len(members)):
            raise ContractError("judge selection requires unique explicit failed members")
        keys.append((row["id"], row["model"]))
    if len(keys) != len(set(keys)):
        raise ContractError("judge selection duplicates a task-model pair")


def publish(output, tasks, models, base_rows, repairs, manifest, errors):
    rows = copy.deepcopy(base_rows)
    for pair, repair in repairs.items():
        if pair not in rows:
            continue
        row, selected = rows[pair], repair["selection"]
        if canonical_hash(row.get("generation", {})) != selected["generation_sha256"]:
            errors["::".join(pair)] = "base generation changed; old judgment repair not overlaid"
            continue
        if canonical_hash(row.get("judgment", {})) != selected["judgment_sha256"]:
            if row.get("judgment", {}).get("status") != "completed":
                errors["::".join(pair)] = "base partial judgment changed; repair not overlaid"
            continue
        if canonical_hash(row.get("correctness", {})) != selected["correctness_sha256"]:
            errors["::".join(pair)] = "base correctness changed; repair not overlaid"
            continue
        row["judgment"] = repair["row"]["judgment"]
    columns = {name: [row[name] for row in rows.values() if name in row]
               for name in ("generation", "correctness", "judgment")}
    table, report = make_report(tasks, models, columns["generation"], columns["correctness"], columns["judgment"])
    for name, values in (("generation_results", columns["generation"]), ("correctness", columns["correctness"]),
                         ("judgments", columns["judgment"]), ("results", table)):
        write_jsonl(output / (name + ".jsonl"), values)
    report.update(
        expected_pairs=len(tasks) * len(models), measured_generations=len(columns["generation"]),
        source_run=manifest["source_run"], source_recovery_run=manifest["base_run"],
        generation_engine_fingerprint=manifest["generation_engine_fingerprint"],
        host_recovery_engine_fingerprint=manifest["base_host_engine_fingerprint"],
        judgment_recovery_records=len(repairs), additional_generation_calls=0, additional_correctness_calls=0,
        additional_judge_calls=sum(record["judge_request_attempts"] for record in repairs.values()),
        judgment_recovery_engines=sorted({record["original_judgment_engine_fingerprint"] for record in repairs.values()}),
        recovery_admission_errors=errors,
    )
    atomic_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--base-run", type=Path)
    parser.add_argument("--repair-selection", type=Path)
    parser.add_argument("--engine-sources", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--child-request", type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30)
    args = parser.parse_args()
    if args.child_request:
        record = recover_one(json.loads(args.child_request.read_text()))
        print(json.dumps({"id": record["row"]["id"], "judgment": record["row"]["judgment"]["status"],
                          "judge_request_attempts": record["judge_request_attempts"],
                          "preserved": record["preserved_accepted_members"]}), flush=True)
        return 0
    if any(value is None for value in (args.config, args.base_run, args.repair_selection,
                                      args.engine_sources, args.out_dir)) or args.poll_seconds < 1:
        parser.error("config, base-run, selection, engine-sources, independent output and bounded polling are required")
    base, output = args.base_run.resolve(), args.out_dir.resolve()
    if base == output or base.is_relative_to(output) or output.is_relative_to(base):
        raise ContractError("judge recovery requires an independent output directory")
    config = load_config(args.config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    base_manifest_file = base / "experiment-manifest.json"
    base_manifest_bytes = base_manifest_file.read_bytes()
    base_manifest = json.loads(base_manifest_bytes)
    if base_manifest["task_ids"] != list(tasks) or base_manifest["tasks_fingerprint"] != canonical_hash(tasks):
        raise ContractError("judge recovery changes the full base task scope")
    models = base_manifest["models"]
    engine_binding = base_engine_binding(base, base_manifest, tasks, models)
    source = Path(base_manifest["source_run"])
    if json.loads((source / "experiment-config.json").read_text())["judge"] != sanitize_config(config)["judge"]:
        raise ContractError("judge recovery changes the original grading configuration")
    selections = json.loads(args.repair_selection.read_text())
    engines = json.loads(args.engine_sources.read_text())
    validate_selections(selections, tasks, models)
    manifest = {
        "schema_version": SCHEMA, "base_run": str(base),
        "base_manifest_sha256": hashlib.sha256(base_manifest_bytes).hexdigest(),
        "source_run": str(source),
        "source_manifest_sha256": (base_manifest["source_manifest_sha256"]
                                   if (base_manifest.get("schema_version") == BOOTSTRAP_BASE_SCHEMA
                                       or "recovery_lineage" in engine_binding)
                                   else file_hash(source / "experiment-manifest.json")),
        "task_ids": list(tasks), "tasks_fingerprint": canonical_hash(tasks), "models": models,
        "expected_pairs": len(tasks) * len(models), "selection_sha256": file_hash(args.repair_selection),
        "engine_sources_sha256": file_hash(args.engine_sources), "controller_sha256": file_hash(Path(__file__)),
        **engine_binding,
        "judge_fingerprint": canonical_hash(config["judge"]), "additional_generation_calls": 0,
        "additional_correctness_calls": 0,
    }
    bound_inputs = (
        (base_manifest_file, manifest["base_manifest_sha256"]),
        (source / "experiment-manifest.json", manifest["source_manifest_sha256"]),
        (args.repair_selection, manifest["selection_sha256"]),
        (args.engine_sources, manifest["engine_sources_sha256"]),
    ) + tuple((Path(item["manifest"]), item["sha256"])
              for field in ("bootstrap_lineage", "recovery_lineage") for item in manifest.get(field, []))
    with output_lock(output):
        path = output / "experiment-manifest.json"
        if path.exists() and json.loads(path.read_text()) != manifest:
            raise ContractError("judge-recovery bindings changed; use a new explicit output")
        atomic_json(path, manifest)
        attempted, repairs, errors = set(), {}, {}
        for selected in selections:
            pair = selected["id"], selected["model"]
            directory = output / "repairs" / component(pair[1]) / component(pair[0]) / selected["generation_sha256"][:16]
            result = directory / "result.json"
            if result.exists():
                value = json.loads(result.read_text())
                if value["selection"] != selected:
                    raise ContractError("cached judge repair has changed selection bindings")
                repairs[pair] = value
        while True:
            for file, digest in bound_inputs:
                if file_hash(file) != digest:
                    raise ContractError("live base/selection/engine bindings changed")
            base_rows = read_base_rows(base, tasks, models)
            recipes = load_recipes(source / "fixed-runtime-recipes.jsonl")
            for selected in selections:
                pair = selected["id"], selected["model"]
                if pair in attempted or pair in repairs or pair not in base_rows:
                    continue
                attempted.add(pair)
                row = base_rows[pair]
                try:
                    validate_partial(row["judgment"], row["generation"], config["judge"], selected["judge_engine"])
                    validate_rejected_member_profile(selected, row["generation"], row["judgment"], config["judge"])
                    if selected["judge_engine"] not in engines:
                        raise ContractError("original judge engine has no explicit retained source")
                    directory = output / "repairs" / component(pair[1]) / component(pair[0]) / selected["generation_sha256"][:16]
                    request_file = directory / "request.json"
                    request = {"config": str(args.config.resolve()), "task": tasks[pair[0]], "row": row,
                               "selection": selected, "recipe": asdict(recipes[pair[0]]), "output": str(directory)}
                    if request_file.exists():
                        if json.loads(request_file.read_text()) != request:
                            raise ContractError("retained child request binding changed")
                    else:
                        atomic_json(request_file, request)
                    command = [sys.executable, str(Path(__file__).resolve()), "--child-request", str(request_file)]
                    engine_source = str(Path(engines[selected["judge_engine"]]).resolve())
                    with (directory / "child.log").open("a") as log:
                        process = subprocess.Popen(command, env={**os.environ, "REVIEWBENCH_ENGINE_SRC": engine_source,
                                                               "PYTHONPATH": engine_source, "PYTHONDONTWRITEBYTECODE": "1"},
                                                   stdout=log, stderr=subprocess.STDOUT)
                        atomic_json(directory / "child-process.json", {"pid": process.pid, "command": command})
                        status = process.wait()
                    if status:
                        raise ContractError("judge recovery child exit " + str(status) + "; see " + str(directory / "child.log"))
                    repairs[pair] = json.loads((directory / "result.json").read_text())
                except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
                    errors["::".join(pair)] = type(exc).__name__ + ": " + str(exc)
            for file, digest in bound_inputs:
                if file_hash(file) != digest:
                    raise ContractError("source/selection bindings changed during judge recovery")
            base_rows = read_base_rows(base, tasks, models)
            for file, digest in bound_inputs:
                if file_hash(file) != digest:
                    raise ContractError("source/selection bindings changed before publication")
            report = publish(output, tasks, models, base_rows, repairs, manifest, dict(errors))
            if not args.watch or report["complete"]:
                print(json.dumps(report, indent=2), flush=True)
                return 0 if report["complete"] else 2
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
