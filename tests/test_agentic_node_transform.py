"""Per-task pin binding, not another kernel/API-readiness test suite.

The normal cases use independent synthetic canonical plans/probes. One optional
read-only integration case checks every lane in the frozen original C3 index;
absence of that local evidence skips only that case. No controller, Docker,
network, model, cloud, or frozen-source write is permitted by these tests.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


PROJECT = Path(__file__).resolve().parents[1]
TRANSFORM_PATH = PROJECT / "scripts/agentic_node_transform.py"
C3 = PROJECT / ".benchmarks/parallel-takeover-20261012/closure-20261012/c3-recovery"
PROBE_MEMBER = "reuse/image/image_identity_probe.py"
PROBE_CONSTANTS = ("IMAGE", "ARCHIVE_SHA", "ARCHIVE_BYTES", "SPEC_SHA", "REPO_DIGEST")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def module_from_bytes(raw, path, name):
    """Execute module definitions without writing import caches beside sources."""
    module = ModuleType(name)
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


T = module_from_bytes(TRANSFORM_PATH.read_bytes(), TRANSFORM_PATH, "node_transform_tests")


def pin(path):
    raw = path.read_bytes()
    return {"path": str(path), "bytes": len(raw), "sha256": sha(raw)}


def checked(ref):
    path = Path(ref["path"])
    assert pin(path) == ref
    return path


def literals(raw, names):
    result = {}
    for node in ast.parse(raw).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names):
            assert node.targets[0].id not in result, "duplicate test-observed assignment"
            result[node.targets[0].id] = ast.literal_eval(node.value)
    assert set(result) == set(names)
    return result


def replace_literal(raw, name, value):
    """Independent fixture binding: do not generate expectations with T.assignment."""
    text, count = re.subn(r"(?m)^" + re.escape(name) + r" = [^\n]*$",
                          lambda _: name + " = " + repr(value), raw.decode())
    assert count == 1
    return text.encode()


def bind_probe(template, ref):
    return replace_literal(replace_literal(template, "ORIGINAL_SHA", ref["sha256"]),
                           "ORIGINAL_BYTES", ref["bytes"])


@pytest.fixture(autouse=True)
def no_external_side_effects(monkeypatch):
    # T.load()/wrapper.load_original() use SourceFileLoader internally. Even when
    # pytest is invoked without -B, never emit caches into production/frozen dirs.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    def forbidden(*args, **kwargs):
        raise AssertionError("node transform tests may not spawn processes or use sockets")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


@pytest.fixture
def case_factory(tmp_path):
    def make(label, padding=0):
        root = tmp_path / label
        root.mkdir()
        probe = root / "image_identity_probe.py"
        expected = {
            "IMAGE": "sha256:" + sha((label + "/image").encode()),
            "ARCHIVE_SHA": sha((label + "/archive").encode()),
            "ARCHIVE_BYTES": 1000 + padding,
            "SPEC_SHA": sha((label + "/spec").encode()),
            "REPO_DIGEST": "sha256:" + sha((label + "/repo-digest").encode()),
        }
        # Minimal loadable original probe interface, never main() execution.
        # The production wrapper's authenticated-loader seams remain real.
        source = '''from pathlib import Path
import os
import time

def require(ok, message):
    if not ok:
        raise RuntimeError(message)

def private_paths(root):
    paths = {"socket": Path(root) / "ii/s", "exec": Path(root) / "x"}
    for endpoint in (str(paths["socket"]),
                     str(paths["exec"]) + "/containerd/containerd.sock",
                     str(paths["exec"]) + "/containerd/containerd-debug.sock",
                     str(paths["exec"]) + "/containerd/containerd.sock.ttrpc"):
        require(len(os.fsencode(endpoint)) < 108, "socket bound")
    return paths

def main():
    raise AssertionError("synthetic original probe main must never run")
    if True:
        deadline = time.monotonic() + 90
        private = [docker, "--host", "unix://" + str(paths["socket"])]
'''
        source += "\n" + "\n".join(k + " = " + repr(v) for k, v in expected.items())
        source += "\n# " + "padding" * padding + "\n"
        probe.write_text(source)
        probe_ref = {k: pin(probe)[k] for k in ("bytes", "sha256")}
        plan = {
            "task_id": "fixture-" + label,
            "model": "fixture-model-" + label,
            "files": {PROBE_MEMBER: probe_ref},
            "native_argv": ["original-controller", "--task", label],
            "native_wall_seconds": 7200,
            "physical_start_once_blob": "fixture/native-once/" + label,
            "image_transport": {"image": expected["IMAGE"], "archive": expected["ARCHIVE_SHA"]},
            "judge": {"models": ["original-judge"], "budget": 300000},
        }
        plan_path = root / "PLAN.json"
        plan_path.write_text(json.dumps(plan, indent=2) + "\n")
        descriptor = {"canonical_plan": pin(plan_path), "canonical": {"model": plan["model"]},
                      "physical_mapping": {"job_id": "fixture-job", "task_id": label}}
        wrapper = bind_probe((T.BASE / "image_wrapper.py").read_bytes(), probe_ref)
        adapter = ("# unrelated adapter fields and function must survive unchanged\n"
                   + "IMAGE_SHA275 = " + repr(sha(wrapper)) + "\n"
                   + "IMAGE_BYTES275 = " + repr(len(wrapper)) + "\n"
                   + "CANONICAL_PLAN_SHA = " + repr(pin(plan_path)["sha256"]) + "\n"
                   + "NATIVE_BUDGET = 7200\n"
                   + "def cleanup_marker():\n    return IMAGE_SHA275, IMAGE_BYTES275\n").encode()
        return SimpleNamespace(root=root, probe=probe, probe_ref=probe_ref, expected=expected,
                               plan_path=plan_path, plan=plan, descriptor=descriptor,
                               wrapper=wrapper, adapter=adapter)
    return make


@pytest.fixture(params=[("alpha", 0), ("beta", 11), ("gamma", 101)],
                ids=["probe-alpha", "probe-beta", "probe-gamma"])
def case(request, case_factory):
    return case_factory(*request.param)


def test_image_and_cleanup_are_bound_to_each_task(case):
    descriptor_before = copy.deepcopy(case.descriptor)
    plan_before, probe_before = case.plan_path.read_bytes(), case.probe.read_bytes()
    before, after = T.images(case.descriptor)
    assert before == case.wrapper
    assert T.transform("IMAGE286.py", before, case.descriptor) == after
    pins = literals(after, ("ORIGINAL_SHA", "ORIGINAL_BYTES"))
    assert pins == {"ORIGINAL_SHA": case.probe_ref["sha256"],
                    "ORIGINAL_BYTES": case.probe_ref["bytes"]}
    cleanup = T.transform("ADAPTER286.py", case.adapter, case.descriptor)
    assert literals(cleanup, ("IMAGE_SHA275", "IMAGE_BYTES275")) == {
        "IMAGE_SHA275": sha(after), "IMAGE_BYTES275": len(after)}
    restored_cleanup = replace_literal(replace_literal(cleanup, "IMAGE_SHA275", sha(before)),
                                       "IMAGE_BYTES275", len(before))
    assert restored_cleanup == case.adapter
    assert case.descriptor == descriptor_before
    assert case.plan_path.read_bytes() == plan_before
    assert case.probe.read_bytes() == probe_before


def test_loaded_transformed_wrapper_preserves_original_image_archive_spec(case):
    transformed = T.transform("IMAGE286.py", case.wrapper, case.descriptor)
    wrapper = module_from_bytes(transformed, case.root / "generated_wrapper.py", "synthetic_wrapper")
    original = wrapper.load_original(case.probe.resolve())
    assert {name: getattr(original, name) for name in PROBE_CONSTANTS} == case.expected
    assert literals(case.probe.read_bytes(), PROBE_CONSTANTS) == case.expected
    assert {k: pin(case.probe)[k] for k in ("bytes", "sha256")} == case.probe_ref


def test_probe_drift_is_still_rejected_by_original_loader(case):
    transformed = T.transform("IMAGE286.py", case.wrapper, case.descriptor)
    wrapper = module_from_bytes(transformed, case.root / "generated_wrapper.py", "probe_drift_wrapper")
    case.probe.write_bytes(case.probe.read_bytes().replace(b"sha256:", b"sha257:", 1))
    with pytest.raises(RuntimeError, match="original image probe bytes changed"):
        wrapper.load_original(case.probe.resolve())


def test_distinct_tasks_do_not_reuse_first_task_pins(case_factory):
    first, second = case_factory("independent-first", 1), case_factory("independent-second", 23)
    assert first.probe_ref["sha256"] != second.probe_ref["sha256"]
    assert first.probe_ref["bytes"] != second.probe_ref["bytes"]
    outputs = []
    # Interleave both directions: no task-specific global/cache may leak.
    for item in (first, second, first, second):
        output = T.transform("IMAGE286.py", item.wrapper, item.descriptor)
        assert literals(output, ("ORIGINAL_SHA", "ORIGINAL_BYTES"))["ORIGINAL_SHA"] == item.probe_ref["sha256"]
        outputs.append(output)
    assert outputs[0] == outputs[2] and outputs[1] == outputs[3] and outputs[0] != outputs[1]
    with pytest.raises(ValueError, match="IMAGE_PROGRAM_DIFFERS"):
        T.transform("IMAGE286.py", first.wrapper, second.descriptor)
    with pytest.raises(ValueError, match="ORIGINAL_IMAGE_CLEANUP_PIN_MISMATCH"):
        T.transform("ADAPTER286.py", first.adapter, second.descriptor)


@pytest.mark.parametrize("role", ["IMAGE286.py", "ADAPTER286.py"])
@pytest.mark.parametrize("drift", ["sha-ref", "bytes-short", "bytes-long", "same-size-content", "truncated-file"])
def test_canonical_plan_drift_rejected_before_transform(case, role, drift):
    descriptor = copy.deepcopy(case.descriptor)
    if drift == "sha-ref":
        descriptor["canonical_plan"]["sha256"] = "0" * 64
    elif drift == "bytes-short":
        descriptor["canonical_plan"]["bytes"] -= 1
    elif drift == "bytes-long":
        descriptor["canonical_plan"]["bytes"] += 1
    elif drift == "same-size-content":
        old = case.plan_path.read_bytes()
        changed = old.replace(b"fixture-", b"Fixture-", 1)
        assert old != changed and len(old) == len(changed)
        case.plan_path.write_bytes(changed)
    else:
        case.plan_path.write_bytes(case.plan_path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="CANONICAL_PLAN_PIN_DRIFT"):
        T.transform(role, case.wrapper if role == "IMAGE286.py" else case.adapter, descriptor)


@pytest.mark.parametrize("change", ["extra-comment", "extra-function", "resource-limit", "guard-bypass", "extra-import"])
def test_wrapper_changes_beyond_two_pin_fields_are_rejected(case, change):
    raw = case.wrapper
    if change == "extra-comment":
        raw += b"\n# unreviewed code bytes\n"
    elif change == "extra-function":
        raw += b"\ndef unreviewed():\n    return True\n"
    elif change == "resource-limit":
        raw = raw.replace(b"MAX_LOG = 8 * 1024**2", b"MAX_LOG = 9 * 1024**2", 1)
    elif change == "guard-bypass":
        raw = raw.replace(b"raise RuntimeError(reason)", b"return None", 1)
    else:
        raw = b"raise AssertionError('unreviewed wrapper must not execute')\n" + raw
    assert raw != case.wrapper
    with pytest.raises(ValueError, match="IMAGE_PROGRAM_DIFFERS_BEYOND_PINNED_TEMPLATE_DATA"):
        T.transform("IMAGE286.py", raw, case.descriptor)


@pytest.mark.parametrize("field,value", [("ORIGINAL_SHA", "f" * 64), ("ORIGINAL_BYTES", 1)])
def test_wrapper_probe_pin_must_match_its_canonical_plan(case, field, value):
    wrong = replace_literal(case.wrapper, field, value)
    with pytest.raises(ValueError, match="IMAGE_PROGRAM_DIFFERS"):
        T.transform("IMAGE286.py", wrong, case.descriptor)


@pytest.mark.parametrize("change", ["sha", "bytes", "missing", "duplicate", "nonliteral"])
def test_cleanup_pin_mismatch_or_ambiguous_field_is_rejected(case, change):
    raw = case.adapter
    if change == "sha":
        raw = replace_literal(raw, "IMAGE_SHA275", "0" * 64)
    elif change == "bytes":
        raw = replace_literal(raw, "IMAGE_BYTES275", len(case.wrapper) + 1)
    elif change == "missing":
        raw = re.sub(rb"(?m)^IMAGE_BYTES275 = [^\n]*\n", b"", raw)
    elif change == "duplicate":
        raw += b"IMAGE_BYTES275 = " + str(len(case.wrapper)).encode() + b"\n"
    else:
        raw = re.sub(rb"(?m)^IMAGE_BYTES275 = [^\n]*", b"IMAGE_BYTES275 = untrusted_call()", raw)
    with pytest.raises(ValueError):
        T.transform("ADAPTER286.py", raw, case.descriptor)


@pytest.mark.parametrize("role", ["IMAGE286.py", "ADAPTER286.py"])
def test_already_transformed_source_is_not_accepted_as_original(case, role):
    raw = case.wrapper if role == "IMAGE286.py" else case.adapter
    modified = T.transform(role, raw, case.descriptor)
    with pytest.raises(ValueError):
        T.transform(role, modified, case.descriptor)


@pytest.mark.parametrize("member,error", [("image_wrapper.py", "IMAGE_PROGRAM_TEMPLATE_DRIFT"),
                                          ("node_runtime_fixes.py", "FIX_PROGRAM_PIN_DRIFT")])
def test_shared_program_pin_drift_rejected_without_touching_production(case, tmp_path, monkeypatch, member, error):
    copied = tmp_path / "template-copy"
    copied.mkdir()
    for name in ("image_wrapper.py", "node_runtime_fixes.py"):
        raw = (T.BASE / name).read_bytes()
        (copied / name).write_bytes(raw + (b"\n# drift\n" if name == member else b""))
    monkeypatch.setattr(T, "BASE", copied)
    with pytest.raises(ValueError, match=error):
        T.transform("IMAGE286.py", case.wrapper, case.descriptor)


@pytest.mark.parametrize("name", ["MODIFIED_FILE.py", "TASK286.py", "PROXY286.py", "DOCKER286.py",
                                  "BOOTSTRAP286.py", "RESOURCE_PROJECTION.json", "unrelated.bin"])
def test_unrelated_roles_are_exact_passthrough_without_loading_plan(name, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("passthrough must not resolve any canonical plan")
    monkeypatch.setattr(T, "images", forbidden)
    raw = b"arbitrary fixture bytes\x00\xff"
    assert T.transform(name, raw, {}) is raw


def test_original_c3_all13_read_only_evidence(record_property):
    index_path = C3 / "INDEX.json"
    if not index_path.exists():
        pytest.skip("optional original C3 evidence is not installed; synthetic unit coverage is independent")
    index = json.loads(index_path.read_bytes())
    assert len(index["pairs"]) == 13
    snapshots = {index_path: index_path.read_bytes()}
    unique_probe_pins, image_ids, transformed_by_probe = set(), set(), {}
    for row_ref in index["pairs"]:
        row_path = checked(row_ref)
        row = json.loads(row_path.read_bytes())
        lane = Path(row["output"]) / "node"
        descriptor_path = lane / "DESCRIPTOR.json"
        descriptor = json.loads(descriptor_path.read_bytes())
        plan_path = checked(descriptor["canonical_plan"])
        plan = json.loads(plan_path.read_bytes())
        probe = plan_path.parent / PROBE_MEMBER
        ref = plan["files"][PROBE_MEMBER]
        assert {k: pin(probe)[k] for k in ("bytes", "sha256")} == ref
        expected = literals(probe.read_bytes(), PROBE_CONSTANTS)
        wrapper_path, adapter_path = lane / "IMAGE286.py", lane / "ADAPTER286.py"
        manifest_path = lane / "NODE_DELIVERY.json"
        manifest = json.loads(manifest_path.read_bytes())
        assert checked(manifest["roles"]["image"]["source"]) == wrapper_path
        assert checked(manifest["roles"]["adapter"]["source"]) == adapter_path
        for path in (row_path, descriptor_path, plan_path, probe, wrapper_path, adapter_path, manifest_path):
            snapshots[path] = path.read_bytes()
        descriptor_before = copy.deepcopy(descriptor)
        after = T.transform("IMAGE286.py", snapshots[wrapper_path], descriptor)
        assert literals(after, ("ORIGINAL_SHA", "ORIGINAL_BYTES")) == {
            "ORIGINAL_SHA": ref["sha256"], "ORIGINAL_BYTES": ref["bytes"]}
        cleanup = T.transform("ADAPTER286.py", snapshots[adapter_path], descriptor)
        assert literals(cleanup, ("IMAGE_SHA275", "IMAGE_BYTES275")) == {
            "IMAGE_SHA275": sha(after), "IMAGE_BYTES275": len(after)}
        # Load only authenticated module definitions, never original main/daemon.
        wrapper = module_from_bytes(after, wrapper_path, "original_c3_wrapper_" + row["binding_id"])
        loaded = wrapper.load_original(probe.resolve())
        assert {name: getattr(loaded, name) for name in PROBE_CONSTANTS} == expected
        assert descriptor == descriptor_before
        key = ref["sha256"], ref["bytes"]
        if key in transformed_by_probe:
            assert transformed_by_probe[key] == after
        transformed_by_probe[key] = after
        unique_probe_pins.add(key)
        image_ids.add(expected["IMAGE"])
    assert len(unique_probe_pins) >= 2, "evidence check must not collapse to the one reference task"
    assert len(image_ids) >= 2
    assert len({sha(raw) for raw in transformed_by_probe.values()}) == len(unique_probe_pins)
    for path, raw in snapshots.items():
        assert path.read_bytes() == raw, "frozen evidence changed: " + str(path)
    record_property("original_c3_lanes", 13)
    record_property("distinct_original_probe_pins", len(unique_probe_pins))
    print(f"Original C3 evidence: 13 lanes, {len(unique_probe_pins)} probe-pin variants, "
          f"{len(image_ids)} image identities preserved; frozen inputs unchanged.")
