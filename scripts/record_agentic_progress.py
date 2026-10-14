#!/usr/bin/env python3
"""Append bounded, read-only observations to an agentic experiment journal."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from zoneinfo import ZoneInfo

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_PUBLISHED_REPORT_BYTES = 8 * 1024 * 1024
PUBLISHED_REPORT_ROOT = Path("/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks")
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_PROOFS = 4096
GIB = 1024 ** 3
DELTA67_REPORT_ROOT = Path("/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-iree73-compiler-original-402-20260916/measurement-67-zero-call-publication-v1/full402-overlay")
DELTA68_AUTHORITY = Path("/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-largo-source-link-audit-402-20260917-v1/first-publication-v1/delta68-archive-v1/authority.json")
DELTA69_AUTHORITY = Path("/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-clickhouse05-original-402-20260917/first-episode-publication-after-delta68-v1/archive-producer-bridge-binding-v1/authority.json")
DELTA69_PINS = {
    "archive_code": "f392898f606c7468c0931a7250f9ea16acc1f851ee92ad0aa61d87acfeb6f5c4",
    "archive_dependencies": "e45ec199e96f18fa182b9bf3897759dbb21977277cc64259e49b29e317548bf5",
    "profile": "b1e99708caabf2f5eade0e93f726a6ce769095b099fda942092c6a4c32ad9513",
    "producer": "a82ed03c24a22ad8c859e3e721dc6fbea4de09f5a7c31bb571f11e01f7d5ccd8",
    "cutoff": "374476c1ba01ca3c4408322e64654317a835023bb49328b8ad0f41943fc63d5b",
    "parent68": "7c01f4432bb22691fbaa3e22260e0dc6ec916cbf4c5d8f16f087fe4a103a607b",
}
DELTA70_AUTHORITY = Path("/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-dpnp14-original-402-20260917/first-episode-publication-after-delta69-v1/delta70-archive-v1/authority.json")
DELTA70_PINS = {
    "authority": "3624557e5ef3b914fe11ace758bb361ce7405ff4b517d29b6347da9688641b71",
    "archive": "d7c47cd5de464a5bb957f9739992e5af7bf1abab677534de8ae893eaf800b578",
    "archive_code": "d34ec5e3da6f3c845e62bd3dc7400aabc458c1abe62f844df8762777a1d15fa1",
    "archive_plan": "24ecdf027467239d7eb5ad317bd8726b6b50eb9605e5d71818e3d49a03b95ed5",
    "publisher": "2dba3d54b090e358b10e3658ba0881489c579e3934bb6613a9357f44bb2a70be",
    "publisher_plan": "cf08c752fb160c23c35fcb60b79acf565f01cd4b8537aea5691f8b520c22c71d",
    "parent": "c483c365475a26eebc8a2f9cbd96327203bd2df0ad1395b21f1dce6b707ea3a3",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    path = Path(path)
    limit = MAX_JSON_BYTES
    # Full402 reports accumulate retained lineage. Permit a larger bounded
    # observation only for canonical, content-addressed publication reports.
    # Authority/hash and denominator validation still occur in the selectors
    # and collect(); ordinary config/state/manual evidence retain 2 MiB.
    if (path.is_absolute() and not path.is_symlink()
            and path.resolve(strict=True) == path
            and path.is_relative_to(PUBLISHED_REPORT_ROOT)
            and len(path.parts) >= 4
            and path.parts[-4:-2] == ("full402-overlay", "publications")
            and re.fullmatch(r"[0-9a-f]{64}", path.parts[-2])
            and path.name == "report.json"):
        limit = MAX_PUBLISHED_REPORT_BYTES
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"progress input exceeds the {limit // (1024 * 1024)}-MiB observation limit: " + str(path))
    return json.loads(data), hashlib.sha256(data).hexdigest()


def select_verified_delta68(config):
    """Read existing local Blob proofs only; keep Delta67/state identity on rejection."""
    if (config["expected_tasks"] != 402
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return config, []
    evidence = []

    def require(ok, message):
        if not ok:
            raise ValueError(message)

    def regular(path):
        p = Path(path)
        require(p.is_absolute() and ".." not in p.parts
                and not any(q.is_symlink() for q in (p, *p.parents))
                and stat.S_ISREG(p.lstat().st_mode), "non-regular or redirected Delta68 proof")
        return p

    def observed(path, ref=None):
        p = regular(path)
        value, checksum = read_json(p)
        require(ref is None or ref == {"path": str(p), "sha256": checksum}, "Delta68 proof SHA/path differs")
        evidence.append({"path": str(p), "sha256": checksum})
        return value

    def readback(path, spec):
        p = regular(path)
        before = p.stat()
        require(type(spec.get("size_bytes")) is int and 0 < spec["size_bytes"] <= 64 * 1024**2
                and before.st_size == spec["size_bytes"], "Delta68 readback size differs")
        sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
        with p.open("rb") as stream:
            while chunk := stream.read(1024**2):
                sha.update(chunk); md5.update(chunk)
        after = p.stat()
        require((before.st_ino, before.st_size, before.st_mtime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns), "Delta68 readback changed while observing")
        require(sha.hexdigest() == spec["sha256"]
                and base64.b64encode(md5.digest()).decode("ascii") == spec["md5_base64"],
                "Delta68 independent readback SHA256/MD5 differs")
        evidence.append({"path": str(p), "sha256": sha.hexdigest()})

    try:
        q = DELTA68_AUTHORITY.parent
        authority = observed(DELTA68_AUTHORITY)
        require(authority.get("archive_verified") is True
                and authority.get("status") == "delta68_independently_Blob_verified"
                and authority.get("delta") == 68 and authority.get("full_scope") == 402
                and authority.get("old67_unchanged") is True, "Delta68 remote authority is not verified")
        archive = observed(q / "archive/publication.json", authority["archive_publication"])
        result = observed(q / "archive-result.json")
        terminal = observed(q / "main-execution-v1/terminal.json")
        process = observed(q / "main-execution-v1/process.json")
        attempt = observed(q / "archive/attempt.json")
        require(terminal["exit_status"] == 0 and terminal["command"] == process["command"]
                and terminal.get("automatic_replay") is False and "--archive" in terminal["command"]
                and attempt["status"] == "completed" and attempt["pid"] == process["pid"]
                and attempt["publication"] == result["publication"] == authority["archive_publication"]
                and result["authority"] == evidence[0] and result["full_chain_count"] == 68,
                "Delta68 actual owner/terminal/result is incomplete")
        proc = Path(f"/proc/{int(process['pid'])}/stat")
        if proc.exists():
            require(proc.read_text().rsplit(")", 1)[1].split()[19] != str(process["process_start"]),
                    "Delta68 archive owner is still running")
        require(archive["status"] == "additive_original_measurement_delta_independently_Blob_verified"
                and archive["delta"] == 68 and archive["full_scope"] == 402
                and all(archive.get(k) is True for k in ("all_bytes_modes_crc_verified",
                    "all_payload_files_physically_restored_and_reopened",
                    "inventory_physically_restored_and_reopened", "other401_results_unchanged"))
                and len(archive["prior_measurement_deltas"]) == authority["inherited_delta_count"] == 67,
                "Delta68 archive/readback closure differs")
        spec = observed(q / "archive/object.json")
        put, get = observed(q / "archive/put.json"), observed(q / "archive/get.json")
        require(spec == archive["object"] == put["object"] == get["object"]
                and put["receipt"] == archive["receipt"] and get["receipt"] == archive["readback"],
                "Delta68 PUT/GET object or receipt differs")
        for receipt, verification in ((put["receipt"], "azure-server-head-and-content-md5"),
                                      (get["receipt"], "streamed-sha256-and-md5")):
            require(all(receipt[k] == spec[k] for k in ("sha256", "size_bytes", "md5_base64"))
                    and receipt["verification"] == verification and receipt["etag"]
                    and receipt["uri"].startswith("https://")
                    and receipt["uri"].endswith("/" + spec["sha256"]), "Delta68 Blob receipt is not bound")
        require(put["receipt"]["uri"] == get["receipt"]["uri"]
                and put["receipt"]["etag"] == get["receipt"]["etag"], "Delta68 readback is another remote object")
        readback(q / "archive/independent-readback.tar.gz", spec)
        local = observed(q.parent / "full402-overlay/publication.json", authority["local_publication"])
        epoch = Path(local["epoch"])
        require(local["status"] == "validated_additive_original_Largo_full402_overlay"
                and local["delta"] == 68 and local["other401_source_rows_byte_preserved"] is True
                and local["prior_measurement_deltas"] == archive["prior_measurement_deltas"]
                and epoch.parent == q.parent / "full402-overlay/publications"
                and epoch.name == local["publication_key"] and archive["epoch"] == str(epoch),
                "Delta68 report escaped its actual immutable epoch")
        payload_rows = archive["verification_files"]
        payload = {row["path"]: row for row in payload_rows}
        require(len(payload) == len(payload_rows) == archive["files"], "duplicate archive payload inventory")
        views = {}
        for key, name in (("report", "report.json"), ("results", "results.jsonl"), ("snapshot", "snapshot.json")):
            ref = authority[key]
            require(ref == local["files"][name] and ref["path"] == str(epoch / name),
                    "Delta68 authority selects a different view")
            p = regular(ref["path"])
            body = p.read_bytes()
            require(len(body) <= MAX_JSON_BYTES and hashlib.sha256(body).hexdigest() == ref["sha256"],
                    "Delta68 selected view SHA differs")
            row = payload["view/" + name]
            restored = regular(q / "archive/physical-restoration" / row["path"])
            require(row["sha256"] == ref["sha256"] and row["bytes"] == len(body)
                    and restored.stat().st_size == row["bytes"]
                    and stat.S_IMODE(restored.stat().st_mode) == row["mode"]
                    and hashlib.sha256(restored.read_bytes()).hexdigest() == row["sha256"],
                    "Delta68 physical readback view differs")
            views[key] = body
            evidence.extend([ref, {"path": str(restored), "sha256": row["sha256"]}])
        report, snapshot = json.loads(views["report"]), json.loads(views["snapshot"])
        require(report["publication_inputs_sha256"] == snapshot["publication_key"] == epoch.name
                and report["task_count"] == report["expected_pairs"] == 402,
                "Delta68 report/snapshot denominator or identity differs")
        for key, name in (("report", "report.json"), ("results", "results.jsonl")):
            require(snapshot["files"][name] == {"bytes": len(views[key]), "sha256": authority[key]["sha256"]},
                    "Delta68 snapshot reference differs")
        rows = [json.loads(line) for line in views["results"].splitlines() if line.strip()]
        metrics = authority["metrics"]
        require(metrics == archive["metrics"] == result["metrics"] == local["metrics"]
                and len(rows) == len({(r["id"], r["model"]) for r in rows}) == 402
                and dict(Counter(r["status"] for r in rows)) == metrics["counts"]
                and sum(r["status"] == "completed" for r in rows) == metrics["completed"]
                and sum(r["resolved"] is True for r in rows) == metrics["resolved"]
                and sum(r["status"] == "completed" and r["resolved"] is False for r in rows) ==
                    metrics["not_resolved_completed"]
                and report["measured_generations"] == metrics["generation_terminals"],
                "Delta68 progress metrics disagree with immutable rows")
        return {**config, "report": authority["report"]["path"],
                "_fallback_report": config["report"],
                "_selected_report_sha256": authority["report"]["sha256"],
                "_verified_archive_authority": {"delta": 68, "metrics": metrics,
                    "authority": evidence[0], "archive_publication": authority["archive_publication"]}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        return {**config, "_authority_rejection": "Delta68核验未闭合，继续Delta67：" + str(exc)}, []


def select_verified_delta69(config):
    """Explicit a82 archive only; inspect retained proofs, never import its producer."""
    fallback, prior_evidence = select_verified_delta68(config)
    if (config["expected_tasks"] != 402
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return fallback, prior_evidence
    if not DELTA69_AUTHORITY.exists():
        return fallback, prior_evidence
    evidence = []
    q, w = DELTA69_AUTHORITY.parent, DELTA69_AUTHORITY.parent.parent
    tables = ("results.jsonl", "generation_results.jsonl", "correctness.jsonl", "judgments.jsonl")
    names = (*tables, "report.json", "snapshot.json")

    def require(ok, message):
        if not ok:
            raise ValueError(message)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate Delta69 JSON key")
            result[key] = value
        return result

    def regular(path):
        p = Path(path)
        require(p.is_absolute() and ".." not in p.parts
                and not any(v.is_symlink() for v in (p, *p.parents))
                and stat.S_ISREG(p.lstat().st_mode), "non-regular or redirected Delta69 input")
        return p

    def fingerprint(path, *, content=False):
        p = regular(path)
        before = p.stat()
        limit = MAX_JSON_BYTES if content else 64 * 1024**2
        require(before.st_size <= limit, "Delta69 observation exceeds bounded file size")
        sha, md5, chunks = hashlib.sha256(), hashlib.md5(usedforsecurity=False), []
        size = 0
        with p.open("rb") as stream:
            while chunk := stream.read(1024**2):
                size += len(chunk)
                require(size <= limit, "Delta69 input grew past observation bound")
                sha.update(chunk); md5.update(chunk)
                if content:
                    chunks.append(chunk)
        after = p.stat()
        require((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                "Delta69 input changed while reading")
        ref = {"path": str(p), "sha256": sha.hexdigest()}
        evidence.append(ref)
        return ref, size, stat.S_IMODE(before.st_mode), base64.b64encode(md5.digest()).decode(), b"".join(chunks)

    def observed(path, ref=None):
        actual, _, _, _, body = fingerprint(path, content=True)
        require(ref is None or ref == actual, "Delta69 metadata SHA/path differs")
        return json.loads(body, object_pairs_hook=unique), actual

    def fixed(path, name):
        return {"path": str(path), "sha256": DELTA69_PINS[name]}

    try:
        parent_ref = fixed(DELTA68_AUTHORITY, "parent68")
        parent_binding = fallback.get("_verified_archive_authority", {})
        require(parent_binding.get("delta") == 68 and parent_binding.get("authority") == parent_ref,
                "pinned parent68 is not independently verified")
        authority, authority_ref = observed(DELTA69_AUTHORITY)
        require(authority.get("archive_verified") is True
                and authority.get("status") == "delta69_independently_Blob_verified"
                and authority.get("delta") == 69 and authority.get("full_scope") == 402
                and authority.get("old67_unchanged") is authority.get("old68_unchanged") is True
                and authority["parent_authority"] == parent_ref
                and authority["inherited_delta_count"] == 68 and authority["full_chain_count"] == 69,
                "Delta69 authority is not the explicit verified successor of68")
        archive, archive_ref = observed(q / "archive/publication.json", authority["archive_publication"])
        result, _ = observed(q / "archive-result.json")
        claim, _ = observed(q / "archive-claim.json")
        attempt, _ = observed(q / "archive/attempt.json")
        require(result["authority"] == authority_ref and result["publication"] == archive_ref
                and result["delta"] == 69 and result["full_chain_count"] == 69
                and result["score_calls"] == result["new_model_test_judge_calls"] == 0
                and attempt["status"] == "completed" and attempt["publication"] == archive_ref
                and attempt["pid"] == claim["pid"] and claim["automatic_retries"] == 0
                and claim["code"] == fixed(q / "adapter.py", "archive_code"),
                "Delta69 seal/result/unique owner is not terminal")
        require(type(claim["pid"]) is int and claim["pid"] > 0
                and str(claim["process_start"]).isdigit(), "archive exact-start owner missing")
        proc = Path(f"/proc/{claim['pid']}/stat")
        if proc.exists():
            fields = proc.read_text().rsplit(")", 1)[1].split()
            require(fields[19] != str(claim["process_start"]) or fields[0] in {"Z", "X"},
                    "Delta69 archive owner remains live")
        parent_archive, _ = observed(parent_binding["archive_publication"]["path"],
                                     parent_binding["archive_publication"])
        chain = [*parent_archive["prior_measurement_deltas"], parent_archive["object"]]
        require(archive["status"] == result["status"] ==
                "additive_original_measurement_delta_independently_Blob_verified"
                and archive["delta"] == 69 and archive["full_scope"] == 402
                and all(archive.get(k) is True for k in ("all_bytes_modes_crc_verified",
                    "all_payload_files_physically_restored_and_reopened",
                    "inventory_physically_restored_and_reopened", "other401_results_unchanged"))
                and archive["prior_measurement_deltas"] == chain
                and len(chain) == len({row["sha256"] for row in chain}) == 68,
                "Delta69 readback closure or inherited67/68 descriptors differ")
        spec, _ = observed(q / "archive/object.json")
        put, _ = observed(q / "archive/put.json")
        get, _ = observed(q / "archive/get.json")
        require(spec == archive["object"] == put["object"] == get["object"]
                and put["receipt"] == archive["receipt"] and get["receipt"] == archive["readback"],
                "Delta69 upload/readback objects differ")
        for receipt, kind in ((put["receipt"], "azure-server-head-and-content-md5"),
                              (get["receipt"], "streamed-sha256-and-md5")):
            require(all(receipt[k] == spec[k] for k in ("sha256", "size_bytes", "md5_base64"))
                    and receipt["verification"] == kind and receipt["etag"]
                    and receipt["uri"].startswith("https://")
                    and receipt["uri"].endswith("/" + spec["sha256"]),
                    "Delta69 receipt is not a real bound Blob verification")
        require(put["receipt"]["uri"] == get["receipt"]["uri"]
                and put["receipt"]["etag"] == get["receipt"]["etag"], "Delta69 GET is a different remote object")
        actual, size, _, md5, _ = fingerprint(q / "archive/independent-readback.tar.gz")
        require(type(spec["size_bytes"]) is int and 0 < size == spec["size_bytes"]
                and actual["sha256"] == spec["sha256"] and md5 == spec["md5_base64"],
                "Delta69 retained independent readback SHA256/MD5 differs")
        rows = archive["verification_files"]
        payload = {row["path"]: row for row in rows}
        require(len(rows) == len(payload) == archive["files"] and len(rows) <= MAX_PROOFS,
                "duplicate/oversized Delta69 archive inventory")

        def payload_file(name, ref, *, content=False):
            require(not name.startswith("/") and ".." not in Path(name).parts, "unsafe payload name")
            row = payload[name]
            actual, size, mode, _, body = fingerprint(ref["path"], content=content)
            require(actual == ref and row["sha256"] == ref["sha256"]
                    and row["bytes"] == size and row["mode"] == mode, "Delta69 original payload differs: " + name)
            restored, copied_size, copied_mode, _, _ = fingerprint(q / "archive/physical-restoration" / name)
            require(restored["sha256"] == ref["sha256"] and copied_size == size and copied_mode == mode,
                    "Delta69 physical restored payload differs: " + name)
            return body

        def payload_json(name, ref):
            return json.loads(payload_file(name, ref, content=True), object_pairs_hook=unique)

        require(claim["plan"]["path"] == str(q / "bound-plan.json"), "foreign archive bound-plan")
        bound = payload_json("archive/bound-plan.json", claim["plan"])
        require(bound["delta"] == 69 and bound["code"] == claim["code"]
                and bound["dependencies"] == fixed(q / "dependencies.json", "archive_dependencies")
                and bound["output"] == str(q / "archive")
                and bound["legacy_API_complete"] is False and bound["prior_measurement_deltas"] == chain,
                "Delta69 archive producer profile changed")
        payload_file("archive/controller.py", bound["code"])
        payload_file("archive/dependencies.json", bound["dependencies"])
        profile_ref = fixed(q / "MODIFIED_FILE.json", "profile")
        profile = payload_json("archive/explicit-producer-profile.json", profile_ref)
        require(bound["explicit_producer_profile"] == profile_ref
                and profile["producer_code"] == fixed(w / "pre-dispatch-publisher-bridge-v1/MODIFIED_FILE.py", "producer")
                and profile["parent_authority"] == parent_ref and profile["legacy_API_complete"] is False
                and profile["canonical_claim_path"] == str(w / "publication-claim.json"),
                "not the pinned a82/canonical W claim")
        payload_file("local/publisher_code.py", profile["producer_code"])
        controls = payload_json("archive/controls-closed.json", claim["controls"])
        control_proof = payload_json("archive/controls-proof.json", controls["proof"])
        require(controls["controls_closed"] is control_proof["controls_closed"] is True
                and controls["code"] == control_proof["code"] == bound["code"]
                and control_proof["copied_rollback_verified"] is True and control_proof["new_tests_passed"] == 9,
                "Delta69 adapter controls not closed")
        local, _ = observed(w / "full402-overlay/publication.json", authority["local_publication"])
        payload_file("local/publication.json", authority["local_publication"])
        pubclaim = payload_json("local/publication-claim.json", bound["publisher_claim"])
        plan = payload_json("local/ready-plan.json", local["plan"])
        require(bound["publisher_claim"]["path"] == str(w / "publication-claim.json")
                and local["plan"]["path"] == profile["expected_publisher_plan_path"]
                and plan["code"] == pubclaim["code"] == profile["producer_code"]
                and local["plan"] == pubclaim["plan"] == bound["publisher_handoff"]["publisher_plan"]
                and plan["delta"] == pubclaim["delta"] == 69
                and plan["status"] == "ready_for_Main_unique_local_delta69_publication"
                and plan["bridge_schema"] == "clickhouse-delta69-independent-predispatch-cutoff-bridge/v1"
                and plan["canonical_publication_claim"] == str(w / "publication-claim.json")
                and pubclaim["automatic_retry"] is pubclaim["automatic_rebase"] is False
                and plan["legacy_API_complete"] is False
                and local["parent_authority"] == pubclaim["parent_authority"] == parent_ref,
                "Delta69 local publisher/claim/cutoff route differs")
        epoch = Path(local["epoch"])
        require(local["status"] == "validated_additive_original_CH_after_Delta68_full402_overlay"
                and local["delta"] == 69 and local["publication_key"] == digest(plan) == epoch.name
                and epoch.parent == w / "full402-overlay/publications"
                and local["epoch"] == archive["epoch"] == bound["epoch"]
                and local["prior_measurement_deltas"] == chain
                and local["other401_source_rows_byte_preserved"] is local["old68_descriptors_preserved"] is True
                and local["raw_engine_fields_rewritten"] is False
                and set(local["files"]) == set(names) and local["files"] == bound["view_files"],
                "Delta69 local views/immutable epoch differ")
        views = {}
        for name in names:
            ref = local["files"][name]
            require(ref["path"] == str(epoch / name)
                    and {k: bound["files"]["view/" + name][k] for k in ("path", "sha256")} == ref,
                    "Delta69 archive plan selects a different view")
            views[name] = payload_file("view/" + name, ref, content=name in (
                "results.jsonl", "generation_results.jsonl", "report.json", "snapshot.json"))
        require(all(authority[k] == local["files"][name] for k, name in (
            ("report", "report.json"), ("results", "results.jsonl"), ("snapshot", "snapshot.json"))),
            "Delta69 authority selects another402 view")
        report, snapshot = (json.loads(views[n], object_pairs_hook=unique) for n in ("report.json", "snapshot.json"))
        require(report["publication_inputs_sha256"] == snapshot["publication_key"] == epoch.name
                and report["task_count"] == report["expected_pairs"] == 402,
                "Delta69 report identity or denominator differs")
        for name in (*tables, "report.json"):
            row = payload["view/" + name]
            require(snapshot["files"][name] == {"bytes": row["bytes"], "sha256": row["sha256"]},
                    "Delta69 snapshot SHA differs: " + name)
        results = [json.loads(line, object_pairs_hook=unique) for line in views["results.jsonl"].splitlines() if line.strip()]
        generations = [json.loads(line, object_pairs_hook=unique) for line in views["generation_results.jsonl"].splitlines() if line.strip()]
        metrics = authority["metrics"]
        require(metrics == archive["metrics"] == result["metrics"] == local["metrics"] == bound["metrics"]
                and len(results) == len({(r["id"], r["model"]) for r in results}) == 402
                and dict(Counter(r["status"] for r in results)) == metrics["counts"]
                and sum(r["status"] == "completed" for r in results) == metrics["completed"]
                and sum(r["resolved"] is True for r in results) == metrics["resolved"]
                and sum(r["status"] == "completed" and r["resolved"] is False for r in results) ==
                    metrics["not_resolved_completed"]
                and report["measured_generations"] == metrics["generation_terminals"] ==
                    len(generations) == len({(r["id"], r["model"]) for r in generations}),
                "Delta69 metrics disagree with original immutable scored rows")
        report_metrics(report, 402)  # Existing presentation checks only, never score a pair.
        actual_evidence = plan["inputs"]["actual_evidence"]
        cutoff_ref = plan["independent_cutoff_proof"]
        inherited = actual_evidence["independent_pre_dispatch_cutoff"]
        require(cutoff_ref == bound["separate_cutoff_proof"] == inherited["proof"]
                and cutoff_ref["path"] == str(w / "pre-dispatch-budget-audit-v1/cutoff-proof.json")
                and inherited["auditor"] == fixed(w / "pre-dispatch-budget-audit-v1/MODIFIED_FILE.py", "cutoff")
                and inherited["legacy_API_complete"] is False
                and inherited["raw_generation_trace_candidate_unchanged"] is True,
                "independent cutoff is absent/foreign, not a rewritten complete bit")
        cutoff = payload_json("evidence/cutoff/proof.json", cutoff_ref)
        payload_file("evidence/cutoff/auditor.py", inherited["auditor"])
        deps_ref = bound["files"]["evidence/cutoff/dependencies.json"]
        cutoff_deps = payload_json("evidence/cutoff/dependencies.json", {k: deps_ref[k] for k in ("path", "sha256")})
        api, separate = cutoff["api"], cutoff["separate_cutoff"]
        lineage, order = separate["source_lineage"], separate["source_lineage"]["effect_order"]
        require(cutoff["status"] == "pre_dispatch_wall_budget_cutoff_proven_for_Main_review_only"
                and cutoff["code"] == inherited["auditor"]
                and cutoff["generation"] == actual_evidence["refs"]["generation"]
                and cutoff["trace"] == actual_evidence["refs"]["trajectory"]
                and api == actual_evidence["api_and_tools"]
                and api["complete_api_and_tool_evidence"] is cutoff["original_api_complete"] is False
                and api["model_api_started"] is True and api["native_api_calls"] == 8
                and api["real_tool_feedbacks"] == 7 and api["unreturned_native_calls"] == api["transport_retries"] == 0
                and api["unterminated_original_command_journals"] == []
                and cutoff["original_trace_inflight"] is True and cutoff["original_stop_reason"] == "wall_time_limit"
                and cutoff["publication_authorized"] is cutoff["scoring_override"] is cutoff["automatic_resume_or_retry"] is False
                and cutoff["new_model_SQL_judge_Blob_calls"] == 0
                and separate["not_dispatched"] is separate["no_command_journal_or_stream"] is True
                and separate["eight_charged_responses_accounted"] is separate["seven_tool_feedbacks_accounted"] is True
                and separate["fabricated_function_call_outputs"] == 0
                and lineage["inflight_reset_skipped_on_this_raise"] is True and lineage["action_execution_by_audit"] == 0
                and order["raise"] < order["uuid"] < order["pending"] < order["journal"] < order["Popen"]
                and all(lineage[k] == cutoff_deps[v] for k, v in (
                    ("environment", "frozen_environment"), ("runner", "frozen_runner"),
                    ("models", "frozen_models"), ("upstream_default_agent", "upstream_default_agent"))),
                "separate pre-dispatch proof is not the original false-API case")
        require(report["clickhouse_first_episode_after_delta68"]["actual_evidence"] == actual_evidence,
                "report dropped or rewrote original API/cutoff evidence")
        return {**config, "report": authority["report"]["path"], "_fallback_report": fallback["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": authority["report"]["sha256"],
                "_verified_archive_authority": {"delta": 69, "metrics": metrics, "authority": authority_ref,
                    "archive_publication": archive_ref, "parent_authority": parent_ref,
                    "independent_pre_dispatch_cutoff": cutoff_ref, "legacy_API_complete": False}}, [*prior_evidence, *evidence]
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        parent = "已核验Delta68" if fallback.get("_verified_archive_authority", {}).get("delta") == 68 else "Delta67"
        reason = "Delta69核验未闭合，继续" + parent + "：" + str(exc)
        if fallback.get("_authority_rejection"):
            reason = fallback["_authority_rejection"] + "；" + reason
        return {**fallback, "_authority_rejection": reason}, prior_evidence


def select_verified_delta70(config):
    """Read only the actual70 closure; invoke the existing69/68/67 route on rejection."""
    if (config["expected_tasks"] != 402
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not DELTA70_AUTHORITY.exists()):
        return select_verified_delta69(config)
    evidence = []
    q, p = DELTA70_AUTHORITY.parent, DELTA70_AUTHORITY.parent.parent

    def require(ok, message):
        if not ok:
            raise ValueError(message)

    def regular(path):
        path = Path(path)
        require(path.is_absolute() and ".." not in path.parts
                and not any(x.is_symlink() for x in (path, *path.parents))
                and stat.S_ISREG(path.lstat().st_mode), "redirected/nonregular70 evidence")
        return path

    def observed(path, ref=None):
        path = regular(path)
        value, checksum = read_json(path)
        actual = {"path": str(path), "sha256": checksum}
        require(ref is None or ref == actual, "Delta70 metadata SHA/path differs")
        evidence.append(actual)
        return value

    def fixed(path, key):
        return {"path": str(path), "sha256": DELTA70_PINS[key]}

    def streamed(path, checksum, size, mode=None, md5=None):
        path = regular(path); before = path.stat()
        require(type(size) is int and 0 <= size <= 64 * 1024**2 and before.st_size == size, "Delta70 size bound differs")
        h, m = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
        with path.open("rb") as stream:
            while chunk := stream.read(1024**2):
                h.update(chunk); m.update(chunk)
        after = path.stat()
        require((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                and h.hexdigest() == checksum
                and (mode is None or stat.S_IMODE(before.st_mode) == mode)
                and (md5 is None or base64.b64encode(m.digest()).decode() == md5),
                "Delta70 readback bytes/mode/hash changed")
        evidence.append({"path": str(path), "sha256": checksum})

    try:
        aref = fixed(DELTA70_AUTHORITY, "authority")
        authority = observed(DELTA70_AUTHORITY, aref)
        require(authority["archive_verified"] is True and authority["delta"] == 70
                and authority["status"] == "delta70_independently_Blob_verified"
                and authority["full_scope"] == 402 and authority["full_chain_count"] == 70
                and authority["inherited_delta_count"] == 69
                and authority["old69_unchanged"] is authority["other401_preserved"] is True
                and authority["parent_authority"] == fixed(DELTA69_AUTHORITY, "parent"),
                "Delta70 authority/parent is not the reviewed closed successor")
        archive = observed(q / "archive/publication.json", fixed(q / "archive/publication.json", "archive"))
        result = observed(q / "result.json")
        claim = observed(q / "archive-claim.json")
        terminal, process = observed(p / "ARCHIVE_EXECUTION.json"), observed(p / "ARCHIVE_PROCESS.json")
        attempt = observed(q / "archive/attempt.json")
        require(result["authority"] == aref and result["archive_publication"] == authority["archive_publication"]
                and authority["archive_publication"] == fixed(q / "archive/publication.json", "archive")
                and terminal["exit_status"] == 0 and terminal["command"] == process["command"]
                and terminal["pid"] == process["pid"] == claim["pid"] == attempt["pid"]
                and str(terminal["process_start"]) == str(process["process_start"]) == str(claim["process_start"])
                and claim["code"] == fixed(p / "archive_adapter.py", "archive_code")
                and claim["plan"] == fixed(p / "archive-plan.json", "archive_plan")
                and attempt["status"] == "completed" and attempt["publication"] == authority["archive_publication"],
                "actual70 owner/terminal/result/code not closed")
        owner = Path(f"/proc/{claim['pid']}/stat")
        if owner.exists():
            fields = owner.read_text().rsplit(")", 1)[1].split()
            require(fields[19] != str(claim["process_start"]) or fields[0] in {"Z", "X"}, "70 archive owner remains live")
        require(archive["status"] == "additive_original_measurement_delta_independently_Blob_verified"
                and archive["delta"] == 70 and archive["full_scope"] == 402
                and all(archive[k] is True for k in ("all_bytes_modes_crc_verified",
                    "all_payload_files_physically_restored_and_reopened", "inventory_physically_restored_and_reopened"))
                and len(archive["prior_measurement_deltas"]) == 69, "70 independent physical closure differs")
        spec, put, get = (observed(q / "archive" / name) for name in ("object.json", "put.json", "get.json"))
        require(spec == archive["object"] == put["object"] == get["object"]
                and put["receipt"] == archive["receipt"] and get["receipt"] == archive["readback"], "70 object identity differs")
        for receipt, verification in ((put["receipt"], "azure-server-head-and-content-md5"),
                                      (get["receipt"], "streamed-sha256-and-md5")):
            require(all(receipt[k] == spec[k] for k in ("sha256", "size_bytes", "md5_base64"))
                    and receipt["verification"] == verification and receipt["etag"]
                    and receipt["uri"].startswith("https://") and receipt["uri"].endswith("/" + spec["sha256"]),
                    "70 retained Blob receipt is not bound")
        require(put["receipt"]["uri"] == get["receipt"]["uri"] and put["receipt"]["etag"] == get["receipt"]["etag"],
                "70 readback is another object")
        streamed(q / "archive/independent-readback.tar.gz", spec["sha256"], spec["size_bytes"], md5=spec["md5_base64"])
        bound = observed(p / "archive-plan.json", claim["plan"])
        local = observed(p / "full402-overlay/publication.json", authority["local_publication"])
        publisher = observed(p / "ready-plan.json", fixed(p / "ready-plan.json", "publisher_plan"))
        pubclaim = observed(p / "publication-claim.json")
        require(local["plan"] == pubclaim["plan"] == bound["publisher_plan"] == fixed(p / "ready-plan.json", "publisher_plan")
                and publisher["code"] == pubclaim["code"] == fixed(p / "MODIFIED_FILE.py", "publisher")
                and local["delta"] == publisher["delta"] == 70
                and local["prior_measurement_deltas"] == bound["prior_measurement_deltas"] ==
                    archive["prior_measurement_deltas"] == publisher["inputs"]["parent"]["prior_measurement_deltas"]
                and local["parent_authority"] == bound["parent_authority"] == authority["parent_authority"]
                and local["other401_source_rows_byte_preserved"] is local["old69_descriptors_preserved"] is True
                and local["raw_engine_fields_rewritten"] is False, "70 local publication/old401 lineage differs")
        epoch = p / "full402-overlay/publications" / digest(publisher)
        require(str(epoch) == local["epoch"] == archive["epoch"] == bound["epoch"]
                and local["publication_key"] == epoch.name, "70 immutable epoch differs")
        rows = archive["verification_files"]; payload = {row["path"]: row for row in rows}
        require(len(payload) == len(rows) == archive["files"], "duplicate70 inventory")
        tables = ("results.jsonl", "generation_results.jsonl", "correctness.jsonl", "judgments.jsonl")
        require(set(local["files"]) == {*tables, "report.json", "snapshot.json"}
                and local["files"] == bound["view_files"], "six complete70 views required")
        for name, ref in local["files"].items():
            row = payload["view/" + name]
            require(ref["path"] == str(epoch / name) and ref["sha256"] == row["sha256"], "70 selected view ref differs")
            streamed(ref["path"], ref["sha256"], row["bytes"], row["mode"])
            streamed(q / "archive/physical-restoration/view" / name, ref["sha256"], row["bytes"], row["mode"])
        require(all(authority[k] == local["files"][name] for k, name in
                    (("report", "report.json"), ("results", "results.jsonl"), ("snapshot", "snapshot.json"))),
                "70 authority chooses another view")
        report, snapshot = observed(epoch / "report.json"), observed(epoch / "snapshot.json")
        require(report["publication_inputs_sha256"] == snapshot["publication_key"] == epoch.name
                and report["task_count"] == report["expected_pairs"] == 402, "70 report identity/denominator differs")
        for name in (*tables, "report.json"):
            row = payload["view/" + name]
            require(snapshot["files"][name] == {"bytes": row["bytes"], "sha256": row["sha256"]}, "70 snapshot differs")
        results = [json.loads(line) for line in (epoch / "results.jsonl").read_bytes().splitlines() if line.strip()]
        generations = [json.loads(line) for line in (epoch / "generation_results.jsonl").read_bytes().splitlines() if line.strip()]
        metrics = authority["metrics"]
        require(metrics == local["metrics"] == archive["metrics"] == result["metrics"] == bound["metrics"]
                and len(results) == len({(r["id"], r["model"]) for r in results}) == 402
                and dict(Counter(r["status"] for r in results)) == metrics["counts"]
                and sum(r["resolved"] is True for r in results) == metrics["resolved"]
                and sum(r["status"] == "completed" for r in results) == metrics["completed"]
                and sum(r["status"] == "completed" and r["resolved"] is False for r in results) == metrics["not_resolved_completed"]
                and len(generations) == len({(r["id"], r["model"]) for r in generations}) ==
                    metrics["generation_terminals"] == report["measured_generations"], "70 progress counters differ from scored rows")
        actual = publisher["inputs"]["actual_evidence"]
        require(report["dpnp_first_episode_after_delta69"]["actual_evidence"] == actual
                and actual["api_and_tools"]["complete_api_and_tool_evidence"] is True
                and actual["source_generation_calls_consumed"] == actual["api_and_tools"]["native_api_calls"] == 10
                and actual["api_and_tools"]["real_tool_feedbacks"] == 6
                and actual["native_audit_lineage"]["cached_bootstrap_executed"] is False
                and actual["native_audit_lineage"]["raw_trace_or_command_records_modified"] is False,
                "original DP responses/cache-receipt evidence changed")
        report_metrics(report, 402)
        return {**config, "report": authority["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": authority["report"]["sha256"],
                "_verified_archive_authority": {"delta": 70, "authority": aref, "metrics": metrics,
                    "archive_publication": authority["archive_publication"], "parent_authority": authority["parent_authority"]}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, proofs = select_verified_delta69(config)
        reason = "Delta70核验未闭合，保留原69/68/67回退：" + str(exc)
        if fallback.get("_authority_rejection"):
            reason += "；" + fallback["_authority_rejection"]
        return {**fallback, "_authority_rejection": reason}, proofs



ANDROID_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/first-episode-publication-after-delta70-v1')
ANDROID_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/first-episode-publication-after-delta70-v1/authority_reader.py', 'sha256': '9f754694de2ab71cdf09e86bb5a4eb1b3339896759a83a31fd8c9c9ee5ccd510'}
ANDROID_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/first-episode-publication-after-delta70-v1/MODIFIED_FILE.py', 'sha256': 'f2a93429085b686cf76a6d8c845352bd5e43cead19ffd95598f24b639cd36fa5'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/first-episode-publication-after-delta70-v1/archive_adapter.py', 'sha256': '2e338201cc6810812c1e377b975361afc2a65ae46b9c67ba516bc3845fd5e3fb'}}
ANDROID_REGISTRY_PARENT = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-dpnp14-original-402-20260917/first-episode-publication-after-delta69-v1/delta70-archive-v1/authority.json', 'sha256': '3624557e5ef3b914fe11ace758bb361ce7405ff4b517d29b6347da9688641b71'}


def select_verified_android_registry(config):
    """Fixed Android IDs and root70 only; highest closed child, then original70/69."""
    maximum = config.get("_android_registry_maximum_delta", 76)
    if (config["expected_tasks"] != 402 or maximum < 71
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (ANDROID_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_delta70(config)
    error = None
    try:
        import importlib.util
        helper = Path(ANDROID_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != ANDROID_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Android registry helper SHA differs")
        spec = importlib.util.spec_from_file_location("_android_closed_registry_reader", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        for ceiling in range(min(maximum, 76), 70, -1):
            try:
                result, evidence = module.verify_head(ANDROID_REGISTRY_ROOT, ANDROID_REGISTRY_CODES,
                                                      ANDROID_REGISTRY_PARENT, maximum_delta=ceiling)
                report, checksum = read_json(result["report"]["path"])
                if checksum != result["report"]["sha256"]:
                    raise ValueError("selected Android report changed")
                report_metrics(report, 402)
                return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                        "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                        "_verified_archive_authority": {k: result[k] for k in
                            ("delta", "authority", "metrics", "archive_publication", "parent_authority")}}, evidence
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                error = str(exc)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        error = str(exc)
    fallback, evidence = select_verified_delta70(config)
    reason = "Android新增authority未闭合，保留已验证70/69回退：" + str(error)
    if fallback.get("_authority_rejection"):
        reason += "；" + fallback["_authority_rejection"]
    return {**fallback, "_authority_rejection": reason}, evidence



REMAINING_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/remaining-android-publication-after-eureka-v1/event-native-cache-recovery-v1')
REMAINING_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/remaining-android-publication-after-eureka-v1/event-native-cache-recovery-v1/authority_reader.py', 'sha256': '2ff7e8041382cd18c1ef2d35de1b615ebd42e0f434195c3f46440250938d82cc'}
REMAINING_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/remaining-android-publication-after-eureka-v1/event-native-cache-recovery-v1/MODIFIED_FILE.py', 'sha256': '78acbbcb49823d44c08e3413182b563264c3a6426584eb3dc9fa8b718adea996'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/remaining-android-publication-after-eureka-v1/event-native-cache-recovery-v1/archive_adapter.py', 'sha256': '045c867879c432e4d1d99e64845f513bce475389e0f2468fac68c8587dd654f5'}}


def select_verified_remaining_android(config):
    """Only the explicit Event native-cache recovery child; failed initial episode remains visible."""
    maximum = config.get("_remaining_registry_maximum_delta", 76)
    if (config["expected_tasks"] != 402
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (REMAINING_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_android_registry(config)
    error = None
    try:
        import importlib.util
        helper = Path(REMAINING_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != REMAINING_REGISTRY_HELPER["sha256"]:
            raise ValueError("remaining-profile registry helper pin changed")
        spec = importlib.util.spec_from_file_location("_remaining_registry_reader", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        for ceiling in range(min(maximum, 76), 71, -1):
            try:
                result, evidence = module.verify(REMAINING_REGISTRY_ROOT, REMAINING_REGISTRY_CODES, ceiling)
                report, checksum = read_json(result["report"]["path"])
                if checksum != result["report"]["sha256"]:
                    raise ValueError("remaining-profile selected report changed")
                report_metrics(report, 402)
                return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                        "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                        "_remaining_profile_registry": True,
                        "_verified_archive_authority": {k: result[k] for k in
                            ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                error = str(exc)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        error = str(exc)
    fallback, evidence = select_verified_android_registry(config)
    return {**fallback, "_authority_rejection": "剩余Android后继未闭合，保留原Eureka73/72及旧链：" + str(error)}, evidence



F_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/last-two-publication-after-delta74-v1')
F_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/last-two-publication-after-delta74-v1/authority_reader.py', 'sha256': '6a06e3ebc27112360b984e097195a8423f5db8435c1a2f34284c6b71df9387a8'}
F_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/last-two-publication-after-delta74-v1/MODIFIED_FILE.py', 'sha256': '556a4fcb52e7c44902b33fc72c721fec67203d796f6f1119e2a100d47262ceee'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-android-original-adapter-402-20260917-v1/last-two-publication-after-delta74-v1/archive_adapter.py', 'sha256': '7d16e04e20be1f2323608204c29936c75e98501ac134b0bc7ec8b3a6ed33c7ff'}}

def select_verified_last_two_F(config):
    """Only two F-owned pairs after pinned74; inherited recovery attribution stays explicit."""
    maximum = config.get("_F_registry_maximum_delta", 76)
    if (config["expected_tasks"] != 402 or maximum < 75
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (F_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_remaining_android(config)
    error = None
    try:
        import importlib.util
        helper = Path(F_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != F_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed F registry helper SHA differs")
        spec = importlib.util.spec_from_file_location("_F_closed_registry_reader", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        for ceiling in range(min(maximum, 76), 74, -1):
            try:
                result, evidence = module.verify(F_REGISTRY_ROOT, F_REGISTRY_CODES, ceiling)
                report, checksum = read_json(result["report"]["path"])
                if checksum != result["report"]["sha256"]:
                    raise ValueError("selected F report changed")
                report_metrics(report, 402)
                return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                        "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                        "_F_profile_registry": True,
                        "_verified_archive_authority": {k: result[k] for k in
                            ("delta", "authority", "metrics", "archive_publication", "parent_authority",
                             "measurement_lineage")}}, evidence
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                error = str(exc)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        error = str(exc)
    fallback, evidence = select_verified_remaining_android(config)
    reason = "新F两题authority未闭合，保留已验证74/73及旧链：" + str(error)
    if fallback.get("_authority_rejection"):
        reason += "；" + fallback["_authority_rejection"]
    return {**fallback, "_authority_rejection": reason}, evidence



KRAFTKIT_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-kraftkit2007-original-402-20260918-v1/publication-after76')
KRAFTKIT_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-kraftkit2007-original-402-20260918-v1/publication-after76/reader-owner-command-shape-v1/authority_reader.py', 'sha256': 'b7232a05bdac85ae0b1070d241cedef03ac310c2a69efb558feaa08f7e5b6c90'}
KRAFTKIT_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-kraftkit2007-original-402-20260918-v1/publication-after76/MODIFIED_FILE.py', 'sha256': 'ebd773fd81d50538e83a5ccc28f2d8ae037412134270b156d9f0d12310284d5b'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-kraftkit2007-original-402-20260918-v1/publication-after76/archive_adapter.py', 'sha256': '16b9cc5f935d8bd8fd16ba0391bac5b04c2cad38e3e8f65cc1ffe636bfab6916'}}

def select_verified_kraftkit77(config):
    """An explicitly sealed single Kraftkit child; otherwise retain verified76/75/74."""
    if (config["expected_tasks"] != 402 or config.get("_kraftkit_maximum_delta", 77) < 77
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (KRAFTKIT_REGISTRY_ROOT/"HEAD.json").is_file()):
        return select_verified_last_two_F(config)
    error = None
    try:
        import importlib.util
        helper = Path(KRAFTKIT_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != KRAFTKIT_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Kraftkit helper SHA differs")
        spec = importlib.util.spec_from_file_location("_kraftkit77_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(KRAFTKIT_REGISTRY_ROOT, KRAFTKIT_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected77 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_kraftkit_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        error = str(exc)
    fallback, evidence = select_verified_last_two_F(config)
    return {**fallback, "_authority_rejection": "Kraftkit77未闭合，保留已验证76及旧链："+str(error)}, evidence



PYOMO_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-pyomo3698-original-402-20260918-v1/publication-after77/alias-recovery-integration-v1')
PYOMO_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-pyomo3698-original-402-20260918-v1/publication-after77/alias-recovery-integration-v1/authority_reader.py', 'sha256': '2b6fa106b788271fe957e0d2cceaa09d41386227e0bd66c0ab352733e793fae0'}
PYOMO_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-pyomo3698-original-402-20260918-v1/publication-after77/alias-recovery-integration-v1/MODIFIED_FILE.py', 'sha256': '4a29e5bbfeb7609aef822240c6417f19ebe84e01ab48d690688bf60dd714817e'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-pyomo3698-original-402-20260918-v1/publication-after77/alias-recovery-integration-v1/archive_adapter.py', 'sha256': '1857382077d82bb7637362d3b70ae33633a01611f8d7d53ea863643b2af089f8'}}

def select_verified_pyomo78(config):
    """Only sealed Pyomo78; all pending/rejected cases use the corrected77 route."""
    if (config["expected_tasks"] != 402 or config.get("_pyomo_maximum_delta", 78) < 78
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (PYOMO_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_kraftkit77(config)
    try:
        import importlib.util
        helper = Path(PYOMO_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != PYOMO_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Pyomo helper SHA differs")
        spec = importlib.util.spec_from_file_location("_pyomo78_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(PYOMO_REGISTRY_ROOT, PYOMO_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected78 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_pyomo_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_kraftkit77(config)
        return {**fallback, "_authority_rejection": "Pyomo78未闭合，保留已核验77及旧链：" + str(exc)}, evidence


EKS_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-eks2274-original-402-20260918-v1/publication-after78')
EKS_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-eks2274-original-402-20260918-v1/publication-after78/authority_reader.py', 'sha256': '8219732f2e10aca3a5ea8d261a8bbd5c03a223c54aaab3f0b8f2abc84fb65aad'}
EKS_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-eks2274-original-402-20260918-v1/publication-after78/MODIFIED_FILE.py', 'sha256': 'bcf4dc63d2772b92ae46527103277a60559902947eb52d7735dd66a0c55a4644'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-eks2274-original-402-20260918-v1/publication-after78/archive_adapter.py', 'sha256': 'ae2ffb4fd00731961af0bb8148b67ae96b5a2dcfe11709056b0ac05dcd63c320'}}

def select_verified_eks79(config):
    """EKS native79 only; retain the latest verified78 route until archive closure."""
    if (config["expected_tasks"] != 402 or config.get("_eks_maximum_delta", 79) < 79
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (EKS_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_pyomo78(config)
    try:
        import importlib.util
        helper = Path(EKS_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != EKS_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed EKS helper SHA differs")
        spec = importlib.util.spec_from_file_location("_eks79_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(EKS_REGISTRY_ROOT, EKS_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected79 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_eks_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_pyomo78(config)
        return {**fallback, "_authority_rejection": "EKS79 not closed; retaining verified78 and inherited lineages: " + str(exc)}, evidence


CODECOV_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-codecov90-original-402-20260918-v1/publication-after79')
CODECOV_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-codecov90-original-402-20260918-v1/publication-after79/authority_reader.py', 'sha256': '3002878706267baf1469abcef1024d8e681acf7a2f86bc307d79d544393becaa'}
CODECOV_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-codecov90-original-402-20260918-v1/publication-after79/MODIFIED_FILE.py', 'sha256': '27db4434f054fa34643218cf8aabef777c983c0e1aad771137633fb46243e2e7'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-codecov90-original-402-20260918-v1/publication-after79/archive_adapter.py', 'sha256': 'a232b402436b961e13af917a22785fe995813f2fd8f00f670b6c58be9bd8a5ae'}}

def select_verified_codecov80(config):
    """Only closed native Codecov80; otherwise retain actual verified79 and its lineages."""
    if (config["expected_tasks"] != 402 or config.get("_codecov_maximum_delta", 80) < 80
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (CODECOV_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_eks79(config)
    try:
        import importlib.util
        helper = Path(CODECOV_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != CODECOV_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Codecov helper SHA differs")
        spec = importlib.util.spec_from_file_location("_codecov80_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(CODECOV_REGISTRY_ROOT, CODECOV_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected80 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_codecov_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_eks79(config)
        return {**fallback, "_authority_rejection": "Codecov80 not closed; retaining verified79: " + str(exc)}, evidence


REDIS_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-redis3350-original-402-20260918-v1/publication-after80/context-admission-v1')
REDIS_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-redis3350-original-402-20260918-v1/publication-after80/context-admission-v1/authority_reader.py', 'sha256': '2da698392181a9903e3a0f1f11ffe2fd826cb7fe6df2c8772ca9a2c5708e920f'}
REDIS_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-redis3350-original-402-20260918-v1/publication-after80/context-admission-v1/MODIFIED_FILE.py', 'sha256': '6ee0bc083e3849a915367bc2493b440b003c37c48c514c00daf8e0b4064e0672'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-redis3350-original-402-20260918-v1/publication-after80/context-admission-v1/archive_adapter.py', 'sha256': 'f636d446749326cf9a0553e001c31e412997b2d37094a678b3e90d59878de2b0'}}

def select_verified_redis81(config):
    """Only a closed complete-pair81 archive; otherwise retain verified80."""
    if (config["expected_tasks"] != 402 or config.get("_redis_maximum_delta", 81) < 81
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (REDIS_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_codecov80(config)
    try:
        import importlib.util
        helper = Path(REDIS_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != REDIS_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Redis helper SHA differs")
        spec = importlib.util.spec_from_file_location("_redis81_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(REDIS_REGISTRY_ROOT, REDIS_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected81 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_redis_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_codecov80(config)
        return {**fallback, "_authority_rejection": "Redis81 not closed; retaining verified80: " + str(exc)}, evidence


ALCHEMISCALE_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-alchemiscale421-original-402-20260918-v1/publication-after81')
ALCHEMISCALE_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-alchemiscale421-original-402-20260918-v1/publication-after81/authority_reader.py', 'sha256': 'd89c839852fa6eaa60ae2cc1a4e3edec648dcc7195d46a2bcf95b63d65eb0eaf'}
ALCHEMISCALE_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-alchemiscale421-original-402-20260918-v1/publication-after81/MODIFIED_FILE.py', 'sha256': 'ec238f96d247dc8514f633760c4945f0269b52bb85a05d9b3ae42655b46d74b5'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-alchemiscale421-original-402-20260918-v1/publication-after81/archive_adapter.py', 'sha256': 'f1bf4d08028ca6505fde353ac2cded6e05736074a66bc11bfec10d915cfa95b7'}}

def select_verified_alchemiscale82(config):
    """Only a closed complete-pair82 archive; otherwise retain verified81."""
    if (config["expected_tasks"] != 402 or config.get("_alchemiscale_maximum_delta", 82) < 82
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (ALCHEMISCALE_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_redis81(config)
    try:
        import importlib.util
        helper = Path(ALCHEMISCALE_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != ALCHEMISCALE_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Alchemiscale helper SHA differs")
        spec = importlib.util.spec_from_file_location("_alchemiscale82_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(ALCHEMISCALE_REGISTRY_ROOT, ALCHEMISCALE_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected82 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_alchemiscale_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_redis81(config)
        return {**fallback, "_authority_rejection": "Alchemiscale82 not closed; retaining verified81: " + str(exc)}, evidence


OPENFORMS_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-openforms3125-original-402-20260918-v1/publication-after82')
OPENFORMS_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-openforms3125-original-402-20260918-v1/publication-after82/authority_reader.py', 'sha256': '8e458788d93e2ccfd8e0cb398da9307557cc3a370798c55ff8d71462bdc4214a'}
OPENFORMS_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-openforms3125-original-402-20260918-v1/publication-after82/MODIFIED_FILE.py', 'sha256': '49c58990c9323155b61839b709266e35db492de2bd4f114b0d0d61186148243a'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-openforms3125-original-402-20260918-v1/publication-after82/archive_adapter.py', 'sha256': '05adced4d44298112ed235028d1f46ff1c29519fd28fc4014f7a3c57570e24c8'}}

def select_verified_openforms83(config):
    """Only a closed complete-pair83 archive; otherwise retain verified82."""
    if (config["expected_tasks"] != 402 or config.get("_openforms_maximum_delta", 83) < 83
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (OPENFORMS_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_alchemiscale82(config)
    try:
        import importlib.util
        helper = Path(OPENFORMS_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != OPENFORMS_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Openforms helper SHA differs")
        spec = importlib.util.spec_from_file_location("_openforms83_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(OPENFORMS_REGISTRY_ROOT, OPENFORMS_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected83 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_openforms_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_alchemiscale82(config)
        return {**fallback, "_authority_rejection": "Openforms83 not closed; retaining verified82: " + str(exc)}, evidence


LYSE_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-lyse119-original-402-20260918-v1/publication-after83')
LYSE_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-lyse119-original-402-20260918-v1/publication-after83/authority_reader.py', 'sha256': '8f526349f532096b81a288bf95c5003864426b262743e210f685298697672e0a'}
LYSE_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-lyse119-original-402-20260918-v1/publication-after83/MODIFIED_FILE.py', 'sha256': 'd73511a5238e0e00764dc5b9d887b7df70c7ac6dbe90f8b33ca8260602a9106e'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-lyse119-original-402-20260918-v1/publication-after83/archive_adapter.py', 'sha256': 'b366a4a99837d9541313ce6e3e9a5b07dbd88ce43727b18517607295983a9586'}}

def select_verified_lyse84(config):
    """Only a closed complete-pair84 archive; otherwise retain verified83."""
    if (config["expected_tasks"] != 402 or config.get("_lyse_maximum_delta", 84) < 84
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (LYSE_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_openforms83(config)
    try:
        import importlib.util
        helper = Path(LYSE_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != LYSE_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Lyse helper SHA differs")
        spec = importlib.util.spec_from_file_location("_lyse84_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(LYSE_REGISTRY_ROOT, LYSE_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected84 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_lyse_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_openforms83(config)
        return {**fallback, "_authority_rejection": "Lyse84 not closed; retaining verified83: " + str(exc)}, evidence


FINCH_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-finch780-original-402-20260918-v1/publication-after84')
FINCH_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-finch780-original-402-20260918-v1/publication-after84/authority_reader.py', 'sha256': '1c55ee9c728cb143a8d3f8d82eaa1f90362e7bbdc3a59de7f1fc445bcbdf8727'}
FINCH_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-finch780-original-402-20260918-v1/publication-after84/MODIFIED_FILE.py', 'sha256': 'ff5fcbf9253147e90db4c3a43b95e2101d3110040446f6bfbc281a555205ca5e'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-finch780-original-402-20260918-v1/publication-after84/archive_adapter.py', 'sha256': '92961180cdfb9aeecb8c67472ed351706f8c71950855bf5920e8a7df5d9d236f'}}

def select_verified_finch85(config):
    """Only a closed complete-pair85 archive; otherwise retain verified84."""
    if (config["expected_tasks"] != 402 or config.get("_finch_maximum_delta", 85) < 85
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (FINCH_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_lyse84(config)
    try:
        import importlib.util
        helper = Path(FINCH_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != FINCH_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Finch helper SHA differs")
        spec = importlib.util.spec_from_file_location("_finch85_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(FINCH_REGISTRY_ROOT, FINCH_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected85 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_finch_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_lyse84(config)
        return {**fallback, "_authority_rejection": "Finch85 not closed; retaining verified84: " + str(exc)}, evidence


OTELMONGO_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-otelmongo6172-original-402-20260918-v1/publication-after85')
OTELMONGO_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-otelmongo6172-original-402-20260918-v1/publication-after85/authority_reader.py', 'sha256': '2ab27bc1c8d5129c38f6a10ec4f88a2729078facf0c1a4a48a6455f10a8c8139'}
OTELMONGO_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-otelmongo6172-original-402-20260918-v1/publication-after85/MODIFIED_FILE.py', 'sha256': 'b2adf56f5037efa156cc9c31d4ad61ab5d183bd47076eafcbef5a68692b7adff'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-otelmongo6172-original-402-20260918-v1/publication-after85/archive_adapter.py', 'sha256': 'a9b62d6500783ff59bc0463c3bb99c45393266e0ddcb4f826fc54ce6b124749e'}}

def select_verified_otelmongo86(config):
    """Only a closed complete-pair86 archive; otherwise retain verified85."""
    if (config["expected_tasks"] != 402 or config.get("_otelmongo_maximum_delta", 86) < 86
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (OTELMONGO_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_finch85(config)
    try:
        import importlib.util
        helper = Path(OTELMONGO_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != OTELMONGO_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed OTelMongo helper SHA differs")
        spec = importlib.util.spec_from_file_location("_otelmongo86_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(OTELMONGO_REGISTRY_ROOT, OTELMONGO_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected86 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_otelmongo_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_finch85(config)
        return {**fallback, "_authority_rejection": "OTelMongo86 not closed; retaining verified85: " + str(exc)}, evidence


METRICS_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-metrics13-original-402-20260918-v1/publication-after86')
METRICS_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-metrics13-original-402-20260918-v1/publication-after86/authority_reader.py', 'sha256': '363345d9bb8d4d4306db2e457e63dea14d070dc413c6509a4dee5f8af5021ab5'}
METRICS_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-metrics13-original-402-20260918-v1/publication-after86/MODIFIED_FILE.py', 'sha256': '662a720e4ef6750267ad860ad1a12544bc6b085729b591bfe5d267dfee89a25f'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-metrics13-original-402-20260918-v1/publication-after86/archive_adapter.py', 'sha256': '4121b6498872e7cba69ef6a6813c4514a89715ddb0e609a39494de7664711b23'}}

def select_verified_metrics87(config):
    """Only a closed complete-pair87 archive; otherwise retain verified86."""
    if (config["expected_tasks"] != 402 or config.get("_metrics_maximum_delta", 87) < 87
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (METRICS_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_otelmongo86(config)
    try:
        import importlib.util
        helper = Path(METRICS_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != METRICS_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Metrics helper SHA differs")
        spec = importlib.util.spec_from_file_location("_metrics87_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(METRICS_REGISTRY_ROOT, METRICS_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected87 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_metrics_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_otelmongo86(config)
        return {**fallback, "_authority_rejection": "Metrics87 not closed; retaining verified86: " + str(exc)}, evidence


SEQUI_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sequi44-original-402-20260918-v1/publication-after87')
SEQUI_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sequi44-original-402-20260918-v1/publication-after87/main-reader-deployment-v1/helper-schema-fix-v1/MODIFIED_FILE.py', 'sha256': '8cedbb4dda57ab17de19e56fb9a309eb5eeff06b77dd40e14f6c6257197c2745'}
SEQUI_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sequi44-original-402-20260918-v1/publication-after87/MODIFIED_FILE.py', 'sha256': '538f0dbf78f7884d3e246474b3f196c8b70c8badb5361f7664e65395305817f9'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sequi44-original-402-20260918-v1/publication-after87/archive_adapter.py', 'sha256': '0dfed956a348f1336683ee07c2beff18a3d82baff74402a4ba8548a97100d57b'}}

def select_verified_sequi88(config):
    """Only a closed complete-pair88 archive; otherwise retain verified87."""
    if (config["expected_tasks"] != 402 or config.get("_sequi_maximum_delta", 88) < 88
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (SEQUI_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_metrics87(config)
    try:
        import importlib.util
        helper = Path(SEQUI_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != SEQUI_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed SeqUI helper SHA differs")
        spec = importlib.util.spec_from_file_location("_sequi88_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(SEQUI_REGISTRY_ROOT, SEQUI_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected88 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_sequi_registry": True, "_verified_archive_authority": {key: result[key] for key in
                    ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_metrics87(config)
        return {**fallback, "_authority_rejection": "SeqUI88 not closed; retaining verified87: " + str(exc)}, evidence


FLUID_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-fluid27466-original-402-20260918-v1/publication-after88')
FLUID_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-fluid27466-original-402-20260918-v1/publication-after88/authority_reader.py', 'sha256': '864531470fc1149242acb395bdbbb38146e122574240c7c16c28960778ec4486'}
FLUID_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-fluid27466-original-402-20260918-v1/publication-after88/MODIFIED_FILE.py', 'sha256': 'bebf1cfcf0512f5a551cdb4b86895ea94830a11c7087413083de496cd91d334c'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-fluid27466-original-402-20260918-v1/publication-after88/archive_adapter.py', 'sha256': '17f05469047fc2f0f68d9d8af9292fad5e34b7669977f88865e4f1aff684e921'}}

def select_verified_fluid89(config):
    """Select only closed complete89; keep corrected verified88 otherwise."""
    if (config["expected_tasks"] != 402 or config.get("_fluid_maximum_delta", 89) < 89
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (FLUID_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_sequi88(config)
    try:
        import importlib.util
        helper = Path(FLUID_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != FLUID_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Fluid helper SHA differs")
        spec = importlib.util.spec_from_file_location("_fluid89_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(FLUID_REGISTRY_ROOT, FLUID_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected89 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_fluid_registry": True, "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_sequi88(config)
        return {**fallback, "_authority_rejection": "Fluid89 not closed; retaining verified88: " + str(exc)}, evidence


DAFFODIL_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-daffodil1112-original-402-20260918-v1/publication-after89-metadata-v2')
DAFFODIL_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-daffodil1112-original-402-20260918-v1/publication-after89-metadata-v2/authority_reader.py', 'sha256': 'a4e3f444adad70ab9e8dad09560756dbebecd3c1c2ef28a6c29653b331734ff9'}
DAFFODIL_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-daffodil1112-original-402-20260918-v1/publication-after89-metadata-v2/MODIFIED_FILE.py', 'sha256': 'b5b4a421315ecdbd680cff49e5001864d39a32381b78aa24ab397a8186e5ad92'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-daffodil1112-original-402-20260918-v1/publication-after89-metadata-v2/archive_adapter.py', 'sha256': 'f58db51e45e8c8aceedb2103450f6b06adcef30d7e29d3d05bdbac6ca8e7bea2'}}

def select_verified_daffodil90(config):
    """Select only closed complete90; otherwise retain actual verified89."""
    if (config["expected_tasks"] != 402 or config.get("_daffodil_maximum_delta", 90) < 90
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (DAFFODIL_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_fluid89(config)
    try:
        import importlib.util
        helper = Path(DAFFODIL_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != DAFFODIL_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Daffodil helper SHA differs")
        spec = importlib.util.spec_from_file_location("_daffodil90_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(DAFFODIL_REGISTRY_ROOT, DAFFODIL_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected90 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_daffodil_registry": True, "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_fluid89(config)
        return {**fallback, "_authority_rejection": "Daffodil90 not closed; retaining verified89: " + str(exc)}, evidence


SNIFFY_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sniffy554-original-402-20260918-v1/publication-after90')
SNIFFY_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sniffy554-original-402-20260918-v1/publication-after90/authority_reader.py', 'sha256': '1e46b49c7b254059241471e0b32c4f213292f0b203b8ccd87580a34e6c48487d'}
SNIFFY_REGISTRY_CODES = {'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sniffy554-original-402-20260918-v1/publication-after90/MODIFIED_FILE.py', 'sha256': '95ae66a27ab5396dad75009eb0d1363385247ffa2b022c6d8890f326beeca17f'}, 'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sniffy554-original-402-20260918-v1/publication-after90/archive_adapter.py', 'sha256': '002f1162ea5ce42d1bd8a27b251777ae5d3cb1d84640c46c44e0674b0e94e8e6'}}

def select_verified_sniffy91(config):
    """Select only closed complete91; otherwise retain actual verified90."""
    if (config["expected_tasks"] != 402 or config.get("_sniffy_maximum_delta", 91) < 91
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not (SNIFFY_REGISTRY_ROOT / "HEAD.json").is_file()):
        return select_verified_daffodil90(config)
    try:
        import importlib.util
        helper = Path(SNIFFY_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != SNIFFY_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Daffodil helper SHA differs")
        spec = importlib.util.spec_from_file_location("_sniffy91_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(SNIFFY_REGISTRY_ROOT, SNIFFY_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected91 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_sniffy_registry": True, "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_daffodil90(config)
        return {**fallback, "_authority_rejection": "Sniffy91 not closed; retaining verified90: " + str(exc)}, evidence


METALS_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-metals4397-original-402-20260920-v1/publication-after91-prep-v1')
METALS_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-metals4397-original-402-20260920-v1/progress-reader92-prep-v1/authority_reader.py', 'sha256': 'afc85bbe39fd50ef85fc19cc325f7a5c561706da7f35f8dd9943faf3fb203996'}
METALS_REGISTRY_CODES = {'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-metals4397-original-402-20260920-v1/publication-after91-prep-v1/archive_adapter.py', 'sha256': '572928425a9196edbde35fbf6539a88791173a7c5361187ef23efd98fb228f2b'}, 'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-metals4397-original-402-20260920-v1/publication-after91-prep-v1/MODIFIED_FILE.py', 'sha256': '8bd9eb4fc81229a490706f23b877ffc7d809a8d5bf84cafd32d281463f4558ad'}}
METALS_MAIN_MARKER = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-metals4397-original-402-20260920-v1/publication-after91-prep-v1/MAIN_VERIFIED92.json')


def select_verified_metals92(config):
    """Select independently Main-verified92; otherwise retain actual verified91."""
    if (config["expected_tasks"] != 402 or config.get("_metals_maximum_delta", 92) < 92
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not METALS_MAIN_MARKER.is_file()):
        return select_verified_sniffy91(config)
    try:
        import importlib.util
        helper = Path(METALS_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != METALS_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Metals92 helper SHA differs")
        spec = importlib.util.spec_from_file_location("_metals92_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(METALS_REGISTRY_ROOT, METALS_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected92 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_metals_registry": True, "_metals_Main_verified": result["Main_verified"],
                "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_sniffy91(config)
        return {**fallback, "_authority_rejection": "Metals92 Main approval/closure invalid; retaining verified91: " + str(exc)}, evidence


TSFILE_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-tsfile823-original-402-20260920-v1/publication-after92-prep-v1')
TSFILE_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-tsfile823-original-402-20260920-v1/progress-reader93-prep-v1/authority_reader.py', 'sha256': 'a52bec56b1331ab3e3026591ea41ff82ae24c311a00e2ee32d77d7db964ef3c5'}
TSFILE_REGISTRY_CODES = {'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-tsfile823-original-402-20260920-v1/publication-after92-prep-v1/archive_adapter.py', 'sha256': 'bb7b80e261445f0331b659527cfa0193869431cdb4b7070643261658ff7dffe6'}, 'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-tsfile823-original-402-20260920-v1/publication-after92-prep-v1/MODIFIED_FILE.py', 'sha256': '86f4b55c73f845cf7209b075af12793427f7b39a2bf3ff360d1ed128d6937ca2'}}
TSFILE_MAIN_MARKER = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-tsfile823-original-402-20260920-v1/publication-after92-prep-v1/MAIN_VERIFIED93.json')

def select_verified_tsfile93(config):
    """Select independently Main-verified93; otherwise retain actual verified92."""
    if (config["expected_tasks"] != 402 or config.get("_tsfile_maximum_delta", 93) < 93
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not TSFILE_MAIN_MARKER.is_file()):
        return select_verified_metals92(config)
    try:
        import importlib.util
        helper = Path(TSFILE_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != TSFILE_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed TsFile93 helper SHA differs")
        spec = importlib.util.spec_from_file_location("_tsfile92_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(TSFILE_REGISTRY_ROOT, TSFILE_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected93 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_tsfile_registry": True, "_tsfile_Main_verified": result["Main_verified"],
                "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback, evidence = select_verified_metals92(config)
        return {**fallback, "_authority_rejection": "TsFile93 Main approval/closure invalid; retaining verified92: " + str(exc)}, evidence


SKLEARN_REGISTRY_ROOT = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sklearn18359-original-402-20260920-v1/publication-after93-prep-v1')
SKLEARN_REGISTRY_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sklearn18359-original-402-20260920-v1/progress-reader94-prep-v1/authority_reader.py', 'sha256': '2131310680fe4373e78b435e5d1f51ed70cc9a3f314a2c2171bacae27f87a86c'}
SKLEARN_REGISTRY_CODES = {'archive': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sklearn18359-original-402-20260920-v1/publication-after93-prep-v1/archive_adapter.py', 'sha256': '856a0f458aa7a92c64b2a05c3dc5ab587a16b763f13fde3021b62b0fa23c67b3'}, 'publisher': {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sklearn18359-original-402-20260920-v1/publication-after93-prep-v1/MODIFIED_FILE.py', 'sha256': '982f27117ef525bb2ba546331bfc807e8353d43449fe07a718b6681486f34407'}}
SKLEARN_MAIN_MARKER = Path('/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sklearn18359-original-402-20260920-v1/publication-after93-prep-v1/MAIN_VERIFIED94.json')


def select_verified_sklearn94(config):
    """Require separately Main-bound94 closure; missing/corrupt/pending retains verified93."""
    if (config["expected_tasks"] != 402 or config.get("_sklearn_maximum_delta", 94) < 94
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())
            or not SKLEARN_MAIN_MARKER.is_file()):
        return select_verified_tsfile93(config)
    try:
        import importlib.util
        helper = Path(SKLEARN_REGISTRY_HELPER["path"])
        if helper.is_symlink() or hashlib.sha256(helper.read_bytes()).hexdigest() != SKLEARN_REGISTRY_HELPER["sha256"]:
            raise ValueError("reviewed Sklearn94 helper SHA differs")
        spec = importlib.util.spec_from_file_location("_sklearn94_readonly", helper)
        module = importlib.util.module_from_spec(spec)
        exec(compile(helper.read_bytes(), str(helper), "exec"), module.__dict__)
        result, evidence = module.verify(SKLEARN_REGISTRY_ROOT, SKLEARN_REGISTRY_CODES)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("selected94 report changed")
        report_metrics(report, 402)
        return {**config, "report": result["report"]["path"], "_fallback_report": config["report"],
                "_delta69_base_config": dict(config), "_selected_report_sha256": checksum,
                "_sklearn_registry": True, "_sklearn_Main_verified": result["Main_verified"],
                "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication", "parent_authority", "measurement_lineage")}}, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
        fallback, evidence = select_verified_tsfile93(config)
        return {**fallback, "_authority_rejection": "Sklearn94 Main approval/closure pending or invalid; retaining verified93: " + str(exc)}, evidence


MFS_READER_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-mfscreener333-original-402-20260920-v1/progress-reader97-prep-v1/authority_reader.py', 'sha256': '1efbc12ad2b7a012c79ff06f509a089689f2b070f6d0a841850ca3c83b960937'}


def select_verified_mfs(config):
    """95/96/97 each require their actual Main closure; never skip a bad parent."""
    maximum = config.get("_mfs_maximum_delta", 97)
    if (config["expected_tasks"] != 402 or maximum < 94
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return select_verified_sklearn94({**config, "_sklearn_maximum_delta": min(maximum, 94)})
    try:
        import importlib.util
        path = Path(MFS_READER_HELPER["path"])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != MFS_READER_HELPER["sha256"]:
            raise ValueError("MFS helper SHA differs")
        spec = importlib.util.spec_from_file_location("_mfs_readonly", path)
        helper = importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(), str(path), "exec"), helper.__dict__)
        result, evidence = helper.verify(maximum_delta=maximum)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("MFS selected report differs")
        report_metrics(report, 402)
        selected = {**config, "report": result["report"]["path"],
                    "_fallback_report": config["report"], "_delta69_base_config": dict(config),
                    "_selected_report_sha256": checksum, "_mfs_registry": True,
                    "_mfs_guard_refs": result["selection_guard_refs"],
                    "_mfs_Main_verified": result["Main_verified"],
                    "_mfs_historical_root": result.get("historical_root"),
                    "_verified_archive_authority": {k: result[k] for k in
                        ("delta", "authority", "metrics", "archive_publication",
                         "parent_authority", "measurement_lineage")}}
        if result["rejections"]:
            selected["_authority_rejection"] = "; ".join(result["rejections"])
        return selected, evidence
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
        fallback, evidence = select_verified_sklearn94(config)
        return {**fallback, "_authority_rejection":
                "MFS reader binding invalid; retaining original verified selector: " + str(exc)}, evidence


# Additive native-repair batch98. Existing95/96/97 selectors stay intact.
INFRA_BATCH98_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-panel-publication-20260920-v1/reader/authority_reader.py', 'sha256': 'fbaed3d646340271ce5380736b4431c036f6f574418a998dee5ac0f925e46693'}
INFRA_BATCH98_AUTHORITY = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-panel-publication-20260920-v1/authority.json', 'sha256': 'fbd88f85c860c94cd97aca935051d5bd7762078c2fcf10990c58352509c0bfe9'}
INFRA_BATCH98_MAIN = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-panel-publication-20260920-v1/MAIN_VERIFIED98.json', 'sha256': '33244244df334848d6095cbaa591a291a44c97ad856c9a247ae1a00c758e0e8d'}


def select_verified_infra_batch98(config):
    if (config.get("expected_tasks") != 402
            or config.get("_infra_batch_maximum_delta", 98) < 98
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return select_verified_mfs(config)
    try:
        import importlib.util
        path = Path(INFRA_BATCH98_HELPER["path"])
        if (path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()
                != INFRA_BATCH98_HELPER["sha256"]):
            raise ValueError("native batch98 reader helper differs")
        spec = importlib.util.spec_from_file_location("_native_batch98_reader", path)
        helper = importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(), str(path), "exec"), helper.__dict__)
        result, evidence = helper.verify(INFRA_BATCH98_AUTHORITY, INFRA_BATCH98_MAIN)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("native batch98 report differs")
        report_metrics(report, 402)
        return {
            **config, "report": result["report"]["path"],
            "_fallback_report": config["report"], "_delta69_base_config": dict(config),
            "_selected_report_sha256": checksum, "_infra_batch_registry": True,
            "_infra_batch_guard_refs": [INFRA_BATCH98_HELPER, *result["selection_guard_refs"]],
            "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication",
                 "parent_authority", "measurement_lineage")},
        }, [INFRA_BATCH98_HELPER, *evidence]
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
        fallback, evidence = select_verified_mfs(config)
        return {**fallback, "_authority_rejection":
                "native batch98 not closed; retaining verified97 or its original fallback: " + str(exc)}, evidence


# Additive native-repair batch99. Existing95/96/97 selectors stay intact.
RIPPLED99_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-panel-publication-20260920-v1/rippled99/reader/authority_reader.py', 'sha256': 'fcec3376de6c94a3d60c38c8b0edc690ff048499426150e7d7f99a2c3e91ff7a'}
RIPPLED99_AUTHORITY = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-panel-publication-20260920-v1/rippled99/authority.json', 'sha256': '3abc5e4ecf8b17322e1bda429c221460e24a3a7beaa1d402912c84a552149b47'}
RIPPLED99_MAIN = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-panel-publication-20260920-v1/rippled99/MAIN_VERIFIED99.json', 'sha256': 'a7c37ecaba1300f880dd47829ad8d3cb90934a83e80fb0e36e5bf661a2fb3288'}


def select_verified_rippled99(config):
    if (config.get("expected_tasks") != 402
            or config.get("_rippled99_maximum_delta", 99) < 99
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return select_verified_infra_batch98(config)
    try:
        import importlib.util
        path = Path(RIPPLED99_HELPER["path"])
        if (path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()
                != RIPPLED99_HELPER["sha256"]):
            raise ValueError("native batch99 reader helper differs")
        spec = importlib.util.spec_from_file_location("_native_batch99_reader", path)
        helper = importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(), str(path), "exec"), helper.__dict__)
        result, evidence = helper.verify(RIPPLED99_AUTHORITY, RIPPLED99_MAIN)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("native batch99 report differs")
        report_metrics(report, 402)
        return {
            **config, "report": result["report"]["path"],
            "_fallback_report": config["report"], "_delta69_base_config": dict(config),
            "_selected_report_sha256": checksum, "_rippled99_registry": True,
            "_rippled99_guard_refs": [RIPPLED99_HELPER, *result["selection_guard_refs"]],
            "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication",
                 "parent_authority", "measurement_lineage")},
        }, [RIPPLED99_HELPER, *evidence]
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
        fallback, evidence = select_verified_infra_batch98(config)
        return {**fallback, "_authority_rejection":
                "native batch99 not closed; retaining verified98 or its original fallback: " + str(exc)}, evidence


# Additive Mindscape100; the complete99 -> 98 -> 97 chain stays unchanged.
MINDSCAPE100_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-mindscape2-reference-completion-20260920-v1/publication100/reader/authority_reader.py', 'sha256': 'dd1ae0e8f5e6e2373ede42296da131003a941c02ab3fc2ba0aacf76d4895fde9'}
MINDSCAPE100_AUTHORITY = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-mindscape2-reference-completion-20260920-v1/publication100/authority.json', 'sha256': '7aecb4c5c2f97e0c722759ddd69395dfd25203de2a7d40d0cd82832e589cbc56'}
MINDSCAPE100_MAIN = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-mindscape2-reference-completion-20260920-v1/publication100/MAIN_VERIFIED100.json', 'sha256': '3bc943d873168cdd5b1ecc3a5489255327200e3c076e2714f3135d3a7a7f58af'}


def select_verified_mindscape100(config):
    if (config.get("expected_tasks") != 402
            or config.get("_mindscape100_maximum_delta", 100) < 100
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return select_verified_rippled99(config)
    try:
        import importlib.util
        path = Path(MINDSCAPE100_HELPER["path"])
        if (any(p.is_symlink() for p in (path, *path.parents))
                or hashlib.sha256(path.read_bytes()).hexdigest() != MINDSCAPE100_HELPER["sha256"]):
            raise ValueError("frozen Mindscape100 helper differs")
        spec = importlib.util.spec_from_file_location("_native_mindscape100_reader", path)
        helper = importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(), str(path), "exec"), helper.__dict__)
        result, evidence = helper.verify(MINDSCAPE100_AUTHORITY, MINDSCAPE100_MAIN)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("Mindscape100 report differs")
        report_metrics(report, 402)
        return {
            **config, "report": result["report"]["path"],
            "_fallback_report": config["report"], "_delta69_base_config": dict(config),
            "_selected_report_sha256": checksum, "_mindscape100_registry": True,
            "_mindscape100_guard_refs": [MINDSCAPE100_HELPER, *result["selection_guard_refs"]],
            "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication",
                 "parent_authority", "measurement_lineage")},
        }, [MINDSCAPE100_HELPER, *evidence]
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, EOFError) as exc:
        fallback, evidence = select_verified_rippled99(config)
        return {**fallback, "_authority_rejection":
                "Mindscape100 not closed; retaining verified99 or its original fallback: " + str(exc)}, evidence


# Additive Percona101; the complete100 and all inherited selectors stay unchanged.
PERCONA101_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-percona1610-reference-completion-20260920-v2/publication101/reader/authority_reader.py', 'sha256': '282c1fa6faa2e97a91f9957f3165f49b2e63bd4bf66eff14e22d713482d98414'}
PERCONA101_AUTHORITY = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-percona1610-reference-completion-20260920-v2/publication101/authority.json', 'sha256': '4a56d790486ac7bd050a847377cdb996b25562a7076b12d4f486680b53a97b82'}
PERCONA101_MAIN = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-percona1610-reference-completion-20260920-v2/publication101/MAIN_VERIFIED101.json', 'sha256': 'e45826771ff0351eb6da1134c1c5038109cf53fdb1e5fd1e92576e632898bdcb'}


def select_verified_percona101(config):
    if (config.get("expected_tasks") != 402
            or config.get("_percona101_maximum_delta", 101) < 101
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return select_verified_mindscape100(config)
    try:
        import importlib.util
        path = Path(PERCONA101_HELPER["path"])
        if (any(p.is_symlink() for p in (path, *path.parents))
                or hashlib.sha256(path.read_bytes()).hexdigest() != PERCONA101_HELPER["sha256"]):
            raise ValueError("frozen Percona101 helper differs")
        spec = importlib.util.spec_from_file_location("_native_percona101_reader", path)
        helper = importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(), str(path), "exec"), helper.__dict__)
        result, evidence = helper.verify(PERCONA101_AUTHORITY, PERCONA101_MAIN)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("Percona101 report differs")
        report_metrics(report, 402)
        return {
            **config, "report": result["report"]["path"],
            "_fallback_report": config["report"], "_delta69_base_config": dict(config),
            "_selected_report_sha256": checksum, "_percona101_registry": True,
            "_percona101_guard_refs": [PERCONA101_HELPER, *result["selection_guard_refs"]],
            "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication",
                 "parent_authority", "measurement_lineage")},
        }, [PERCONA101_HELPER, *evidence]
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, EOFError) as exc:
        fallback, evidence = select_verified_mindscape100(config)
        return {**fallback, "_authority_rejection":
                "Percona101 not closed; retaining verified100 or its original fallback: " + str(exc)}, evidence


# Additive Sourcegraph102; the complete101 and all inherited selectors stay unchanged.
SOURCEGRAPH102_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sourcegraph46774-reference-completion-20260921-v1/publication102/reader/authority_reader.py', 'sha256': '90e1cf2c623056b4df736f5227b045e2956d9ab7b131ec310457cbed409f3daa'}
SOURCEGRAPH102_AUTHORITY = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sourcegraph46774-reference-completion-20260921-v1/publication102/authority.json', 'sha256': '1ca428fc775a23a3798ca9527112a39475b364d30f0fe5c29d7f4e8d9cec48fb'}
SOURCEGRAPH102_MAIN = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-sourcegraph46774-reference-completion-20260921-v1/publication102/MAIN_VERIFIED102.json', 'sha256': '525c6f7c56275469e0120eaeeb5b972fff5aa4a037e44319cc1d2ae0264a6f9e'}


def select_verified_sourcegraph102(config):
    if (config.get("expected_tasks") != 402
            or config.get("_sourcegraph102_maximum_delta", 102) < 102
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return select_verified_percona101(config)
    try:
        import importlib.util
        path = Path(SOURCEGRAPH102_HELPER["path"])
        if (any(p.is_symlink() for p in (path, *path.parents))
                or hashlib.sha256(path.read_bytes()).hexdigest() != SOURCEGRAPH102_HELPER["sha256"]):
            raise ValueError("frozen Sourcegraph102 helper differs")
        spec = importlib.util.spec_from_file_location("_native_sourcegraph102_reader", path)
        helper = importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(), str(path), "exec"), helper.__dict__)
        result, evidence = helper.verify(SOURCEGRAPH102_AUTHORITY, SOURCEGRAPH102_MAIN)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("Sourcegraph102 report differs")
        report_metrics(report, 402)
        return {
            **config, "report": result["report"]["path"],
            "_fallback_report": config["report"], "_delta69_base_config": dict(config),
            "_selected_report_sha256": checksum, "_sourcegraph102_registry": True,
            "_sourcegraph102_guard_refs": [SOURCEGRAPH102_HELPER, *result["selection_guard_refs"]],
            "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication",
                 "parent_authority", "measurement_lineage")},
        }, [SOURCEGRAPH102_HELPER, *evidence]
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, EOFError) as exc:
        fallback, evidence = select_verified_percona101(config)
        return {**fallback, "_authority_rejection":
                "Sourcegraph102 not closed; retaining verified101 or its original fallback: " + str(exc)}, evidence


# Additive Delta103; the complete102 and all inherited selectors stay unchanged.
DELTA103_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-delta4805-reference-completion-20260921-v2/publication103/reader/authority_reader.py', 'sha256': 'a63bf34ed5d82faa443b848d663f57297a14621c1cb006fb0eb0fe4ec4debf9c'}
DELTA103_AUTHORITY = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-delta4805-reference-completion-20260921-v2/publication103/authority.json', 'sha256': '4f9b4199d158bdcf1c468a14e144c8823f65d440fdeb525ab9d907ca560e74c4'}
DELTA103_MAIN = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-delta4805-reference-completion-20260921-v2/publication103/MAIN_VERIFIED103.json', 'sha256': '54ae1b8001fb11a4b7e4e4a55d2788c880f1831dd3d43eaa28de79e5c546b6e7'}


def select_verified_delta103(config):
    if (config.get("expected_tasks") != 402
            or config.get("_delta103_maximum_delta", 103) < 103
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return select_verified_sourcegraph102(config)
    try:
        import importlib.util
        path = Path(DELTA103_HELPER["path"])
        if (any(p.is_symlink() for p in (path, *path.parents))
                or hashlib.sha256(path.read_bytes()).hexdigest() != DELTA103_HELPER["sha256"]):
            raise ValueError("frozen Delta103 helper differs")
        spec = importlib.util.spec_from_file_location("_native_delta103_reader", path)
        helper = importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(), str(path), "exec"), helper.__dict__)
        result, evidence = helper.verify(DELTA103_AUTHORITY, DELTA103_MAIN)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("Delta103 report differs")
        report_metrics(report, 402)
        return {
            **config, "report": result["report"]["path"],
            "_fallback_report": config["report"], "_delta69_base_config": dict(config),
            "_selected_report_sha256": checksum, "_delta103_registry": True,
            "_delta103_guard_refs": [DELTA103_HELPER, *result["selection_guard_refs"]],
            "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication",
                 "parent_authority", "measurement_lineage")},
        }, [DELTA103_HELPER, *evidence]
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, EOFError) as exc:
        fallback, evidence = select_verified_sourcegraph102(config)
        return {**fallback, "_authority_rejection":
                "Delta103 not closed; retaining verified102 or its original fallback: " + str(exc)}, evidence


# IoTDB104 additive selector. All <=103 selectors remain byte-for-byte unchanged.
IOTDB104_HELPER = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-iotdb5976-reference-completion-20260921-v1/progress-reader104/authority_reader.py', 'sha256': '47137f3e70abb5c380e98171a30616493c97065be1a13fb247b13b3eabd6f2d1'}
IOTDB104_AUTHORITY = {'path': '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-iotdb5976-reference-completion-20260921-v1/publication104/authority.json', 'sha256': '38b2aa1fadb5a95de7b7d8bd49b3cd826afb6a2e38bbded71725e0b72ab5613e'}
IOTDB104_MAIN = None
IOTDB104_MAIN_PATH = '/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/.benchmarks/agentic-iotdb5976-reference-completion-20260921-v1/publication104/MAIN_VERIFIED104.json'


def select_verified_iotdb104(config):
    if (config.get("expected_tasks") != 402
            or config.get("_iotdb104_maximum_delta", 104) < 104
            or not Path(config["report"]).resolve().is_relative_to(DELTA67_REPORT_ROOT.resolve())):
        return select_verified_delta103(config)
    try:
        import importlib.util
        path = Path(IOTDB104_HELPER["path"])
        code = path.read_bytes()
        if (any(p.is_symlink() for p in (path, *path.parents))
                or hashlib.sha256(code).hexdigest() != IOTDB104_HELPER["sha256"]):
            raise ValueError("frozen IoTDB104 helper differs")
        spec = importlib.util.spec_from_file_location("_iotdb104_archive_reader", path)
        helper = importlib.util.module_from_spec(spec)
        exec(compile(code, str(path), "exec"), helper.__dict__)
        main_ref = IOTDB104_MAIN or helper.pin(Path(IOTDB104_MAIN_PATH))
        result, evidence = helper.verify(IOTDB104_AUTHORITY, main_ref)
        report, checksum = read_json(result["report"]["path"])
        if checksum != result["report"]["sha256"]:
            raise ValueError("IoTDB104 report differs")
        report_metrics(report, 402)
        return {
            **config, "report": result["report"]["path"],
            "_fallback_report": config["report"], "_delta69_base_config": dict(config),
            "_selected_report_sha256": checksum, "_iotdb104_registry": True,
            "_iotdb104_guard_refs": [IOTDB104_HELPER, *result["selection_guard_refs"]],
            "_verified_archive_authority": {key: result[key] for key in
                ("delta", "authority", "metrics", "archive_publication",
                 "parent_authority", "measurement_lineage")},
        }, [IOTDB104_HELPER, *evidence]
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, EOFError) as exc:
        fallback, evidence = select_verified_delta103(config)
        return {**fallback, "_authority_rejection":
                "IoTDB104 not closed; retaining verified103: " + str(exc)}, evidence


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def count(value, name, maximum=None):
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise ValueError("invalid progress count: " + name)
    return value


def report_metrics(report, expected):
    if not isinstance(report, dict):
        raise ValueError("report must be a JSON object")
    if report.get("task_count") != expected or report.get("expected_pairs") != expected:
        raise ValueError("report denominator differs from the fixed " + str(expected))
    if type(report.get("complete")) is not bool:
        raise ValueError("report completion state is not explicit")
    boards = report.get("leaderboard")
    if not isinstance(boards, dict) or not boards:
        raise ValueError("report has no model measurements")
    models = {}
    for name, row in sorted(boards.items()):
        if not isinstance(row, dict) or row.get("tasks") != expected or type(row.get("complete")) is not bool:
            raise ValueError("model denominator or completion state differs")
        models[name] = {
            field: count(row[field], field, expected if field != "votes" else None)
            for field in ("resolved", "missing_measurements", "incomplete_gate_rows",
                          "generation_failed", "votes")
        }
        models[name]["complete"] = row["complete"]
    if report["complete"] and any(not row["complete"] or row["missing_measurements"]
                                  or row["incomplete_gate_rows"] for row in models.values()):
        raise ValueError("complete report still contains measurement gaps")
    return {
        "tasks": expected,
        "generations": count(report["measured_generations"], "generations", expected),
        "complete": report["complete"], "models": models,
        "engine": report.get("generation_engine_fingerprint"),
    }


def service_states(names):
    result = {}
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", name):
            raise ValueError("progress observer needs a literal service unit name")
        try:
            process = subprocess.run(
                ["systemctl", "--user", "show", name, "-p", "ActiveState", "-p", "SubState",
                 "-p", "NRestarts"], capture_output=True, text=True, timeout=3)
            if process.returncode:
                result[name] = {"ActiveState": "unknown"}
            else:
                result[name] = dict(line.split("=", 1) for line in process.stdout.splitlines()
                                    if "=" in line)
        except (OSError, subprocess.TimeoutExpired):
            result[name] = {"ActiveState": "unknown"}
    return result


def scoped_ids(config):
    if not config.get("scope_ids"):
        return None
    path = Path(config["scope_ids"])
    with path.open("rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("scope inventory exceeds the observation limit")
    values = data.decode().splitlines()
    if (len(values) != config["expected_tasks"] or len(set(values)) != len(values)
            or any(not value.strip() for value in values)):
        raise ValueError("scope inventory differs from the fixed denominator")
    return set(values)


def fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("generation engine fingerprint must be a lowercase SHA256")
    return value


def resolve_proof_engine(config, metrics):
    """Bind calibration counts independently of optional overlay report fields."""
    reported = metrics.get("engine")
    if reported is not None:
        fingerprint(reported)
    path = config.get("generation_manifest")
    pin = config.get("generation_manifest_sha256")
    if not path:
        if pin is not None:
            raise ValueError("generation manifest hash requires its manifest path")
        if reported is None:
            raise ValueError("generation engine binding is missing; calibration count is unknown")
        return reported, []
    fingerprint(pin)
    manifest, checksum = read_json(path)
    if checksum != pin:
        raise ValueError("generation manifest differs from its pinned SHA256")
    if not isinstance(manifest, dict) or (
        manifest.get("schema_version") != "reviewbench-progressive-experiment/v1"
        or manifest.get("protocol") != "reviewbench-agentic-exec/v1"
    ):
        raise ValueError("generation manifest schema or protocol differs")
    ids, models = manifest.get("task_ids"), manifest.get("models")
    if (not isinstance(ids, list) or any(not isinstance(value, str) or not value for value in ids)
            or len(ids) != config["expected_tasks"] or len(set(ids)) != len(ids)):
        raise ValueError("generation manifest task inventory differs")
    selected = scoped_ids(config)
    if selected is not None and set(ids) != selected:
        raise ValueError("generation manifest task identities differ from the fixed scope")
    if (not isinstance(models, list) or any(not isinstance(value, str) for value in models)
            or len(models) != len(set(models)) or set(models) != set(metrics["models"])
            or type(manifest.get("expected_pairs")) is not int
            or manifest["expected_pairs"] != len(ids) * len(models)):
        raise ValueError("generation manifest models or pair denominator differs")
    engine = fingerprint(manifest.get("engine_fingerprint"))
    if reported is not None and reported != engine:
        raise ValueError("report and pinned generation manifest engine bindings disagree")
    return engine, [{"path": str(path), "sha256": checksum}]


def observe_proofs(config, engine):
    """Read small proof files only, never source trees, logs, or build caches."""
    if not engine:
        return {}, []
    packages = list(config.get("extra_packages", []))
    errors = []
    registry = config.get("runtime_packages")
    if registry:
        try:
            values, _ = read_json(registry)
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError("runtime package registry is not a path list")
            packages += values
        except (OSError, ValueError) as exc:
            errors.append("runtime registry: " + str(exc))
    roots = {Path(value) / "execution/preparation/verified" for value in packages}
    roots.update(Path(value) for value in config.get("verification_roots", []))
    selected = scoped_ids(config)
    proofs = {}
    visited = 0
    expected_runs = Counter((variant, repetition) for variant in ("start", "golden")
                            for repetition in range(3))
    for root in sorted(roots):
        for path in sorted(root.glob("*/verification.json")):
            visited += 1
            if visited > MAX_PROOFS:
                raise ValueError("proof inventory exceeds the bounded observation limit")
            try:
                row, checksum = read_json(path)
                if not isinstance(row, dict):
                    raise ValueError("proof must be a JSON object")
                ident = row.get("id")
                if selected is not None and ident not in selected:
                    continue
                runs = row.get("runs", [])
                if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
                    raise ValueError("proof runs must be objects")
                valid = (
                    row.get("status") == "ready" and row.get("engine_fingerprint") == engine
                    and Counter((run["variant"], run["repetition"]) for run in runs) == expected_runs
                    and all(isinstance(run.get("tests"), dict) and run["tests"]
                            and all(value is True for value in run["tests"].values()) for run in runs)
                )
                if not valid:
                    continue
                key = digest([ident, row["runtime_fingerprint"], engine])
                proofs[key] = {"id": ident, "path": str(path), "sha256": checksum}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(str(path) + ": " + type(exc).__name__)
    return proofs, errors


def collect(config, now):
    snapshot = {
        "time": datetime.fromtimestamp(now, ZoneInfo(config["timezone"])).isoformat(timespec="seconds"),
        "metrics": None, "runtime_bound": None, "proofs": {}, "errors": [], "evidence": [],
        "proof_engine": None, "proof_inventory_status": "unknown",
        "services": service_states(config.get("services", [])),
        "disk_free_gib": round(shutil.disk_usage(config["disk_path"]).free / GIB, 2),
        "memory_available_gib": None,
    }
    if config.get("_authority_rejection"):
        snapshot["errors"].append(config["_authority_rejection"])
    if config.get("_verified_archive_authority"):
        snapshot["archive_authority"] = config["_verified_archive_authority"]
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                snapshot["memory_available_gib"] = round(int(line.split()[1]) * 1024 / GIB, 2)
                break
    except OSError:
        pass
    try:
        if config.get("_iotdb104_registry"):
            for ref in config["_iotdb104_guard_refs"]:
                path = Path(ref["path"])
                if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))
                        or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]):
                    raise ValueError("IoTDB104 closure changed before observation: " + str(path))
        if config.get("_delta103_registry"):
            for ref in config["_delta103_guard_refs"]:
                path = Path(ref["path"])
                if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))
                        or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]):
                    raise ValueError("Delta103 closure changed before observation: " + str(path))
        if config.get("_sourcegraph102_registry"):
            for ref in config["_sourcegraph102_guard_refs"]:
                path = Path(ref["path"])
                if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))
                        or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]):
                    raise ValueError("Sourcegraph102 closure changed before observation: " + str(path))
        if config.get("_percona101_registry"):
            for ref in config["_percona101_guard_refs"]:
                path = Path(ref["path"])
                if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))
                        or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]):
                    raise ValueError("Percona101 closure changed before observation: " + str(path))
        if config.get("_mindscape100_registry"):
            for ref in config["_mindscape100_guard_refs"]:
                path = Path(ref["path"])
                if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))
                        or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]):
                    raise ValueError("Mindscape100 closure changed before observation: " + str(path))
        if config.get("_rippled99_registry"):
            for ref in config["_rippled99_guard_refs"]:
                path = Path(ref["path"])
                if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))
                        or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]):
                    raise ValueError("batch99 closure changed before observation: " + str(path))
        if config.get("_infra_batch_registry"):
            for ref in config["_infra_batch_guard_refs"]:
                path = Path(ref["path"])
                if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))
                        or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]):
                    raise ValueError("batch98 closure changed before observation: " + str(path))
        if config.get("_mfs_registry"):
            for ref in config["_mfs_guard_refs"]:
                path = Path(ref["path"])
                if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))
                        or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]):
                    raise ValueError("MFS selected closure changed before observation: " + str(path))

        if config.get("_sklearn_registry"):
            approval = config["_sklearn_Main_verified"]
            if Path(approval["path"]).is_symlink():
                raise ValueError("selected94 Main marker became a symlink")
            _, approval_sha = read_json(approval["path"])
            if approval_sha != approval["sha256"]:
                raise ValueError("selected94 Main marker changed before observation")
        if config.get("_tsfile_registry"):
            approval = config["_tsfile_Main_verified"]
            if Path(approval["path"]).is_symlink():
                raise ValueError("selected93 Main marker became a symlink")
            _, approval_sha = read_json(approval["path"])
            if approval_sha != approval["sha256"]:
                raise ValueError("selected93 Main marker changed before observation")
        if config.get("_metals_registry"):
            approval = config["_metals_Main_verified"]
            if Path(approval["path"]).is_symlink():
                raise ValueError("selected92 Main marker became a symlink")
            _, approval_sha = read_json(approval["path"])
            if approval_sha != approval["sha256"]:
                raise ValueError("selected92 Main marker changed before observation")
        report, checksum = read_json(config["report"])
        if config.get("_selected_report_sha256") and checksum != config["_selected_report_sha256"]:
            raise ValueError("selected verified authority report changed before observation")
        snapshot["metrics"] = report_metrics(report, config["expected_tasks"])
        snapshot["evidence"].append({"path": config["report"], "sha256": checksum})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        snapshot["errors"].append("authoritative report: " + str(exc))
        if config.get("_fallback_report"):
            snapshot.pop("archive_authority", None)
            fallback_config = None
            if config.get("_verified_archive_authority", {}).get("delta", 0) >= 69:
                # A report may change between selection and observation. Recheck68,
                # rather than retaining a69 label or trusting an unchecked68 path.
                fallback_selector = (((((((lambda base: select_verified_tsfile93({**base, '_tsfile_maximum_delta': config['_verified_archive_authority']['delta'] - 1})) if config.get('_tsfile_registry') else ((lambda base: select_verified_metals92({**base, '_metals_maximum_delta': config['_verified_archive_authority']['delta'] - 1})) if config.get('_metals_registry') else ((lambda base: select_verified_sniffy91({**base, '_sniffy_maximum_delta': config['_verified_archive_authority']['delta'] - 1})) if config.get('_sniffy_registry') else ((lambda base: select_verified_daffodil90({**base, '_daffodil_maximum_delta': config['_verified_archive_authority']['delta'] - 1})) if config.get('_daffodil_registry') else ((lambda base: select_verified_fluid89({**base, '_fluid_maximum_delta': config['_verified_archive_authority']['delta'] - 1})) if config.get('_fluid_registry') else ((lambda base: select_verified_sequi88({
                    **base, '_sequi_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_sequi_registry') else ((lambda base: select_verified_metrics87({
                    **base, '_metrics_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_metrics_registry') else ((lambda base: select_verified_otelmongo86({
                    **base, '_otelmongo_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_otelmongo_registry') else ((lambda base: select_verified_finch85({
                    **base, '_finch_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_finch_registry') else ((lambda base: select_verified_lyse84({
                    **base, '_lyse_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_lyse_registry') else ((lambda base: select_verified_openforms83({
                    **base, '_openforms_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_openforms_registry') else ((lambda base: select_verified_alchemiscale82({
                    **base, '_alchemiscale_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_alchemiscale_registry') else ((lambda base: select_verified_redis81({
                    **base, '_redis_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_redis_registry') else ((lambda base: select_verified_codecov80({
                    **base, '_codecov_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_codecov_registry') else ((lambda base: select_verified_eks79({
                    **base, '_eks_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_eks_registry') else ((lambda base: select_verified_pyomo78({
                    **base, '_pyomo_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_pyomo_registry') else ((lambda base: select_verified_kraftkit77({
                    **base, '_kraftkit_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_kraftkit_registry') else ((lambda base: select_verified_last_two_F({
                    **base, '_F_registry_maximum_delta': config['_verified_archive_authority']['delta'] - 1}))
                    if config.get('_F_profile_registry') else ((lambda base: select_verified_remaining_android({
                    **base, "_remaining_registry_maximum_delta": config["_verified_archive_authority"]["delta"] - 1}))
                    if config.get("_remaining_profile_registry") else
                    (lambda base: select_verified_android_registry({
                    **base, "_android_registry_maximum_delta": config["_verified_archive_authority"]["delta"] - 1}))
                    if config["_verified_archive_authority"]["delta"] >= 71 else
                    select_verified_delta69 if config["_verified_archive_authority"]["delta"] == 70 else select_verified_delta68))))))))))))))))))))))))
                if config.get("_sklearn_registry"):
                    fallback_selector = select_verified_tsfile93
                if config.get("_mfs_registry"):
                    fallback_selector = lambda base: select_verified_mfs({
                        **base, "_mfs_maximum_delta": config["_verified_archive_authority"]["delta"] - 1})
                if config.get("_infra_batch_registry"):
                    fallback_selector = select_verified_mfs
                if config.get("_rippled99_registry"):
                    fallback_selector = select_verified_infra_batch98
                if config.get("_mindscape100_registry"):
                    fallback_selector = select_verified_rippled99
                if config.get("_percona101_registry"):
                    fallback_selector = select_verified_mindscape100
                if config.get("_sourcegraph102_registry"):
                    fallback_selector = select_verified_percona101
                if config.get("_delta103_registry"):
                    fallback_selector = select_verified_sourcegraph102
                if config.get("_iotdb104_registry"):
                    fallback_selector = select_verified_delta103
                fallback_config, fallback_evidence = fallback_selector(config["_delta69_base_config"])
                snapshot["evidence"].extend(fallback_evidence)
            try:
                fallback_path = fallback_config["report"] if fallback_config else config["_fallback_report"]
                fallback, checksum = read_json(fallback_path)
                if fallback_config and fallback_config.get("_selected_report_sha256"):
                    if checksum != fallback_config["_selected_report_sha256"]:
                        raise ValueError("fallback68 changed before observation")
                snapshot["metrics"] = report_metrics(fallback, config["expected_tasks"])
                snapshot["evidence"].append({"path": fallback_path, "sha256": checksum})
                if fallback_config and fallback_config.get("_verified_archive_authority"):
                    snapshot["archive_authority"] = fallback_config["_verified_archive_authority"]
            except (OSError, ValueError, KeyError, TypeError) as fallback_error:
                snapshot["errors"].append("Delta67 fallback: " + str(fallback_error))
                if fallback_config:
                    try:
                        path = config["_delta69_base_config"]["report"]
                        fallback, checksum = read_json(path)
                        snapshot["metrics"] = report_metrics(fallback, config["expected_tasks"])
                        snapshot["evidence"].append({"path": path, "sha256": checksum})
                    except (OSError, ValueError, KeyError, TypeError) as final_error:
                        snapshot["errors"].append("Delta67 fallback: " + str(final_error))
    if config.get("source_report"):
        try:
            source, checksum = read_json(config["source_report"])
            if source.get("task_count") != config["expected_tasks"]:
                raise ValueError("source report denominator differs")
            snapshot["runtime_bound"] = count(source["runtime_bound_tasks"], "runtime_bound",
                                               config["expected_tasks"])
            snapshot["evidence"].append({"path": config["source_report"], "sha256": checksum})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            snapshot["errors"].append("source report: " + str(exc))
    if snapshot["metrics"] is not None:
        try:
            engine, evidence = resolve_proof_engine(config, snapshot["metrics"])
            snapshot["proof_engine"] = engine
            snapshot["evidence"].extend(evidence)
            snapshot["proofs"], errors = observe_proofs(config, engine)
            snapshot["errors"].extend(errors)
            snapshot["proof_inventory_status"] = "partial" if errors else "complete"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            snapshot["errors"].append("proof inventory: " + str(exc))
    return snapshot


def milestones(previous, current):
    if not previous:
        return []
    events = []
    old, new = previous.get("metrics"), current["metrics"]
    old_engine = previous.get("proof_engine", (old or {}).get("engine"))
    new_engine = current.get("proof_engine", (new or {}).get("engine"))
    restored_binding = bool(new_engine and not old_engine)
    if old and new:
        if new_engine and old_engine and new_engine != old_engine:
            events.append("执行引擎指纹发生变化；旧证明不计作新引擎证明")
        elif restored_binding:
            events.append("回放证明观察绑定已补齐；本次重读存量证明，未重跑校准")
        if new["generations"] // 25 > old["generations"] // 25:
            events.append("模型生成记录达到 " + str(new["generations"]))
        if new["generations"] < old["generations"]:
            events.append("模型生成计数回退，保留原记录并检查报告来源")
        if new["complete"] != old["complete"]:
            events.append("整体 complete 变为 " + str(new["complete"]).lower())
        for name, row in new["models"].items():
            before = old["models"].get(name, {})
            if row["resolved"] // 10 > before.get("resolved", row["resolved"]) // 10:
                events.append(name + " 已确认解决数达到 " + str(row["resolved"]))
            if row["generation_failed"] > before.get("generation_failed", row["generation_failed"]):
                events.append(name + " 新增生成失败记录")
            if row["missing_measurements"] == 0 and before.get("missing_measurements", 0):
                events.append(name + " 测量缺口归零")
    if current["services"] != previous.get("services"):
        events.append("主评测/恢复服务状态发生变化")
    if current["errors"] != previous.get("errors"):
        events.append("观察源异常：" + "; ".join(current["errors"]) if current["errors"]
                      else "观察源恢复正常")
    added = set(current["proofs"]) - set(previous.get("proofs", {}))
    for key in sorted(added if not restored_binding else ()):
        events.append("新增当前引擎六轮成功回放报告：" + current["proofs"][key]["id"])
    return events


def render(snapshot, events, entry_id, periodic):
    title = "定期进度" if periodic else "关键结果"
    lines = ["", "## " + snapshot["time"] + " — " + title, ""]
    metrics = snapshot["metrics"]
    if metrics:
        proof_status = snapshot.get("proof_inventory_status", "unknown")
        proof_count = str(len({row["id"] for row in snapshot["proofs"].values()}))
        if proof_status == "unknown":
            proof_count = "未知（观察绑定或证明读取未闭合，不代表零项）"
        elif proof_status == "partial":
            proof_count = "至少 " + proof_count + "（部分证明读取异常）"
        lines += [
            f"- 模型生成记录：**{metrics['generations']} / {metrics['tasks']}**。",
            "- 整体完成状态：`complete=" + str(metrics["complete"]).lower() + "`。",
            "- 已绑定运行环境：" + str(snapshot["runtime_bound"]) + "（独立于已完成评测数）。",
            "- 当前引擎六轮成功回放报告（按任务去重）：" + proof_count + "。",
            "", "| 模型 | 已确认解决 | 测量缺口 | 未闭合门 | 生成失败 | 判定票数 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for name, row in metrics["models"].items():
            lines.append("| " + name + " | " + " | ".join(str(row[field]) for field in
                         ("resolved", "missing_measurements", "incomplete_gate_rows",
                          "generation_failed", "votes")) + " |")
        if snapshot.get("archive_authority"):
            verified = snapshot["archive_authority"]["metrics"]
            delta = snapshot["archive_authority"]["delta"]
            lines += ["", f"- 正式完整评测：**{verified['completed']} / {metrics['tasks']}**"
                      f"＝{verified['resolved']} resolved＋{verified['not_resolved_completed']} not resolved。",
                      f"- 权威来源：**Delta{delta}，Blob独立readback及本地凭据核验通过**；"
                      + ("旧Delta67/68/69/70及已验证父链保持原位。" if delta >= 71 else
                         "旧Delta67/68/69保持原位。" if delta == 70 else
                         "旧Delta67/68保持原位。" if delta == 69 else "旧Delta67保持原位。")]
            if snapshot["archive_authority"].get("measurement_lineage"):
                lines.append("- Event：首候选初测因cache metadata环境缺口失败；同一候选追加1次原生正确性测量后PASS。原39次生成、gold-overlay工具错误及三票true/false/true原样保留；Sol负票使最终结果仍为not resolved。")
            if delta == 69:
                lines.append("- 原API complete=false保持；独立pre-dispatch cutoff证明已随归档核验，未伪造工具反馈。")
    else:
        lines.append("- 当前权威报告不可用；本条不推算任务进度或成功率。")
    lines += ["", f"- 本地可用磁盘：{snapshot['disk_free_gib']} GiB；"
              f"可用内存：{snapshot['memory_available_gib']} GiB。"]
    for name, row in snapshot["services"].items():
        lines.append(f"- `{name}`：{row.get('ActiveState', 'unknown')}/"
                     f"{row.get('SubState', 'unknown')}；重启数 {row.get('NRestarts', 'unknown')}。")
    for event in events:
        lines.append("- **记录：** " + event.replace("\n", " / "))
    for error in snapshot["errors"]:
        lines.append("- **观察异常：** " + error.replace("\n", " / "))
    lines += ["", "证据（本次读取时的 SHA256）："]
    for row in snapshot["evidence"]:
        lines.append("- `" + row["path"] + "` — `" + row["sha256"] + "`")
    for key, row in snapshot["proofs"].items():
        if any(row["id"] in event for event in events):
            lines.append("- `" + row["path"] + "` — `" + row["sha256"] + "`")
    lines += ["", "<!-- reviewbench-progress:" + entry_id + " -->", ""]
    return "\n".join(lines)


_iotdb104_previous_render = render


def render_iotdb104(snapshot, events, entry_id, periodic):
    text = _iotdb104_previous_render(snapshot, events, entry_id, periodic)
    authority = snapshot.get("archive_authority") or {}
    if authority.get("delta") != 104:
        return text
    row = authority["measurement_lineage"]
    votes = "/".join("T" if v else "F" for v in row["panel_votes"])
    detail = (
        f"- IoTDB104：原 {row['model_calls']} 次 API、{row['tool_calls']} 次工具反馈；"
        f"correctness {row['correctness_passed']}/{row['correctness_total']}，"
        f"原三票 {votes}，resolved={str(row['resolved']).lower()}；"
        f"wrapper exit={row['controller_exit_status']} 不代表全402 complete。"
        f"参考版本 {row['reference_revision']}；完整参考构建不等于全库测试通过。"
    )
    return text.replace('- Event：首候选初测因cache metadata环境缺口失败；同一候选追加1次原生正确性测量后PASS。原39次生成、gold-overlay工具错误及三票true/false/true原样保留；Sol负票使最终结果仍为not resolved。', detail)


render = render_iotdb104


def progress_binding(config, document):
    """Opt in to a manifest-owned logical report path; preserve legacy identities."""
    report = Path(config["report"])
    binding = {"report": str(report.resolve()), "document": str(document.resolve()),
               "expected_tasks": config["expected_tasks"]}
    manifest_name, expected = config.get("report_manifest"), config.get("report_manifest_sha256")
    if manifest_name is None and expected is None:
        observed, selected_evidence = select_verified_iotdb104(config)
        return binding, observed, selected_evidence
    if (not isinstance(manifest_name, str) or not Path(manifest_name).is_absolute()
            or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        raise ValueError("stable report identity requires an absolute manifest and SHA256")
    manifest = Path(manifest_name)
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("report manifest must be a regular file")
    value, checksum = read_json(manifest)
    if not isinstance(value, dict) or checksum != expected:
        raise ValueError("report manifest differs from its pinned SHA256")
    manifest = manifest.resolve()
    logical = report.parent.resolve() / report.name
    resolved = report.resolve()
    if logical.parent != manifest.parent or not resolved.is_relative_to(manifest.parent):
        raise ValueError("report must use an owner-local alias beside its pinned manifest")
    binding.update(report=str(logical), report_identity="manifest-owned-logical-report/v1",
                   report_manifest=str(manifest), report_manifest_sha256=checksum)
    # Pin the report target once so evidence reads cannot cross a current-pointer swap.
    observed = {**config, "report": str(resolved)}
    observed, selected_evidence = select_verified_iotdb104(observed)
    return binding, observed, [{"path": str(manifest), "sha256": checksum}, *selected_evidence]


def record(config, *, now=None, events=(), evidence=(), force=False):
    now = time.time() if now is None else now
    interval = config.get("interval_seconds", 1800)
    if type(interval) is not int or interval < 60:
        raise ValueError("progress interval must be at least 60 seconds")
    count(config["expected_tasks"], "expected_tasks")
    document, state_path = Path(config["document"]), Path(config["state"])
    evidence = list(evidence)
    inputs = {Path(config[key]).resolve() for key in
              ("report", "source_report", "runtime_packages", "scope_ids", "generation_manifest",
               "report_manifest", "_config_file")
              if config.get(key)}
    inputs.update(Path(path).resolve() for path in evidence)
    outputs = {document.resolve(), state_path.resolve(), state_path.with_suffix(".lock").resolve()}
    if len(outputs) != 3 or inputs & outputs:
        raise ValueError("journal/state/lock outputs must differ from all observation inputs")
    if document.is_symlink() or state_path.is_symlink():
        raise ValueError("journal and state must be regular files")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    document.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(state_path)[0] if state_path.exists() else {}
        binding, observed_config, report_evidence = progress_binding(config, document)
        if Path(observed_config["report"]).resolve() in outputs:
            raise ValueError("journal/state/lock outputs must differ from the pinned report")
        if state and state.get("binding") != binding:
            raise ValueError("progress state belongs to a different experiment")
        snapshot = collect(observed_config, now)
        snapshot["evidence"].extend(report_evidence)
        supplied = []
        for name in evidence:
            path = Path(name).resolve()
            with path.open("rb") as stream:
                data = stream.read(MAX_JSON_BYTES + 1)
            if len(data) > MAX_JSON_BYTES:
                raise ValueError("manual progress evidence exceeds the observation limit")
            supplied.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest()})
        manual_key = digest([list(events), supplied]) if events else None
        manual_seen = state.get("manual_seen", [])
        fresh_manual = list(events) if manual_key and manual_key not in manual_seen else []
        changes = [*milestones(state.get("observation"), snapshot), *fresh_manual]
        slot = int(now // interval)
        periodic = state.get("periodic_slot") != slot
        should_write = not state or periodic or bool(changes) or force
        snapshot["evidence"].extend(supplied if fresh_manual else [])
        sequence = state.get("record_sequence", 0) + (1 if should_write else 0)
        entry_id = digest({"binding": binding, "slot": slot, "events": changes,
                           "metrics": snapshot["metrics"], "forced": force,
                           "sequence": sequence, "services": snapshot["services"],
                           "errors": snapshot["errors"], "manual_evidence": supplied if fresh_manual else [],
                           "proofs": sorted(snapshot["proofs"])})
        appended = False
        if should_write:
            if document.exists() and document.stat().st_size > MAX_JOURNAL_BYTES:
                raise ValueError("journal reached its 16-MiB bound; archive it before continuing")
            existing = document.read_text() if document.exists() else ""
            marker = "<!-- reviewbench-progress:" + entry_id + " -->"
            if marker not in existing:
                with document.open("a", encoding="utf-8") as stream:
                    if not existing:
                        stream.write("# ReviewBench Agentic 402 — 进度日志\n\n最新记录见文末。\n")
                    stream.write(render(snapshot, changes, entry_id, periodic))
                    stream.flush()
                    os.fsync(stream.fileno())
                appended = True
        if manual_key and manual_key not in manual_seen:
            manual_seen = [*manual_seen, manual_key][-4096:]
        next_state = {"binding": binding, "periodic_slot": slot, "observation": snapshot,
                      "manual_seen": manual_seen, "last_check": snapshot["time"],
                      "record_sequence": sequence}
        atomic_json(state_path, next_state)
        return {"status": "recorded" if appended else "unchanged", "document": str(document),
                "periodic": periodic, "events": len(changes), "entry_id": entry_id,
                "observation_errors": len(snapshot["errors"]), "model_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--event", action="append", default=[])
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    config, _ = read_json(args.config)
    config["_config_file"] = str(args.config.resolve())
    result = record(config, events=args.event, evidence=args.evidence, force=args.force)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
