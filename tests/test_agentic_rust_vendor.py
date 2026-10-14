"""Offline subprocess regressions for source-disambiguated Cargo assets."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_lane
from reviewbench_maintainability.agentic.contracts import ContractError


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()


def commit(root):
    git(root, "init", "-q")
    git(root, "config", "user.name", "Rust vendor fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-qm", "immutable fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def cargo_tool():
    executable = os.environ.get("REVIEWBENCH_TEST_CARGO") or shutil.which("cargo")
    if not executable:
        pytest.skip("a preinstalled Cargo is required for offline metadata subprocesses")
    executable = Path(executable)
    # Do not let a rustup proxy install a toolchain during a test.
    env = dict(os.environ, RUSTUP_AUTO_INSTALL="0")
    result = subprocess.run([str(executable), "--version"], env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        pytest.skip("no preinstalled Cargo: " + result.stderr)
    return executable


def context(tmp_path, cargo, repository):
    output = tmp_path / "output"
    output.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    registry = tmp_path / "toolchains.json"
    registry.write_text(json.dumps({"fixture": {"root": str(cargo.parent.parent)}}))

    def bundle(root, **kwargs):
        copied = tmp_path / "bundled"
        shutil.copytree(root, copied)
        return {"path": str(copied), "sha256": "bundle"}

    return SimpleNamespace(
        config={"rust_toolchains": str(registry)}, output=output, scratch=scratch,
        repo=lambda _: repository, bundle=bundle,
        copy_asset=lambda path, _: {"path": str(path), "sha256": sha(path)},
    )


def duplicate_project(tmp_path, cargo):
    sources = []
    for number in (1, 2):
        root = tmp_path / ("upstream-" + str(number))
        (root / "bn/src").mkdir(parents=True)
        (root / "Cargo.toml").write_text(
            '[workspace]\nmembers=["bn"]\n[workspace.package]\nversion="0.6.0"\nedition="2021"\n'
        )
        (root / "bn/Cargo.toml").write_text(
            '[package]\nname="substrate-bn"\nversion.workspace=true\nedition.workspace=true\nbuild="build.rs"\n'
        )
        (root / "bn/build.rs").write_text("fn main() {}\n")
        (root / "bn/src/lib.rs").write_text("pub const SOURCE: u8 = " + str(number) + ";\n")
        sources.append((root, commit(root)))
    repository = tmp_path / "workspace"
    (repository / "src").mkdir(parents=True)
    manifest = '[package]\nname="consumer"\nversion="0.1.0"\nedition="2021"\n[dependencies]\n'
    for alias, (root, revision) in zip(("left", "right"), sources):
        manifest += alias + "={package=\"substrate-bn\",git=" + json.dumps(root.as_uri()) + ',rev="' + revision + '"}\n'
    (repository / "Cargo.toml").write_text(manifest)
    (repository / "src/lib.rs").write_text("pub fn sources() -> (u8, u8) { (left::SOURCE, right::SOURCE) }\n")
    env = dict(os.environ, CARGO_HOME=str(tmp_path / "initial-cache"), RUSTUP_AUTO_INSTALL="0",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0")
    # All dependencies are file:// Git repositories; no registry or HTTP source.
    subprocess.run([str(cargo), "generate-lockfile"], cwd=repository, env=env,
                   check=True, capture_output=True, timeout=30)
    return repository, commit(repository)


def test_cargo_vendor_same_name_version_sources_are_kept_distinct(tmp_path, cargo_tool):
    repository, revision = duplicate_project(tmp_path, cargo_tool)
    ctx = context(tmp_path, cargo_tool, repository)
    lock_hash = sha(repository / "Cargo.lock")
    task = {"id": "source-collision-fixture", "start_sha": revision}
    plan = {"input_index": 1, "toolchain": "fixture", "command": "cargo test --locked"}
    archive, config = rust_lane.cargo_vendor(ctx, task, plan, "", {}, "Cargo.lock")
    assert sha(repository / "Cargo.lock") == lock_hash
    root = Path(archive["path"])
    packages = sorted(root.glob("*/substrate-bn-0.6.0"))
    assert len(packages) == 2
    assert {p.joinpath("src/lib.rs").read_text() for p in packages} == {
        "pub const SOURCE: u8 = 1;\n", "pub const SOURCE: u8 = 2;\n"
    }
    assert all(p.joinpath("build.rs").read_text() == "fn main() {}\n" for p in packages)
    text = Path(config["path"]).read_text()
    assert str(ctx.scratch) not in text
    replacements = tomllib.loads(text)["source"]
    assert len({r["directory"] for r in replacements.values() if "directory" in r}) == 2
    offline = tmp_path / "offline.toml"
    offline.write_text(text.replace("/opt/reviewbench-assets/vendor", str(root)))
    env = dict(os.environ, CARGO_HOME=str(tmp_path / "empty-cache"), RUSTUP_AUTO_INSTALL="0",
               CARGO_NET_OFFLINE="true")
    result = subprocess.run(
        [str(cargo_tool), "--config", str(offline), "metadata", "--locked", "--offline", "--format-version", "1"],
        cwd=repository, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    resolved = [p for p in json.loads(result.stdout)["packages"] if p["name"] == "substrate-bn"]
    assert len({p["source"] for p in resolved}) == 2
    assert sha(repository / "Cargo.lock") == lock_hash


def test_vector_recipe_carries_pinned_protoc(tmp_path, monkeypatch):
    """Exercise the real recipe assembly, without fetching/building the project."""
    plan = {"input_index": 110, "toolchain": "1.66.1", "command": "cargo test --locked --no-default-features"}
    monkeypatch.setattr(rust_lane, "load_plan", lambda *args: plan)
    monkeypatch.setattr(rust_lane, "missing_lock_assets", lambda *args: {})
    monkeypatch.setattr(rust_lane, "compiler", lambda *args: {"path": "rust", "sha256": "a" * 64})
    monkeypatch.setattr(rust_lane, "vendor", lambda *args, **kwargs: ({"path": "vendor", "sha256": "b" * 64}, None))
    monkeypatch.setattr(rust_lane.RuntimeRecipe, "validate", lambda self: None)
    # The helper exists only after the repair. Leave the baseline recipe untouched.
    try:
        from agentic_runtime_migration import rust_protoc
    except ImportError:
        pass
    else:
        monkeypatch.setattr(rust_protoc, "build_asset", lambda ctx: {"path": "protoc", "sha256": "c" * 64})
    ctx = SimpleNamespace(
        scratch=tmp_path, task_directory=lambda *args: tmp_path,
        golden=lambda *args: {"path": "golden", "sha256": "d" * 64},
        image=lambda _: (rust_lane.IMAGE, {}), modules=lambda _: [], immutable_tests=lambda _: [],
    )
    task = {"id": "github__vectordotdev__vector__pull_17037__comment_1157500473__duplication",
            "repo": "vectordotdev/vector", "start_sha": "83e6cc06830ada9ebe7691dd4bfceeb9ca28ad92"}
    recipe = rust_lane.migrate(ctx, task, {"pass_to_pass_tests": ["unchanged::assertion"]})
    assert recipe.env.get("PROTOC") == "/opt/reviewbench-assets/protoc/bin/protoc"
    assert recipe.env["PROTOC_INCLUDE"] == "/opt/reviewbench-assets/protoc/include"
    assert recipe.env["PATH"].startswith("/opt/reviewbench-assets/protoc/bin:")
    assets = [a for a in recipe.assets if a["target"] == "/opt/reviewbench-assets/protoc"]
    assert len(assets) == 1 and assets[0]["visibility"] == "developer"
    assert all("curl" not in command and "wget" not in command for command in recipe.bootstrap)
    assert recipe.grading["commands"][0]["expected_tests"] == ["unchanged::assertion"]


def test_source_config_preserves_git_selectors_and_separates_registry(tmp_path):
    from agentic_runtime_migration.rust_vendor import CRATES_IO, source_configuration, source_collisions
    sources = [CRATES_IO, "git+https://example.invalid/bn?tag=release%2Fv2#" + "a" * 40,
               "git+https://example.invalid/bn?branch=other#" + "b" * 40]
    text, roots = source_configuration(sources)
    config = tomllib.loads(text)["source"]
    assert len(set(roots.values())) == 3
    assert config["crates-io"]["replace-with"] in config
    assert any(row.get("tag") == "release/v2" for row in config.values())
    assert any(row.get("branch") == "other" for row in config.values())
    assert source_configuration(reversed(sources)) == (text, roots)
    packages = [{"name": "substrate-bn", "version": "0.6.0", "source": s} for s in sources]
    assert source_collisions([{"package": packages[:1]}, {"package": packages[1:]}])[0]["sources"] == sorted(sources)
    assert not source_collisions([{"package": packages[:1]}, {"package": packages[:1]}])


@pytest.mark.parametrize("source", [
    "git+https://example.invalid/bn#short",
    "git+https://example.invalid/bn?tag=v1&rev=v2#" + "a" * 40,
    "git+https://example.invalid/bn?tag=#" + "a" * 40,
    "git+https://example.invalid/bn?unknown=v1#" + "a" * 40,
    "git+https://example.invalid/bn?tag#" + "a" * 40,
    "registry+https://unbound.invalid/index",
])
def test_source_config_rejects_ambiguous_or_unpinned_sources(source):
    from agentic_runtime_migration.rust_vendor import source_configuration
    with pytest.raises(ContractError):
        source_configuration([source])


def test_synced_locks_cannot_conflate_two_commits_of_one_git_selector():
    from agentic_runtime_migration.rust_vendor import source_configuration
    with pytest.raises(ContractError, match="multiple commits"):
        source_configuration(["git+https://example.invalid/bn?tag=v1#" + commit for commit in ("a" * 40, "b" * 40)])


def test_lock_checksum_conflicts_use_full_identity_not_just_name_version():
    from agentic_runtime_migration.rust_vendor import CRATES_IO, locked_packages
    registry = {"name": "bn", "version": "0.6.0", "source": CRATES_IO, "checksum": "a" * 64}
    upstream = {"name": "bn", "version": "0.6.0", "source": "git+https://example.invalid/bn#" + "b" * 40}
    assert len(locked_packages([{"package": [registry, upstream]}])) == 2
    with pytest.raises(ContractError, match="conflicting checksums"):
        locked_packages([{"package": [registry]}, {"package": [dict(registry, checksum="c" * 64)]}])
    with pytest.raises(ContractError, match="checksum missing"):
        locked_packages([{"package": [{k: v for k, v in registry.items() if k != "checksum"}]}])
    with pytest.raises(ContractError, match="archive provider"):
        locked_packages([{"package": [dict(registry, source="registry+https://other.invalid")]}])
    old = {"package": [{k: v for k, v in registry.items() if k != "checksum"}],
           "metadata": {"checksum bn 0.6.0 (" + CRATES_IO + ")": "a" * 64}}
    assert locked_packages([old]) == {("bn", "0.6.0", CRATES_IO): "a" * 64}


def mixed_inputs(tmp_path):
    """A real registry archive plus an independently committed Git namesake."""
    from agentic_runtime_migration.rust_vendor import CRATES_IO
    cache = tmp_path / "cargo"
    repository = cache / "git/checkouts/bn/locked"
    (repository / "src").mkdir(parents=True)
    (repository / "Cargo.toml").write_text('[package]\nname="substrate-bn"\nversion="0.6.0"\nbuild="build.rs"\n')
    (repository / "src/lib.rs").write_text("pub const SOURCE: u8 = 2;\n")
    (repository / "build.rs").write_text("fn main() {}\n")
    revision = commit(repository)
    source = "git+" + repository.as_uri() + "?rev=" + revision + "#" + revision
    archive = tmp_path / "substrate-bn-0.6.0.crate"
    with tarfile.open(archive, "w:gz") as stream:
        for name, data in [("Cargo.toml", b'[package]\nname="substrate-bn"\nversion="0.6.0"\n'),
                           ("src/lib.rs", b"pub const SOURCE: u8 = 1;\n")]:
            member = tarfile.TarInfo("substrate-bn-0.6.0/" + name)
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
    lock = {"version": 3, "package": [
        {"name": "substrate-bn", "version": "0.6.0", "source": CRATES_IO, "checksum": sha(archive)},
        {"name": "substrate-bn", "version": "0.6.0", "source": source},
    ]}
    metadata = {"packages": [dict(lock["package"][1], manifest_path=str(repository / "Cargo.toml"))]}
    return cache, repository, archive, lock, metadata


def test_registry_and_git_namesakes_resolve_offline_without_lock_or_code_changes(tmp_path, cargo_tool):
    import tomli_w
    from agentic_runtime_migration.rust_vendor import CRATES_IO, TARGET, materialize
    cache, upstream, archive, lock, metadata = mixed_inputs(tmp_path)
    # Even a dirty cached build script must not become an asset.
    (upstream / "build.rs").write_text("mutable cache is not an input")
    (upstream / "src/lib.rs").write_text("mutable cache is not source code")
    root = tmp_path / "vendor"
    config, audit = materialize([lock], metadata, cache, root, lambda *args: archive)
    assert len(audit) == 2
    assert {row["package_checksum"] for row in audit} == {None, sha(archive)}
    git_row = next(row for row in audit if row["source"].startswith("git+"))
    assert (root / git_row["directory"] / "build.rs").read_text() == "fn main() {}\n"
    for row in audit:
        package = root / row["directory"]
        checksums = json.loads((package / ".cargo-checksum.json").read_text())
        assert all(sha(package / path) == digest for path, digest in checksums["files"].items())
    consumer = tmp_path / "consumer"
    (consumer / "src").mkdir(parents=True)
    revision = git(upstream, "rev-parse", "HEAD")
    (consumer / "Cargo.toml").write_text(
        '[package]\nname="consumer"\nversion="0.1.0"\n[dependencies]\n'
        'left={package="substrate-bn",version="=0.6.0"}\n'
        'right={package="substrate-bn",git=' + json.dumps(upstream.as_uri()) + ',rev="' + revision + '"}\n'
    )
    (consumer / "src/lib.rs").write_text("pub fn sources() -> (u8,u8) { (left::SOURCE,right::SOURCE) }\n")
    lock["package"].insert(0, {"name": "consumer", "version": "0.1.0", "dependencies": [
        "substrate-bn 0.6.0 (" + CRATES_IO + ")", "substrate-bn 0.6.0 (" + git_row["source"] + ")",
    ]})
    (consumer / "Cargo.lock").write_text(tomli_w.dumps(lock))
    before = sha(consumer / "Cargo.lock")
    config_file = tmp_path / "sources.toml"
    config_file.write_text(config.replace(TARGET, str(root)))
    result = subprocess.run(
        [str(cargo_tool), "--config", str(config_file), "metadata", "--offline", "--locked", "--format-version", "1"],
        cwd=consumer, env=dict(os.environ, CARGO_HOME=str(tmp_path / "empty-cache"), CARGO_NET_OFFLINE="true",
                              RUSTUP_AUTO_INSTALL="0"),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    resolved = [p for p in json.loads(result.stdout)["packages"] if p["name"] == "substrate-bn"]
    assert {p["source"] for p in resolved} == {CRATES_IO, git_row["source"]}
    assert sha(consumer / "Cargo.lock") == before
    assert (upstream / "build.rs").read_text() == "mutable cache is not an input"


@pytest.mark.parametrize("failure", ["checksum", "foreign_metadata", "wrong_commit", "missing_package", "symlink"])
def test_materialization_failures_leave_no_partial_vendor(tmp_path, failure):
    from agentic_runtime_migration.rust_vendor import materialize
    cache, repository, archive, lock, metadata = mixed_inputs(tmp_path)
    if failure == "checksum":
        archive.write_bytes(b"wrong archive")
    elif failure == "foreign_metadata":
        metadata["packages"][0]["manifest_path"] = str(tmp_path / "outside/Cargo.toml")
    elif failure == "wrong_commit":
        (repository / "extra").write_text("new commit")
        git(repository, "add", ".")
        git(repository, "commit", "-qm", "unbound commit")
    elif failure == "missing_package":
        metadata["packages"] = []
    else:
        # A tracked link within the repository but outside the package boundary.
        (repository / "src/link.rs").symlink_to("../../../outside")
        git(repository, "add", ".")
        git(repository, "commit", "-qm", "escaping link")
        revision = git(repository, "rev-parse", "HEAD")
        source = "git+" + repository.as_uri() + "?rev=" + revision + "#" + revision
        lock["package"][1]["source"] = metadata["packages"][0]["source"] = source
    with pytest.raises(ContractError):
        materialize([lock], metadata, cache, tmp_path / "vendor", lambda *args: archive)
    assert not (tmp_path / "vendor").exists()


def test_git_workspace_inheritance_preserves_renames_target_features_and_files(tmp_path, cargo_tool):
    from agentic_runtime_migration.rust_vendor import materialize, TARGET
    cache = tmp_path / "cargo"
    repository = cache / "git/checkouts/workspace/locked"
    (repository / "member/src").mkdir(parents=True)
    (repository / "helper/src").mkdir(parents=True)
    (repository / "README.md").write_text("workspace readme\n")
    (repository / "LICENSE").write_text("workspace license\n")
    (repository / "Cargo.toml").write_text(
        '[workspace]\nmembers=["member","helper"]\n[workspace.package]\nversion="0.6.0"\nedition="2021"\n'
        'readme="README.md"\nlicense-file="LICENSE"\n'
        '[workspace.dependencies]\nrenamed={package="helper",path="helper",version="0.6.0",features=["base"]}\n'
        '[workspace.lints.rust]\nunsafe_code="forbid"\n'
    )
    (repository / "member/Cargo.toml").write_text(
        '[package]\nname="member"\nversion.workspace=true\nedition.workspace=true\n'
        'readme.workspace=true\nlicense-file.workspace=true\n'
        '[target."cfg(unix)".dependencies]\nrenamed={workspace=true,features=["extra"]}\n[lints]\nworkspace=true\n'
    )
    (repository / "member/src/lib.rs").write_text("pub fn value() -> u8 { renamed::VALUE }\n")
    (repository / "helper/Cargo.toml").write_text(
        '[package]\nname="helper"\nversion.workspace=true\nedition.workspace=true\n[features]\nbase=[]\nextra=[]\n'
    )
    (repository / "helper/src/lib.rs").write_text("pub const VALUE:u8=1;\n")
    revision = commit(repository)
    consumer = tmp_path / "consumer"
    (consumer / "src").mkdir(parents=True)
    (consumer / "src/lib.rs").write_text("pub fn value()->u8{member::value()}\n")
    (consumer / "Cargo.toml").write_text(
        '[package]\nname="consumer"\nversion="0.1.0"\n[dependencies]\nmember={git=' +
        json.dumps(repository.as_uri()) + ',rev="' + revision + '"}\n')
    env = dict(os.environ, CARGO_HOME=str(cache), RUSTUP_AUTO_INSTALL="0")
    result = subprocess.run([str(cargo_tool), "metadata", "--format-version", "1"], cwd=consumer, env=env,
                            capture_output=True, text=True, check=True, timeout=30)
    metadata = json.loads(result.stdout)
    lock = tomllib.loads((consumer / "Cargo.lock").read_text())
    before = sha(consumer / "Cargo.lock")
    root = tmp_path / "vendor"
    config, audit = materialize([lock], metadata, cache, root, lambda *args: pytest.fail("no registry input"))
    member = root / next(row["directory"] for row in audit if row["name"] == "member")
    assert (member / "reviewbench-workspace-files/readme").read_text() == "workspace readme\n"
    assert (member / "reviewbench-workspace-files/license-file").read_text() == "workspace license\n"
    local = tmp_path / "sources.toml"
    local.write_text(config.replace(TARGET, str(root)))
    result = subprocess.run(
        [str(cargo_tool), "--config", str(local), "metadata", "--locked", "--offline", "--format-version", "1"],
        cwd=consumer, env=dict(env, CARGO_HOME=str(tmp_path / "empty")), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    original = next(p for p in metadata["packages"] if p["name"] == "member")["dependencies"][0]
    vendored = next(p for p in resolved["packages"] if p["name"] == "member")["dependencies"][0]
    for field in ("name", "rename", "source", "req", "target", "features", "optional", "uses_default_features"):
        assert original[field] == vendored[field]
    assert sha(consumer / "Cargo.lock") == before


def test_source_command_timeout_kills_process_and_records_real_exit(tmp_path):
    from agentic_runtime_migration.rust_vendor import _run
    with pytest.raises(ContractError, match="1200-second budget"):
        _run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, env=os.environ,
             prefix=tmp_path / "timeout", deadline=time.monotonic() - 1, scratch=tmp_path)
    record = json.loads((tmp_path / "timeout.json").read_text())
    assert record["exit_status"] < 0
    assert record["command"] == [sys.executable, "-c", "import time; time.sleep(60)"]
    assert "1200-second budget" in record["error"]
