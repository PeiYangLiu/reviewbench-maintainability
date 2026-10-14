"""Immutable inheritance and cached publications for terminal host recovery.

This module never invokes a model, judge, compiler, or correctness runner.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.reporting import make_report
from reviewbench_maintainability.agentic.evaluation import (
    RUBRIC, RUBRIC_VERSION, parse_judge_verdict, verdict_passes,
)
from recover_agentic_measurements import validate_completed_judgment

FILES = ("generation_results.jsonl", "correctness.jsonl", "judgments.jsonl", "results.jsonl", "report.json")
TERMINAL_CHECKS = {"pass", "fail", "scope_error", "apply_error"}


def require(value, message):
    if not value:
        raise ContractError("host terminal ownership: " + message)


def read_bytes(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "missing regular input: " + str(path))
    before = path.stat()
    with path.open("rb") as stream:
        data = stream.read(64 * 1024**2 + 1)
    after = path.stat()
    require(len(data) <= 64 * 1024**2 and
            (before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), "input changed during snapshot read")
    return data


def reference(row):
    require(isinstance(row, dict) and isinstance(row.get("path"), str) and
            Path(row["path"]).is_absolute() and re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", "")),
            "invalid immutable reference")
    data = read_bytes(row["path"])
    require(hashlib.sha256(data).hexdigest() == row["sha256"] and
            ("bytes" not in row or len(data) == row["bytes"]), "immutable reference changed")
    return json.loads(data)


def load_inherited(path, source, source_manifest, tasks, models):
    """No imported attempt becomes eligible again when a controller changes."""
    document = json.loads(read_bytes(path))
    require(document["schema_version"] == "reviewbench-host-recovery-inheritance/v1" and
            Path(document["source_run"]).resolve() == Path(source).resolve() and
            document["new_attempt_allowances"] == 0, "inheritance changes source or budgets")
    require(reference(document["source_manifest"]) == source_manifest, "source manifest changed")
    original = reference(document["original_manifest"])
    require(original["source_manifest_sha256"] == document["source_manifest"]["sha256"] and
            original["generation_engine_fingerprint"] == source_manifest["engine_fingerprint"] and
            original["task_ids"] == list(tasks) and original["models"] == models and
            original["expected_pairs"] == len(tasks) * len(models), "foreign inherited owner")
    result = {}
    for item in document["records"]:
        value = reference(item["copy"])
        require(item["copy"]["sha256"] == item["original"]["sha256"], "inheritance is not a byte copy")
        row = value["row"]
        key = row["id"], row["model"], value["source_generation_fingerprint"]
        require(key[0] in tasks and key[1] in models and key not in result and
                re.fullmatch(r"[0-9a-f]{64}", key[2]) and
                value["host_engine_fingerprint"] == original["host_recovery_engine_fingerprint"] and
                (item["id"], item["model"], item["source_generation_fingerprint"]) == key,
                "duplicate or foreign inherited attempt")
        result[key] = value
    require(len(result) == len(document["records"]), "inherited attempt set changed")
    return result, {"path": str(Path(path).resolve()), "sha256": file_hash(Path(path)),
                    "count": len(result), "original_manifest": document["original_manifest"]}


def original_owned_row(original, selected):
    """Generated source episodes own even failed/partial panels; never resample."""
    row = deepcopy(selected)
    if original.get("generation", {}).get("status") == "generated":
        require(row.get("generation") == original["generation"], "correctness recovery changed generation")
        if "judgment" in original:
            row["judgment"] = deepcopy(original["judgment"])
        else:
            row.pop("judgment", None)
    return row


def validate_inherited_sources(inherited, originals, generation_engine):
    for key, record in inherited.items():
        original = originals.get(key[:2])
        require(original is not None and canonical_hash(original.get("generation", {})) == key[2],
                "inherited attempt no longer matches the original source generation")
        generation = record["row"].get("generation", {})
        require(generation.get("engine_fingerprint") == generation_engine and
                generation.get("usage") == original["generation"].get("usage") and
                generation.get("runtime_fingerprint") == original["generation"].get("runtime_fingerprint"),
                "inherited attempt changed the original engine, runtime or consumed budget")


def accepted_primary_members(judgment, generation, config, generation_engine):
    """A rejected sibling does not invalidate a completed original member."""
    require(all(judgment.get(k) == generation.get(k) for k in
                ("id", "model", "protocol", "task_fingerprint", "patch_sha256")) and
            judgment.get("engine_fingerprint") == generation_engine and
            judgment.get("evidence_complete") is True and
            re.fullmatch(r"[0-9a-f]{64}", judgment.get("evidence_sha256", "")) and
            judgment.get("judge_fingerprint") == canonical_hash(
                {"rubric": RUBRIC, "version": RUBRIC_VERSION, "config": config}),
            "partial primary panel lost its original evidence/configuration binding")
    models = config["models"]
    panel = judgment.get("panel", [])
    require([row.get("judge_model") for row in panel] == models, "partial primary panel model order changed")
    accepted = {}
    for member in panel:
        if type(member.get("passed")) is not bool:
            continue
        name = member["judge_model"]
        raw = member.get("raw_response")
        require(type(member.get("http_status")) is int and 200 <= member["http_status"] < 300 and
                isinstance(raw, dict) and raw.get("model") in config["expected_response_models"][name] and
                json.loads(member["raw_response_text"]) == raw,
                "accepted primary member lacks a native expected response")
        choice = raw["choices"][0]
        require(choice.get("finish_reason") in {"stop", "end_turn"} and
                parse_judge_verdict(choice["message"]["content"]) == member["verdict"] and
                verdict_passes(member["verdict"]) == member["passed"],
                "accepted primary member is incomplete or differs from its verdict")
        accepted[name] = member
    return accepted


def effective_tables(base, originals, local, judge_config, generation_engine, inherited=()):
    """Merge new host work without overwriting unrelated downstream repairs."""
    tables = {name: {(r["id"], r["model"]): deepcopy(r) for r in rows} for name, rows in base.items()}
    for name in ("generation", "correctness", "judgment"):
        require(len(tables[name]) == len(base[name]), "duplicate row in base " + name)
    pending, merged, primary, changed_primary, errors, member_corrections = {}, [], [], [], {}, []
    for token, record in local.items():
        key = token[:2]
        original = originals.get(key)
        row = record["row"]
        if not original or canonical_hash(original.get("generation", {})) != token[2]:
            pending["::".join(token)] = "original source version not visible"
            continue
        visible = tables["generation"].get(key)
        if visible not in (original.get("generation"), row.get("generation")):
            pending["::".join(token)] = "base publication has not reached original source version"
            continue
        if not record.get("completed"):
            pending["::".join(token)] = "host transaction has not completed"
            continue
        generation = row.get("generation", {})
        if generation.get("status") not in {"generated", "generation_failed"}:
            pending["::".join(token)] = row.get("error", "host answer recovery is not terminal")
            continue
        require(generation.get("engine_fingerprint") == generation_engine, "generation engine changed")
        if original.get("generation", {}).get("status") == "generated":
            require(generation == original["generation"], "correctness-only recovery altered the answer")
        else:
            require(generation.get("additional_generation_calls") == 0 and generation.get("host_recovery") and
                    generation.get("usage") == original["generation"]["usage"],
                    "serialization recovery lacks preserved answer budget")
            tables["generation"][key] = deepcopy(generation)
        check = row.get("correctness")
        current = tables["correctness"].get(key, {})
        if check and current.get("status") not in TERMINAL_CHECKS:
            tables["correctness"][key] = deepcopy(check)
        elif check and current.get("status") in TERMINAL_CHECKS and check != current:
            pending["::".join(token)] = "base already owns a terminal correctness verdict"
        # A generated source's original or downstream-admitted panel is never
        # replaced by the correctness worker's copy, including partial panels.
        if original.get("generation", {}).get("status") != "generated":
            judgment = row.get("judgment")
            existing = tables["judgment"].get(key)
            if judgment and not existing:
                if judgment.get("status") == "completed":
                    validate_completed_judgment(
                        judgment, generation, judge_config, {record["host_engine_fingerprint"]})
                tables["judgment"][key] = deepcopy(judgment)
            elif judgment and existing != judgment:
                pending["::".join(token)] = "base already owns a serialized-answer panel"
        merged.append(token)
    for key, original in originals.items():
        generation, judgment = original.get("generation"), original.get("judgment", {})
        if (not generation or generation.get("status") != "generated" or
                generation != tables["generation"].get(key)):
            continue
        if judgment.get("status") != "completed":
            if not any(type(member.get("passed")) is bool for member in judgment.get("panel", [])):
                continue
            try:
                accepted = accepted_primary_members(judgment, generation, judge_config, generation_engine)
                current = tables["judgment"].get(key, {})
                require(current.get("evidence_sha256") == judgment["evidence_sha256"] and
                        current.get("judge_fingerprint") == judgment["judge_fingerprint"] and
                        [m.get("judge_model") for m in current.get("panel", [])] == judge_config["models"],
                        "partial primary/current panel evidence or configuration differs")
                changed = [name for name, value in accepted.items()
                           if next(m for m in current["panel"] if m["judge_model"] == name) != value]
                if changed:
                    replacement = deepcopy(current)
                    replacement["panel"] = [deepcopy(accepted.get(member["judge_model"], member))
                                            for member in current["panel"]]
                    if replacement.get("status") == "completed":
                        require(all(type(m.get("passed")) is bool for m in replacement["panel"]),
                                "complete current panel has an incomplete sibling")
                        replacement["maintainability_pass"] = all(m["passed"] for m in replacement["panel"])
                    tables["judgment"][key] = replacement
                    member_corrections.append({"id": key[0], "model": key[1], "judge_models": changed,
                                               "source_judgment_fingerprint": canonical_hash(judgment)})
                    changed_primary.append(key)
            except (ContractError, KeyError, ValueError, TypeError, IndexError, StopIteration) as exc:
                errors["::".join(key)] = str(exc)
            continue
        try:
            validate_completed_judgment(judgment, generation, judge_config, {generation_engine})
        except (ContractError, KeyError, ValueError, TypeError, IndexError) as exc:
            errors["::".join(key)] = str(exc)
            continue
        primary.append(key)
        if tables["judgment"].get(key) != judgment:
            changed_primary.append(key)
            tables["judgment"][key] = deepcopy(judgment)
    # Keep every archived duplicate as incurred cost. These are observed
    # distinct successful response pairs, not an estimate of total traffic.
    duplicates = []
    for record in inherited:
        row = record["row"]
        key = row["id"], row["model"]
        original = originals.get(key, {})
        if original.get("generation") != row.get("generation"):
            continue
        original_panel = {m.get("judge_model"): m for m in original.get("judgment", {}).get("panel", [])}
        for member in row.get("judgment", {}).get("panel", []):
            first = original_panel.get(member.get("judge_model"), {})
            a, b = first.get("raw_response", {}).get("id"), member.get("raw_response", {}).get("id")
            if (a and b and a != b and first.get("http_status") == member.get("http_status") == 200 and
                    type(first.get("passed")) is bool and type(member.get("passed")) is bool):
                duplicates.append({"id": key[0], "model": key[1], "judge_model": member["judge_model"],
                                   "primary_response_id": a, "duplicate_response_id": b,
                                   "primary_passed": first["passed"], "duplicate_passed": member["passed"]})
    return tables, {"merged_local": [list(t) for t in merged], "pending": pending,
                    "primary_panels": [list(k) for k in primary],
                    "changed_primary_panels": [list(k) for k in changed_primary],
                    "primary_member_corrections": member_corrections,
                    "provenance_errors": errors, "observed_duplicate_responses": duplicates}


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _jsonl(rows):
    return b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in rows)


def publish_combined(output, base_owner, base_manifest_sha, tasks, models, originals, local, inherited,
                     manifest, judge_config):
    output, base_owner = Path(output).resolve(), Path(base_owner).resolve()
    require(output != base_owner and not output.is_relative_to(base_owner) and
            not base_owner.is_relative_to(output), "publication overlaps its source")
    require(file_hash(base_owner / "experiment-manifest.json") == base_manifest_sha, "base owner changed")
    epoch = (base_owner / "current").resolve(strict=True)
    require(epoch.parent == base_owner / "publications", "base pointer escapes its owner")
    data = {name: read_bytes(epoch / name) for name in FILES}
    parsed = {name: [json.loads(line) for line in data[name].splitlines() if line.strip()]
              for name in FILES if name.endswith(".jsonl")}
    base_report = json.loads(data["report.json"])
    require(base_report["expected_pairs"] == len(tasks) * len(models) and
            base_report["task_count"] == len(tasks) and
            base_report["generation_engine_fingerprint"] == manifest["generation_engine_fingerprint"],
            "base scope or generation engine changed")
    base = {"generation": parsed["generation_results.jsonl"], "correctness": parsed["correctness.jsonl"],
            "judgment": parsed["judgments.jsonl"]}
    tables, changes = effective_tables(base, originals, local, judge_config,
                                       manifest["generation_engine_fingerprint"], inherited.values())
    ordered = {name: [values[key] for key in sorted(values)] for name, values in tables.items()}
    results, report = make_report(tasks, models, ordered["generation"], ordered["correctness"], ordered["judgment"])
    before = {(r["id"], r["model"]): r for r in parsed["results.jsonl"]}
    affected = {tuple(key[:2]) for key in changes["merged_local"] + changes["changed_primary_panels"]}
    require(set(before) == {(r["id"], r["model"]) for r in results}, "publication denominator changed")
    require(all(before[(r["id"], r["model"])] == r for r in results if (r["id"], r["model"]) not in affected),
            "unrelated result changed during host merge")
    inputs = {
        "base_epoch": str(epoch), "base_files": {name: hashlib.sha256(body).hexdigest() for name, body in data.items()},
        "controller_manifest_sha256": canonical_hash(manifest),
        "new_recovery_records": {"/".join(key): canonical_hash(value) for key, value in local.items()},
        "selected_source_panels": {"/".join(key): canonical_hash(originals[tuple(key)]["judgment"])
                                  for key in changes["primary_panels"]},
        "selected_primary_members": changes["primary_member_corrections"],
        "merge": changes,
    }
    key = canonical_hash(inputs)
    public_manifest = {
        "schema_version": "reviewbench-terminal-host-publication/v1", "base_owner": str(base_owner),
        "base_manifest_sha256": base_manifest_sha, "recovery_owner_manifest": manifest,
        "task_ids": list(tasks), "models": models, "expected_pairs": len(tasks) * len(models),
        "generation_engine_fingerprint": manifest["generation_engine_fingerprint"],
        "publisher_has_generation_judge_SUT_calls": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    owner = output / "experiment-manifest.json"
    if owner.exists():
        require(json.loads(read_bytes(owner)) == public_manifest, "publication owner changed")
    else:
        atomic_json(owner, public_manifest)
    final_report = deepcopy(base_report)
    for field in ("publication_policy", "provenance_publication_policy"):
        if field in final_report:
            final_report["inherited_" + field] = final_report.pop(field)
    final_report.update(report)
    final_report.update(
        schema_version=public_manifest["schema_version"], measured_generations=len(ordered["generation"]),
        expected_pairs=len(tasks) * len(models), terminal_host_recovery_records=len(local),
        original_primary_panels_bound=len(changes["primary_panels"]),
        original_panel_provenance_corrections=len(changes["changed_primary_panels"]),
        original_primary_member_corrections=changes["primary_member_corrections"],
        observed_duplicate_successful_responses=len(changes["observed_duplicate_responses"]),
        publication_inputs_sha256=key, provenance_admission_errors=changes["provenance_errors"],
        pending_host_publication=changes["pending"],
        publication_policy={"owner_output": str(output), "pointer": "current", "epochs": "publications",
                            "mode": "cached-only", "primary_panel": "original source episode, regardless of votes"},
        host_recovery_engine_fingerprint=manifest["host_recovery_engine_fingerprint"],
        additional_generation_calls=0, additional_judge_calls=0, additional_correctness_calls=0,
    )
    payloads = {
        "generation_results.jsonl": _jsonl(ordered["generation"]), "correctness.jsonl": _jsonl(ordered["correctness"]),
        "judgments.jsonl": _jsonl(ordered["judgment"]), "results.jsonl": _jsonl(results),
        "report.json": _json_bytes(final_report),
    }
    # Keep original raw row ordering/format whenever a column is unchanged.
    for filename, field in (("generation_results.jsonl", "generation"), ("correctness.jsonl", "correctness"),
                            ("judgments.jsonl", "judgment")):
        if {(r["id"], r["model"]): r for r in parsed[filename]} == tables[field]:
            payloads[filename] = data[filename]
    if before == {(r["id"], r["model"]): r for r in results}:
        payloads["results.jsonl"] = data["results.jsonl"]
    epochs = output / "publications"
    epochs.mkdir(exist_ok=True)
    target = epochs / key
    if not target.exists():
        temporary = epochs / ("." + key + "." + str(os.getpid()))
        temporary.mkdir()
        for name, body in payloads.items():
            with (temporary / name).open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        atomic_json(temporary / "snapshot.json", {
            "schema_version": "reviewbench-terminal-host-publication-snapshot/v1", "publication_key": key,
            "inputs": inputs, "files": {name: {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
                                       for name, body in payloads.items()},
            "affected_pairs": [list(k) for k in sorted(affected)], "new_generation_judge_SUT_calls": 0,
        })
        temporary.rename(target)
    for name, body in payloads.items():
        require(read_bytes(target / name) == body, "existing publication content changed")
    require(json.loads(read_bytes(target / "snapshot.json"))["inputs"] == inputs, "publication input identity changed")
    for name in (*FILES, "snapshot.json"):
        alias = output / name
        if alias.is_symlink():
            require(os.readlink(alias) == "current/" + name, "foreign publication alias")
        else:
            require(not alias.exists(), "publication alias collides with a file")
            alias.symlink_to("current/" + name)
    current = output / "current"
    if current.is_symlink():
        old = Path(os.readlink(current))
        require(len(old.parts) == 2 and old.parts[0] == "publications", "foreign current pointer")
    else:
        require(not current.exists(), "current pointer is not a symlink")
    temporary = output / (".current." + str(os.getpid()))
    temporary.symlink_to("publications/" + key, target_is_directory=True)
    os.replace(temporary, current)
    return {"epoch": str(target), "report_sha256": file_hash(target / "report.json"),
            "new_recovery_records": len(local), "changed_primary_panels": len(changes["changed_primary_panels"]),
            "duplicate_successful_responses": len(changes["observed_duplicate_responses"]),
            "new_generation_judge_SUT_calls": 0, "provenance_errors": changes["provenance_errors"]}
