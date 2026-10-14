"""Pure planning/binding helpers for two candidate-owned, lockless Ruby setups.

Parent hooks: extract the original container payload, call setup_plan and
inspect_start, then prepare_or_reuse before executing resolution_command in the
parent's bounded image. Install into payload/gems and pass the successful report
to capture_resolution. Bundle that payload unchanged. At runtime, copy its gems
into a lease-local bundle path, run replay_command before each check, and apply
runtime_environment to the original test wrapper without changing its payload,
selectors, RUBYOPT, or coverage target. No engine/preparer/lane edits live here.

The only generated manifest is in .reviewbench. Current gemspecs, helpers, tests,
and all other production entries stay live; they are never packaged as gems or
restored from preparation. Offline replay never invokes bundle add: even with
--skip-install its injector resolves. Instead it applies the same pinned DSL
dependency/subtraction rules, without calling a resolver, to the current copy.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import tarfile

from reviewbench_maintainability.agentic.contracts import ContractError, repo_path
from reviewbench_maintainability.agentic.snapshots import git


ADAPTER = "ruby-candidate/v1"
ORIGIN = "resolved-from-start-manifest"
BUNDLER_VERSION = "2.4.19"
KNAPSACK = "github__KnapsackPro__knapsack_pro-ruby__pull_237__comment_1480323908__testability"
PROMETHEUS = "github__discourse__prometheus_exporter__pull_250__comment_1007454567__api_design"
INSTALL = "bundle config set path /work/.bundle && bundle install --jobs 8"
ADDITIONS = [["redis", "~> 5.0"], ["redis-client", "~> 0.9.0"], ["connection_pool", "~> 2.4.1"]]
TARGETS = {
    KNAPSACK: ("KnapsackPro/knapsack_pro-ruby", "knapsack_pro.gemspec", INSTALL, []),
    PROMETHEUS: (
        "discourse/prometheus_exporter", "prometheus_exporter.gemspec",
        'bundle add redis --version "~> 5.0" --skip-install && '
        'bundle add redis-client --version "~> 0.9.0" --skip-install && '
        'bundle add connection_pool --version "~> 2.4.1" --skip-install && ' + INSTALL,
        ADDITIONS,
    ),
}


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value):
    return _sha(_json(value).encode())


def setup_plan(task_id, command, *, bundler_version, jobs=2):
    """Accept only the two original literal payloads, not arbitrary setup shell."""
    if task_id not in TARGETS or command != TARGETS[task_id][2]:
        raise ContractError("Ruby setup is not one of the two original literal chains")
    if bundler_version != BUNDLER_VERSION:
        raise ContractError("Ruby candidate adapter requires the explicit Bundler 2.4.19 pin")
    if type(jobs) is not int or not 1 <= jobs <= 8:
        raise ContractError("Ruby install jobs must be bounded between one and eight")
    return {
        "adapter": ADAPTER, "kind": "bundler", "task_id": task_id, "workspace": ".",
        "manifest": "Gemfile", "lock": "Gemfile.lock", "gemspec": TARGETS[task_id][1],
        "original_command": command, "original_jobs": 8, "jobs": jobs,
        "additions": [list(item) for item in TARGETS[task_id][3]],
        "bundler_version": bundler_version,
    }


def _plan(plan):
    expected = setup_plan(plan["task_id"], plan["original_command"],
                          bundler_version=plan["bundler_version"], jobs=plan["jobs"])
    if plan != expected:
        raise ContractError("Ruby candidate plan changed")


def tree_fingerprint(root, *, source=False):
    """Content/mode/link fingerprint; never dereference or package source links."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ContractError("Ruby tree must be a real directory")
    root = root.resolve()
    rows = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        if source:
            dirs[:] = [name for name in dirs if name not in {".git", ".reviewbench"}]
            files = [name for name in files if name not in {".git", ".reviewbench"}]
        for name in sorted(files + [name for name in dirs if (Path(directory) / name).is_symlink()]):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                if os.path.isabs(target) or not path.resolve().is_relative_to(root) or not path.exists():
                    raise ContractError("Ruby tree link escapes or has no local target: " + relative)
                rows.append([relative, "symlink", target, False])
            elif path.is_file():
                rows.append([relative, "file", _sha(path.read_bytes()), bool(path.stat().st_mode & 0o111)])
            else:
                raise ContractError("Ruby tree has a non-regular entry: " + relative)
    return _hash(sorted(rows))


def inspect_start(repository, task, plan):
    """Read exact Git objects, including absence; never consult an old/local lock."""
    _plan(plan)
    if (task["id"] != plan["task_id"] or task["repo"] != TARGETS[task["id"]][0]
            or not re.fullmatch(r"[0-9a-f]{40}", task["start_sha"])):
        raise ContractError("Ruby start identity differs from the literal case")
    entries = []
    for entry in git(repository, "ls-tree", "-r", "-z", task["start_sha"]).split(b"\0"):
        if not entry:
            continue
        header, raw_name = entry.split(b"\t", 1)
        mode, kind, blob = header.decode().split()
        name = repo_path(raw_name.decode())
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise ContractError("Ruby start requires a complete regular-file checkout: " + name)
        entries.append((name, mode, blob))
    # One batch read avoids per-source-file Git subprocesses or a source archive.
    data = git(repository, "cat-file", "--batch", input_bytes=b"".join(
        blob.encode() + b"\n" for _, _, blob in entries))
    cursor, rows, hashes = 0, [], {}
    for name, mode, blob in entries:
        end = data.index(b"\n", cursor)
        observed, kind, size = data[cursor:end].split()
        length = int(size)
        value = data[end + 1:end + 1 + length]
        if observed.decode() != blob or kind != b"blob" or len(value) != length:
            raise ContractError("Ruby Git input batch changed")
        cursor = end + length + 2
        if mode == "120000":
            target = value.decode()
            repo_path(os.path.normpath(os.path.join(os.path.dirname(name), target)), allow_root=True)
            if os.path.isabs(target):
                raise ContractError("Ruby start has an absolute source link")
            rows.append([name, "symlink", target, False])
        else:
            hashes[name] = _sha(value)
            rows.append([name, "file", hashes[name], mode == "100755"])
    for name in ("Gemfile", plan["gemspec"]):
        if name not in hashes:
            raise ContractError("Ruby start needs a regular " + name)
    lock_entry = next((entry for entry in entries if entry[0] == "Gemfile.lock"), None)
    if lock_entry and "Gemfile.lock" not in hashes:
        raise ContractError("Ruby start lock is not a regular file")
    return {
        "id": task["id"], "repo": task["repo"], "start_sha": task["start_sha"],
        "source_git_tree": git(repository, "rev-parse", task["start_sha"] + "^{tree}").decode().strip(),
        "source_sha256": _hash(sorted(rows)),
        "input_sha256": {name: hashes[name] for name in ("Gemfile", plan["gemspec"])},
        "start_lock_present": lock_entry is not None, "start_lock_sha256": hashes.get("Gemfile.lock"),
    }


def _image(image):
    if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ContractError("Ruby resolution requires an immutable actual image ID")


def prepare_or_reuse(plan, start, image, *, resolve_unlocked=False, payload=None, prepared=None):
    """Return a request or the verified record; a prepared mismatch never floats."""
    _plan(plan)
    _image(image)
    if start["id"] != plan["task_id"] or start["repo"] != TARGETS[plan["task_id"]][0]:
        raise ContractError("Ruby resolution start belongs to another case")
    if prepared is not None:
        validate_prepared(payload, prepared, plan=plan, start=start, image=image)
        return {"action": "reuse", "record": prepared}
    if payload is not None and (Path(payload) / "ruby-resolution").exists():
        raise ContractError("published Ruby resolution needs validation, not re-resolution")
    if start["start_lock_present"] or start["start_lock_sha256"] is not None:
        raise ContractError("fresh Ruby resolution requires Gemfile.lock absent at frozen start")
    if resolve_unlocked is not True:
        raise ContractError("Ruby lockless preparation requires explicit --resolve-unlocked")
    return {"action": "resolve", "adapter": ADAPTER, "origin": ORIGIN, "explicit_opt_in": True,
            "plan": plan, "start": start, "image": image}


def _locked(lock):
    text = lock.decode("utf-8")
    versions = re.findall(r"(?m)^BUNDLED WITH\n[ \t]+(\d+\.\d+\.\d+)[ \t]*$", text)
    if versions != [BUNDLER_VERSION]:
        raise ContractError("Ruby lock does not bind the exact Bundler resolver")
    if re.search(r"(?m)^GIT\n", text):
        raise ContractError("Ruby Git dependencies need a separate pinned export adapter")
    for block in re.findall(r"(?ms)^PATH\n(.*?)(?=^\S|\Z)", text):
        if re.findall(r"(?m)^  remote: (.+)$", block) != ["."]:
            raise ContractError("Ruby path dependencies must use the current candidate root")


def runtime_environment(plan, *, source, state, bundle_path, bootstrap_path=None):
    _plan(plan)
    for path in (source, state, bundle_path):
        if not Path(path).is_absolute() or "\0" in str(path):
            raise ContractError("Ruby candidate paths must be literal absolute paths")
    if (not Path(state).is_relative_to(Path(source) / ".reviewbench")
            or ".." in Path(state).parts or ".." in Path(bundle_path).parts):
        raise ContractError("Ruby build view must be under candidate-private .reviewbench")
    if Path(bundle_path).is_relative_to(source) and not Path(bundle_path).is_relative_to(Path(source) / ".reviewbench"):
        raise ContractError("Ruby dependency cache must not overwrite production source")
    environment = {
        "BUNDLE_GEMFILE": str(Path(state) / "source/Gemfile"), "BUNDLE_PATH": str(bundle_path),
        "BUNDLE_APP_CONFIG": str(Path(state) / "bundle-config"),
        "BUNDLE_USER_HOME": str(Path(state) / "bundle-home"),
        "BUNDLE_USER_CACHE": str(Path(state) / "bundle-cache"),
        "BUNDLE_CACHE_PATH": str(Path(state) / "bundle-cache"),
        "BUNDLE_FROZEN": "true", "BUNDLE_IGNORE_CONFIG": "1", "BUNDLE_DISABLE_SHARED_GEMS": "true",
        "BUNDLE_VERSION": BUNDLER_VERSION, "BUNDLER_VERSION": BUNDLER_VERSION,
        "GEM_SPEC_CACHE": str(Path(state) / "rubygems-cache"),
    }
    if bootstrap_path is not None:
        bootstrap = Path(bootstrap_path)
        if (not bootstrap.is_absolute() or ".." in bootstrap.parts
                or (bootstrap.is_relative_to(source) and not bootstrap.is_relative_to(Path(source) / ".reviewbench"))):
            raise ContractError("Ruby bootstrap must be private, not production source")
        environment.update(GEM_HOME=str(bootstrap), GEM_PATH=str(bootstrap),
                           PATH=str(bootstrap / "bin") + ":/usr/local/bin:/usr/bin:/bin")
    return environment


# This is the only runtime implementation, tested with real Ruby/Bundler. It
# needs no Python in the Ruby image and receives data, never a shell payload.
RUBY_ENTRY = r'''
require "json"
require "digest"
require "fileutils"
require "pathname"
require "rbconfig"

def fail!(message)
  warn("Ruby candidate: #{message}")
  exit 2
end
def regular!(path)
  fail!("expected regular file: #{path}") unless File.file?(path) && !File.symlink?(path)
end
def directories!(path)
  Pathname.new(path).ascend do |part|
    fail!("directory link or non-directory: #{part}") if File.symlink?(part) ||
      (File.exist?(part) && !File.directory?(part))
  end
end
def sha(path)
  Digest::SHA256.file(path).hexdigest
end
def fingerprint(root, source = false)
  rows = []
  visit = lambda do |directory|
    Dir.children(directory).sort.each do |name|
      next if source && [".git", ".reviewbench"].include?(name)
      path = File.join(directory, name)
      relative = Pathname.new(path).relative_path_from(Pathname.new(root)).to_s
      stat = File.lstat(path)
      if stat.symlink?
        target = File.readlink(path)
        fail!("absolute or unresolved tree link") if target.start_with?("/") || !File.exist?(path)
        real = File.realpath(path)
        fail!("tree link escapes") unless real == root || real.start_with?(root + "/")
        rows << [relative, "symlink", target, false]
      elsif stat.directory?
        visit.call(path)
      elsif stat.file?
        rows << [relative, "file", sha(path), (stat.mode & 0111) != 0]
      else
        fail!("non-regular tree entry")
      end
    end
  end
  visit.call(root)
  Digest::SHA256.hexdigest(JSON.generate(rows.sort))
end
def lock!(path, version)
  regular!(path)
  text = File.read(path)
  fail!("lock changes the pinned Bundler resolver") unless
    text.scan(/^BUNDLED WITH\n[ \t]+(\d+\.\d+\.\d+)[ \t]*$/).flatten == [version]
  fail!("Git dependency needs a pinned export adapter") if text.match?(/^GIT\n/)
  text.scan(/^PATH\n(.*?)(?=^\S|\z)/m).each do |block|
    fail!("path dependency is not current-candidate root") unless
      block.first.scan(/^  remote: (.+)$/).flatten == ["."]
  end
end

spec = JSON.parse(ARGV.fetch(0))
plan, source, state, bundle_path = spec.values_at("plan", "source", "state", "bundle_path")
bootstrap_path = spec["bootstrap_path"]
phase = spec.fetch("phase")
fail!("unknown phase") unless ["resolve", "offline"].include?(phase)
[source, state, bundle_path].each { |path| directories!(path) }
directories!(bootstrap_path) if bootstrap_path
fail!("candidate root redirected") unless File.realpath(source) == source
fail!("view is not candidate-private") unless state.start_with?(source + "/.reviewbench/")
fail!("bundle path overlaps production") if bundle_path.start_with?(source + "/") &&
  !bundle_path.start_with?(source + "/.reviewbench/")
regular!(File.join(source, "Gemfile"))
regular!(File.join(source, plan.fetch("gemspec")))
before = fingerprint(source, true)
if phase == "resolve"
  request = spec.fetch("request")
  fail!("fresh resolution was not explicitly requested") unless
    request["action"] == "resolve" && request["explicit_opt_in"] == true
  fail!("not the unchanged exact start") unless before == request.fetch("start").fetch("source_sha256")
  fail!("substituted start lock") if File.exist?(File.join(source, "Gemfile.lock")) ||
    File.symlink?(File.join(source, "Gemfile.lock"))
else
  fail!("prepared dependency bytes changed") unless fingerprint(bundle_path) == spec.fetch("dependencies_sha256")
  fail!("prepared Bundler bootstrap bytes changed") if bootstrap_path &&
    fingerprint(bootstrap_path) != spec.fetch("bootstrap_sha256")
end

owner = File.join(state, "owner.json")
identity = {"adapter" => plan.fetch("adapter"), "task_id" => plan.fetch("task_id"), "source" => source}
if File.exist?(state) && !Dir.empty?(state)
  regular!(owner)
  fail!("build state is not owned") unless JSON.parse(File.read(owner)) == identity
end
FileUtils.mkdir_p(state)
File.write(owner, JSON.generate(identity))
view = File.join(state, "source")
fail!("build view is a link") if File.symlink?(view)
fail!("build view is a non-directory") if File.exist?(view) && !File.directory?(view)
FileUtils.rm_rf(view) if File.exist?(view)
FileUtils.mkdir_p(view)
Dir.children(source).sort.each do |name|
  next if [".git", ".reviewbench", "Gemfile", "Gemfile.lock"].include?(name)
  target = Pathname.new(File.join(source, name)).relative_path_from(Pathname.new(view))
  File.symlink(target.to_s, File.join(view, name))
end
gemfile, lockfile = File.join(view, "Gemfile"), File.join(view, "Gemfile.lock")
FileUtils.cp(File.join(source, "Gemfile"), gemfile)
lock_origin = "freshly-resolved"
if phase == "offline"
  candidate_lock = File.join(source, "Gemfile.lock")
  if File.exist?(candidate_lock) || File.symlink?(candidate_lock)
    lock!(candidate_lock, plan.fetch("bundler_version"))
    FileUtils.cp(candidate_lock, lockfile)
    lock_origin = "candidate"
  else
    pinned = spec.fetch("default_lock")
    lock!(pinned.fetch("path"), plan.fetch("bundler_version"))
    fail!("prepared lock bytes changed") unless sha(pinned.fetch("path")) == pinned.fetch("sha256")
    FileUtils.cp(pinned.fetch("path"), lockfile)
    lock_origin = "prepared-default"
  end
end
ENV.keys.grep(/\ABUNDLE_/).each { |key| ENV.delete(key) }
spec.fetch("environment").each { |key, value| ENV[key] = value }
ENV["BUNDLE_FROZEN"] = phase == "offline" ? "true" : "false"
gem "bundler", "= #{plan.fetch("bundler_version")}"
require "bundler"
require "bundler/cli"
require "bundler/injector"
require "bundler/dsl"
fail!("Ruby version is not 3.2") unless RUBY_VERSION.start_with?("3.2.")
fail!("Bundler pin changed") unless Bundler::VERSION == plan.fetch("bundler_version")
resolver = {
  "bundler_version" => Bundler::VERSION, "ruby_version" => RUBY_VERSION, "ruby_platform" => RUBY_PLATFORM,
  "ruby_sha256" => sha(RbConfig.ruby),
  "files_sha256" => %w[bundler.rb bundler/cli.rb bundler/dsl.rb bundler/injector.rb].to_h do |suffix|
    path = $LOADED_FEATURES.find { |entry| entry.end_with?("/" + suffix) }
    fail!("resolver source not loaded: #{suffix}") unless path
    [suffix, sha(path)]
  end
}
if phase == "offline"
  fail!("actual Ruby/Bundler resolver differs from preparation") unless resolver == spec.fetch("resolver")
  # Reproduce only Injector's manifest effect. Never call Injector.inject,
  # Definition.resolve*, bundle add, or bundle lock during offline replay.
  plan.fetch("additions").each do |name, requirement|
    builder = Bundler::Dsl.new
    builder.eval_gemfile(gemfile)
    dep = Bundler::Dependency.new(name, [requirement])
    next if ([dep] - builder.dependencies).empty?
    line = "gem #{name.dump}, #{dep.requirement.as_list.map(&:dump).join(", ")}"
    builder.eval_gemfile("injected gems", line)
    File.open(gemfile, "a") { |file| file.puts; file.puts(line) }
    Bundler.reset!
  end
end
launcher = 'gem "bundler", "= ' + plan.fetch("bundler_version") +
  '"; require "bundler"; require "bundler/cli"; Bundler::CLI.start(ARGV)'
commands = phase == "resolve" ? plan.fetch("additions").map do |name, requirement|
  ["add", name, "--version", requirement, "--skip-install"]
end : []
commands << ["install", *(phase == "offline" ? ["--local"] : []), "--jobs", plan.fetch("jobs").to_s]
old_lock = sha(lockfile) if phase == "offline"
Dir.chdir(view) do
  commands.each do |args|
    ok = system(RbConfig.ruby, "-e", launcher, "--", *args)
    exit($?.exitstatus || 2) unless ok
  end
end
lock!(lockfile, plan.fetch("bundler_version"))
fail!("frozen lock changed") if old_lock && sha(lockfile) != old_lock
after = fingerprint(source, true)
fail!("candidate source changed during setup") unless before == after
report = {
  "phase" => phase, "lock_origin" => lock_origin, "source_before_sha256" => before,
  "source_after_sha256" => after, "build_manifest_sha256" => sha(gemfile),
  "lock_sha256" => sha(lockfile), "resolver" => resolver, "commands" => commands,
  "environment" => spec.fetch("environment").merge("BUNDLE_FROZEN" => ENV.fetch("BUNDLE_FROZEN")),
  "dependencies_sha256" => fingerprint(bundle_path),
  "bootstrap_sha256" => bootstrap_path ? fingerprint(bootstrap_path) : nil
}
report_path = File.join(state, "report.json")
regular!(report_path) if File.exist?(report_path) || File.symlink?(report_path)
File.write(report_path, JSON.generate(report))
puts "RUBY_CANDIDATE #{phase} #{lock_origin}"
'''


def _command(plan, *, phase, source, state, bundle_path, bootstrap_path=None, **binding):
    env = runtime_environment(plan, source=source, state=state, bundle_path=bundle_path, bootstrap_path=bootstrap_path)
    spec = {"plan": plan, "phase": phase, "source": str(source), "state": str(state),
            "bundle_path": str(bundle_path), "bootstrap_path": str(bootstrap_path) if bootstrap_path is not None else None,
            "environment": env, **binding}
    return shlex.join(["ruby", "-e", RUBY_ENTRY, _json(spec)])


def resolution_command(request, *, source, state, bundle_path, bootstrap_path=None):
    plan = request["plan"]
    expected = prepare_or_reuse(plan, request["start"], request["image"], resolve_unlocked=True)
    if request != expected:
        raise ContractError("Ruby resolution needs the explicit unchanged-start request")
    return _command(plan, phase="resolve", source=source, state=state, bundle_path=bundle_path,
                    bootstrap_path=bootstrap_path, request=request)


def _resolver(resolver):
    if (resolver.get("bundler_version") != BUNDLER_VERSION
            or not re.fullmatch(r"3\.2\.\d+", resolver.get("ruby_version", ""))
            or not resolver.get("ruby_platform")
            or not re.fullmatch(r"[0-9a-f]{64}", resolver.get("ruby_sha256", ""))
            or set(resolver.get("files_sha256", {})) != {
                "bundler.rb", "bundler/cli.rb", "bundler/dsl.rb", "bundler/injector.rb"}
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in resolver["files_sha256"].values())):
        raise ContractError("Ruby package lacks the actual pinned resolver evidence")


def _dependencies(payload, plan):
    payload = Path(payload)
    if payload.is_symlink() or set(path.name for path in payload.iterdir()) - {"gems", "bootstrap", "ruby-resolution"}:
        raise ContractError("Ruby payload must contain dependencies, not a candidate source snapshot")
    gem_name = plan["gemspec"].removesuffix(".gemspec")
    for directory in ("gems", "bootstrap"):
        for path in (payload / directory).rglob(gem_name + "-*.gemspec"):
            if path.parent.name == "specifications":
                raise ContractError("Ruby root gem must remain current-candidate source, not an installed snapshot")
    return tree_fingerprint(payload / "gems")


def _bootstrap(payload):
    path = Path(payload) / "bootstrap"
    return tree_fingerprint(path) if path.exists() or path.is_symlink() else None


def capture_resolution(payload, request, *, source, state):
    """Package only lock/provenance plus external dependencies; return metadata."""
    plan = request["plan"]
    if request != prepare_or_reuse(plan, request["start"], request["image"], resolve_unlocked=True):
        raise ContractError("Ruby capture lacks an explicit resolution request")
    payload, source, state = Path(payload), Path(source), Path(state)
    target = payload / "ruby-resolution"
    if target.exists() or target.is_symlink():
        raise ContractError("Ruby resolution is already published; never overwrite or re-resolve")
    report_path, lock_path = state / "report.json", state / "source/Gemfile.lock"
    if any(path.is_symlink() or not path.is_file() for path in (report_path, lock_path)):
        raise ContractError("Ruby resolution report/lock is missing or redirected")
    report = json.loads(report_path.read_text())
    dependencies = _dependencies(payload, plan)
    expected_commands = [["add", name, "--version", requirement, "--skip-install"] for name, requirement in plan["additions"]]
    expected_commands.append(["install", "--jobs", str(plan["jobs"])])
    if (report.get("phase") != "resolve" or report.get("lock_origin") != "freshly-resolved"
            or report.get("source_before_sha256") != request["start"]["source_sha256"]
            or report.get("source_after_sha256") != request["start"]["source_sha256"]
            or tree_fingerprint(source, source=True) != request["start"]["source_sha256"]
            or report.get("commands") != expected_commands
            or report.get("dependencies_sha256") != dependencies
            or report.get("bootstrap_sha256") != _bootstrap(payload)
            or report.get("lock_sha256") != _sha(lock_path.read_bytes())
            or report.get("build_manifest_sha256") != _sha((state / "source/Gemfile").read_bytes())):
        raise ContractError("Ruby resolution does not bind unchanged start inputs and actual install outputs")
    _resolver(report["resolver"])
    _locked(lock_path.read_bytes())
    provenance = {
        "adapter": ADAPTER, "origin": ORIGIN, "historical_lock": False, "explicit_opt_in": True,
        "plan": plan, "start": request["start"], "image": request["image"],
        "adapter_sha256": _sha(RUBY_ENTRY.encode()), "report": report,
    }
    target.mkdir()
    (target / "Gemfile.lock").write_bytes(lock_path.read_bytes())
    (target / "provenance.json").write_text(_json(provenance) + "\n")
    with tarfile.open(target / "resolution.tar", "w") as archive:
        for name in ("Gemfile.lock", "provenance.json"):
            data = (target / name).read_bytes()
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644
            archive.addfile(entry, io.BytesIO(data))
    return {
        "status": "prepared", "adapter": ADAPTER, "origin": ORIGIN, "plan": plan, "start": request["start"],
        "image": request["image"], "resolver": report["resolver"], "dependencies_sha256": dependencies,
        "bootstrap_sha256": report["bootstrap_sha256"],
        "lock_sha256": report["lock_sha256"], "adapter_sha256": provenance["adapter_sha256"],
        "provenance_sha256": _sha((target / "provenance.json").read_bytes()),
        "archive_sha256": _sha((target / "resolution.tar").read_bytes()),
        "payload_sha256": tree_fingerprint(payload),
    }


def validate_prepared(payload, record, *, plan, start, image):
    _plan(plan)
    _image(image)
    if (record.get("status") != "prepared" or record.get("adapter") != ADAPTER
            or record.get("origin") != ORIGIN or record.get("plan") != plan or record.get("start") != start
            or start["start_lock_present"] or start["start_lock_sha256"] is not None
            or record.get("image") != image or record.get("adapter_sha256") != _sha(RUBY_ENTRY.encode())):
        raise ContractError("prepared Ruby inputs differ; never re-resolve the package")
    _resolver(record["resolver"])
    payload = Path(payload)
    if _dependencies(payload, plan) != record["dependencies_sha256"]:
        raise ContractError("prepared Ruby dependency bytes changed")
    if _bootstrap(payload) != record["bootstrap_sha256"]:
        raise ContractError("prepared Bundler bootstrap bytes changed")
    target = payload / "ruby-resolution"
    if target.is_symlink():
        raise ContractError("prepared Ruby resolution is redirected")
    for name, field in (("Gemfile.lock", "lock_sha256"), ("provenance.json", "provenance_sha256"),
                        ("resolution.tar", "archive_sha256")):
        path = target / name
        if path.is_symlink() or not path.is_file() or _sha(path.read_bytes()) != record[field]:
            raise ContractError("prepared Ruby lock/provenance bytes changed")
    _locked((target / "Gemfile.lock").read_bytes())
    provenance = json.loads((target / "provenance.json").read_text())
    if (any(provenance.get(key) != record[key] for key in ("adapter", "origin", "plan", "start", "image", "adapter_sha256"))
            or provenance.get("historical_lock") is not False or provenance.get("explicit_opt_in") is not True
            or provenance["report"]["resolver"] != record["resolver"]
            or provenance["report"]["lock_sha256"] != record["lock_sha256"]
            or provenance["report"]["dependencies_sha256"] != record["dependencies_sha256"]
            or provenance["report"]["bootstrap_sha256"] != record["bootstrap_sha256"]):
        raise ContractError("Ruby resolution provenance is not its recorded new resolution")
    with tarfile.open(target / "resolution.tar") as archive:
        members = archive.getmembers()
        if ([member.name for member in members] != ["Gemfile.lock", "provenance.json"]
                or not all(member.isfile() for member in members)
                or any(archive.extractfile(member).read() != (target / member.name).read_bytes() for member in members)):
            raise ContractError("Ruby resolution archive differs from its lock/provenance")
    if tree_fingerprint(payload) != record["payload_sha256"]:
        raise ContractError("prepared Ruby payload bytes changed")
    return record


def replay_command(record, *, source, state, bundle_path, assets, bootstrap_path=None):
    """No add/update/lock commands, downloads, or historical source replacement."""
    plan = record["plan"]
    _plan(plan)
    _resolver(record["resolver"])
    if (record.get("status") != "prepared" or record.get("origin") != ORIGIN
            or record.get("adapter_sha256") != _sha(RUBY_ENTRY.encode())):
        raise ContractError("Ruby replay requires a prepared pinned package")
    if (record["bootstrap_sha256"] is not None) != (bootstrap_path is not None):
        raise ContractError("Ruby replay must use its recorded private Bundler bootstrap")
    return _command(
        plan, phase="offline", source=source, state=state, bundle_path=bundle_path,
        bootstrap_path=bootstrap_path, bootstrap_sha256=record["bootstrap_sha256"],
        resolver=record["resolver"], dependencies_sha256=record["dependencies_sha256"],
        default_lock={"path": str(Path(assets) / "ruby-resolution/Gemfile.lock"), "sha256": record["lock_sha256"]},
    )
