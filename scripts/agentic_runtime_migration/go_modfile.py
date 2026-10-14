"""Answer-free module metadata transport for retained Go local-replacement setups."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import shlex

from reviewbench_maintainability.agentic.contracts import ContractError

RUNTIME = "/opt/reviewbench-assets/go-runtime"
SHADOW = "/workspace/.reviewbench/go-module-shadow"
PROTECTED = "export REVIEWBENCH_GO_PROTECTED_MODULES=1\n"
BIGTABLE_WRAPPER_SHA256 = "7e6d527c7cdcfa564fcb88625a0efdf038a5931ac740571887fc1346d0a87c76"


def _bigtable_pin_edits(contract):
    """Enable only the retained nested-module wrapper, never arbitrary pins."""
    from .go_contracts import SHELL_PROFILES

    profile = SHELL_PROFILES[BIGTABLE_WRAPPER_SHA256]
    wrapper = getattr(contract, "wrapper_text", None)
    if (not isinstance(wrapper, str)
            or hashlib.sha256(wrapper.encode()).hexdigest() != BIGTABLE_WRAPPER_SHA256
            or getattr(contract, "wrapper", None) != "run_oracle.sh"
            or getattr(contract, "cwd", None) != "bigtable"
            or getattr(contract, "module_cwd", None) != "bigtable"
            or getattr(contract, "command", None) != ["bash", "{task_dir}/run_oracle.sh", "{coverage_out}"]
            or getattr(contract, "public_target", None) != profile["target"]
            or getattr(contract, "module_pins", None) != profile["pins"]
            or getattr(contract, "setup", None) != profile["setup"]
            or getattr(contract, "build_flags", None) != []):
        raise ContractError("module-shadow pinned Bigtable contract differs")
    # The original `go mod download all` remains in setup.sh and run_oracle.sh.
    # Only the destination of the already-attested require edit is shadowed.
    return ["-require=" + pin for pin in profile["pins"]]


def module_edits(contract, support_root):
    """Enable reviewed local replacements and the exact Bigtable module pin."""
    if contract.kind == "module-pin-test":
        return _bigtable_pin_edits(contract)
    if contract.kind != "local-replace-test":
        return []
    result = []
    for command in contract.setup:
        words = shlex.split(command.replace("${REVIEWBENCH_GO_SUPPORT}", support_root))
        if (len(words) != 4 or words[:3] != ["go", "mod", "edit"]
                or not words[3].startswith("-replace=") or "\n" in words[3]):
            raise ContractError("module-shadow setup differs from the reviewed local replacement")
        result.append(words[3])
    if not result:
        raise ContractError("module-shadow local replacement has no retained edit")
    return result


def install_shadow_transport(directory, edits):
    """Add a public compiler shim; preserve original setup and private assertions."""
    directory = Path(directory)
    if not edits or any(not value.startswith(("-replace=", "-require=")) or "\n" in value for value in edits):
        raise ContractError("module-shadow requires explicit dependency edit arguments")
    entry = directory / "go_oracle_entry.sh"
    text = entry.read_text()
    anchor = "set -euo pipefail\n"
    if text.count(anchor) != 1 or PROTECTED in text:
        raise ContractError("module-shadow runtime entry differs or is already wrapped")
    entry.write_text(text.replace(anchor, anchor + PROTECTED, 1))
    shim = directory / "go"
    if shim.exists() or shim.is_symlink():
        raise ContractError("module-shadow compiler path already exists")
    shim.write_bytes(Path(__file__).with_name("go_modfile_entry.sh").read_bytes())
    shim.chmod(0o755)
    (directory / "module-edits.args").write_text("".join(value + "\n" for value in edits))


def shadow_environment(original):
    required = {"REVIEWBENCH_GO", "REVIEWBENCH_GO_MODULE_DIR", "SOURCE_ROOT", "GOFLAGS", "PATH"}
    if not required <= set(original) or original["SOURCE_ROOT"] != "/workspace":
        raise ContractError("module-shadow needs a complete candidate-relative Go environment")
    if any(flag == "-modfile" or flag.startswith("-modfile=") for flag in shlex.split(original["GOFLAGS"])):
        raise ContractError("module-shadow does not override an existing alternate modfile")
    if original["REVIEWBENCH_GO"] != "/opt/reviewbench-assets/go/bin/go":
        raise ContractError("module-shadow compiler differs from the pinned Go asset")
    return {
        **original,
        "REVIEWBENCH_REAL_GO": original["REVIEWBENCH_GO"],
        "REVIEWBENCH_GO": RUNTIME + "/go",
        "REVIEWBENCH_GO_SHADOW_DIR": SHADOW,
        "REVIEWBENCH_GO_SHADOW_EDITS": RUNTIME + "/module-edits.args",
        "PATH": RUNTIME + ":" + original["PATH"],
    }


def repaired_recipe(original, runtime_asset):
    """Replace only the public transport asset/environment/compiler entry path."""
    if original.lane != "gopy" or not original.env.get("REVIEWBENCH_GO"):
        raise ContractError("module-shadow requires a pinned Go recipe")
    matches = [asset for asset in original.assets if asset.get("target") == RUNTIME]
    if len(matches) != 1 or matches[0].get("visibility") != "developer" or matches[0].get("kind") != "archive":
        raise ContractError("module-shadow public runtime asset is missing or ambiguous")
    if set(runtime_asset) != {"path", "sha256"}:
        raise ContractError("module-shadow asset must be a pinned descriptor")
    old_binary = original.env["REVIEWBENCH_GO"]
    checks = []
    for command in original.dev_checks:
        if command.count(old_binary) != 1:
            raise ContractError("module-shadow public command has no unique pinned compiler")
        checks.append(command.replace(old_binary, RUNTIME + "/go", 1))
    assets = [{**asset, **runtime_asset} if asset is matches[0] else dict(asset) for asset in original.assets]
    value = replace(original, assets=assets, env=shadow_environment(original.env), dev_checks=checks)
    value.validate()
    return value
