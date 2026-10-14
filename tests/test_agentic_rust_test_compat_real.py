"""Real small offline Rust crates exercise the selector, not FROST readiness."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_agentic_rust_test_compat import runtime_spec, admission, entry


def toolchain(monkeypatch):
    cargo = os.environ.get("FROST_TEST_CARGO")
    if not cargo:
        pytest.skip("set FROST_TEST_CARGO to the retained Cargo1.64.0 binary")
    directory = Path(cargo).parent
    assert directory.is_dir()
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("RUSTC", str(directory / "rustc"))
    monkeypatch.setenv("RUSTUP_AUTO_INSTALL", "0")
    assert subprocess.check_output([cargo, "--version"], text=True).startswith("cargo 1.64.0 ")


def project(root, api="unrenamed", wrong_new=False, broken=False):
    spec = runtime_spec(root)
    (root / "Cargo.toml").write_text('[workspace]\nmembers=["frost-core","helper"]\nresolver="2"\n')
    (root / "helper/src").mkdir(parents=True)
    (root / "helper/Cargo.toml").write_text('[package]\nname="helper"\nversion="0.0.0"\nedition="2021"\n')
    (root / "helper/src/lib.rs").write_text(
        "pub fn value()->u32{" + ("missing_production_function()" if broken else "7") + "}\n")
    (root / "frost-core/Cargo.toml").write_text(
        '[package]\nname="frost-core"\nversion="0.0.0"\nedition="2021"\n'
        '[dependencies]\nhelper={path="../helper"}\n[features]\ntest-impl=[]\n')
    (root / "frost-core/src/lib.rs").write_text(
        'pub mod frost { pub mod keys; }\n#[cfg(feature="test-impl")] pub mod tests;\n')
    (root / "frost-core/src/frost").mkdir()

    def secret(name, method, wrong=False):
        args = "_: &mut ()" if method == "random" else "_: &str"
        ret = "Self" if method == "random" else "Result<Self, ()>"
        value = "Self(helper::value()" + ("+1" if wrong else "") + ",std::marker::PhantomData)"
        if method != "random":
            value = "Ok(" + value + ")"
        return (f"pub struct {name}<C>(pub u32,std::marker::PhantomData<C>);\n"
                f"impl<C> {name}<C>{{pub fn {method}({args})->{ret}{{{value}}}}}\n")

    code = ""
    if api in {"unrenamed", "both"}:
        code += secret("SecretShareValue", "random")
        code += "impl<C> SecretShareValue<C>{pub fn from_hex(_: &str)->Result<Self,()>{Ok(Self::random(&mut ()))}}\n"
        code += "pub struct PublicVerificationShare(pub u32);\nimpl<C> From<SecretShareValue<C>> for PublicVerificationShare{fn from(x:SecretShareValue<C>)->Self{Self(x.0)}}\n"
    if api in {"renamed", "both"}:
        code += secret("SharedSecret", "random", wrong_new) + secret("SigningShare", "from_hex", wrong_new)
        code += "pub struct VerifyingShare(pub u32);\nimpl<C> From<SigningShare<C>> for VerifyingShare{fn from(x:SigningShare<C>)->Self{Self(x.0)}}\n"
    (root / "frost-core/src/frost/keys.rs").write_text(code)
    contents = {
        "frost-core/src/tests.rs": (
            "use crate::frost;\npub mod vectors;\npub fn check<C>(){\n"
            "let x=frost::keys::SecretShareValue::<C>::random(&mut ());\n"
            "assert_eq!(x.0,7);vectors::check::<C>();}\n"),
        "frost-core/src/tests/vectors.rs": (
            "use crate::frost;use frost::keys::*;\npub fn check<C>(){\n"
            'let x=SecretShareValue::<C>::from_hex("7").unwrap();\n'
            "let p=frost::keys::PublicVerificationShare::from(x);\nassert_eq!(p.0,7);}\n"),
    }
    for name, content in contents.items():
        (root / name).write_text(content)
    current = {name: data.encode() for name, data in contents.items()}
    spec["files"] = [{"path": name, "sha256": entry.digest(data)} for name, data in current.items()]
    spec["chain"] = []
    for name, old, new in entry.OPERATIONS:
        before = entry.digest(current[name])
        current[name] = current[name].replace(old.encode(), new.encode(), 1)
        spec["chain"].append({"path": name, "old": old, "new": new, "before": before,
                              "after": entry.digest(current[name])})
    (root / "frost-core/tests").mkdir()
    (root / "frost-core/tests/tests.rs").write_text(
        "#[test] fn check_share_generation_ristretto255_sha512(){frost_core::tests::check::<()>();}\n"
        "#[test] fn check_sign_with_test_vectors(){frost_core::tests::vectors::check::<()>();}\n"
        "mod common {mod ciphersuite {#[test] fn use_parameterized_types(){assert_eq!(helper::value(),7);}}}\n")
    (root / "Cargo.lock").write_text(
        'version = 3\n[[package]]\nname="frost-core"\nversion="0.0.0"\ndependencies=["helper"]\n'
        '[[package]]\nname="helper"\nversion="0.0.0"\n')
    return spec


@pytest.mark.parametrize("api", ["unrenamed", "renamed", "both"])
def test_real_current_multifile_api_selection_preserves_all_three_tests(tmp_path, monkeypatch, api):
    toolchain(monkeypatch)
    source = tmp_path / "source"
    spec = project(source, api)
    original = entry.fingerprint(source)
    monkeypatch.setenv("ORACLE_VARIANT", "golden" if api == "unrenamed" else "start")
    result = entry.select(spec, source, tmp_path / "build")
    assert result["selected"] == (["unrenamed", "renamed"] if api == "both" else [api])
    assert result["results"] == dict.fromkeys(admission.EXPECTED, True)
    assert entry.fingerprint(source) == original


def test_real_changed_candidate_behavior_fails_original_assertions_and_rollback_restores_them(tmp_path, monkeypatch):
    toolchain(monkeypatch)
    source = tmp_path / "source"
    spec = project(source)
    helper = source / "helper/src/lib.rs"
    original = helper.read_bytes()
    helper.write_text("pub fn value()->u32{8}\n")
    failed = entry.select(spec, source, tmp_path / "changed")
    assert failed["selected"] == ["unrenamed"]
    assert not any(failed["results"].values())
    helper.write_bytes(original)
    restored = entry.select(spec, source, tmp_path / "restored")
    assert all(restored["results"].values())


def test_real_unrelated_production_compile_error_does_not_select_another_form(tmp_path, monkeypatch):
    toolchain(monkeypatch)
    source = tmp_path / "source"
    spec = project(source, "renamed", broken=True)
    with pytest.raises(entry.SelectionError, match="outside an attested rename") as error:
        entry.select(spec, source, tmp_path / "build")
    assert len(error.value.evidence["probes"]) == 1
    assert not error.value.evidence["tests"]


def test_real_both_forms_compilable_one_wrong_means_conjunctive_failure(tmp_path, monkeypatch):
    toolchain(monkeypatch)
    source = tmp_path / "source"
    spec = project(source, "both", wrong_new=True)
    result = entry.select(spec, source, tmp_path / "build")
    assert result["selected"] == ["unrenamed", "renamed"]
    assert len(result["tests"]) == 2
    assert not all(result["results"].values())


@pytest.mark.parametrize("signal", [9, 15])
def test_real_signal_is_infrastructure_even_with_shell_encoding(tmp_path, signal):
    from agentic_runtime_migration import rust_api_entry
    import time
    r = rust_api_entry.run_command(
        ["bash", "-c", f"{sys.executable} -c 'import os;os.kill(os.getpid(),{signal})'; exit $?" ],
        tmp_path, "signal", time.monotonic() + 20, 1024 * 1024)
    assert r["returncode"] in {-signal, 128 + signal}
    with pytest.raises(entry.SelectionError, match="infrastructure"):
        entry.infrastructure(r, {})


@pytest.mark.parametrize("signal", [9, 15])
def test_real_cargo_101_after_rustc_signal_is_infrastructure(tmp_path, monkeypatch, signal):
    toolchain(monkeypatch)
    rustc = os.environ["RUSTC"]
    wrapper = tmp_path / "rustc-wrapper"
    wrapper.write_text(
        "#!/bin/bash\nfor arg in \"$@\"; do\n"
        f"  if test \"$arg\" = frost_core; then kill -{signal} $$; fi\n"
        "done\nexec " + str(Path(rustc)) + ' "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("RUSTC", str(wrapper))
    source = tmp_path / "source"
    spec = project(source)
    with pytest.raises(entry.SelectionError, match="infrastructure") as error:
        entry.select(spec, source, tmp_path / "build")
    assert len(error.value.evidence["probes"]) == 1
    assert error.value.evidence["probes"][0]["returncode"] == 101
    assert not error.value.evidence["tests"]


def diagnostic_crash_wrapper(folder, rustc, signal):
    """The real compiler emits its missing-name JSON, then its driver crashes."""
    wrapper = folder / "rustc-after-diagnostic"
    events = folder / "rustc-after-diagnostic-events.jsonl"
    wrapper.write_text(
        "#!" + sys.executable + "\n"
        "import hashlib,json,os,resource,subprocess,sys\n"
        "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
        "args=sys.argv[1:]\n"
        "compiler=" + repr(rustc) + "\n"
        "if '--crate-name' not in args or args[args.index('--crate-name')+1]!='frost_core':\n"
        "    os.execv(compiler,[compiler,*args])\n"
        "r=subprocess.run([compiler,*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n"
        "sys.stdout.buffer.write(r.stdout);sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(r.stderr);sys.stderr.buffer.flush()\n"
        "errors=[]\n"
        "for line in r.stderr.splitlines():\n"
        "    try: row=json.loads(line)\n"
        "    except ValueError: continue\n"
        "    if row.get('level')=='error' and row.get('code'): errors.append(row['code']['code'])\n"
        "if r.returncode==1 and errors:\n"
        "    with open(" + repr(str(events)) + ",'a') as log:\n"
        "        log.write(json.dumps({'compiler_argv':[compiler,*args],'compiler_exit':r.returncode,"
        "'emitted_error_codes':errors,'diagnostic_sha256':hashlib.sha256(r.stderr).hexdigest(),"
        "'driver_signal':" + repr(signal) + "})+'\\n')\n"
        "    os.kill(os.getpid()," + repr(signal) + ")\n"
        "sys.exit(r.returncode)\n")
    wrapper.chmod(0o755)
    return wrapper, events


@pytest.mark.parametrize("signal", [6, 9, 11, 15])
@pytest.mark.parametrize("mode", ["public", "grading"])
def test_real_pinned_cargo_diagnostic_then_driver_crash_aborts_before_reselection(
        tmp_path, monkeypatch, signal, mode):
    import hashlib
    import json
    toolchain(monkeypatch)
    wrapper, events = diagnostic_crash_wrapper(tmp_path, os.environ["RUSTC"], signal)
    monkeypatch.setenv("RUSTC", str(wrapper))
    source = tmp_path / "source"
    spec = project(source, "renamed")
    if mode == "public":
        spec["mode"] = "public"
        spec.pop("expected_tests")
        spec["command"] = ["cargo", "--offline", "check", "--locked", "-p", "frost-core", "--features", "test-impl"]
    before = entry.fingerprint(source)
    with pytest.raises(entry.SelectionError, match="infrastructure") as error:
        entry.select(spec, source, tmp_path / "build")
    evidence = error.value.evidence
    assert len(evidence["probes"]) == 1 and evidence["tests"] == []
    assert evidence["selected"] == [] and evidence["selection_uses_test_results"] is False
    probe = evidence["probes"][0]
    assert probe["returncode"] == 101
    emitted = [json.loads(line) for line in events.read_text().splitlines()]
    assert len(emitted) == 1 and emitted[0]["compiler_exit"] == 1
    assert emitted[0]["driver_signal"] == signal and emitted[0]["emitted_error_codes"]
    # The emitted diagnostics really are the permitted incompatibility control;
    # the concurrently observed process failure is what makes it indeterminate.
    ordinary = {**probe, "stderr": "", "stderr_total_bytes": 0, "stderr_captured_bytes": 0}
    assert entry.rename_mismatch(ordinary, tmp_path / "build/unrenamed/source", spec, "unrenamed")
    assert entry.fingerprint(source) == before
    output = Path(os.environ.get("FROST_TEST_EVIDENCE_DIR", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    (output / f"cargo-diagnostic-then-signal-{signal}-{mode}.json").write_text(json.dumps({
        "fixture": "synthetic offline workspace; not a FROST benchmark crash",
        "cargo": os.environ["FROST_TEST_CARGO"],
        "cargo_version": subprocess.check_output([os.environ["FROST_TEST_CARGO"], "--version"], text=True).strip(),
        "entry_sha256": hashlib.sha256(Path(entry.__file__).read_bytes()).hexdigest(),
        "child_events": emitted, "result": "infrastructure_abort_before_reselection_or_tests",
        "evidence": evidence, "source_unchanged": True,
    }, indent=2) + "\n")
