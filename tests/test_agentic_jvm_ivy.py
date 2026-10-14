"""Remote Ivy cache closure is independently bound, not a copy of build outputs."""
from pathlib import Path
import hashlib
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agentic_runtime_migration.jvm_ivy import remote_ivy_files, validate_remote_ivy_overlay
from prepare_agentic_sbt_dependencies import dependency_overlay, refresh_prepared_overlay
from reviewbench_maintainability.agentic.contracts import ContractError


def put(home, name, data="fixture"):
    path = home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def module(home, *, kind="jar", version="1.2.3", classifier="", local=False, resolver=True):
    prefix = f"ivy/cache/org.example/library"
    url = f"https://repo.maven.apache.org/maven2/org/example/library/{version}/library-{version}"
    descriptor = put(
        home, f"{prefix}/ivy-{version}.xml",
        f'<ivy-module version="2.0"><info organisation="org.example" module="library" revision="{version}"/>'
        "</ivy-module>")
    original = put(home, f"{prefix}/ivy-{version}.xml.original", "<project/>")
    entries = [("ivy#ivy#xml#-10", url + ".pom"),
               ("library#pom.original#pom#20", url + ".pom")]
    names = {descriptor.relative_to(home).as_posix(), original.relative_to(home).as_posix()}
    if kind:
        artifact = put(home, f"{prefix}/{kind}s/library-{version}{classifier}.jar", "remote artifact")
        entries.append((f"library#{kind}#jar#30", url + classifier + ".jar"))
        names.add(artifact.relative_to(home).as_posix())
    lines = ["#ivy cached data file", "resolver=" + ("local" if local else "sbt-chain")] if resolver else []
    for key, location in entries:
        key = "artifact\\:" + key.replace("#", "\\#")
        lines += [key + ".exists=true", key + ".is-local=" + str(local).lower(),
                  key + ".location=" + location.replace(":", "\\:")]
    receipt = put(home, f"{prefix}/ivydata-{version}.properties", "\n".join(lines) + "\n")
    names.add(receipt.relative_to(home).as_posix())
    return names, receipt


@pytest.mark.parametrize("kind,classifier", [("jar", ""), ("bundle", ""), ("jar", "-data")])
def test_remote_ivy_exports_complete_origin_bound_module_only(tmp_path, kind, classifier):
    expected, _ = module(tmp_path, kind=kind, classifier=classifier)
    put(tmp_path, "ivy/local/org.example/project/0.1/jars/answer.jar")
    put(tmp_path, ".ivy2/local/org.example/project/0.1/jars/answer.jar")
    put(tmp_path, "ivy/cache/org.example/library/jars/unlisted-answer.jar")
    put(tmp_path, "ivy/cache/org.example/library/target/classes/Answer.class")
    put(tmp_path, "global/zinc/local-project.jar")
    assert set(remote_ivy_files(tmp_path)) == expected


def test_parent_pom_without_resolver_field_still_has_remote_origins(tmp_path):
    expected, _ = module(tmp_path, kind=None, resolver=False)
    assert set(remote_ivy_files(tmp_path)) == expected
    assert not any(name.endswith(".jar") for name in expected)


@pytest.mark.parametrize("version", ["1.2.3", "1.2-SNAPSHOT"])
def test_local_publication_in_repository_cache_is_not_exported(tmp_path, version):
    module(tmp_path, local=True, version=version)
    assert remote_ivy_files(tmp_path) == []


@pytest.mark.parametrize("url", [
    "file:/workspace/target/library-1.2.3.jar",
    "https://user:password@repo.maven.apache.org/maven2/x.jar",
    "https://user%40repo.maven.apache.org/maven2/x.jar",
    "http://localhost/maven2/x.jar",
    "https://repo.maven.apache.org/maven2/x.jar?token=hidden",
])
def test_any_local_or_credentialed_origin_excludes_entire_module(tmp_path, url):
    _, receipt = module(tmp_path)
    lines = receipt.read_text().splitlines()
    lines[-1] = lines[-1].split("=", 1)[0] + "=" + url.replace(":", "\\:")
    receipt.write_text("\n".join(lines) + "\n")
    assert remote_ivy_files(tmp_path) == []


@pytest.mark.parametrize("mutation,match", [
    ("module", "different dependency coordinates"),
    ("version", "different dependency coordinates"),
    ("missing-artifact", "artifact is absent"),
    ("foreign-url", "different dependency coordinates"),
    ("exists-false", "successful origin"),
    ("incomplete-record", "incomplete artifact"),
    ("duplicate", "duplicate"),
    ("originless-descriptor", "descriptor has no remote origin"),
])
def test_remote_ivy_rejects_inconsistent_origin_and_descriptor_bindings(tmp_path, mutation, match):
    _, receipt = module(tmp_path)
    descriptor = receipt.with_name("ivy-1.2.3.xml")
    if mutation == "module":
        descriptor.write_text(descriptor.read_text().replace('module="library"', 'module="answer"'))
    elif mutation == "version":
        descriptor.write_text(descriptor.read_text().replace('revision="1.2.3"', 'revision="2.0"'))
    elif mutation == "missing-artifact":
        (receipt.parent / "jars/library-1.2.3.jar").unlink()
    elif mutation == "foreign-url":
        receipt.write_text(receipt.read_text().replace("/org/example/library/", "/other/project/"))
    elif mutation == "exists-false":
        receipt.write_text(receipt.read_text().replace(".exists=true", ".exists=false"))
    elif mutation == "incomplete-record":
        receipt.write_text(receipt.read_text() + "artifact\\:unbound\\#jar\\#jar\\#99.exists=true\n")
    elif mutation == "duplicate":
        receipt.write_text(receipt.read_text() + "resolver=other\n")
    elif mutation == "originless-descriptor":
        receipt.write_text("\n".join(line for line in receipt.read_text().splitlines()
                                    if not line.startswith("artifact\\:ivy\\#")) + "\n")
    with pytest.raises(ContractError, match=match):
        remote_ivy_files(tmp_path)


@pytest.mark.parametrize("target", [
    "ivy/cache/org.example/library/ivydata-1.2.3.properties",
    "ivy/cache/org.example/library/jars/library-1.2.3.jar",
    "ivy/cache/org.example/library/ivy-1.2.3.xml",
    "ivy/cache/org.example/library",
    "ivy/cache",
])
def test_remote_ivy_rejects_symlinked_inputs(tmp_path, target):
    home, outside = tmp_path / "home", tmp_path / "outside"
    module(home)
    path = home / target
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    with pytest.raises(ContractError):
        remote_ivy_files(home)


@pytest.mark.parametrize("version", ["1.2-SNAPSHOT", "latest", "RELEASE", "[1,2)"])
def test_remote_ivy_requires_pinned_release_coordinates(tmp_path, version):
    module(tmp_path, version=version)
    with pytest.raises(ContractError, match="pinned release"):
        remote_ivy_files(tmp_path)


def test_ivy_java_properties_escapes_and_continuations_are_interpreted(tmp_path):
    expected, receipt = module(tmp_path)
    text = receipt.read_text().replace("repo.maven.apache.org", "repo.maven.\\\n   apache.org")
    text = text.replace("resolver=sbt-chain", "resolver=sbt-\\u0063hain")
    receipt.write_text(text)
    assert set(remote_ivy_files(tmp_path)) == expected


def test_dependency_overlay_hashes_remote_ivy_and_excludes_local_outputs(tmp_path):
    seed, cache, output = (tmp_path / name for name in ("seed", "cache", "overlay"))
    names, _ = module(cache)
    put(cache, "ivy/local/org.example/candidate/1/jars/answer.jar")
    changed, removed = dependency_overlay(seed, cache, [], output)
    assert set(changed) == names and removed == []
    assert changed == {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in names}
    assert set(remote_ivy_files(output)) == names
    validate_remote_ivy_overlay(output, changed)


@pytest.mark.parametrize("unbound", [
    "ivy/local/org.example/candidate/1/jars/answer.jar",
    ".ivy2/local/org.example/candidate/1/jars/answer.jar",
    "ivy/cache/org.example/library/jars/unlisted-answer.jar",
])
def test_runtime_admission_rejects_unbound_and_local_ivy_manifest_entries(tmp_path, unbound):
    names, _ = module(tmp_path)
    put(tmp_path, unbound)
    with pytest.raises(ContractError, match="unbound or local Ivy"):
        validate_remote_ivy_overlay(tmp_path, names | {unbound})


def test_runtime_admission_requires_origin_receipts_in_the_hash_manifest(tmp_path):
    names, receipt = module(tmp_path)
    with pytest.raises(ContractError, match="unbound or local Ivy"):
        validate_remote_ivy_overlay(tmp_path, names - {receipt.relative_to(tmp_path).as_posix()})


def test_refresh_requires_explicit_ivy_admission_and_bound_successful_public_build(tmp_path):
    target, seed = tmp_path / "prepared", tmp_path / "seed"
    names, _ = module(target / "thin-home")
    log = put(target, "install.log", "[success] public compile passed\n")
    previous = {
        "status": "prepared", "exit_status": 0, "oracle_fingerprint": "frozen",
        "base_asset": {"path": "/base", "sha256": "1" * 64}, "overlay_files": {}, "removed_files": [],
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
    }
    with pytest.raises(ContractError, match="explicit remote Ivy"):
        refresh_prepared_overlay(target, seed, [], previous, "frozen", previous["base_asset"])
    result = refresh_prepared_overlay(
        target, seed, [], previous, "frozen", previous["base_asset"], include_remote_ivy=True)
    assert set(result["remote_ivy_additions"]) == names
    assert result["negative_cache_additions"] == []
    assert result["log_sha256"] == previous["log_sha256"]
    assert result["refresh_remote_ivy"] is True
    assert set(remote_ivy_files(target / "overlay")) == names
    log.write_text("changed build evidence")
    with pytest.raises(ContractError, match="hash-bound successful public build"):
        refresh_prepared_overlay(
            target, seed, [], previous, "frozen", previous["base_asset"], include_remote_ivy=True)


def test_refresh_never_overwrites_previously_bound_positive_artifacts(tmp_path):
    target, seed = tmp_path / "prepared", tmp_path / "seed"
    names, _ = module(target / "thin-home")
    log = put(target, "install.log", "success")
    changed, removed = dependency_overlay(seed, target / "thin-home", [], target / "overlay")
    previous = {
        "status": "prepared", "exit_status": 0, "oracle_fingerprint": "frozen",
        "base_asset": {"sha256": "1" * 64}, "overlay_files": changed, "removed_files": removed,
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
    }
    jar = next(name for name in names if name.endswith(".jar"))
    (target / "thin-home" / jar).write_text("substituted jar")
    with pytest.raises(ContractError, match="previously bound positive dependency"):
        refresh_prepared_overlay(
            target, seed, [], previous, "frozen", previous["base_asset"], include_remote_ivy=True)
