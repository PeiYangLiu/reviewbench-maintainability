"""Filesystem-only Bundler fixtures; no project downloads, images, or replays."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import ruby_candidate as ruby
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.snapshots import collect_patch, git, initialize_git


IMAGE = "sha256:" + "a" * 64
PIN = "2.4.19"
LAUNCHER = f'gem "bundler", "= {PIN}"; require "bundler"; require "bundler/cli"; Bundler::CLI.start(ARGV)'


def plan(task_id=ruby.KNAPSACK, **kwargs):
    return ruby.setup_plan(task_id, ruby.TARGETS[task_id][2], bundler_version=PIN, **kwargs)


def run(argv, *, cwd=None, env=None, timeout=30):
    return subprocess.run(argv, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)


def shell(command, env=None):
    return run(["bash", "-c", command], env=env)


def sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.mark.parametrize("task_id", [ruby.KNAPSACK, ruby.PROMETHEUS])
def test_original_literal_shapes_require_an_explicit_exact_bundler_pin(task_id):
    result = plan(task_id)
    assert result["original_command"] == ruby.TARGETS[task_id][2]
    assert result["bundler_version"] == PIN
    assert result["original_jobs"] == 8 and result["jobs"] == 2
    assert result["workspace"] == "." and result["kind"] == "bundler"
    assert result["additions"] == ([] if task_id == ruby.KNAPSACK else ruby.ADDITIONS)


@pytest.mark.parametrize("suffix", ["; true", " && true", "\ntrue", " || true", " | cat", " > output"])
@pytest.mark.parametrize("task_id", [ruby.KNAPSACK, ruby.PROMETHEUS])
def test_no_extra_setup_shell_is_accepted(task_id, suffix):
    with pytest.raises(ContractError, match="literal chains"):
        ruby.setup_plan(task_id, ruby.TARGETS[task_id][2] + suffix, bundler_version=PIN)


@pytest.mark.parametrize("command", [
    ruby.INSTALL.replace("--jobs 8", "--jobs 2"),
    ruby.INSTALL.replace("/work/.bundle", "/work/lib"),
    ruby.TARGETS[ruby.PROMETHEUS][2].replace("~> 0.9.0", ">= 0"),
    ruby.TARGETS[ruby.PROMETHEUS][2].replace("--skip-install", "--skip-install --source https://other.invalid", 1),
    "bundle update && " + ruby.INSTALL,
    "bundle install --jobs $(printf 8)",
])
def test_nearby_shapes_and_new_requirements_are_not_adapted(command):
    with pytest.raises(ContractError):
        ruby.setup_plan(ruby.PROMETHEUS, command, bundler_version=PIN)


@pytest.mark.parametrize("pin", ["latest", "2.4", "2.4.20", "", "2.4.19; true"])
def test_resolver_version_is_not_floating_or_silently_changed(pin):
    with pytest.raises(ContractError, match="explicit Bundler"):
        ruby.setup_plan(ruby.KNAPSACK, ruby.INSTALL, bundler_version=pin)


@pytest.mark.parametrize("jobs", [True, 0, -1, 9, "2"])
def test_install_workers_are_bounded(jobs):
    with pytest.raises(ContractError, match="bounded"):
        plan(jobs=jobs)


@pytest.fixture(scope="session")
def local_ruby():
    executable = shutil.which("ruby")
    if executable is None:
        pytest.skip("the parent-owned Ruby image supplies the pinned interpreter")
    result = run([executable, "-e", f'gem "bundler", "= {PIN}"; require "bundler"; puts RUBY_VERSION; puts Bundler::VERSION'])
    if result.returncode or not result.stdout.startswith("3.2."):
        pytest.skip("local execution requires Ruby 3.2 and Bundler 2.4.19")
    return executable


@pytest.fixture(scope="session")
def gem_repository(tmp_path_factory, local_ruby):
    """Four tiny local .gem files plus a file:// index, never a network service."""
    root = tmp_path_factory.mktemp("ruby-fixture-gems")
    script = r'''
require "rubygems/package"
require "rubygems/indexer"
require "fileutils"
root = ARGV.fetch(0)
FileUtils.mkdir_p(File.join(root, "gems"))
[["tiny_fixture_dep", "1.0.0"], ["redis", "5.0.9"], ["redis-client", "0.9.0"],
 ["connection_pool", "2.4.1"]].each do |name, version|
  work = File.join(root, "build", name)
  FileUtils.mkdir_p(File.join(work, "lib"))
  File.write(File.join(work, "lib", name + ".rb"), "# local dependency fixture\n")
  spec = Gem::Specification.new do |s|
    s.name = name
    s.version = version
    s.summary = "Local offline regression fixture"
    s.authors = ["Fixture"]
    s.license = "MIT"
    s.files = ["lib/" + name + ".rb"]
    s.require_paths = ["lib"]
  end
  Dir.chdir(work) do
    built = Gem::Package.build(spec)
    FileUtils.mv(built, File.join(root, "gems", built))
  end
end
Gem::Indexer.new(root).generate_index
FileUtils.rm_rf(File.join(root, "build"))
'''
    result = run([local_ruby, "-e", script, str(root)])
    assert result.returncode == 0, result.stdout
    return root


def source_fixture(tmp_path, *, task_id=ruby.KNAPSACK, gem_repository=None, tracked_lock=None):
    source = tmp_path / "candidate"
    (source / "lib").mkdir(parents=True)
    (source / "test").mkdir()
    (source / "spec").mkdir()
    selected = plan(task_id)
    gem_name = selected["gemspec"].removesuffix(".gemspec")
    # RubyGems' file fetcher reads the literal path (including the local user's
    # @ character); percent-encoding it would name a different filesystem file.
    origin = "file://" + gem_repository.as_posix() if gem_repository else "file:///fixture-only-no-network"
    gemfile = f'source "{origin}"\n\ngemspec\n'
    if task_id == ruby.KNAPSACK:
        gemfile += '\ngroup :test do\n  gem "tiny_fixture_dep", "~> 1.0"\nend\n'
    (source / "Gemfile").write_text(gemfile)
    (source / selected["gemspec"]).write_text(
        'lib = File.expand_path("../lib", __FILE__)\n$LOAD_PATH.unshift(lib)\n' +
        'require "fixture_version"\nGem::Specification.new do |s|\n' +
        f'  s.name = "{gem_name}"\n  s.version = FixtureVersion::VALUE\n' +
        '  s.summary = "Current candidate fixture"\n  s.authors = ["Fixture"]\n' +
        '  s.files = `git ls-files -z`.split("\\x0")\n  s.require_paths = ["lib"]\n' +
        ('  s.add_development_dependency "redis", "> 5"\n' if task_id == ruby.PROMETHEUS else '') +
        'end\n')
    (source / "lib/fixture_version.rb").write_text('module FixtureVersion; VALUE = "0.1.0"; end\n')
    (source / ("lib/" + gem_name + ".rb")).write_text(
        'module CandidateFixture\n  def self.value\n    "original"\n  end\nend\n')
    (source / "test/fixed.rb").write_text(
        f'require "{gem_name}"\nraise "bad candidate" unless CandidateFixture.value == ENV.fetch("EXPECTED")\nputs "FIXTURE_PASS"\n')
    (source / "spec/fixed_spec.rb").write_text("# fixed test selector bytes\n")
    (source / ".source-helper.rb").write_text("# hidden source stays live\n")
    if tracked_lock is not None:
        (source / "Gemfile.lock").write_bytes(tracked_lock)
    initialize_git(source)
    task = {"id": task_id, "repo": ruby.TARGETS[task_id][0],
            "start_sha": git(source, "rev-parse", "HEAD").decode().strip()}
    start = ruby.inspect_start(source, task, selected)
    return SimpleNamespace(source=source, plan=selected, task=task, start=start,
                           state=source / ".reviewbench/ruby-candidate", gem_name=gem_name)


def test_frozen_absence_is_not_a_local_or_historical_lock_substitution(tmp_path):
    fixture = source_fixture(tmp_path)
    assert fixture.start["start_lock_present"] is False
    assert fixture.start["source_sha256"] == ruby.tree_fingerprint(fixture.source, source=True)
    (fixture.source / "Gemfile.lock").write_text("untracked historical/local lock")
    assert ruby.inspect_start(fixture.source, fixture.task, fixture.plan) == fixture.start
    with pytest.raises(ContractError, match="explicit --resolve-unlocked"):
        ruby.prepare_or_reuse(fixture.plan, fixture.start, IMAGE)
    request = ruby.prepare_or_reuse(fixture.plan, fixture.start, IMAGE, resolve_unlocked=True)
    assert request["action"] == "resolve" and request["explicit_opt_in"] is True
    assert request["origin"] == "resolved-from-start-manifest"
    assert (fixture.source / "Gemfile.lock").read_text() == "untracked historical/local lock"


def test_tracked_start_lock_is_never_re_resolved_even_with_opt_in(tmp_path):
    fixture = source_fixture(tmp_path, tracked_lock=b"original tracked lock\n")
    assert fixture.start["start_lock_present"] is True
    with pytest.raises(ContractError, match="absent at frozen start"):
        ruby.prepare_or_reuse(fixture.plan, fixture.start, IMAGE, resolve_unlocked=True)
    assert (fixture.source / "Gemfile.lock").read_bytes() == b"original tracked lock\n"


@pytest.mark.parametrize("change", ["id", "repo", "sha", "symlink-manifest", "symlink-lock"])
def test_start_identity_and_regular_input_boundaries(tmp_path, change):
    fixture = source_fixture(tmp_path)
    task = dict(fixture.task)
    if change in {"id", "repo", "sha"}:
        task[{"id": "id", "repo": "repo", "sha": "start_sha"}[change]] = "wrong"
    else:
        name = "Gemfile" if change == "symlink-manifest" else "Gemfile.lock"
        (fixture.source / name).unlink(missing_ok=True)
        (fixture.source / name).symlink_to(fixture.plan["gemspec"])
        git(fixture.source, "add", "-A")
        git(fixture.source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-m", "non-regular input")
        task["start_sha"] = git(fixture.source, "rev-parse", "HEAD").decode().strip()
    with pytest.raises(ContractError):
        ruby.inspect_start(fixture.source, task, fixture.plan)


def prepared_fixture(tmp_path, gem_repository, *, task_id=ruby.KNAPSACK, bootstrap=False):
    fixture = source_fixture(tmp_path, task_id=task_id, gem_repository=gem_repository)
    fixture.payload = tmp_path / "payload"
    fixture.bundle_path = fixture.payload / "gems"
    fixture.bundle_path.mkdir(parents=True)
    if bootstrap:
        (fixture.payload / "bootstrap").mkdir()
    fixture.request = ruby.prepare_or_reuse(
        fixture.plan, fixture.start, IMAGE, resolve_unlocked=True, payload=fixture.payload)
    fixture.command = ruby.resolution_command(
        fixture.request, source=fixture.source, state=fixture.state, bundle_path=fixture.bundle_path,
        bootstrap_path=fixture.payload / "bootstrap" if bootstrap else None)
    result = shell(fixture.command)
    assert result.returncode == 0, result.stdout
    fixture.report = json.loads((fixture.state / "report.json").read_text())
    fixture.record = ruby.capture_resolution(
        fixture.payload, fixture.request, source=fixture.source, state=fixture.state)
    return fixture


@pytest.mark.parametrize("task_id", [ruby.KNAPSACK, ruby.PROMETHEUS])
def test_real_original_resolution_only_changes_private_manifest_and_captures_new_lock(tmp_path, gem_repository, task_id):
    fixture = prepared_fixture(tmp_path, gem_repository, task_id=task_id)
    assert ruby.tree_fingerprint(fixture.source, source=True) == fixture.start["source_sha256"]
    assert not (fixture.source / "Gemfile.lock").exists()
    assert fixture.record["origin"] == "resolved-from-start-manifest"
    assert fixture.record["resolver"]["bundler_version"] == PIN
    assert fixture.record["resolver"]["ruby_version"].startswith("3.2.")
    assert fixture.report["source_before_sha256"] == fixture.report["source_after_sha256"]
    assert fixture.report["environment"]["BUNDLE_FROZEN"] == "false"
    assert fixture.report["commands"][-1] == ["install", "--jobs", "2"]
    build = (fixture.state / "source/Gemfile").read_text()
    original = (fixture.source / "Gemfile").read_text()
    if task_id == ruby.PROMETHEUS:
        assert build.startswith(original)
        assert fixture.report["commands"][:-1] == [
            ["add", name, "--version", requirement, "--skip-install"] for name, requirement in ruby.ADDITIONS]
        for name, requirement in ruby.ADDITIONS:
            assert f'gem "{name}", "{requirement}"' in build
            assert f'gem "{name}", "{requirement}"' not in original
        assert 's.add_development_dependency "redis", "> 5"' in (fixture.source / fixture.plan["gemspec"]).read_text()
    else:
        assert build == original
    target = fixture.payload / "ruby-resolution"
    provenance = json.loads((target / "provenance.json").read_text())
    assert provenance["historical_lock"] is False and provenance["explicit_opt_in"] is True
    assert provenance["image"] == IMAGE
    assert provenance["start"] == fixture.start
    assert provenance["report"]["resolver"] == fixture.record["resolver"]
    with tarfile.open(target / "resolution.tar") as archive:
        assert archive.getnames() == ["Gemfile.lock", "provenance.json"]
        assert archive.extractfile("Gemfile.lock").read() == (target / "Gemfile.lock").read_bytes()
    assert set(path.name for path in fixture.payload.iterdir()) == {"gems", "ruby-resolution"}
    assert not list((fixture.payload / "gems").glob("**/specifications/" + fixture.gem_name + "-*.gemspec"))
    ruby.validate_prepared(fixture.payload, fixture.record, plan=fixture.plan, start=fixture.start, image=IMAGE)


@pytest.mark.parametrize("task_id", [ruby.KNAPSACK, ruby.PROMETHEUS])
def test_offline_replay_uses_live_gemspec_helpers_and_tests_without_bundle_add(tmp_path, gem_repository, task_id):
    # Give this test an isolated origin that can be removed before offline replay.
    origin = tmp_path / "origin"
    shutil.copytree(gem_repository, origin)
    fixture = prepared_fixture(tmp_path, origin, task_id=task_id)
    original_manifest = (fixture.source / "Gemfile").read_bytes()
    original_spec = (fixture.source / fixture.plan["gemspec"]).read_bytes()
    original_tests = (fixture.source / "test/fixed.rb").read_bytes()
    shutil.rmtree(origin)
    cache = tmp_path / "lease-gems"
    shutil.copytree(fixture.bundle_path, cache, symlinks=True)
    current_manifest = original_manifest + b"\n# current candidate manifest change\n"
    (fixture.source / "Gemfile").write_bytes(current_manifest)
    current_spec = original_spec + b"\n# current candidate gemspec change\n"
    (fixture.source / fixture.plan["gemspec"]).write_bytes(current_spec)
    (fixture.source / "lib/NewHelper.rb").write_text('module NewHelper; VALUE = "new helper"; end\n')
    (fixture.source / ("lib/" + fixture.gem_name + ".rb")).write_text(
        'require_relative "NewHelper"\nmodule CandidateFixture\n  def self.value\n    NewHelper::VALUE\n  end\nend\n')
    (fixture.source / "new-top-level-helper.rb").write_text("# added after preparation\n")
    command = ruby.replay_command(fixture.record, source=fixture.source, state=fixture.state,
                                  bundle_path=cache, assets=fixture.payload)
    guard = tmp_path / "no-resolution.rb"
    guard.write_text(
        'require "bundler"\nclass Bundler::Definition\n'
        '  def start_resolution(*args); raise "unexpected fresh resolution"; end\n'
        '  def resolve_remotely!(*args); raise "unexpected remote resolution"; end\nend\n')
    result = shell(command, env={**os.environ, "RUBYOPT": "-r" + str(guard)})
    assert result.returncode == 0, result.stdout
    report = json.loads((fixture.state / "report.json").read_text())
    assert report["commands"] == [["install", "--local", "--jobs", "2"]]
    assert report["environment"]["BUNDLE_FROZEN"] == "true"
    assert report["lock_origin"] == "prepared-default"
    assert report["source_before_sha256"] == report["source_after_sha256"]
    assert (fixture.state / "source/new-top-level-helper.rb").read_bytes() == (fixture.source / "new-top-level-helper.rb").read_bytes()
    assert (fixture.state / ("source/" + fixture.plan["gemspec"])).is_symlink()
    env = {**os.environ, **ruby.runtime_environment(fixture.plan, source=fixture.source,
                                                  state=fixture.state, bundle_path=cache), "EXPECTED": "new helper"}
    checked = run(["ruby", "-e", LAUNCHER, "--", "exec", "ruby", "test/fixed.rb"],
                  cwd=fixture.source, env=env)
    assert checked.returncode == 0 and "FIXTURE_PASS" in checked.stdout, checked.stdout
    assert (fixture.source / "Gemfile").read_bytes() == current_manifest
    assert (fixture.state / "source/Gemfile").read_bytes().startswith(current_manifest)
    assert (fixture.source / fixture.plan["gemspec"]).read_bytes() == current_spec
    assert (fixture.source / "test/fixed.rb").read_bytes() == original_tests
    assert not (fixture.source / "Gemfile.lock").exists()
    assert ruby.tree_fingerprint(cache) == fixture.record["dependencies_sha256"]


def test_candidate_supplied_lock_is_preserved_and_is_not_production_scratch(tmp_path, gem_repository):
    fixture = prepared_fixture(tmp_path, gem_repository)
    base = tmp_path / "baseline"
    shutil.copytree(fixture.source, base, ignore=shutil.ignore_patterns(".git", ".reviewbench"))
    captured = (fixture.payload / "ruby-resolution/Gemfile.lock").read_bytes()
    candidate = captured + b"\n"  # same graph, distinct candidate-owned bytes
    (fixture.source / "Gemfile.lock").write_bytes(candidate)
    # The inactive default is never read or substituted for the candidate lock.
    (fixture.payload / "ruby-resolution/Gemfile.lock").unlink()
    command = ruby.replay_command(fixture.record, source=fixture.source, state=fixture.state,
                                  bundle_path=fixture.bundle_path, assets=fixture.payload)
    result = shell(command)
    assert result.returncode == 0, result.stdout
    assert (fixture.source / "Gemfile.lock").read_bytes() == candidate
    assert json.loads((fixture.state / "report.json").read_text())["lock_origin"] == "candidate"
    patch = tmp_path / "candidate.patch"
    collect_patch(base, fixture.source, patch)
    assert "diff --git a/Gemfile.lock b/Gemfile.lock" in patch.read_text()
    assert ".reviewbench" not in patch.read_text()


def test_generated_lock_and_original_additions_never_enter_production_patches(tmp_path, gem_repository):
    fixture = prepared_fixture(tmp_path, gem_repository, task_id=ruby.PROMETHEUS)
    base = tmp_path / "baseline"
    shutil.copytree(fixture.source, base, ignore=shutil.ignore_patterns(".git", ".reviewbench"))
    result = shell(ruby.replay_command(fixture.record, source=fixture.source, state=fixture.state,
                                      bundle_path=fixture.bundle_path, assets=fixture.payload))
    assert result.returncode == 0, result.stdout
    assert json.loads((fixture.state / "report.json").read_text())["build_manifest_sha256"] == fixture.report["build_manifest_sha256"]
    patch = tmp_path / "no-setup-changes.patch"
    collect_patch(base, fixture.source, patch)
    assert patch.read_text() == ""


def test_prepared_packages_are_reused_without_opt_in_and_mismatches_do_not_float(tmp_path, gem_repository):
    fixture = prepared_fixture(tmp_path, gem_repository)
    before = ruby.tree_fingerprint(fixture.payload)
    reused = ruby.prepare_or_reuse(fixture.plan, fixture.start, IMAGE, payload=fixture.payload,
                                  prepared=fixture.record)
    assert reused == {"action": "reuse", "record": fixture.record}
    assert ruby.tree_fingerprint(fixture.payload) == before
    with pytest.raises(ContractError, match="validation, not re-resolution"):
        ruby.prepare_or_reuse(fixture.plan, fixture.start, IMAGE, payload=fixture.payload, resolve_unlocked=True)
    changed = copy.deepcopy(fixture.record)
    changed["resolver"]["bundler_version"] = "2.5.0"
    with pytest.raises(ContractError):
        ruby.prepare_or_reuse(fixture.plan, fixture.start, IMAGE, payload=fixture.payload,
                              prepared=changed, resolve_unlocked=True)
    assert ruby.tree_fingerprint(fixture.payload) == before
    with pytest.raises(ContractError, match="already published"):
        ruby.capture_resolution(fixture.payload, fixture.request, source=fixture.source, state=fixture.state)


@pytest.mark.parametrize("change", ["lock", "provenance", "archive", "dependency", "image", "input", "resolver"])
def test_prepared_lock_payload_provenance_and_actual_resolver_are_bound(tmp_path, gem_repository, change):
    fixture = prepared_fixture(tmp_path, gem_repository)
    record, start, image = copy.deepcopy(fixture.record), copy.deepcopy(fixture.start), IMAGE
    if change in {"lock", "provenance", "archive"}:
        name = {"lock": "Gemfile.lock", "provenance": "provenance.json", "archive": "resolution.tar"}[change]
        path = fixture.payload / "ruby-resolution" / name
        path.write_bytes(path.read_bytes() + b"\nchanged")
    elif change == "dependency":
        (fixture.bundle_path / "unexpected.rb").write_text("changed dependency payload")
    elif change == "image":
        image = "sha256:" + "b" * 64
    elif change == "input":
        start["input_sha256"]["Gemfile"] = "b" * 64
    else:
        record["resolver"]["files_sha256"]["bundler/dsl.rb"] = "b" * 64
    with pytest.raises(ContractError):
        ruby.validate_prepared(fixture.payload, record, plan=fixture.plan, start=start, image=image)


@pytest.mark.parametrize("change", [
    "gemfile-link", "gemspec-link", "candidate-lock-link", "candidate-lock-directory",
    "state-link", "view-link", "owner-link", "unowned-state", "payload-link",
    "default-lock-hash", "different-bundler", "changed-resolver", "dependency-bytes",
])
def test_runtime_boundaries_reject_without_rewriting_candidate_inputs(tmp_path, gem_repository, change):
    fixture = prepared_fixture(tmp_path, gem_repository)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged external content")
    record = copy.deepcopy(fixture.record)
    if change in {"gemfile-link", "gemspec-link", "candidate-lock-link"}:
        name = {"gemfile-link": "Gemfile", "gemspec-link": fixture.plan["gemspec"],
                "candidate-lock-link": "Gemfile.lock"}[change]
        (fixture.source / name).unlink(missing_ok=True)
        (fixture.source / name).symlink_to(sentinel)
    elif change == "candidate-lock-directory":
        (fixture.source / "Gemfile.lock").mkdir()
    elif change == "state-link":
        shutil.rmtree(fixture.state)
        fixture.state.symlink_to(tmp_path, target_is_directory=True)
    elif change == "view-link":
        shutil.rmtree(fixture.state / "source")
        (fixture.state / "source").symlink_to(fixture.source, target_is_directory=True)
    elif change == "owner-link":
        (fixture.state / "owner.json").unlink()
        (fixture.state / "owner.json").symlink_to(sentinel)
    elif change == "unowned-state":
        (fixture.state / "owner.json").unlink()
    elif change == "payload-link":
        (fixture.bundle_path / "source-link").symlink_to(fixture.source, target_is_directory=True)
    elif change == "default-lock-hash":
        path = fixture.payload / "ruby-resolution/Gemfile.lock"
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "different-bundler":
        (fixture.source / "Gemfile.lock").write_bytes(
            (fixture.payload / "ruby-resolution/Gemfile.lock").read_bytes().replace(b"2.4.19", b"2.5.0"))
    elif change == "changed-resolver":
        record["resolver"]["ruby_sha256"] = "b" * 64
    else:
        (fixture.bundle_path / "unprepared-file").write_text("new bytes")
    command = ruby.replay_command(record, source=fixture.source, state=fixture.state,
                                  bundle_path=fixture.bundle_path, assets=fixture.payload)
    result = shell(command + " && printf 'unexpected continuation'")
    assert result.returncode != 0 and "unexpected continuation" not in result.stdout
    assert sentinel.read_text() == "unchanged external content"
    assert (fixture.source / "test/fixed.rb").is_file()


def test_candidate_gemspec_dependency_changes_fail_frozen_instead_of_resolving(tmp_path, gem_repository):
    fixture = prepared_fixture(tmp_path, gem_repository)
    gemspec = fixture.source / fixture.plan["gemspec"]
    gemspec.write_text(gemspec.read_text().replace(
        's.require_paths = ["lib"]', 's.require_paths = ["lib"]\n  s.add_dependency "never_prepared_fixture", "= 1.0"'))
    current = gemspec.read_bytes()
    config = fixture.state / "bundle-config/config"
    config.parent.mkdir(exist_ok=True)
    config.write_text('---\nBUNDLE_FROZEN: "false"\nBUNDLE_PATH: "' + str(fixture.source / "lib") + '"\n')
    before = ruby.tree_fingerprint(fixture.bundle_path)
    result = shell(ruby.replay_command(fixture.record, source=fixture.source, state=fixture.state,
                                      bundle_path=fixture.bundle_path, assets=fixture.payload))
    assert result.returncode != 0
    assert "frozen" in result.stdout.lower()
    assert gemspec.read_bytes() == current
    assert not (fixture.source / "Gemfile.lock").exists()
    assert ruby.tree_fingerprint(fixture.bundle_path) == before


def test_conflicting_current_gemfile_requirements_are_not_rewritten_to_original_additions(tmp_path, gem_repository):
    fixture = prepared_fixture(tmp_path, gem_repository, task_id=ruby.PROMETHEUS)
    manifest = fixture.source / "Gemfile"
    manifest.write_text(manifest.read_text() + '\ngem "redis", "= 99.0"\n')
    current = manifest.read_bytes()
    result = shell(ruby.replay_command(fixture.record, source=fixture.source, state=fixture.state,
                                      bundle_path=fixture.bundle_path, assets=fixture.payload))
    assert result.returncode != 0
    assert manifest.read_bytes() == current
    assert not (fixture.source / "Gemfile.lock").exists()


def test_payload_rejects_a_frozen_root_gem_source_snapshot(tmp_path, gem_repository):
    fixture = prepared_fixture(tmp_path, gem_repository)
    specifications = fixture.bundle_path / "ruby/3.2.0/specifications"
    specifications.mkdir(parents=True, exist_ok=True)
    (specifications / (fixture.gem_name + "-0.1.0.gemspec")).write_text("stale installed root gem")
    with pytest.raises(ContractError, match="current-candidate source"):
        ruby.validate_prepared(fixture.payload, fixture.record, plan=fixture.plan, start=fixture.start, image=IMAGE)


def test_runtime_environment_only_controls_isolated_bundler_setup_not_fixed_tests(tmp_path):
    fixture = source_fixture(tmp_path)
    env = ruby.runtime_environment(fixture.plan, source=fixture.source, state=fixture.state,
                                   bundle_path=tmp_path / "lease-gems")
    assert env["BUNDLE_GEMFILE"] == str(fixture.state / "source/Gemfile")
    assert env["BUNDLE_FROZEN"] == "true"
    assert env["BUNDLER_VERSION"] == env["BUNDLE_VERSION"] == PIN
    assert all(not key.startswith("ORACLE_") for key in env)
    assert "RUBYOPT" not in env and "HOME" not in env
    with pytest.raises(ContractError, match="candidate-private"):
        ruby.runtime_environment(fixture.plan, source=fixture.source, state=fixture.source / "lib",
                                 bundle_path=tmp_path / "lease-gems")
    with pytest.raises(ContractError, match="production source"):
        ruby.runtime_environment(fixture.plan, source=fixture.source, state=fixture.state,
                                 bundle_path=fixture.source / "lib")


def test_generated_command_contains_only_data_and_pinned_launcher_not_original_shell(tmp_path):
    fixture = source_fixture(tmp_path, task_id=ruby.PROMETHEUS)
    request = ruby.prepare_or_reuse(fixture.plan, fixture.start, IMAGE, resolve_unlocked=True)
    command = ruby.resolution_command(request, source=fixture.source, state=fixture.state,
                                      bundle_path=tmp_path / "payload/gems")
    words = shlex.split(command)
    assert words[:2] == ["ruby", "-e"] and words[2] == ruby.RUBY_ENTRY
    spec = json.loads(words[3])
    assert spec["request"] == request
    assert spec["plan"]["additions"] == ruby.ADDITIONS
    assert 'system(RbConfig.ruby, "-e", launcher, "--", *args)' in words[2]
    bad = copy.deepcopy(request)
    bad["plan"]["additions"].append(["another_gem", ">= 0"])
    with pytest.raises(ContractError):
        ruby.resolution_command(bad, source=fixture.source, state=fixture.state,
                                bundle_path=tmp_path / "payload/gems")


def test_private_bundler_bootstrap_is_hash_bound_and_mandatory_on_replay(tmp_path, gem_repository):
    fixture = prepared_fixture(tmp_path, gem_repository, bootstrap=True)
    bootstrap = fixture.payload / "bootstrap"
    assert fixture.record["bootstrap_sha256"] == ruby.tree_fingerprint(bootstrap)
    with pytest.raises(ContractError, match="recorded private"):
        ruby.replay_command(fixture.record, source=fixture.source, state=fixture.state,
                            bundle_path=fixture.bundle_path, assets=fixture.payload)
    command = ruby.replay_command(fixture.record, source=fixture.source, state=fixture.state,
                                  bundle_path=fixture.bundle_path, assets=fixture.payload, bootstrap_path=bootstrap)
    result = shell(command)
    assert result.returncode == 0, result.stdout
    env = ruby.runtime_environment(fixture.plan, source=fixture.source, state=fixture.state,
                                   bundle_path=fixture.bundle_path, bootstrap_path=bootstrap)
    assert env["GEM_HOME"] == env["GEM_PATH"] == str(bootstrap)
    assert env["PATH"].startswith(str(bootstrap / "bin") + ":")
    (bootstrap / "changed-bootstrap.rb").write_text("changed resolver payload")
    with pytest.raises(ContractError, match="bootstrap"):
        ruby.validate_prepared(fixture.payload, fixture.record, plan=fixture.plan, start=fixture.start, image=IMAGE)
    rejected = shell(command)
    assert rejected.returncode == 2 and "bootstrap bytes changed" in rejected.stdout
