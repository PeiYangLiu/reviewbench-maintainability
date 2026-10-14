"""Public dependency selection precedes every unchanged original X11 test."""
import copy
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_manifest_service as host, rust_manifest_service_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_image import output, ref


def fixture(root):
    source = root / "candidate"
    (source / "alacritty/src").mkdir(parents=True)
    (source / "alacritty/tests").mkdir()
    (source / "Cargo.toml").write_text('[workspace]\nmembers=["alacritty"]\n')
    (source / "alacritty/src/main.rs").write_text("fn main() {}\n")
    (source / "alacritty/tests/oracle_launch.rs").write_text("#[test]\nfn original() {}\n")
    manifest = ('[package]\nname="alacritty"\nversion="0.1.0"\nedition="2021"\n'
                '[dependencies.glutin]\ngit="https://github.com/rust-windowing/glutin"\n'
                'default-features=false\nfeatures=["egl","wgl"]\n')
    forms = []
    for index, name in enumerate(("git-beta", "registry-stable")):
        m, lock = root / ("manifest-" + str(index)), root / ("lock-" + str(index))
        m.write_text(manifest if not index else manifest.replace(
            'git="https://github.com/rust-windowing/glutin"', 'version="0.30.0"'))
        lock.write_text(
            'version=3\n[[package]]\nname="alacritty"\nversion="0.1.0"\n'
            '[[package]]\nname="glutin"\nversion="' + ("0.30.0" if index else "0.30.0-beta.2")
            + '"\nsource="' + (entry.core.CRATES_IO if index else
              "git+https://github.com/rust-windowing/glutin#" + "a" * 40) + '"\n')
        forms.append({"name": name, "manifest": ref(m), "lock": ref(lock), "operations": []})
    forms[1]["operations"] = entry.manifest_operations(*[
        entry.tomllib.loads(Path(f["manifest"]["path"]).read_text()) for f in forms])
    (source / "alacritty/Cargo.toml").write_text(manifest)
    (source / "Cargo.lock").write_bytes(Path(forms[0]["lock"]["path"]).read_bytes())
    for name in ("cargo", "rustc", "sources.toml"):
        (root / name).write_text("fixed input " + name)
    public = {
        "schema_version": entry.SCHEMA, "task_id": host.ID,
        "selection": "full-candidate-metadata-and-public-compile; all-compatible-private-results",
        "manifest_path": "alacritty/Cargo.toml", "dependency_key": ["dependencies", "glutin"],
        "toolchain": "1.65.0", "package": "alacritty", "forms": forms,
        "cargo": ref(root / "cargo"), "rustc": ref(root / "rustc"), "source_config": ref(root / "sources.toml"),
        "public_command": ["check", "--locked", "-p", "alacritty", "--no-default-features",
                           "--features", "x11", "--message-format=json"],
        "cpus": 2, "timeout_seconds": 60, "metadata_timeout_seconds": 10, "max_log_bytes": 1024**2,
    }
    shell = ('export DISPLAY=127.0.0.1:99.0 WINIT_UNIX_BACKEND=x11 LIBGL_ALWAYS_SOFTWARE=1; '
             'Xvfb :99 -screen 0 1024x768x24 -ac -nolock -nolisten unix -listen tcp & xvfb_pid=$!; '
             'sleep 1; cargo +1.65.0 ' + shlex.join(entry.TEST_COMMAND)
             + '; rc=$?; kill "$xvfb_pid"; wait "$xvfb_pid"; exit "$rc"')
    private = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": host.ID,
        "package_manifest": "alacritty/Cargo.toml", "command": entry.TEST_COMMAND[:],
        "harness": {"path": "alacritty/tests/oracle_launch.rs",
                    "sha256": ref(source / "alacritty/tests/oracle_launch.rs")["sha256"]},
        "expected_tests": [entry.TEST], "original_shell": shell,
        "original_shell_sha256": entry.core.digest(shell.encode()),
    }
    return source, public, private


def diagnostics(command, cwd, *, summary=True, change=None):
    target = {"src_path": str(cwd / "alacritty/src/main.rs"), "kind": ["bin"]}
    message = {"level": "error", "code": {"code": "E0599"}, "message": "no method named example",
               "spans": [{"is_primary": True, "file_name": "alacritty/src/main.rs"}]}
    row = {"reason": "compiler-message", "target": target,
           "manifest_path": str(cwd / "alacritty/Cargo.toml"), "message": message}
    if change == "outside":
        message["spans"][0]["file_name"] = "/vendor/glutin/src/lib.rs"
    elif change == "private":
        message["spans"][0]["file_name"] = "alacritty/tests/oracle_launch.rs"
    elif change == "linker":
        message["code"] = None
    elif change == "no-span":
        message["spans"] = []
    elif change == "foreign-target":
        target["src_path"] = "/vendor/glutin/src/lib.rs"
    elif change == "build-script":
        target["kind"] = ["custom-build"]
    rows = [row]
    if summary:
        rows.append({**row, "message": {"level": "error", "code": None, "spans": [],
                                       "message": "aborting due to 1 previous error"}})
    rows.append({"reason": "build-finished", "success": False})
    return output(command, cwd, "\n".join(json.dumps(r) for r in rows), code=101,
                  limit="deadline" if change == "limit" else None)


def runner(calls, *, compatible=("git-beta",), failed=(), error=None, graph_change=None):
    def run(command, cwd, env, folder, name, deadline, limit):
        manifest = entry.tomllib.loads((cwd / "alacritty/Cargo.toml").read_text())
        form = "git-beta" if "git" in manifest["dependencies"]["glutin"] else "registry-stable"
        calls.append((name, form, command, manifest))
        lock = entry.tomllib.loads((cwd / "Cargo.lock").read_text())
        if "metadata" in command:
            if ("beta" in lock["package"][1]["version"]) != (form == "git-beta"):
                return output(command, cwd, code=101,
                              stderr="error: the lock file needs to be updated but --locked was passed\n")
            packages = [dict(p, id=p["name"] + "@" + p["version"], targets=[]) for p in lock["package"]]
            packages[0]["manifest_path"] = str(cwd / "alacritty/Cargo.toml")
            packages[0]["targets"] = [{"name": "oracle_launch", "kind": ["test"],
                                      "src_path": str(cwd / "alacritty/tests/oracle_launch.rs")}]
            if graph_change == "owner":
                packages[0]["manifest_path"] = str(cwd / "Cargo.toml")
            elif graph_change == "target":
                packages[0]["targets"][0]["src_path"] = str(cwd / "alacritty/src/main.rs")
            graph = {"workspace_root": str(cwd), "workspace_members": [packages[0]["id"]],
                     "packages": packages, "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}
            return output(command, cwd, json.dumps(graph))
        if "check" in command:
            if form not in compatible:
                return diagnostics(command, cwd, change=error)
            return output(command, cwd, json.dumps({"reason": "build-finished", "success": True}))
        assert command[:2] == ["/bin/bash", "-c"]
        assert "Xvfb :99" in command[2] and "--test oracle_launch" in command[2]
        ok = form not in failed
        return output(command, cwd, "test " + entry.TEST + " ... " + ("ok" if ok else "FAILED") +
                      "\ntest result: " + ("ok." if ok else "FAILED.") + "\n", code=0 if ok else 101)
    return run


@pytest.mark.parametrize("grading", [False, True])
@pytest.mark.parametrize("compatible", [("git-beta",), ("registry-stable",), ("git-beta", "registry-stable")])
def test_public_compiles_freeze_forms_before_any_original_assertion(tmp_path, grading, compatible):
    source, public, private = fixture(tmp_path)
    before, calls = entry.core.snapshot(source), []
    result = entry.run_candidate(public, source, tmp_path / "work", private if grading else None,
                                 run=runner(calls, compatible=compatible))
    assert result["status"] == "completed" and result["exit_status"] == 0
    assert result["compatible_forms"] == list(compatible) and not result["selection_uses_test_results"]
    assert result["source_before"] == result["source_after"] == before == entry.core.snapshot(source)
    assert len(result["probes"]) == 4 and len(result["public_compiles"]) == 2
    tests = [i for i, c in enumerate(calls) if c[0].startswith("original-test")]
    assert len(tests) == (len(compatible) if grading else 0)
    assert not tests or min(tests) > max(i for i, c in enumerate(calls) if c[0].startswith("public-compile"))
    for c in calls:
        if c[0].startswith("original-test"):
            assert c[2][2] == private["original_shell"].replace("cargo +1.65.0", shlex.quote(public["cargo"]["path"]))


def test_one_passing_and_one_failing_form_remains_failed(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "work", private,
                                 run=runner(calls, compatible=("git-beta", "registry-stable"), failed=("git-beta",)))
    assert result["exit_status"] == 1 and result["test_results"] == {entry.TEST: False}
    assert len(result["executions"]) == 2 and result["executions"][1]["command_result"]["returncode"] == 0
    assert len(calls) == 8


def test_no_public_form_compiles_without_executing_private_tests(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="no original dependency form") as caught:
        entry.run_candidate(public, source, tmp_path / "work", private, run=runner(calls, compatible=()))
    assert caught.value.evidence["executions"] == []
    assert all(c[2][0] != "/bin/bash" for c in calls)


@pytest.mark.parametrize("edit", ["unrelated", "features", "explicit-source", "owned-lock"])
def test_current_candidate_manifest_and_lock_edits_survive(tmp_path, edit):
    source, public, private = fixture(tmp_path)
    path = source / "alacritty/Cargo.toml"
    if edit == "unrelated":
        path.write_text(path.read_text() + '\n[package.metadata]\nanswer=42\n')
    elif edit == "features":
        path.write_text(path.read_text().replace('["egl","wgl"]', '["egl","wgl","x11"]'))
    elif edit == "explicit-source":
        path.write_bytes(Path(public["forms"][1]["manifest"]["path"]).read_bytes())
    else:
        (source / "Cargo.lock").write_bytes((source / "Cargo.lock").read_bytes() + b"\n# owned lock\n")
    before, calls = entry.core.snapshot(source), []
    result = entry.run_candidate(public, source, tmp_path / "work", private,
                                 run=runner(calls, compatible=("git-beta", "registry-stable")))
    assert entry.core.snapshot(source) == before
    if edit in {"explicit-source", "owned-lock"}:
        assert result["compatible_forms"] == ["current-candidate"]
        assert len(result["probes"]) == (1 if edit == "owned-lock" else 2)
        assert result["candidate_lock_preserved"] is (edit == "owned-lock")
    else:
        assert len(result["public_compiles"]) == 2
        for _, _, _, manifest in calls:
            if edit == "unrelated":
                assert manifest["package"]["metadata"]["answer"] == 42
            else:
                assert manifest["dependencies"]["glutin"]["features"] == ["egl", "wgl", "x11"]


@pytest.mark.parametrize("change", ["outside", "private", "linker", "no-span", "foreign-target", "build-script", "limit"])
def test_infrastructure_and_nonproduction_errors_never_select_another_form(tmp_path, change):
    source, public, private = fixture(tmp_path)
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "work", private,
                            run=runner([], compatible=(), error=change))


@pytest.mark.parametrize("change", ["owner", "target", "disabled"])
def test_current_manifest_cannot_redirect_or_disable_original_test(tmp_path, change):
    source, public, private = fixture(tmp_path)
    if change == "disabled":
        path = source / "alacritty/Cargo.toml"
        path.write_text(path.read_text() + '\n[[test]]\nname="oracle_launch"\nharness=false\n')
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "work", private,
                            run=runner(calls, graph_change=change))
    assert all("metadata" in c[2] for c in calls)


@pytest.mark.parametrize("field", ["cargo", "rustc", "source_config", "manifest", "lock", "harness"])
def test_changed_fixed_inputs_reject_before_any_command(tmp_path, field):
    source, public, private = fixture(tmp_path)
    row = public["forms"][0][field] if field in {"manifest", "lock"} else public.get(field)
    path = source / private["harness"]["path"] if field == "harness" else Path(row["path"])
    path.write_bytes(path.read_bytes() + b"\nchanged\n")
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "work", private, run=runner(calls))
    assert calls == []


@pytest.mark.parametrize("field", ["command", "package_manifest", "expected_tests", "original_shell_sha256"])
def test_private_contract_is_exact(tmp_path, field):
    _, public, private = fixture(tmp_path)
    private[field] = [] if isinstance(private[field], list) else "changed"
    with pytest.raises(entry.core.VariantError):
        entry.validate(public, private)


def test_form_operations_cannot_change_unrelated_candidate_fields(tmp_path):
    source, public, private = fixture(tmp_path)
    public["forms"][1]["operations"].append({"kind": "add", "key": ["package", "build"], "value": False})
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "work", private, run=runner([]))


def test_opt_in_is_explicit_and_only_the_original_task_routes():
    assert not host.requested(SimpleNamespace(), {"id": host.ID})
    context = SimpleNamespace(config={"migration": {host.OPTION: {host.ID: {}}}})
    assert host.requested(context, {"id": host.ID})
    assert not host.requested(context, {"id": "another-task"})
    context.config["migration"][host.OPTION]["another-task"] = {}
    with pytest.raises(ContractError):
        host.requested(context, {"id": host.ID})


def test_existing_rust_route_is_preserved_without_opt_in(monkeypatch):
    from agentic_runtime_migration import rust_lane, rust_relay_service
    marker = object()
    monkeypatch.setattr(rust_relay_service, "requested", lambda *_: True)
    monkeypatch.setattr(rust_relay_service, "migrate", lambda *_: marker)
    assert rust_lane.migrate(SimpleNamespace(), {"id": "existing"}, {}) is marker


@pytest.mark.parametrize("extras,accepted", [
    ([], True), (["libxcb1-dev"], True), (["libxcb1-dev", "libxcb1-dev"], False),
    (["unrelated-package"], False), (["libxcb1-dev", "unrelated-package"], False),
])
def test_original_xcb_link_input_is_explicit_without_arbitrary_package_extensions(extras, accepted):
    assert host.supported_build_packages([*host.BUILD_PACKAGES, *extras]) is accepted
