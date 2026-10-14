"""Small contract and adversarial selector fixtures, not FROST runtime proof."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_test_compat as admission
from agentic_runtime_migration import rust_test_compat_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError


def runtime_spec(root, mode="grading"):
    root.mkdir()
    contents = {
        "frost-core/src/tests.rs":
            b"fn helper<C>() { frost::keys::SecretShareValue::<C>::random(&mut rng); assert!(original()); }\n",
        "frost-core/src/tests/vectors.rs":
            b"fn vector<C>() { SecretShareValue::<C>::from_hex(input); frost::keys::PublicVerificationShare::from(secret); assert!(original()); }\n",
        "frost-core/src/lib.rs": b"pub mod tests;\n",
        "frost-core/Cargo.toml": b'[package]\nname="frost-core"\nversion="0.0.0"\n',
        "Cargo.toml": b'[workspace]\nmembers=["frost-core"]\n',
    }
    for name, data in contents.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    files = [{"path": p, "sha256": entry.digest(contents[p])}
             for p in dict.fromkeys(r[0] for r in entry.OPERATIONS)]
    current, chain = dict(contents), []
    for path, old, new in entry.OPERATIONS:
        before = entry.digest(current[path])
        current[path] = current[path].replace(old.encode(), new.encode(), 1)
        chain.append({"path": path, "old": old, "new": new, "before": before,
                      "after": entry.digest(current[path])})
    spec = {"schema_version": entry.SCHEMA, "profile": "frost106-public-helper-renames-v1",
            "task_id": admission.TASK, "toolchain": "1.64.0", "selection": entry.SELECTION,
            "files": files, "chain": chain, "cpus": 1, "timeout_seconds": 120,
            "max_log_bytes": 1024 * 1024, "mode": mode,
            "command": ["cargo", "--offline", "check" if mode == "public" else "test",
                        "--locked", "-p", "frost-core", "--features", "test-impl"]}
    if mode == "grading":
        spec["command"] += ["--test", "tests", "--", "--nocapture"]
        spec["expected_tests"] = list(admission.EXPECTED)
    return spec


def record(code=0, stdout="", stderr="", limit=None):
    return {"returncode": code, "stdout": stdout, "stderr": stderr, "limit": limit}


def success(passed=True):
    return record(0 if passed else 101,
                  "".join("test " + n + " ... " + ("ok" if passed else "FAILED") + "\n"
                          for n in admission.EXPECTED) + "test result: " + ("ok." if passed else "FAILED.") + "\n")


def mismatch(root, *, outside=False, only_abort=False):
    p = root / "frost-core/src/tests.rs"
    data = p.read_bytes()
    token = b"SharedSecret" if b"SharedSecret" in data else b"SecretShareValue"
    start = data.index(token)
    message = {
        "reason": "compiler-message", "manifest_path": str(root / "frost-core/Cargo.toml"),
        "target": {"kind": ["lib"], "src_path": str(root / "frost-core/src/lib.rs")},
        "message": {"level": "error", "code": {"code": "E0433"},
                    "message": "could not find " + token.decode() + " in keys",
                    "spans": [{"is_primary": True,
                               "file_name": str(root / "frost-core/src/lib.rs") if outside else str(p),
                               "byte_start": start, "byte_end": start + len(token)}]},
    }
    abort = copy.deepcopy(message)
    abort["message"] = {"level": "error", "code": None, "spans": [],
                        "message": "aborting due to previous error"}
    return record(101, "\n".join(json.dumps(m) for m in ([abort] if only_abort else [message, abort])))


@pytest.mark.parametrize("mutation", ["profile", "task", "toolchain", "hash", "chain", "body", "features",
                                     "target", "outcome", "reference_hash", "prefix", "missing_locked"])
def test_changed_bindings_and_outcome_predicates_are_rejected(tmp_path, mutation):
    spec = runtime_spec(tmp_path / "source")
    if mutation in {"profile", "toolchain"}:
        spec[mutation] += "-changed"
    elif mutation == "task":
        spec["task_id"] = "foreign"
    elif mutation == "hash":
        spec["files"][0]["sha256"] = "bad"
    elif mutation == "chain":
        spec["chain"][1]["before"] = "0" * 64
    elif mutation == "body":
        spec["chain"][0]["new"] += "; assert!(true)"
    elif mutation == "features":
        spec["command"][spec["command"].index("test-impl")] = "other"
    elif mutation == "target":
        spec["command"][spec["command"].index("--test") + 1] = "narrowed"
    elif mutation == "outcome":
        spec["select_if_passed"] = True
    elif mutation == "reference_hash":
        spec["reference_production_sha256"] = "a" * 64
    elif mutation == "prefix":
        spec["command"][2:2] = ["--config", 'build.rustc="untrusted"']
    else:
        spec["command"].remove("--locked")
    with pytest.raises(entry.SelectionError):
        entry.validate(spec)


@pytest.mark.parametrize("form,label", [("unrenamed", "golden"), ("renamed", "start")])
def test_compile_selection_ignores_replay_labels_and_freezes_before_tests(tmp_path, monkeypatch, form, label):
    root = tmp_path / "source"
    spec = runtime_spec(root)
    monkeypatch.setenv("ORACLE_VARIANT", label)
    calls = []

    def run(command, *_):
        current = Path.cwd().parent.name
        probe = "--no-run" in command
        calls.append((probe, current))
        return (record() if current == form else mismatch(Path.cwd())) if probe else success()

    result = entry.select(spec, root, tmp_path / "work", run=run)
    assert result["selected"] == [form] and all(result["results"].values())
    assert calls == [(True, "unrenamed"), (True, "renamed"), (False, form)]


@pytest.mark.parametrize("second_passes", [False, True])
def test_all_compilable_forms_execute_conjunctively_without_outcome_reselection(tmp_path, second_passes):
    root = tmp_path / "source"
    spec = runtime_spec(root)
    calls = []

    def run(command, *_):
        form = Path.cwd().parent.name
        probe = "--no-run" in command
        calls.append((probe, form))
        return record() if probe else success(form == "unrenamed" or second_passes)

    result = entry.select(spec, root, tmp_path / "work", run=run)
    assert calls == [(True, "unrenamed"), (True, "renamed"), (False, "unrenamed"), (False, "renamed")]
    assert all(result["results"].values()) is second_passes


@pytest.mark.parametrize("code,text,limit", [
    (-9, "", None), (137, "", None), (143, "", None), (124, "", None),
    (101, "signal: 9, SIGKILL", None), (101, "internal compiler error", None),
    (101, "No space left on device", None), (101, "Disk quota exceeded", None),
    (101, "Input/output error", None), (101, "", "deadline"), (101, "", "output_limit"),
])
def test_infrastructure_never_becomes_name_incompatibility(tmp_path, code, text, limit):
    root = tmp_path / "source"
    spec = runtime_spec(root)
    calls = []

    def run(command, *_):
        calls.append(command)
        r = mismatch(Path.cwd())
        r.update(returncode=code, stderr=text, limit=limit)
        return r

    with pytest.raises(entry.SelectionError, match="infrastructure"):
        entry.select(spec, root, tmp_path / "work", run=run)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["production", "abort_only", "mutation", "helper_link"])
def test_production_errors_missing_diagnostics_and_mutations_fail_closed(tmp_path, kind):
    root = tmp_path / "source"
    spec = runtime_spec(root)
    if kind == "helper_link":
        p = root / spec["files"][0]["path"]
        data = p.read_bytes()
        p.unlink()
        (p.parent / "alternate.rs").write_bytes(data)
        p.symlink_to("alternate.rs")

    def run(command, *_):
        if kind == "mutation":
            (root / "frost-core/src/lib.rs").write_text("modified during compile")
            return record()
        return mismatch(Path.cwd(), outside=kind == "production", only_abort=kind == "abort_only")

    with pytest.raises(entry.SelectionError):
        entry.select(spec, root, tmp_path / "work", run=run)


def test_public_mode_compiles_both_forms_without_executing_assertions_or_exposing_ids(tmp_path):
    root = tmp_path / "source"
    spec = runtime_spec(root, "public")
    assert "expected_tests" not in spec
    calls = []
    result = entry.select(spec, root, tmp_path / "work",
                          run=lambda command, *_: calls.append(command) or record())
    assert len(calls) == 2 and all("check" in c and "--message-format=json" in c for c in calls)
    assert not result["tests"] and not result["results"] and result["public_build_ok"]


def test_hash_chain_preserves_every_nonrename_byte_and_rejects_tampering(tmp_path):
    root = tmp_path / "source"
    spec = runtime_spec(root)
    original = {r["path"]: (root / r["path"]).read_bytes() for r in spec["files"]}
    updated = entry.transformed(original, spec, "renamed")
    restored = dict(updated)
    for path, old, new in reversed(entry.OPERATIONS):
        restored[path] = restored[path].replace(new.encode(), old.encode(), 1)
    assert restored == original
    spec["chain"][-1]["after"] = "a" * 64
    with pytest.raises(entry.SelectionError, match="assertion/body"):
        entry.transformed(original, spec, "renamed")


def test_unknown_test_replacement_profile_stays_rejected():
    with pytest.raises(ContractError, match="unknown or altered"):
        admission.validate_profile({"id": "unknown"}, {"test_compatibility_replacements": [{"old": "a"}]}, {})


def test_binding_keeps_private_ids_out_of_public_tools_and_spec(tmp_path):
    from types import SimpleNamespace
    root = tmp_path / "source"
    spec = runtime_spec(root)
    execute = spec.pop("command")
    spec.pop("expected_tests")
    spec.pop("mode")
    public = ["cargo", "--offline", "check", "--locked", "-p", "frost-core", "--features", "test-impl"]
    output = tmp_path / "output"
    output.mkdir()
    bundled = []
    def bundle(directory, **kwargs):
        bundled.append({p.name: p.read_bytes() for p in directory.iterdir()})
        return {"path": "/fixture/tools.json", "sha256": "a" * 64}
    ctx = SimpleNamespace(output=output, bundle=bundle,
                          copy_asset=lambda p, category: {"path": str(p), "sha256": entry.digest(p.read_bytes())})
    grade_cmd, public_cmd, assets = admission.binding(ctx, spec, execute, public)
    assert grade_cmd[-1] == admission.GRADER_SPEC and public_cmd[-1] == admission.PUBLIC_SPEC
    assert set(bundled[0]) == {"rust_api_entry.py", "rust_test_compat_entry.py"}
    for asset in assets:
        if asset["kind"] == "file":
            value = json.loads(Path(asset["path"]).read_text())
            if asset["visibility"] == "developer":
                assert "expected_tests" not in value and value["command"] == public
            else:
                assert value["expected_tests"] == admission.EXPECTED and value["command"] == execute
    for data in bundled[0].values():
        assert all(name.encode() not in data for name in admission.EXPECTED)


@pytest.mark.parametrize("target", [admission.PUBLIC_SPEC, admission.GRADER_SPEC])
def test_spec_files_do_not_need_mountpoints_inside_readonly_tools_archive(target):
    # Docker must not create an absent nested file in the readonly bind mount.
    assert not Path(target).is_relative_to(Path(admission.TOOLS))
