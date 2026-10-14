"""Capture external wheels without shipping any installed candidate package.

This module only reads a specifically selected retained site. It never imports
it, follows editable links, installs packages, or rewrites the retained cache.
"""
from __future__ import annotations

from email.parser import Parser
import json
from pathlib import Path
import re

from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from .python_dependencies import dependency_tree_files


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def external_site(venv: Path, *, owned_distributions: tuple[str, ...],
                  owned_packages: tuple[str, ...]) -> tuple[dict, list[str]]:
    """Inventory a retained venv; exclude code, metadata and hooks of the SUT."""
    config = venv / "pyvenv.cfg"
    if not config.is_file() or config.is_symlink():
        raise ContractError("retained Python interpreter declaration is absent")
    text = config.read_text()
    if re.search(r"^include-system-site-packages\s*=\s*true\s*$", text, re.M | re.I):
        raise ContractError("retained Python environment inherits uncaptured dependencies")
    version = re.search(r"^(?:version|version_info)\s*=\s*(\d+\.\d+\.\d+)\s*$", text, re.M)
    sites = sorted((venv / "lib").glob("python*/site-packages"))
    if (not version or len(sites) != 1
            or sites[0].parent.name != "python" + ".".join(version[1].split(".")[:2])):
        raise ContractError("retained Python dependency ABI is missing/ambiguous")
    site = sites[0]
    excluded = set()
    records = []
    seen = set()
    own = {normalized_name(n) for n in owned_distributions}
    for directory in sorted(site.glob("*.dist-info")):
        metadata_path = directory / "METADATA"
        if directory.is_symlink() or metadata_path.is_symlink() or not metadata_path.is_file():
            raise ContractError("Python dependency metadata must be regular files")
        metadata = Parser().parsestr(metadata_path.read_text())
        name, release = metadata["Name"], metadata["Version"]
        if not name or not release or normalized_name(name) in seen:
            raise ContractError("Python dependency distribution identity is absent/duplicated")
        seen.add(normalized_name(name))
        if normalized_name(name) in own:
            excluded.add(directory.name)
            continue
        direct = directory / "direct_url.json"
        if direct.is_file():
            if direct.is_symlink():
                raise ContractError("Python dependency origin metadata is a symlink")
            origin = json.loads(direct.read_text())
            if origin.get("dir_info", {}).get("editable") or origin.get("url", "").startswith("file:"):
                raise ContractError("foreign local/editable Python dependency needs its own source closure: " + name)
        records.append({"name": name, "version": release, "metadata_sha256": file_hash(metadata_path)})
    names = []
    excluded_files = []
    for name, path in sorted(dependency_tree_files(site).items()):
        parts = Path(name).parts
        # A native extension can be an owned top-level module as well.
        top_module = parts[0].split(".", 1)[0]
        if (parts[0] in excluded or top_module in owned_packages
                or parts[0].startswith(("__editable__", "_virtualenv"))
                or parts[0].endswith(".egg-info") or "__pycache__" in parts
                or name.endswith((".pyc", ".pyo", ".pth", ".egg-link"))):
            excluded_files.append(name)
            continue
        if path.is_symlink():
            raise ContractError("retained Python dependency needs explicit link flattening: " + name)
        names.append(name)
    if not names or not records:
        raise ContractError("source-free Python dependency snapshot is empty")
    return {
        "venv": str(venv), "site": str(site), "python_version": version[1],
        "pyvenv_sha256": file_hash(config), "distributions": records,
        "owned_distributions": list(owned_distributions), "owned_packages": list(owned_packages),
        "excluded_distribution_dirs": sorted(excluded),
        "excluded_files": excluded_files, "files": len(names),
        "input_bytes": sum((site / n).stat().st_size for n in names),
        "runtime_verified": False,
    }, names
