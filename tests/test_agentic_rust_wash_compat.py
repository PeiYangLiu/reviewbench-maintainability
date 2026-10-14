"""Adapter controls only; these fixtures never replace the original wash test."""
import copy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_test_compat, rust_wash_compat as admission
from agentic_runtime_migration import rust_wash_compat_entry as entry
from agentic_runtime_migration.rust_build_entry import overlay_files
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash

BLOCK = b"""#[cfg(test)]
mod test {
    use super::*;
    #[test]
    fn test_ctl_comprehensive() {
        let command = LinkPutCommand { value: crate::behavior::value() };
        assert_eq!(command.value, 7);
    }
}
"""

TOKEN_CONTAINERS = [
    (b"(", b")"), (b"[", b"]"), (b"{", b"}"),
    (b"([", b"])"), (b"[({", b"})]"),
]
LEXICAL_PREFIXES = [
    b'const TEXT: &str = ")]} ({[";\n',
    b'const RAW: &str = r###"([)] /* not syntax */ }"###;\n',
    b'const BYTES: &[u8] = br##")] ([{ // not syntax"##;\n',
    b"/* ([ /* nested )] */ } */\n// ([}])\n",
    b"const CHARS: [char; 3] = ['(', ']', '}'];\n",
    b"fn lifetime<'a>(x: &'a str) -> &'a str { x }\n",
    b"#[allow(dead_code, unused_variables)]\n"
    b"fn nested<'a>(x: &'a str) -> &'a str { let pair = ([1, 2], (3, 4)); x }\n",
    b"#[allow(dead_code)]\nconst VALUES: [u8; 2] = [1, 2];\n",
]


def macro_container(tokens, opening=b"(", closing=b")"):
    return (b"macro_rules! discard_wash_test { ($($tokens:tt)*) => {}; }\n"
            b"discard_wash_test!" + opening + b";\n" + tokens + closing + b";\n")


def fixture_source(root, style="moved", mode="grading"):
    root.mkdir()
    files = {
        "Cargo.toml": b'[package]\nname="wash-cli"\nversion="0.1.0"\nedition="2021"\n'
                      b'[[bin]]\nname="wash"\npath="src/main.rs"\n'
                      b'[dependencies]\nwash-lib={path="deps/wash-lib"}\n',
        "Cargo.lock": b"version = 3\n",
        "src/main.rs": b"mod ctl;\nmod behavior;\nmod common;\nfn main() {}\n",
        "src/common/mod.rs": b"pub mod link_cmd;\n",
        "src/common/link_cmd.rs": b"pub struct LinkPutCommand { pub value: u8 }\n",
        "src/behavior.rs": b"pub fn value() -> u8 { 7 }\n",
        "deps/wash-lib/Cargo.toml": b'[package]\nname="wash-lib"\nversion="0.1.0"\nedition="2021"\n',
        "deps/wash-lib/src/lib.rs": (
            b"pub mod cli { pub mod link { pub struct LinkPutCommand { pub value: u8 } } }\n"
            if style != "legacy" else b"pub mod cli {}\n"),
        "src/ctl/mod.rs": (
            b"#[allow(unused_imports)]\nuse crate::common::link_cmd::LinkPutCommand;\n\n"
            if style == "legacy" else entry.IMPORT if style == "both" else b""
        ) + BLOCK,
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    spec = {"schema_version": entry.SCHEMA, "profile": entry.PROFILE, "task_id": entry.TASK,
            "toolchain": "1.65.0", "selection": entry.SELECTION, "files": [],
            "test_block": {"path": entry.PATH, "bytes": len(BLOCK), "sha256": entry.guard.digest(BLOCK)},
            "cpus": 1, "timeout_seconds": 120, "max_log_bytes": 2 * 1024**2, "mode": mode,
            "command": ["cargo", "--offline"] + (
                ["check", "--locked"] if mode == "public" else
                ["test", "--locked", "--bin", "wash", "test_ctl_comprehensive", "--", "--nocapture"])}
    if mode == "grading":
        spec["expected_tests"] = list(admission.EXPECTED)
    return spec


def record(code=0, stdout="", stderr="", **extra):
    if code == 0 and not stdout:
        stdout = '{"reason":"build-finished","success":true}\n'
    return {"returncode": code, "stdout": stdout, "stderr": stderr, "limit": None, **extra}


def metadata(root):
    return record(stdout=json.dumps({"workspace_root": str(root),
        "resolve": {"nodes": [{"id": "wash-cli", "dependencies": [], "deps": [], "features": []}]},
        "workspace_members": ["wash-cli"], "packages": [
            {"id": "wash-cli", "source": None, "manifest_path": str(root / "Cargo.toml")}] }))


def success(passed=True):
    return record(0 if passed else 101,
        "test ctl::test::test_ctl_comprehensive ... " + ("ok" if passed else "FAILED") + "\n"
        + "test result: " + ("ok." if passed else "FAILED.") + "\n")


def mismatch(root, spec, form, *, outside=False, foreign=False):
    data = (root / entry.PATH).read_bytes()
    start, end = entry.test_block(data, spec)
    if form == "as-is":
        at = data.index(b"LinkPutCommand", start)
        token, code = b"LinkPutCommand", "E0422"
        message = "cannot find struct, variant or union type `LinkPutCommand` in this scope"
    else:
        at = data.index(entry.IMPORT) + entry.IMPORT.index(b"link::")
        token, code = b"link", "E0432"
        message = "could not find `link` in `cli`"
    envelope = {"reason": "compiler-message", "package_id": "wash-cli",
        "manifest_path": str(root / ("deps/wash-lib/Cargo.toml" if foreign else "Cargo.toml")),
        "target": {"kind": ["bin"], "name": "wash", "src_path": str(root / "src/main.rs")},
        "message": {"level": "error", "code": {"code": code}, "message": message, "children": [],
            "spans": [{"is_primary": True, "file_name": str(root / ("src/behavior.rs" if outside else entry.PATH)),
                       "byte_start": at, "byte_end": at + len(token)}]}}
    footer = copy.deepcopy(envelope)
    footer["message"] = {"level": "error", "code": None, "spans": [], "children": [],
                         "message": "aborting due to previous error"}
    return record(101, "\n".join(map(json.dumps, [envelope, footer, {"reason": "build-finished", "success": False}])),
                  "error: could not compile `wash-cli` due to 2 previous errors\n")


@pytest.mark.parametrize("mutation", ["task", "profile", "toolchain", "selector", "feature", "body",
                                     "outcome", "reference_hash", "cpus", "timeout", "unknown_prefix",
                                     "missing_locked", "fixed_id"])
def test_unknown_or_altered_runtime_contract_rejected(tmp_path, mutation):
    spec = fixture_source(tmp_path / "source")
    if mutation in {"profile", "toolchain"}:
        spec[mutation] += "-changed"
    elif mutation == "task":
        spec["task_id"] = "other"
    elif mutation == "selector":
        spec["command"][spec["command"].index("test_ctl_comprehensive")] = "narrowed"
    elif mutation == "feature":
        spec["command"][2:2] = ["--features", "different"]
    elif mutation == "body":
        spec["test_block"]["sha256"] = "bad"
    elif mutation in {"outcome", "reference_hash"}:
        spec[mutation] = "untrusted"
    elif mutation == "cpus":
        spec["cpus"] = 4
    elif mutation == "timeout":
        spec["timeout_seconds"] = 3001
    elif mutation == "unknown_prefix":
        spec["command"][2:2] = ["--config", 'build.rustc="alternate"']
    elif mutation == "missing_locked":
        spec["command"].remove("--locked")
    else:
        spec["expected_tests"] = ["other"]
    with pytest.raises(entry.SelectionError):
        entry.validate(spec)


@pytest.mark.parametrize("form,label", [("as-is", "golden"), ("with-import", "start")])
def test_current_compile_selects_before_all_assertions_and_ignores_labels(tmp_path, monkeypatch, form, label):
    source = tmp_path / "source"
    spec = fixture_source(source)
    monkeypatch.setenv("ORACLE_VARIANT", label)
    calls = []
    def run(command, *_):
        current = Path.cwd().parent.name
        kind = "graph" if "metadata" in command else "probe" if "--no-run" in command else "tests"
        calls.append((kind, current))
        if kind == "graph":
            return metadata(Path.cwd())
        if kind == "tests":
            return success()
        return record() if current == form else mismatch(Path.cwd(), spec, current)
    result = entry.select(spec, source, tmp_path / "forms", run=run)
    assert result["selected"] == [form]
    assert all(result["results"].values())
    assert calls == [("graph", "as-is"), ("probe", "as-is"), ("probe", "with-import"), ("tests", form)]


@pytest.mark.parametrize("first_passes,second_passes", [(False, True), (True, False), (True, True)])
def test_every_compilable_form_executes_conjunctively_without_reselection(tmp_path, first_passes, second_passes):
    source = tmp_path / "source"
    spec = fixture_source(source)
    calls = []
    def run(command, *_):
        if "metadata" in command:
            return metadata(Path.cwd())
        form = Path.cwd().parent.name
        calls.append(("probe" if "--no-run" in command else "tests", form))
        return record() if "--no-run" in command else success(first_passes if form == "as-is" else second_passes)
    result = entry.select(spec, source, tmp_path / "forms", run=run)
    assert calls == [("probe", "as-is"), ("probe", "with-import"), ("tests", "as-is"), ("tests", "with-import")]
    assert all(result["results"].values()) == (first_passes and second_passes)


@pytest.mark.parametrize("failure", ["signal11", "SIGABRT", "custom_build", "unstructured", "invalid_json",
                                    "foreign_error", "other_source", "truncated", "timeout", "shell137",
                                    "driver1", "negative_signal", "false_count", "zero_exit_error",
                                    "missing_finish", "zero_exit_background_failure"])
def test_mixed_or_indeterminate_failures_abort_before_any_test(tmp_path, failure):
    source = tmp_path / "source"
    spec = fixture_source(source)
    calls = []
    def run(command, *_):
        if "metadata" in command:
            return metadata(Path.cwd())
        calls.append("probe" if "--no-run" in command else "tests")
        r = mismatch(Path.cwd(), spec, "as-is", outside=failure == "other_source", foreign=failure == "foreign_error")
        if failure in {"signal11", "SIGABRT", "custom_build", "unstructured"}:
            r["stderr"] += {"signal11": "process failed (signal: 11, SIGSEGV: invalid memory reference)",
                           "SIGABRT": "process aborted SIGABRT",
                           "custom_build": "failed to run custom build command for `dependency`",
                           "unstructured": "unaccounted compiler failure"}[failure]
        elif failure == "invalid_json":
            r["stdout"] += "\n{\"truncated\":"
        elif failure == "truncated":
            r.update(stdout_total_bytes=100, stdout_captured_bytes=50)
        elif failure == "timeout":
            r["limit"] = "timeout"
        elif failure in {"shell137", "driver1", "negative_signal"}:
            r["returncode"] = {"shell137": 137, "driver1": 1, "negative_signal": -15}[failure]
        elif failure == "false_count":
            r["stderr"] = "error: could not compile `wash-cli` due to 7 previous errors\n"
        elif failure == "zero_exit_error":
            r["returncode"] = 0
        elif failure == "missing_finish":
            r["stdout"] = r["stdout"].rsplit("\n", 1)[0]
        elif failure == "zero_exit_background_failure":
            r = record(stderr="warning: build failed, waiting for other jobs to finish...\n")
        return r
    with pytest.raises(entry.SelectionError):
        entry.select(spec, source, tmp_path / "forms", run=run)
    assert calls == ["probe"]


@pytest.mark.parametrize("mutation", ["body", "hidden_string", "outer_cfg", "enclosing_module", "target_output"])
def test_preserved_test_module_and_no_old_target_guard(tmp_path, mutation):
    source = tmp_path / "source"
    spec = fixture_source(source)
    path = source / entry.PATH
    if mutation == "body":
        path.write_bytes(path.read_bytes().replace(b"assert_eq!(command.value, 7)", b"assert_eq!(command.value, 8)"))
    elif mutation == "hidden_string":
        path.write_bytes(b'const HIDDEN: &str = r####"' + path.read_bytes() + b'"####;\n')
    elif mutation == "outer_cfg":
        path.write_bytes(b"#[cfg(any())]\n" + path.read_bytes())
    elif mutation == "enclosing_module":
        path.write_bytes(b"mod wrapper {\n" + path.read_bytes() + b"}\n")
    else:
        (source / "target").mkdir()
        (source / "target/old-candidate.rlib").write_bytes(b"old binary is not an input")
    with pytest.raises(entry.SelectionError):
        entry.select(spec, source, tmp_path / "forms", run=lambda *a: pytest.fail("compiler ran before input guard"))


def test_current_production_changes_preserved_and_import_repair_is_idempotent(tmp_path):
    source = tmp_path / "source"
    spec = fixture_source(source)
    (source / "src/behavior.rs").write_text("pub fn value() -> u8 { 99 }\n")
    data = (source / entry.PATH).read_bytes()
    changed = entry.transformed(data, spec, "with-import")
    assert changed == entry.transformed(changed, spec, "with-import")
    assert changed.count(BLOCK) == 1 and changed.count(entry.IMPORT) == 1
    assert data == (source / entry.PATH).read_bytes()
    assert "99" in (source / "src/behavior.rs").read_text()


@pytest.mark.parametrize("text", [b'const X:&str=r###"{ /* not syntax */ }"###;\n',
                                 b"/* { /* nested */ } */\n",
                                 b"fn lifetime<'a>(x:&'a str)->&'a str{x}\n",
                                 b"const C:char='}';\n"])
def test_rust_literal_comment_and_lifetime_mask_preserves_real_module_scope(tmp_path, text):
    source = tmp_path / "source"
    spec = fixture_source(source)
    original = (source / entry.PATH).read_bytes()
    entry.test_block(text + original, spec)


@pytest.mark.parametrize("mode", ["public", "grading"])
@pytest.mark.parametrize("opening,closing", TOKEN_CONTAINERS)
def test_hidden_token_tree_module_rejected_before_metadata_or_compiler(
        tmp_path, mode, opening, closing):
    source = tmp_path / "source"
    spec = fixture_source(source, mode=mode)
    path = source / entry.PATH
    hidden = macro_container(path.read_bytes(), opening, closing)
    assert hidden.count(BLOCK) == 1
    path.write_bytes(hidden)
    before = entry.candidate_snapshot(source)
    for form in entry.FORMS:
        with pytest.raises(entry.SelectionError, match="enclosing/conditional"):
            entry.transformed(hidden, spec, form)
    with pytest.raises(entry.SelectionError, match="enclosing/conditional"):
        entry.select(spec, source, tmp_path / "forms",
                     run=lambda *a: pytest.fail("hidden test reached metadata/compiler"))
    assert entry.candidate_snapshot(source) == before
    assert not (tmp_path / "forms").exists()


@pytest.mark.parametrize("prefix,suffix", [
    (b"([)];\n", b""), (b"[(]);\n", b""), (b"({)};\n", b""),
    (b") (;\n", b""), (b"] [;\n", b""), (b"} {;\n", b""),
    (b"(;\n", b""), (b"[;\n", b""), (b"{;\n", b""),
    (b"([;\n", b"]);\n"), (b"([;\n", b")];\n"),
    (b"", b"\n([)];\n"), (b"", b"\n[;\n"), (b"", b"\n);\n"),
])
def test_mixed_mismatched_or_unclosed_delimiters_abort_before_any_command(tmp_path, prefix, suffix):
    source = tmp_path / "source"
    spec = fixture_source(source)
    path = source / entry.PATH
    path.write_bytes(prefix + path.read_bytes() + suffix)
    with pytest.raises(entry.SelectionError):
        entry.select(spec, source, tmp_path / "forms",
                     run=lambda *a: pytest.fail("invalid token-tree scope reached a command"))


@pytest.mark.parametrize("opening,closing", TOKEN_CONTAINERS)
def test_existing_import_in_token_tree_is_not_an_idempotent_top_level_import(
        tmp_path, opening, closing):
    source = tmp_path / "source"
    spec = fixture_source(source)
    path = source / entry.PATH
    data = macro_container(entry.IMPORT, opening, closing) + path.read_bytes()
    path.write_bytes(data)
    entry.test_block(data, spec)  # The test is active; only the import is hidden.
    with pytest.raises(entry.SelectionError, match="ambiguous pre-existing test import"):
        entry.transformed(data, spec, "with-import")
    with pytest.raises(entry.SelectionError, match="ambiguous pre-existing test import"):
        entry.select(spec, source, tmp_path / "forms",
                     run=lambda *a: pytest.fail("hidden import reached a command"))


def test_conditional_import_is_not_an_idempotent_top_level_import(tmp_path):
    source = tmp_path / "source"
    spec = fixture_source(source)
    data = b"#[cfg(any())]\n" + entry.IMPORT + (source / entry.PATH).read_bytes()
    with pytest.raises(entry.SelectionError, match="ambiguous pre-existing test import"):
        entry.transformed(data, spec, "with-import")


@pytest.mark.parametrize("opening,closing", TOKEN_CONTAINERS)
def test_hidden_anchor_is_ignored_and_exact_import_is_appended_at_top_level(
        tmp_path, opening, closing):
    source = tmp_path / "source"
    spec = fixture_source(source)
    data = (macro_container(entry.ANCHOR + b"\n};\n", opening, closing)
            + (source / entry.PATH).read_bytes())
    changed = entry.transformed(data, spec, "with-import")
    assert changed == data + entry.IMPORT
    assert entry.top_level_item(entry.masked_rust(changed), changed.index(entry.IMPORT))
    assert changed == entry.transformed(changed, spec, "with-import")


def test_conditional_anchor_is_not_used_for_insertion(tmp_path):
    source = tmp_path / "source"
    spec = fixture_source(source)
    data = (b"#[cfg(any())]\n" + entry.ANCHOR + b"\n};\n"
            + (source / entry.PATH).read_bytes())
    assert entry.transformed(data, spec, "with-import") == data + entry.IMPORT


@pytest.mark.parametrize("prefix", LEXICAL_PREFIXES)
@pytest.mark.parametrize("placement", ["append", "existing-import", "anchor"])
def test_matching_delimiters_in_comments_literals_attributes_and_items_preserve_valid_forms(
        tmp_path, prefix, placement):
    source = tmp_path / "source"
    spec = fixture_source(source)
    original = (source / entry.PATH).read_bytes()
    if placement == "existing-import":
        data = prefix + entry.IMPORT + original
        expected = data
    elif placement == "anchor":
        data = prefix + entry.ANCHOR + b"\n};\n" + original
        expected = prefix + entry.IMPORT + entry.ANCHOR + b"\n};\n" + original
    else:
        data = prefix + original
        expected = data + entry.IMPORT
    start, end = entry.test_block(data, spec)
    assert data[start:end] == BLOCK
    assert entry.transformed(data, spec, "as-is") == data
    assert entry.transformed(data, spec, "with-import") == expected
    assert entry.transformed(expected, spec, "with-import") == expected


def original_inputs():
    path = os.environ.get("WASH_ORIGINAL_INPUTS")
    if not path:
        pytest.skip("explicit retained wash113 inputs are required for original-input controls")
    return json.loads(Path(path).read_text())


@pytest.mark.parametrize("variant", ["start", "golden"])
@pytest.mark.parametrize("opening,closing", TOKEN_CONTAINERS)
@pytest.mark.parametrize("form", entry.FORMS)
def test_exact_original_13649_byte_block_is_rejected_inside_every_token_container(
        variant, opening, closing, form):
    original_inputs()  # Require the explicit retained-input fixture, never search.
    path = Path(os.environ["WASH_ORIGINAL_INPUTS"]).parent / "source-inputs" / variant / entry.PATH
    data = path.read_bytes()
    spec = {"test_block": admission.TEST_BLOCK}
    start, end = entry.test_block(data, spec)
    block = data[start:end]
    assert len(block) == 13649
    assert entry.guard.digest(block) == admission.TEST_BLOCK["sha256"]
    hidden = data[:start] + macro_container(block, opening, closing) + data[end:]
    assert hidden.count(block) == 1
    with pytest.raises(entry.SelectionError, match="enclosing/conditional"):
        entry.transformed(hidden, spec, form)
    assert path.read_bytes() == data


class OriginalContext:
    def __init__(self, tmp_path, data):
        self.output = tmp_path / "output"
        self.output.mkdir()
        self.scratch = tmp_path
        self.legacy = Path(data["golden"]["path"]).parents[3]
        self.repository = self.legacy.parent / "repos/wasmCloud__wash-archive"
    def repo(self, task):
        return self.repository
    def copy_asset(self, source, role):
        path = self.output / (file_hash(source) + "-" + Path(source).name)
        path.write_bytes(Path(source).read_bytes())
        return {"path": str(path), "sha256": file_hash(path)}
    def bundle(self, root, role):
        path = self.output / "tool-bundle-fixture.json"
        path.write_text(json.dumps({p.name: file_hash(p) for p in sorted(root.iterdir())}, sort_keys=True))
        return {"path": str(path), "sha256": file_hash(path)}


def test_full_original_plan_hashes_body_chain_locks_and_six_logs_admitted(tmp_path):
    data = original_inputs()
    ctx = OriginalContext(tmp_path, data)
    assert canonical_hash(data["resolved_plan"]) == admission.PLAN
    spec = admission.specification(ctx, data["task"], data["resolved_plan"], data["oracle"], data["golden"])
    assert spec["test_block"] == admission.TEST_BLOCK
    assert spec["files"] == []  # Do not freeze the mixed production file.
    build, locks = admission.build_specification(ctx, data["task"], data["resolved_plan"], data["oracle"], data["golden"])
    assert build["manifests"] == [] and len(build["locks"]) == 1
    assert build["locks"][0]["when"] == admission.LOCK_WHEN
    assert locks["Cargo.lock"]["sha256"] == admission.LOCKS["golden"]
    assert all("reference" not in json.dumps(condition) for condition in build["locks"][0]["when"])
    grading, public, assets = admission.binding(ctx, spec,
        ["cargo", "--offline", "test", "--locked", "--bin", "wash", "test_ctl_comprehensive", "--", "--nocapture"],
        ["cargo", "--offline", "check", "--locked"])
    assert grading[-1] == admission.GRADER_SPEC and public[-1] == admission.PUBLIC_SPEC
    assert not admission.PUBLIC_SPEC.startswith(admission.TOOLS + "/")
    assert not admission.GRADER_SPEC.startswith(admission.TOOLS + "/")
    public_file = next(a for a in assets if a["target"] == admission.PUBLIC_SPEC)
    public_spec = json.loads(Path(public_file["path"]).read_text())
    assert not any(k in public_spec for k in ("expected_tests", "golden", "original_runs", "reference_hash"))


@pytest.mark.parametrize("mutation", ["id", "golden", "toolchain", "selector", "lock_receipt", "body_receipt"])
def test_altered_original_task_or_plan_stays_rejected(tmp_path, mutation):
    data = copy.deepcopy(original_inputs())
    if mutation == "id":
        data["task"]["id"] = "foreign"
    elif mutation == "golden":
        data["oracle"]["golden_patch_sha256"] = "0" * 64
    elif mutation == "toolchain":
        data["resolved_plan"]["toolchain"] = "1.85.1"
    elif mutation == "selector":
        data["resolved_plan"]["command"] = "cargo test different"
    elif mutation == "lock_receipt":
        data["oracle"]["environment_replacements"]["golden"][0]["sha256"] = "0" * 64
    else:
        data["oracle"]["test_compatibility_replacements"]["golden"][0]["sha256"] = "0" * 64
    with pytest.raises(ContractError):
        rust_test_compat.validate_profile(data["task"], data["resolved_plan"], data["oracle"])


def test_conditional_lock_uses_only_current_cargo_data_and_preserves_custom_lock(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "crates/wash-lib").mkdir(parents=True)
    import tomli_w
    manifest = {"package": {"name": "wash-cli", "version": "0.18.0-alpha.2"},
                "workspace": {"dependencies": {"wash-lib": {"version": "0.9.0-alpha.2", "path": "./crates/wash-lib"}}}}
    (source / "Cargo.toml").write_text(tomli_w.dumps(manifest))
    (source / "crates/wash-lib/Cargo.toml").write_text('[package]\nversion="0.9.0-alpha.2"\n')
    (source / "Cargo.lock").write_bytes(b"original")
    asset = tmp_path / "bound-lock"
    asset.write_bytes(b"new fixed dependency lock")
    record = {"path": "Cargo.lock", "asset_target": str(asset), "sha256": file_hash(asset),
              "replace_if_sha256": [entry.guard.digest(b"original")], "allow_missing": False,
              "when": copy.deepcopy(admission.LOCK_WHEN)}
    spec = {"manifests": [], "locks": [record], "private_members": []}
    assert overlay_files(source, spec) == {"Cargo.lock": asset.read_bytes()}
    (source / "production.rs").write_text("arbitrary current production change")
    assert overlay_files(source, spec) == {"Cargo.lock": asset.read_bytes()}
    (source / "Cargo.lock").write_bytes(b"candidate-authored lock")
    assert overlay_files(source, spec) == {}
    (source / "Cargo.lock").write_bytes(b"original")
    manifest["package"]["version"] = "0.18.0-alpha.1"
    (source / "Cargo.toml").write_text(tomli_w.dumps(manifest))
    assert overlay_files(source, spec) == {}
