"""Add cached first panels to the latest authority, without replacing older repairs."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import uuid

from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from reviewbench_maintainability.agentic.reporting import make_report
from recover_agentic_judgments import validate_context_admission_metadata

SCHEMA = "reviewbench-additive-original-context-publication/v1"
FILES = ("generation_results.jsonl", "correctness.jsonl", "judgments.jsonl", "results.jsonl", "report.json")
ERROR = "ContractError: judge_context_limit: complete evidence does not fit"


def require(value, message):
    if not value:
        raise ContractError("cached original context publication: " + message)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def jsonl(values):
    return b"".join((json.dumps(value, sort_keys=True) + "\n").encode() for value in values)


def keyed(values):
    result = {(row["id"], row["model"]): row for row in values}
    require(len(result) == len(values), "duplicate measurement rows")
    return result


def verified_registry_records(entries, tasks):
    """Read sealed owners and durable claims; never create or dispatch a job."""
    import recover_agentic_context_judgments as context
    import recover_agentic_judgments as retained

    require(isinstance(entries, list), "registry must be a list")
    records, bindings, names, selected_pairs = {}, {}, set(), set()
    fields = {"name", "input_bundle", "profile_file", "profile_sha256",
              "owner_output", "owner_manifest_sha256"}
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == fields, "registry entry shape changed")
        require(entry["name"] not in names, "duplicate registry batch")
        names.add(entry["name"])
        profile_file = Path(entry["profile_file"])
        require(profile_file.is_absolute() and not profile_file.is_symlink()
                and file_hash(profile_file) == entry["profile_sha256"], "registry profile changed")
        profile = json.loads(profile_file.read_text())
        previous = retained.ADDITIONAL_CONTEXT_PROFILES.get(profile["name"])
        if previous is None:
            retained.ADDITIONAL_CONTEXT_PROFILES[profile["name"]] = deepcopy(profile)
        else:
            require(previous == profile, "registry profile conflicts with its sealed controller")
        bundle = context.load_inputs(entry["input_bundle"])
        require(bundle["profile"] == profile, "registry bundle/profile differs")
        owner = Path(entry["owner_output"]).resolve()
        require(file_hash(owner / "experiment-manifest.json") == entry["owner_manifest_sha256"],
                "registry owner manifest changed")
        manifest = json.loads((owner / "experiment-manifest.json").read_text())
        require(manifest["validation_only"] is False, "preview is not a measured original panel")
        retained.validate_context_publication_policy(manifest, owner)
        require(manifest["task_ids"] == list(tasks) and manifest["models"] == ["gpt-5.4-responses"],
                "registry owner changed the original full402 scope")
        require(manifest["context_admission"] == bundle["override"], "registry owner admission changed")
        for selected in bundle["selections"]:
            pair = selected["id"], selected["model"]
            require(pair not in selected_pairs, "duplicate original panel across registry groups")
            selected_pairs.add(pair)
            original = bundle["originals"][pair]
            directory = context.output_directory(owner, selected)
            record_file = directory / "result.json"
            record = json.loads(record_file.read_text())
            context.validate_cached_claims(bundle, selected, original, record, directory)
            key = entry["name"] + "::" + selected["id"]
            records[key] = record
            bindings[key] = {
                **entry, "task_id": selected["id"], "model": selected["model"],
                "source_record": str(record_file), "source_record_sha256": file_hash(record_file),
            }
    return records, bindings


def merge(tables, records):
    """Callers must verify each owner's sealed inputs and durable source claims first."""
    result = deepcopy(tables)
    selected = set()
    for record in records.values():
        old, new = record["original_row"], record["row"]
        key = old["id"], old["model"]
        require(key not in selected, "duplicate original panel across owners")
        selected.add(key)
        require(record["schema_version"] == "reviewbench-context-admission-result/v1", "wrong record schema")
        require(record["judge_request_attempts"] == 3 and type(record["judge_request_attempts"]) is int,
                "first-member intent count changed")
        require(record["additional_generation_calls"] == record["additional_correctness_calls"] == 0,
                "context record attempted another generation or correctness run")
        require((new["id"], new["model"]) == key, "replacement changes task/model")
        for field in ("generation", "correctness"):
            require(key in tables[field] and tables[field][key] == old[field] == new[field],
                    "base does not contain the exact original " + field)
        previous = old["judgment"]
        require(previous.get("status") == "infra_error" and previous.get("panel") == []
                and previous.get("error") == ERROR and previous.get("maintainability_pass") is None,
                "accepted/partial/non-context original panel is not eligible")
        require(tables["judgment"].get(key) == previous, "original base panel changed")
        admission = record["context_admission"]
        require(admission["changed_fields"] == ["max_context_chars"]
                and admission["max_first_attempts_per_member"] == 1, "override changes the first-panel contract")
        validate_context_admission_metadata(admission)
        old_config, new_config = admission["original_judge_config"], admission["admitted_judge_config"]
        require(old_config["max_context_chars"] == 300000
                and new_config == {**old_config, "max_context_chars": new_config["max_context_chars"]}, "override changes more than the cap")
        judgment = new["judgment"]
        require(judgment["context_admission"] == admission and judgment["evidence_complete"] is True,
                "first panel lacks the complete admitted evidence")
        require([member["judge_model"] for member in judgment["panel"]] == old_config["models"],
                "first panel identities/order changed")
        require(len(judgment["panel"]) == 3, "first panel is missing member outcomes")
        require(all(type(member["passed"]) is bool for member in judgment["panel"] if "passed" in member),
                "first panel contains a non-boolean verdict")
        complete = all("passed" in member for member in judgment["panel"])
        require(judgment["status"] == ("completed" if complete else "infra_error"), "incorrect completion status")
        expected_pass = all(member["passed"] for member in judgment["panel"]) if complete else None
        require(judgment["maintainability_pass"] == expected_pass, "negative or unknown outcome was changed")
        for field in ("id", "model", "protocol", "task_fingerprint", "patch_sha256"):
            require(judgment[field] == new["generation"][field], "first panel answer binding changed")
        result["judgment"][key] = deepcopy(judgment)
    return result, selected


def write_file(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def publish(output, base, base_manifest_sha256, tasks, models, records, registrations, publisher):
    output, base = Path(output).resolve(), Path(base).resolve()
    require(output != base and not output.is_relative_to(base) and not base.is_relative_to(output),
            "output overlaps the previous authority")
    require(file_hash(base / "experiment-manifest.json") == base_manifest_sha256, "base manifest changed")
    require(len(tasks) == 402 and models == ["gpt-5.4-responses"], "original full402 scope/model changed")
    require(set(records) == set(registrations), "registered owners and cached records differ")
    epoch = (base / "current").resolve(strict=True)
    require(epoch.parent == base / "publications" and not epoch.is_symlink(), "base pointer escaped its owner")
    raw = {name: (epoch / name).read_bytes() for name in FILES}
    receipt = json.loads((epoch / "snapshot.json").read_text())
    for name, data in raw.items():
        require(hashlib.sha256(data).hexdigest() == receipt["files"][name]["sha256"], "base epoch checksum changed")
    values = {name: [json.loads(line) for line in data.splitlines() if line.strip()]
              for name, data in raw.items() if name.endswith(".jsonl")}
    base_report = json.loads(raw["report.json"])
    columns = {"generation": values["generation_results.jsonl"], "correctness": values["correctness.jsonl"],
               "judgment": values["judgments.jsonl"]}
    expected_table, expected_report = make_report(tasks, models, *[columns[k] for k in columns])
    require(keyed(expected_table) == keyed(values["results.jsonl"]), "base results do not match its measurements")
    require(all(base_report.get(k) == value for k, value in expected_report.items()),
            "base report does not match its measurements")
    tables = {name: keyed(rows) for name, rows in columns.items()}
    combined, selected = merge(tables, records)
    ordered = {name: [rows[key] for key in sorted(rows)] for name, rows in combined.items()}
    results, report = make_report(tasks, models, *[ordered[k] for k in columns])
    before = keyed(values["results.jsonl"])
    require(all(before[(row["id"], row["model"])] == row for row in results
                if (row["id"], row["model"]) not in selected), "unrelated result changed")
    if (output / "current").is_symlink():
        prior_epoch = (output / "current").resolve(strict=True)
        require(prior_epoch.parent == output / "publications", "previous output pointer escaped")
        prior = json.loads((prior_epoch / "snapshot.json").read_text())["inputs"]["registrations"]
        require(all(registrations.get(name) == binding for name, binding in prior.items()),
                "a previously registered first panel was removed or replaced")
    inherited = base_report["all_context_first_attempts_consumed"]
    require(type(inherited) is int and inherited >= 42, "upstream consumed history disappeared")
    consumed = sum(record["judge_request_attempts"] for record in records.values())
    manifest = {
        "schema_version": SCHEMA, "base_owner": str(base), "base_manifest_sha256": base_manifest_sha256,
        "task_ids": list(tasks), "models": models, "expected_pairs": 402,
        "generation_engine_fingerprint": base_report["generation_engine_fingerprint"],
        "publisher": publisher, "publisher_has_generation_judge_SUT_calls": False,
        "registered_panels_policy": "append-only sealed owners; preserve all original outcomes",
    }
    inputs = {
        "base_epoch": str(epoch), "base_hashes": {name: hashlib.sha256(data).hexdigest() for name, data in raw.items()},
        "manifest_sha256": hashlib.sha256(json_bytes(manifest)).hexdigest(),
        "registrations": registrations, "records": {name: canonical_hash(row) for name, row in records.items()},
    }
    key = canonical_hash(inputs)
    final_report = deepcopy(base_report)
    final_report["inherited_publication_policy"] = final_report.pop("publication_policy", None)
    final_report["inherited_context_admission"] = final_report.pop("context_admission", None)
    final_report.update(report)
    final_report.update(
        schema_version=SCHEMA, measured_generations=len(ordered["generation"]),
        inherited_context_first_attempts_consumed=inherited, historical_first_attempts_consumed=consumed,
        historical_first_attempts_scope="current_context_registry", all_context_first_attempts_consumed=inherited + consumed,
        context_registry_records=len(records), context_registry_admissions={
            name: record["context_admission"] for name, record in records.items()},
        publication_inputs_sha256=key,
        publication_policy={"owner_output": str(output), "pointer": "current", "epochs": "publications",
                            "mode": "cached-only", "first_panels": "append-only; retain negatives and unknowns"},
        additional_generation_calls=0, additional_correctness_calls=0, additional_judge_calls=0,
        activity={"mode": "cached-only", "source_jobs": 0, "child_dispatches": 0, "new_first_attempt_intents": 0,
                  "generation_calls": 0, "correctness_calls": 0, "judge_calls": 0},
    )
    payloads = {
        "generation_results.jsonl": raw["generation_results.jsonl"],
        "correctness.jsonl": raw["correctness.jsonl"],
        "judgments.jsonl": jsonl(ordered["judgment"]), "results.jsonl": jsonl(results),
        "report.json": json_bytes(final_report),
    }
    if combined["judgment"] == tables["judgment"]:
        payloads["judgments.jsonl"] = raw["judgments.jsonl"]
    if keyed(results) == before:
        payloads["results.jsonl"] = raw["results.jsonl"]
    output.mkdir(parents=True, exist_ok=True)
    owner = output / "experiment-manifest.json"
    if owner.exists():
        require(owner.read_bytes() == json_bytes(manifest), "publication manifest changed")
    else:
        write_file(owner, json_bytes(manifest))
    epochs = output / "publications"
    epochs.mkdir(exist_ok=True)
    target = epochs / key
    if not target.exists():
        temporary = epochs / ("." + key + "." + uuid.uuid4().hex)
        temporary.mkdir()
        for name, data in payloads.items():
            write_file(temporary / name, data)
        write_file(temporary / "snapshot.json", json_bytes({
            "schema_version": "reviewbench-additive-original-context-snapshot/v1",
            "publication_key": key, "inputs": inputs,
            "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                      for name, data in payloads.items()},
            "affected_pairs": [list(value) for value in sorted(selected)],
            "new_generation_judge_SUT_calls": 0,
        }))
        descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.rename(target)
    for name, data in payloads.items():
        require((target / name).read_bytes() == data, "immutable publication data changed")
    require(json.loads((target / "snapshot.json").read_text())["inputs"] == inputs, "epoch input identity changed")
    for name in (*FILES, "snapshot.json"):
        alias = output / name
        if alias.is_symlink():
            require(os.readlink(alias) == "current/" + name, "foreign output alias")
        else:
            require(not alias.exists(), "output alias collides with an existing file")
            alias.symlink_to("current/" + name)
    pointer = output / "current"
    require(not pointer.exists() or pointer.is_symlink(), "current is not a publication pointer")
    temporary = output / (".current." + uuid.uuid4().hex)
    temporary.symlink_to("publications/" + key, target_is_directory=True)
    os.replace(temporary, pointer)
    descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {"epoch": str(target), "report_sha256": file_hash(target / "report.json"),
            "registered_first_panels": len(records), "inherited_consumed": inherited,
            "current_registry_consumed": consumed, "total_context_consumed": inherited + consumed,
            "new_generation_judge_SUT_calls": 0}
