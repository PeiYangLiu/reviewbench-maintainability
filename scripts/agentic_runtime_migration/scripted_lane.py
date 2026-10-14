"""Run frozen Ruby/PHP assertions in the complete isolated candidate checkout.

The host-side Docker wrapper is not executed. Its payload and environment are
relocated into the already resource-limited ReviewBench container. Reference
patches, assertions, and coverage helpers remain grader-only assets.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import tarfile
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git, tree_files
from . import composer_candidate
from .common import relocate

TOOLS = "/opt/reviewbench-assets/scripted-tools"
PRIVATE = "/opt/reviewbench-assets/scripted-tests"
RUNTIME = "/workspace/.reviewbench/scripted"
DEPENDENCIES = "/opt/reviewbench-assets/scripted-dependencies"
DEPENDENCY_CACHE = "/opt/reviewbench-cache/scripted-dependencies"

COMPOSER_CHAIN_INSTALL = "composer install --no-interaction --prefer-dist --no-scripts --ignore-platform-reqs"
COMPOSER_VENDOR_ALIAS = {"path": "core-bundle/vendor", "target": "../vendor"}
RESOLVED_COMPOSER_LOCK = "resolved-from-start-manifest"
UNLOCKED_COMPOSER_TASKS = frozenset({
    "github__contao__contao__pull_8838__comment_2459042154__modularity",
    "github__sulu__sulu__pull_6872__comment_2059330528__api_design",
    "github__shopware__shopware__pull_13722__comment_2567973929__modularity",
    "github__shopware__shopware__pull_17596__comment_3452238525__modularity",
    "github__googleapis__google-cloud-php__pull_6708__comment_1397564001__api_design",
    "github__spryker-sdk__integrator__pull_34__comment_957304473__complexity",
})
UNLOCKED_COMPOSER_WORKSPACES = {
    ident: ("Spanner" if "__googleapis__google-cloud-php__" in ident else ".")
    for ident in UNLOCKED_COMPOSER_TASKS
}
# Missing keys inherit the isolated home. Existing keys must not supersede it.
# Composer's newer policy.advisories section replaces the legacy audit settings
# as a whole, so it needs a separate adapter rather than a silent policy change.
COMPOSER_POLICY_RULES = [
    ([], "object"), (["config"], "object"), (["config", "audit"], "object"),
    (["config", "policy"], "object"),
    (["config", "audit", "block-insecure"], "false"),
    (["config", "policy", "advisories"], "absent"),
]


def bundler_environment(config_root):
    return {
        "BUNDLE_PATH": DEPENDENCY_CACHE + "/gems", "BUNDLE_FROZEN": "true",
        "BUNDLE_APP_CONFIG": config_root,
        "GEM_HOME": DEPENDENCY_CACHE + "/bootstrap", "GEM_PATH": DEPENDENCY_CACHE + "/bootstrap",
        "PATH": DEPENDENCY_CACHE + "/bootstrap/bin:/usr/local/bin:/usr/bin:/bin",
    }


def relocate_harness_paths(text):
    """Only translate quoted absolute checkout prefixes, not assertion logic."""
    return re.sub(r"""(['"])/work(?=/|['"])""", r"\1/workspace", text)


def container_payload(template: str, image: str) -> tuple[str, dict[str, str], str]:
    """Extract one explicitly declared container invocation without host access."""
    words = shlex.split(template)
    starts = [i for i, word in enumerate(words[:-1]) if word == "docker" and words[i + 1] == "run"]
    if len(starts) != 1:
        raise ContractError("scripted case needs one explicit container invocation")
    cursor, env, cwd = starts[0] + 2, {}, "/work"
    flags = {"--rm"}
    valued = {"--user", "-u", "-v", "--volume", "-w", "--workdir", "-e", "--env"}
    while cursor < len(words) and words[cursor] != image:
        token = words[cursor]
        if token in flags:
            cursor += 1
            continue
        if token not in valued or cursor + 1 >= len(words):
            raise ContractError("unhandled scripted container flag: " + token)
        value = words[cursor + 1]
        if token in {"-w", "--workdir"}:
            cwd = value
        elif token in {"-e", "--env"}:
            key, separator, entry = value.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_]\w*", key):
                raise ContractError("scripted environment must contain literal key=value entries")
            env[key] = entry
        # Host identity and volume specifications are deliberately not reused.
        cursor += 2
    if cursor >= len(words):
        raise ContractError("declared scripted image is absent from the command")
    command = words[cursor + 1:]
    if command[:2] in (["bash", "-lc"], ["bash", "-c"]):
        if len(command) != 3:
            raise ContractError("scripted command has a host-side suffix")
        command_text = command[2]
    else:
        if not command or any(token in {";", "&&", "||", "|", "&"} for token in command):
            raise ContractError("scripted command has an uncurated host-side suffix")
        command_text = shlex.join(command)
    return command_text, env, cwd


def dependency_plan(config):
    """Only frozen installs, never update/require/add or arbitrary setup code."""
    setup = config.get("setup_host_command", "")
    if setup == "mkdir -p {task_dir}/artifacts":
        return None
    image = config.get("environment", {}).get("docker", "")
    command, env, cwd = container_payload(setup, image)
    if cwd != "/work" and not cwd.startswith("/work/"):
        raise ContractError("dependency install workdir does not map to the candidate")
    workspace = repo_path(cwd.removeprefix("/work").lstrip("/"), allow_root=True)
    chain = {}
    if command == COMPOSER_CHAIN_INSTALL + " && ln -sfn ../vendor core-bundle/vendor":
        chain = {"vendor_alias": dict(COMPOSER_VENDOR_ALIAS)}
    elif command == "composer config audit.block-insecure false && " + COMPOSER_CHAIN_INSTALL:
        chain = {"audit_block_insecure": False}
    if chain:
        if workspace != ".":
            raise ContractError("Composer setup chain requires the candidate root")
        command = COMPOSER_CHAIN_INSTALL
    # Split literal spaces, not shell words: quoting, substitutions, newlines,
    # redirections, and additional commands are not install options.
    words = command.split(" ")
    if words[:2] == ["composer", "install"]:
        allowed = {"--no-interaction", "--prefer-dist", "--no-dev", "--no-scripts",
                   "--no-plugins", "--ignore-platform-reqs", "--ignore-platform-req=php",
                   "--ignore-platform-req=ext-gd", "--ignore-platform-req=ext-vips",
                   "--ignore-platform-req=ext-ffi"}
        if any(word not in allowed for word in words[2:]):
            raise ContractError("Composer setup is not one frozen install")
        return {"kind": "composer", "workspace": workspace, "lock": "composer.lock",
                "manifest": "composer.json", "no_dev": "--no-dev" in words,
                "install_flags": words[2:],
                "platform_flags": [word for word in words if word.startswith("--ignore-platform-req")],
                "root_version": env.get("COMPOSER_ROOT_VERSION"), **chain}
    if re.fullmatch(r"bundle config set path /work/\.bundle && bundle install --jobs \d+", command):
        return {"kind": "bundler", "workspace": workspace, "lock": "Gemfile.lock", "manifest": "Gemfile"}
    raise ContractError("scripted dependency setup needs an explicit locked-install adapter")


def composer_home_config(plan, manifest):
    """Check the unchanged root manifest before relocating the literal policy."""
    if "audit_block_insecure" not in plan:
        return {}
    if plan["audit_block_insecure"] is not False:
        raise ContractError("unsupported Composer audit policy")
    for path, kind in COMPOSER_POLICY_RULES:
        value = manifest
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            if (kind == "absent" or (kind == "object" and not isinstance(value, dict))
                    or (kind == "false" and value is not False)):
                raise ContractError("candidate overrides Composer audit policy: " + ".".join(path))
    return {"config": {"audit": {"block-insecure": False}}}


def composer_policy_command(plan, home):
    """Recheck the *current* root with PHP, available in every PHP tool image."""
    policy = composer_home_config(plan, {})
    if not policy:
        return None
    # The same declarative rules govern preparation and every runtime refresh.
    # Only the isolated home's config.json is written, never composer.json.
    program = r'''
$fail = function ($message) { fwrite(STDERR, $message . "\n"); exit(2); };
if (is_link("composer.json") || !is_file("composer.json")) {
    $fail("Composer policy requires a regular candidate manifest");
}
$manifest = json_decode(file_get_contents("composer.json"));
if (json_last_error() !== JSON_ERROR_NONE) { $fail("invalid candidate Composer manifest"); }
foreach (json_decode($argv[1]) as $rule) {
    $value = $manifest;
    $present = true;
    foreach ($rule[0] as $key) {
        if (!is_object($value) || !property_exists($value, $key)) { $present = false; break; }
        $value = $value->$key;
    }
    if ($present && ($rule[1] === "absent"
        || ($rule[1] === "object" && !is_object($value))
        || ($rule[1] === "false" && $value !== false))) {
        $fail("candidate overrides Composer audit policy: " . implode(".", $rule[0]));
    }
}
$home = $argv[2];
if (getenv("COMPOSER_HOME") !== $home || $home[0] !== "/") {
    $fail("Composer home is not the isolated configuration directory");
}
for ($path = $home; $path !== "/"; $path = dirname($path)) {
    if (is_link($path) || (file_exists($path) && !is_dir($path))) {
        $fail("Composer home has a non-directory or symlink component");
    }
}
if (!is_dir($home) && !mkdir($home, 0700, true)) { $fail("Composer home creation failed"); }
$file = $home . "/config.json";
if (is_link($file) || (file_exists($file) && !is_file($file))) {
    $fail("Composer home configuration is not a regular file");
}
if (file_put_contents($file, $argv[3] . "\n") === false) { $fail("Composer home write failed"); }
'''
    return shlex.join(["php", "-r", program, json.dumps(COMPOSER_POLICY_RULES), home, json.dumps(policy)])


def composer_alias_paths(plan):
    alias = plan.get("vendor_alias")
    if alias is None:
        return []
    if alias != COMPOSER_VENDOR_ALIAS or plan["workspace"] != ".":
        raise ContractError("unsupported Composer vendor alias")
    return [alias["path"]]


def composer_alias_command(plan, workdir, state_dir, *, create):
    """Only create an absent alias, or reuse this scratch owner's exact link."""
    if not composer_alias_paths(plan):
        return None
    if not Path(workdir).is_absolute() or not Path(state_dir).is_absolute():
        raise ContractError("Composer alias needs absolute candidate and scratch directories")
    quote = shlex.quote
    marker = str(Path(state_dir) / "composer-vendor-alias")
    owner = "core-bundle/vendor -> ../vendor"
    state_parents = [str(path) for path in reversed(Path(state_dir).parents)] + [state_dir]
    script = [
        "set -eu",
        "fail() { printf '%s\\n' 'invalid current-candidate Composer vendor alias or scratch owner' >&2; exit 2; }",
        "cd -- " + quote(workdir) + " || fail",
        '[ "$(pwd -P)" = ' + quote(workdir) + " ] || fail",
        "[ -d core-bundle ] && [ ! -L core-bundle ] || fail",
        "[ ! -L vendor ] && { [ ! -e vendor ] || [ -d vendor ]; } || fail",
        "for path in " + shlex.join(state_parents) +
        '; do [ ! -L "$path" ] && { [ ! -e "$path" ] || [ -d "$path" ]; } || fail; done',
        "if [ -e " + quote(marker) + " ] || [ -L " + quote(marker) + " ]; then",
        "  [ -f " + quote(marker) + " ] && [ ! -L " + quote(marker) +
        ' ] && [ "$(cat -- ' + quote(marker) + ')" = ' + quote(owner) + " ] || fail",
        "fi",
        "if [ -e core-bundle/vendor ] || [ -L core-bundle/vendor ]; then",
        '  [ -L core-bundle/vendor ] && [ "$(readlink -- core-bundle/vendor)" = ../vendor ] && [ -f ' +
        quote(marker) + " ] || fail",
        "fi",
    ]
    if create:
        script += [
            "[ -d vendor ] || fail",
            "if [ ! -L core-bundle/vendor ]; then",
            # No -f: even a newly introduced destination must not be replaced.
            "  ln -sT -- ../vendor core-bundle/vendor || fail",
            "  mkdir -p -- " + quote(state_dir) + " || fail",
            "  printf '%s\\n' " + quote(owner) + " > " + quote(marker) + " || fail",
            "fi",
        ]
    return "(\n" + "\n".join(script) + "\n)"


def dependency_start_inputs(repository, task, plan, *, resolve_unlocked=False):
    """Distinguish an absent start lock from any Git/read/type error."""
    prefix = plan["workspace"] + "/" if plan["workspace"] != "." else ""
    inputs = {}
    for field in ("manifest", "lock"):
        name = prefix + plan[field]
        entry = git(repository, "ls-tree", "-z", task["start_sha"], "--", name)
        if not entry:
            if field != "lock":
                raise ContractError("frozen dependency manifest is absent")
            inputs[field] = None
            continue
        header, path = entry.rstrip(b"\0").split(b"\t", 1)
        mode, kind, _ = header.split()
        if mode not in {b"100644", b"100755"} or kind != b"blob" or path.decode() != name:
            raise ContractError("frozen dependency input is not a regular file: " + name)
        inputs[field] = git(repository, "show", task["start_sha"] + ":" + name)
    if inputs["lock"] is None:
        if not resolve_unlocked:
            raise ContractError("frozen dependency lock is absent; explicit --resolve-unlocked is required")
        if (plan["kind"] != "composer"
                or plan["workspace"] != UNLOCKED_COMPOSER_WORKSPACES.get(task["id"])):
            raise ContractError("unlocked resolution is not an allowlisted Composer case/workspace")
    return inputs


def validate_prepared_dependencies(repository, task, config, plan, record, payload, *, image=None):
    """Validate a frozen package; mismatch is never an invitation to resolve."""
    if (record.get("status") != "prepared" or record.get("id") != task["id"]
            or record.get("start_sha") != task["start_sha"]
            or record.get("config_fingerprint") != canonical_hash(config)
            or record.get("plan") != plan
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", record.get("image", ""))
            or (image is not None and record["image"] != image)):
        raise ContractError("prepared scripted dependencies differ from frozen task/config/image")
    resolved = record.get("lock_origin") == RESOLVED_COMPOSER_LOCK
    inputs = dependency_start_inputs(repository, task, plan, resolve_unlocked=resolved)
    if hashlib.sha256(inputs["manifest"]).hexdigest() != record["manifest_sha256"]:
        raise ContractError("scripted dependency manifest differs from the start")
    if inputs["lock"] is not None:
        if resolved or hashlib.sha256(inputs["lock"]).hexdigest() != record["lock_sha256"]:
            raise ContractError("scripted dependency lock differs from the start")
    else:
        lock = payload / "resolution/composer.lock"
        provenance_file = payload / "resolution/provenance.json"
        archive = payload / "resolution.tar"
        for path, digest in ((lock, record["lock_sha256"]),
                             (provenance_file, record["resolution_provenance_sha256"]),
                             (archive, record["resolution_archive_sha256"])):
            if path.is_symlink() or not path.is_file() or file_hash(path) != digest:
                raise ContractError("resolved Composer lock/provenance bytes changed")
        provenance = json.loads(provenance_file.read_text())
        expected = {
            "kind": RESOLVED_COMPOSER_LOCK, "historical_lock": False, "start_lock_present": False,
            "id": task["id"], "repo": task["repo"], "start_sha": task["start_sha"],
            "workspace": plan["workspace"], "manifest_sha256": record["manifest_sha256"],
            "lock_sha256": record["lock_sha256"], "config_fingerprint": canonical_hash(config),
            "plan_sha256": canonical_hash(plan), "image": record["image"], "resolver": record["resolver"],
            "source_git_tree": git(repository, "rev-parse", task["start_sha"] + "^{tree}").decode().strip(),
            "source_tree_sha256": record["source_tree_sha256"],
            "environment_sha256": record["environment_sha256"],
            "command_sha256": hashlib.sha256(record["command"].encode()).hexdigest(),
        }
        if (provenance != expected or not record["resolver"].get("version")
                or not re.fullmatch(r"[0-9a-f]{64}", record["resolver"].get("sha256", ""))):
            raise ContractError("resolved Composer provenance differs from the actual start/resolver inputs")
        with tarfile.open(archive) as stream:
            members = stream.getmembers()
            if ([member.name for member in members] != ["composer.lock", "provenance.json"]
                    or not all(member.isfile() for member in members)
                    or stream.extractfile(members[0]).read() != lock.read_bytes()
                    or stream.extractfile(members[1]).read() != provenance_file.read_bytes()):
                raise ContractError("resolved Composer archive differs from its lock/provenance")
    if dependency_tree_fingerprint(payload) != record["payload_tree_sha256"]:
        raise ContractError("prepared scripted dependency bytes changed")
    if composer_candidate.applies_to(task, plan):
        descriptor = record.get("composer_candidate", {})
        if (descriptor.get("schema") != composer_candidate.SCHEMA
                or descriptor.get("path") != "composer-current"
                or type(descriptor.get("extra_scratch_mb")) is not int
                or descriptor["extra_scratch_mb"] < 1 or record.get("scratch_paths") != []):
            raise ContractError("candidate-preserving Composer payload descriptor is absent or invalid")
        source = payload / descriptor["path"] / composer_candidate.RECORD
        if source.is_symlink() or not source.is_file() or file_hash(source) != descriptor.get("record_sha256"):
            raise ContractError("candidate-preserving Composer metadata changed")
        composer_candidate.validate_candidate_inputs(inputs["manifest"], inputs["lock"], json.loads(source.read_text()))
    elif record.get("composer_candidate"):
        raise ContractError("candidate-preserving Composer payload was assigned to a different task")


def composer_source_ancestor_links(workdir, view, workspace):
    """Keep sibling candidate source visible to a nested Composer workspace."""
    if workspace == ".":
        return []
    parts = Path(repo_path(workspace)).parts
    source_root = Path(workdir).parents[len(parts) - 1]
    if Path(workdir).relative_to(source_root).as_posix() != workspace:
        raise ContractError("Composer workspace differs from its current-candidate path")
    quote = shlex.quote
    commands = []
    for depth, part in enumerate(parts):
        source = quote(str(source_root.joinpath(*parts[:depth])))
        target = quote(str(Path(view).joinpath(*parts[:depth])))
        commands += [
            "mkdir -p -- " + target + " || fail",
            "for path in " + source + "/* " + source + "/.[!.]* " + source + "/..?*; do",
            '  [ -e "$path" ] || [ -L "$path" ] || continue',
            '  name=${path##*/}',
            '  case "$name" in .git|.reviewbench|' + quote(part) + ") continue;; esac",
            '  ln -sT -- "$path" ' + target + '/"$name" || fail',
            "done",
        ]
    commands += [
        "view=" + quote(str(Path(view) / workspace)),
        'mkdir -p -- "$view" || fail',
    ]
    return commands


def composer_locked_install_command(plan, record, workdir, state_dir, *, assets=DEPENDENCIES):
    """Install offline in a complete live-source view, not in production files.

    Only manifest/lock are copied. Every other current candidate entry remains
    live through a link, including newly added helpers. Vendor stays attached to
    the candidate, so the original PHP/Xdebug wrapper still sees its real paths.
    The default lock and any Composer-generated setup files stay under
    .reviewbench; candidate-supplied locks are neither replaced nor excluded from
    production patches.
    """
    digest = record["lock_sha256"]
    if (record.get("lock_origin") != RESOLVED_COMPOSER_LOCK
            or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ContractError("runtime Composer default needs a bound resolved lock")
    quote = shlex.quote
    view = str(Path(state_dir) / "composer-source")
    marker = str(Path(state_dir) / "composer-source-owner")
    lock = str(Path(assets) / "resolution/composer.lock")
    parents = [str(path) for path in reversed(Path(state_dir).parents)] + [state_dir]
    script = [
        "set -eu",
        "fail() { printf '%s\\n' 'invalid pinned Composer install inputs or scratch owner' >&2; exit 2; }",
        "cd -- " + quote(workdir) + " || fail",
        '[ "$(pwd -P)" = ' + quote(workdir) + " ] || fail",
        "[ -f composer.json ] && [ ! -L composer.json ] || fail",
        "[ -d vendor ] && [ ! -L vendor ] || fail",
        "if [ -e composer.lock ] || [ -L composer.lock ]; then",
        "  [ -f composer.lock ] && [ ! -L composer.lock ] || fail",
        "  lock=" + quote(workdir + "/composer.lock"),
        "else",
        "  lock=" + quote(lock),
        '  [ -f "$lock" ] && [ ! -L "$lock" ] || fail',
        '  [ "$(sha256sum -- "$lock" | cut -d " " -f 1)" = ' + quote(digest) + " ] || fail",
        "fi",
        "for path in " + shlex.join(parents) +
        '; do [ ! -L "$path" ] && { [ ! -e "$path" ] || [ -d "$path" ]; } || fail; done',
        "view=" + quote(view),
        "owner=" + quote(marker),
        'if [ -e "$owner" ] || [ -L "$owner" ]; then',
        '  [ -f "$owner" ] && [ ! -L "$owner" ] && [ "$(cat -- "$owner")" = ' + quote(workdir) + " ] || fail",
        "fi",
        'if [ -e "$view" ] || [ -L "$view" ]; then',
        '  [ -d "$view" ] && [ ! -L "$view" ] && [ -f "$owner" ] || fail',
        '  rm -rf -- "$view" || fail',
        "fi",
        'mkdir -p -- "$view" || fail',
        "printf '%s\\n' " + quote(workdir) + ' > "$owner" || fail',
    ]
    script += composer_source_ancestor_links(workdir, view, plan["workspace"])
    script += [
        # Bash globs include all non-private hidden entries as well as ordinary
        # sources. Do not dereference candidate links while constructing the view.
        "for path in ./* ./.[!.]* ./..?*; do",
        '  [ -e "$path" ] || [ -L "$path" ] || continue',
        '  name=${path#./}',
        '  case "$name" in .git|.reviewbench|composer.json|composer.lock|vendor) continue;; esac',
        "  ln -sT -- " + quote(workdir) + '/"$name" "$view/$name" || fail',
        "done",
        'cp -- composer.json "$view/composer.json" || fail',
        'cp -- "$lock" "$view/composer.lock" || fail',
        "ln -sT -- " + quote(workdir + "/vendor") + ' "$view/vendor" || fail',
        'cd -- "$view" || fail',
        'before=$(sha256sum -- composer.json composer.lock) || fail',
        "export COMPOSER=composer.json COMPOSER_DISABLE_NETWORK=1",
    ]
    policy = composer_policy_command(plan, str(Path(state_dir) / "composer-home"))
    if policy:
        script.append(policy + " || exit $?")
    script += [
        '[ "$(composer --no-plugins --no-scripts config lock)" = true ] || fail',
        '[ "$(composer --no-plugins --no-scripts config vendor-dir)" = vendor ] || fail',
        shlex.join(["composer", "install", *plan["install_flags"]]) + " || exit $?",
        '[ "$(sha256sum -- composer.json composer.lock)" = "$before" ] || fail',
    ]
    return "(\n" + "\n".join(script) + "\n)"


def dependency_tree_fingerprint(root):
    return canonical_hash([
        (name, path.readlink().as_posix() if path.is_symlink() else file_hash(path),
         "symlink" if path.is_symlink() else bool(path.stat().st_mode & 0o111))
        for name, path in sorted(tree_files(root).items())
    ])


def prepared_dependencies(ctx, task, config, image):
    plan = dependency_plan(config)
    if plan is None:
        return None
    configured = ctx.config.get("migration", {}).get("scripted_dependency_root")
    if not configured or not Path(configured).is_absolute():
        raise ContractError("scripted case requires a prepared pinned dependency snapshot")
    directory = Path(configured) / component(task["id"])
    manifest = directory / "preparation.json"
    if not manifest.is_file():
        raise ContractError("prepared scripted dependency manifest is missing")
    record = json.loads(manifest.read_text())
    payload = directory / "payload"
    validate_prepared_dependencies(ctx.repo(task), task, config, plan, record, payload, image=image)
    return payload, record


def completion_pattern(ctx, oracle, config):
    """Bind to the actual identical assertion-completion line in all six runs."""
    pattern = config.get("outcome_regex")
    if not pattern:
        raise ContractError("scripted completion expression is absent")
    completed, matched = [], []
    for variant in ("start", "golden"):
        runs = oracle.get("runs", {}).get(variant, [])
        if len(runs) != 3:
            raise ContractError("scripted frozen triple-run evidence is incomplete")
        for run in runs:
            if run.get("exit_code") != 0:
                raise ContractError("scripted frozen run is not successful")
            text = (ctx.legacy / "scripted" / repo_path(run["log"])).read_text(errors="replace")
            match = re.search(pattern, text, re.MULTILINE)
            if not match:
                raise ContractError("scripted completion is missing from its frozen log")
            matched.append(match.group())
            # Some original PHPUnit expressions identify only the "OK (N
            # tests," prefix. Anchor its complete observed summary, not that
            # prefix as an impossible end-of-line assertion.
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            completed.append(text[start:len(text) if end == -1 else end].rstrip("\r"))
    if pattern == r"OK \((\d+) tests?,":
        # This frozen contract fixes the successful test count, not assertion
        # counts: PHPUnit mock expectations legitimately differ for start/golden.
        if len(set(matched)) != 1:
            raise ContractError("scripted successful test count differs between frozen runs")
        complete = re.escape(matched[0]) + r" \d+ assertions?\)"
        if not all(re.fullmatch(complete, line) for line in completed):
            raise ContractError("frozen PHPUnit prefix lacks a complete successful summary")
        return r"^" + complete + r"\s*$"
    if len(set(completed)) != 1:
        raise ContractError("scripted assertion completion differs between frozen runs")
    # A zero exit or a partial assertion count is not a passing test suite.
    return r"^" + re.escape(completed[0]) + r"\s*$"


def migrate(ctx, task, oracle):
    from .ruby_candidate import TARGETS as RUBY_CANDIDATE_TASKS
    if task["id"] in RUBY_CANDIDATE_TASKS:
        from .ruby_candidate_lane import migrate as migrate_ruby_candidate
        return migrate_ruby_candidate(ctx, task, oracle)
    if task["id"] == "github__getkirby__kirby__pull_7745__comment_2596448494__testability":
        from .composer_test_tools_lane import migrate as migrate_kirby
        return migrate_kirby(ctx, task, oracle)
    config = json.loads((ctx.legacy / "scripted/oracle_config.json").read_text())["tasks"][str(oracle["index"])]
    if config.get("environment", {}).get("vue"):
        from .vue_lane import migrate as migrate_vue
        return migrate_vue(ctx, task, oracle)
    image_name = config.get("environment", {}).get("docker", "")
    if not (image_name.startswith("ruby:") or image_name.startswith("rb-scripted-php")):
        raise ContractError("scripted compiled/runtime-specific dependency adapter pending")
    raw, private_env, cwd = container_payload(config["test_host_command"], image_name)
    if cwd != "/work" and not cwd.startswith("/work/"):
        raise ContractError("scripted workdir does not map to the candidate checkout")
    image, audit = ctx.image(image_name)
    directory = ctx.task_directory(task, oracle, "scripted")
    public = ("ruby -c " if image_name.startswith("ruby:") else "php -l ") + shlex.quote(
        "/workspace/" + repo_path(task["locality"]["path"]))
    helper = "ruby_coverage.rb" if image_name.startswith("ruby:") else "php_coverage_prepend.php"
    tools = ctx.bundle(ctx.legacy / "scripted/tools", [helper], role="grader")
    assets = [{**tools, "kind": "archive", "visibility": "grader", "target": TOOLS}]
    bootstrap = ["mkdir -p /workspace/.reviewbench/tmp"]
    development = [public]
    runtime_env = {"TMPDIR": "/workspace/.reviewbench/tmp", "CI": "true"}
    scratch_paths, cache_mounts = [], {}
    dependencies = prepared_dependencies(ctx, task, config, image)
    autoload, candidate_wrapper = None, None
    extra_scratch_mb = 0
    if dependencies:
        payload, record = dependencies
        plan = record["plan"]
        dependency = ctx.bundle(payload)
        assets.append({**dependency, "kind": "archive", "visibility": "developer", "target": DEPENDENCIES})
        workdir = "/workspace" + ("/" + plan["workspace"] if plan["workspace"] != "." else "")
        if plan["kind"] == "composer" and record.get("composer_candidate"):
            descriptor = record["composer_candidate"]
            with tempfile.TemporaryDirectory(prefix="composer-candidate-tool-", dir=ctx.scratch) as temporary:
                entry_root = Path(temporary)
                composer_candidate.write_php_runtime(entry_root / "entry.php")
                entry_asset = ctx.bundle(entry_root)
            entry_directory = "/opt/reviewbench-assets/composer-candidate"
            assets.append({**entry_asset, "kind": "archive", "visibility": "developer",
                           "target": entry_directory})
            candidate_wrapper = (entry_directory + "/entry.php", DEPENDENCIES + "/" + descriptor["path"],
                                 RUNTIME + "/composer-current")
            extra_scratch_mb = descriptor["extra_scratch_mb"]
            autoload = composer_candidate.runtime_command(*candidate_wrapper)
            bootstrap.append(autoload)
            development.append(autoload)
        elif plan["kind"] == "composer":
            aliases = composer_alias_paths(plan)
            if not set(aliases) <= set(record["scratch_paths"]):
                raise ContractError("Composer vendor alias lacks declared scratch ownership")
            alias_check = composer_alias_command(plan, workdir, RUNTIME, create=False)
            if alias_check:
                bootstrap.append(alias_check)
            bootstrap.append(
                "cd " + shlex.quote(workdir) + ' && [ "$(pwd -P)" = ' + shlex.quote(workdir) +
                " ] && [ ! -L vendor ] && { [ ! -e vendor ] || [ -d vendor ]; }")
            # Real directories keep Composer's __DIR__-based paths attached
            # to this candidate, including new classmap/PSR helpers.
            bootstrap.append("mkdir -p " + shlex.quote(workdir + "/vendor") + " && cp -a " +
                             DEPENDENCIES + "/vendor/. " + shlex.quote(workdir + "/vendor"))
            prefix = (plan["workspace"] + "/" if plan["workspace"] != "." else "") + "vendor/"
            for value in record["scratch_paths"]:
                relative = repo_path(value)
                if not relative.startswith(prefix) and relative not in aliases:
                    raise ContractError("scripted dependency scratch escapes its vendor directory")
                scratch_paths.append(relative)
            runtime_env.update(COMPOSER_HOME=RUNTIME + "/composer-home", COMPOSER_CACHE_DIR=RUNTIME + "/composer-cache",
                               COMPOSER_DISABLE_NETWORK="1")
            if plan.get("root_version"):
                runtime_env["COMPOSER_ROOT_VERSION"] = plan["root_version"]
            refresh = [
                composer_policy_command(plan, runtime_env["COMPOSER_HOME"]),
                composer_alias_command(plan, workdir, RUNTIME, create=True),
                shlex.join(["composer", "dump-autoload", "--no-interaction",
                            *[flag for flag in plan["install_flags"]
                              if flag in {"--no-scripts", "--no-plugins", "--no-dev"}],
                            *plan["platform_flags"]]),
            ]
            if record.get("lock_origin") == RESOLVED_COMPOSER_LOCK:
                refresh = [
                    composer_alias_command(plan, workdir, RUNTIME, create=True),
                    composer_locked_install_command(plan, record, workdir, RUNTIME),
                ]
            autoload = "cd " + shlex.quote(workdir) + " && " + " && ".join(part for part in refresh if part)
            bootstrap.append(autoload)
            development.append(autoload)
        else:
            cache_mounts["scripted-dependencies"] = DEPENDENCY_CACHE
            bootstrap.append("cp -a " + DEPENDENCIES + "/. " + DEPENDENCY_CACHE)
            runtime_env.update(bundler_environment(RUNTIME + "/bundle-config"))
            development.append("cd " + shlex.quote(workdir) + " && bundle check")
    grade = ["mkdir -p " + RUNTIME + "/artifacts"]
    names = [repo_path(value) for value in config.get("session_files", [])]
    if names:
        with tempfile.TemporaryDirectory(prefix="scripted-paths-", dir=ctx.scratch) as temporary:
            curated = Path(temporary)
            for name in names:
                original = directory / name
                destination = curated / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(relocate_harness_paths(original.read_bytes().decode("utf-8")).encode("utf-8"))
            harness = ctx.bundle(curated, names, role="grader")
        assets.append({**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE})
        for name in names:
            destination = "/workspace/" + name
            grade.append("mkdir -p " + shlex.quote(str(Path(destination).parent)) +
                         " && cp " + shlex.quote(PRIVATE + "/" + name) + " " + shlex.quote(destination))

    def normalize(value):
        value = relocate(value, {"mode": "candidate", "run": "1",
                                 "task_dir": RUNTIME, "work": "/workspace"})
        # Longest prefixes first; never reinterpret shell braces/expressions.
        value = value.replace("/oracle-tools", TOOLS).replace("/oracle-task", RUNTIME)
        if dependencies and record["plan"]["kind"] == "bundler":
            value = value.replace("/work/.bundle", DEPENDENCY_CACHE + "/gems")
        value = re.sub(r"/work(?=/|$)", "/workspace", value)
        if any(prefix in value for prefix in ("{root}", "{repo}", "/data/", "/home/")):
            raise ContractError("scripted payload still contains host-only dependencies")
        return value

    command = shlex.join(["env", *[key + "=" + normalize(value) for key, value in sorted(private_env.items())],
                         "bash", "-c", normalize(raw)])
    if candidate_wrapper:
        command = composer_candidate.runtime_command(
            *candidate_wrapper, normalize(raw), env={key: normalize(value) for key, value in private_env.items()})
    expected = oracle.get("pass_to_pass_tests") or [
        task["id"] + "::" + value for value in config.get("test_selectors", [])]
    if not expected:
        raise ContractError("scripted fixed test selectors are absent")
    if autoload and not candidate_wrapper:
        # Rebuild mappings against the final candidate and restored tests,
        # rather than reusing the start snapshot's generated class index.
        grade.append(autoload)
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="scripted",
        image=image, image_audit=audit, workdir=cwd.replace("/work", "/workspace", 1),
        cpus=2, memory_mb=2048, build_scratch_mb=2048 + extra_scratch_mb,
        command_timeout_seconds=config.get("timeout", 300),
        submodules=ctx.modules(task), assets=assets, cache_mounts=cache_mounts, scratch_paths=scratch_paths,
        env=runtime_env, bootstrap=bootstrap, dev_checks=development,
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": ctx.golden(task, oracle, "scripted", directory),
                 "bootstrap": grade, "commands": [{
                     "command": command, "parser": "regex", "expected_tests": expected,
                     "success_pattern": completion_pattern(ctx, oracle, config)}]},
    )
    recipe.validate()
    return recipe
