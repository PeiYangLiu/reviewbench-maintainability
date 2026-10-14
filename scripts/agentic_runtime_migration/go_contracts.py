"""Read fixed Go contracts without running a checkout, a shell, or a resolver.

Shell profiles describe *retained* wrappers, not replacement assertions. Unknown
wrapper bytes need a new audited profile. The automatic route requires original
named run evidence; a release's PASS_TO_PASS count is not a test selector.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
import shlex

from reviewbench_maintainability.agentic.contracts import ContractError, file_hash, repo_path
from reviewbench_maintainability.agentic.snapshots import git


@dataclass
class GoContract:
    kind: str
    spec: dict
    command: list[str]
    cwd: str
    module_cwd: str
    expected: list[str]
    signature: str | None
    public_target: str
    build_flags: list[str] = field(default_factory=list)
    grader_files: list[str] = field(default_factory=list)
    support_files: list[str] = field(default_factory=list)
    wrapper: str | None = None
    wrapper_text: str | None = None
    setup: list[str] = field(default_factory=list)
    module_pins: list[str] = field(default_factory=list)
    compile_injections: list[dict] = field(default_factory=list)
    provenance: list[dict] = field(default_factory=list)
    transport_changes: list[dict] = field(default_factory=list)


# These hashes bind command order, test selectors, assertions, and dependency
# edits to the original artifacts. Files are read from ctx.legacy, never copied
# from a repository's historical production tree.
SHELL_PROFILES = {
    "7e6d527c7cdcfa564fcb88625a0efdf038a5931ac740571887fc1346d0a87c76": {
        "kind": "module-pin-test",
        "arguments": ["{coverage_out}"],
        "target": "cloud.google.com/go/bigtable",
        "pins": ["google.golang.org/genproto@v0.0.0-20220725144611-272f38e5d71b"],
        "setup": [
            "go mod edit -require=google.golang.org/genproto@v0.0.0-20220725144611-272f38e5d71b",
            "go mod download all",
        ],
    },
    "2dc2dd3409740d675ebe2bdb7e64dd7862c7c9daa1d42382f63039c42d0476e8": {
        "kind": "cli-boundary-test",
        "arguments": ["{coverage_out}", "{task_dir}/fake-bin"],
        "target": "github.com/hashicorp/go-azure-sdk/sdk/auth",
        "grader": ["fake-bin/az"],
    },
    "9eb65356465e7ba02100c8563038aadf547f9555cf5de91ac0f6a0ba6f6d653f": {
        "kind": "generated-package-test",
        "arguments": [],
        "target": "github.com/pokt-network/poktroll/pkg/relayer/proxy",
        "support": ["bin/ignite", "bin/mockgen"],
    },
    "a826ed1b644c64edf55183a0681ac1562dcc86555dafa2008cdbb725bbd71b53": {
        "kind": "local-replace-test",
        "arguments": ["{coverage_out}", "{task_dir}/dependency-stubs/enterprise-credentials"],
        "target": "github.com/weaveworks/weave-gitops-enterprise/cmd/gitops/app/bootstrap",
        "support": [
            "dependency-stubs/enterprise-credentials/go.mod",
            "dependency-stubs/enterprise-credentials/pkg/entitlement/entitlement.go",
        ],
        "setup": [
            'go mod edit -replace="github.com/weaveworks/weave-gitops-enterprise-credentials='
            '${REVIEWBENCH_GO_SUPPORT}/dependency-stubs/enterprise-credentials"',
        ],
    },
    "f75d7311579e3ea0377d47c0dcd84cbf1326d3ee7e2be4d7e9058cfbbc3fd139": {
        "kind": "covered-cli",
        "arguments": ["{coverage_out}"],
        "target": "./cmd/agent",
    },
}

COMPILE_BOUNDARIES = {
    # The original Oracle's 73-byte missing-symbol fixture, not a production
    # file taken from a target commit. It is needed by public compile checks too.
    "a662a2d693e7f59fadac5c2f5265d8fce04e585cebd5e7035302996f3faebfa0":
        "op-chain-ops/genesis/session_compat.go",
}


def artifact(directory: Path, name: str, role: str) -> dict:
    """Do not follow artifact symlinks or implicitly include a historical home."""
    name = repo_path(name)
    path = directory / name
    for part in [path, *path.parents]:
        if part == directory:
            break
        if part.is_symlink():
            raise ContractError("go_artifact_symlink: " + name)
    if not path.is_file():
        raise ContractError("go_original_artifact_missing: " + str(path))
    return {"path": str(path), "sha256": file_hash(path), "role": role}


def working_directory(value: str) -> str:
    if value == "{repo}":
        return ""
    if not isinstance(value, str) or not value.startswith("{repo}/"):
        raise ContractError("go_workdir_not_candidate_relative")
    value = value[len("{repo}/"):]
    repo_path(value, allow_root=True)
    if any(c in value for c in "{}$"):
        raise ContractError("go_unresolved_workdir")
    return value


def command_list(value) -> list[str]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(x, str) or not x or "\0" in x for x in value)):
        raise ContractError("go_invalid_original_command")
    return list(value)


def is_go(value: str) -> bool:
    return bool(re.fullmatch(r"go(?:1\.\d+\.\d+)?", Path(value).name))


def test_flags(arguments: list[str]) -> tuple[list[str], list[str]]:
    """Separate package targets without dropping paired or equals-form flags."""
    paired = {
        "-run", "-tags", "-coverpkg", "-coverprofile", "-count", "-timeout",
        "-parallel", "-p", "-cpu", "-shuffle", "-vet", "-mod", "-modfile",
        "-overlay", "-gcflags", "-asmflags", "-ldflags", "-buildmode",
        "-bench", "-benchtime", "-blockprofile", "-mutexprofile", "-cpuprofile",
        "-memprofile", "-trace", "-list", "-outputdir",
    }
    # Public compile checks retain build constraints, not private selectors.
    build = {"-tags", "-race", "-msan", "-asan", "-gcflags", "-asmflags", "-ldflags"}
    flags, targets = [], []
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        key = arg.split("=", 1)[0]
        if arg in {"-args", "--"}:
            break
        if key in paired and "=" not in arg:
            if index + 1 == len(arguments):
                raise ContractError("go_flag_value_missing: " + arg)
            if key in build:
                flags += arguments[index:index + 2]
            index += 2
            continue
        if key in build:
            flags.append(arg)
        elif not arg.startswith("-"):
            targets.append(arg)
        index += 1
    if any(p.endswith(".go") for p in targets):
        raise ContractError("go_file_list_is_not_a_whole_candidate_package")
    return flags, targets or ["."]


def flag_value(arguments: list[str], flag: str) -> str | None:
    for index, arg in enumerate(arguments):
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
        if arg == flag and index + 1 < len(arguments):
            return arguments[index + 1]
    return None


def module_directory(ctx, task: dict, cwd: str) -> str:
    """Resolve the module from the frozen tree, not the cache's dirty worktree."""
    names = set(git(ctx.repo(task), "ls-tree", "-r", "--name-only", task["start_sha"]).decode().splitlines())
    here = PurePosixPath(cwd or ".")
    while True:
        if str(here / "go.mod") in names:
            return "" if str(here) == "." else str(here)
        if str(here) == ".":
            raise ContractError("go_candidate_module_missing: " + cwd)
        here = here.parent


def _signatures(directory: Path, oracle: dict, spec: dict) -> tuple[list[str], str, list[dict]]:
    expected = oracle.get("pass_to_pass_tests") or spec.get("pass_to_pass_tests")
    if (not isinstance(expected, list) or not expected
            or any(not isinstance(x, str) or not x for x in expected)
            or len(expected) != len(set(expected))):
        raise ContractError("go_frozen_test_identities_missing")
    if spec.get("pass_to_pass_tests") and expected != spec["pass_to_pass_tests"]:
        raise ContractError("go_session_test_identities_differ")
    if oracle.get("pass_to_pass") is not None and oracle["pass_to_pass"] != len(expected):
        raise ContractError("go_session_test_count_differs")
    observations, refs = [], []
    for phase in ("start", "golden"):
        runs = oracle.get("runs", {}).get(phase, [])
        if len(runs) != 3 or sorted(row.get("run", 0) for row in runs) != [1, 2, 3]:
            raise ContractError("go_six_original_signature_runs_missing: " + phase)
        for row in runs:
            signature = row.get("signature")
            if not signature or row.get("ok") is not True or row.get("exit_code") != 0:
                raise ContractError("go_original_signature_run_failed: " + phase)
            log = f"{phase}-{row['run']}.log"
            ref = artifact(directory, log, "original-run")
            text = (directory / log).read_text()
            if re.findall(r"^P2P_SIGNATURE=(.+)$", text, re.M) != [signature]:
                raise ContractError("go_original_signature_log_differs: " + log)
            refs.append(ref)
            observations.append(signature)
    if len(set(observations)) != 1:
        raise ContractError("go_original_signature_unstable")
    return expected, observations[0], refs


def _inline_go(command: list[str], cwd: str) -> tuple[list[str], str]:
    if len(command) != 3 or command[:2] not in (["bash", "-lc"], ["bash", "-c"]):
        raise ContractError("go_inline_shell_shape_pending")
    lexer = shlex.shlex(command[2], posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    tokens = list(lexer)
    if len(tokens) >= 5 and tokens[0] == "cd" and tokens[2] == "&&":
        directory = repo_path(tokens[1].removeprefix("./"), allow_root=True)
        cwd = str(PurePosixPath(cwd) / directory).removeprefix("./")
        if cwd == ".":
            cwd = ""
        tokens = tokens[3:]
    if (len(tokens) < 2 or not is_go(tokens[0]) or tokens[1] != "test"
            or any(token and all(c in ";&|<>()" for c in token) for token in tokens)
            or any(c in command[2] for c in ("`", "\n")) or "$(" in command[2]):
        raise ContractError("go_inline_shell_shape_pending")
    return tokens, cwd


def load_session(ctx, task: dict, oracle: dict, directory: Path) -> GoContract:
    spec = json.loads((directory / "session-spec.json").read_text())
    command = command_list(spec.get("command"))
    cwd = working_directory(spec.get("cwd", "{repo}"))
    expected, signature, observations = _signatures(directory, oracle, spec)
    result = GoContract("direct-test", spec, command, cwd, cwd, expected, signature, "")
    result.provenance = [artifact(directory, "session-spec.json", "original-session-spec"), *observations]
    files = spec.get("inject_files") or []
    if not isinstance(files, list):
        raise ContractError("go_invalid_injections")
    destinations = set()
    for item in files:
        destination = repo_path(item["destination"])
        if destination in destinations:
            raise ContractError("go_duplicate_injection: " + destination)
        destinations.add(destination)
        # A retained fixture may add a compile-time boundary (Optimism's
        # genesis_compat.go), but never replace an existing production file.
        reference = artifact(directory, item["source"], "original-injection")
        if not destination.endswith("_test.go"):
            tracked = git(ctx.repo(task), "ls-tree", task["start_sha"], "--", destination).decode()
            if tracked:
                raise ContractError("go_injection_overwrites_candidate_source: " + destination)
            if COMPILE_BOUNDARIES.get(reference["sha256"]) != destination:
                raise ContractError("go_original_compile_boundary_profile_pending: " + destination)
            result.compile_injections.append(dict(item))
            result.support_files.append(repo_path(item["source"]))
        result.grader_files.append(repo_path(item["source"]))
        result.provenance.append(reference)
    test_command = None
    if is_go(command[0]):
        if len(command) < 2 or command[1] != "test":
            raise ContractError("go_direct_command_requires_test")
        if not files:
            raise ContractError("go_session_test_source_missing")
        test_command = command
    elif command[0] == "bash" and len(command) > 1 and command[1] in {"-lc", "-c"}:
        test_command, result.module_cwd = _inline_go(command, cwd)
        result.kind = "inline-module-test"
    elif command[0] == "bash" and len(command) > 1 and command[1].startswith("{task_dir}/"):
        name = repo_path(command[1].removeprefix("{task_dir}/"))
        ref = artifact(directory, name, "original-shell-wrapper")
        profile = SHELL_PROFILES.get(ref["sha256"])
        if profile is None:
            raise ContractError("go_shell_profile_pending: sha256=" + ref["sha256"])
        if command[2:] != profile["arguments"]:
            raise ContractError("go_original_wrapper_arguments_differ")
        result.kind = profile["kind"]
        result.wrapper, result.wrapper_text = name, (directory / name).read_text()
        result.public_target = profile["target"]
        result.setup = list(profile.get("setup", []))
        result.module_pins = list(profile.get("pins", []))
        result.grader_files += [name, *profile.get("grader", [])]
        result.support_files += list(profile.get("support", []))
        result.provenance.append(ref)
        for path in [*profile.get("grader", []), *result.support_files]:
            result.provenance.append(artifact(directory, path, "original-boundary-or-tool"))
    else:
        raise ContractError("go_fixed_command_wrapper_pending")
    if test_command:
        result.build_flags, targets = test_flags(test_command[2:])
        result.public_target = flag_value(test_command, "-coverpkg") or " ".join(targets)
        # A private sessionoracle package is absent in the developer container.
        # Its -coverpkg names the complete production package instead.
        if not flag_value(test_command, "-coverpkg") and any("sessionoracle" in t for t in targets):
            raise ContractError("go_public_compile_target_missing")
    result.module_cwd = module_directory(ctx, task, result.module_cwd)
    return result


def load_contract(ctx, task: dict, oracle: dict, directory: Path | None = None) -> GoContract:
    for key in ("id", "repo", "start_sha"):
        if key in oracle and oracle[key] != task[key]:
            raise ContractError("go_original_oracle_task_binding_differs: " + key)
    directory = directory or ctx.task_directory(task, oracle, "gopy")
    if (directory / "session-spec.json").is_file():
        return load_session(ctx, task, oracle, directory)
    from .go_public_inputs import load_original_runs
    return load_original_runs(ctx, task, oracle, directory)


def transport_wrapper(contract: GoContract) -> tuple[str, list[dict]]:
    """Only runtime-path/cleanup relocation; selectors and assertions stay bytes."""
    text = contract.wrapper_text
    if text is None:
        raise ContractError("go_wrapper_source_missing")
    changes = []
    replacements = []
    if contract.kind == "covered-cli":
        replacements.append((
            "/home/v-peiyangliu/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.24.10.linux-amd64/bin/go",
            "${REVIEWBENCH_GO}",
            "absolute pinned compiler relocated to the same-version compiler asset",
        ))
    if contract.kind == "generated-package-test":
        replacements.append((
            "git checkout -- go.mod go.sum",
            'reviewbench_restore_candidate_modules',
            "undo generator metadata side effects using this candidate, not Git HEAD",
        ))
    for old, new, basis in replacements:
        if text.count(old) != 1:
            raise ContractError("go_wrapper_transport_anchor_differs")
        text = text.replace(old, new)
        changes.append({"old": old, "new": new, "basis": basis})
    return text, changes


def generation_setup(contract: GoContract) -> str:
    """Reuse the original generation prefix, with the same conditional checks."""
    if contract.kind != "generated-package-test":
        return ""
    text, _ = transport_wrapper(contract)
    prefix = text.split("\ntrap ", 1)[0]
    old = 'task_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    if prefix.count(old) != 1 or "go test" in prefix:
        raise ContractError("go_generator_prefix_differs")
    return prefix.replace(old, 'task_dir="${REVIEWBENCH_GO_SUPPORT}"')
