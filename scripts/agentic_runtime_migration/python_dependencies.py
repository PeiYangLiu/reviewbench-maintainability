"""Enumerate external Python dependencies without shipping VCS metadata."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from reviewbench_maintainability.agentic.contracts import ContractError, repo_path
from reviewbench_maintainability.agentic.snapshots import validate_link


def dependency_tree_files(root: Path) -> dict[str, Path]:
    """Prune exactly `.git` files, directories and symlinks before validation.

    An installed dependency can contain a submodule's `.git` *file*, e.g.
    archspec/json/.git. The candidate snapshot validator correctly rejects that
    path, but its directory-only pruning happens too early for a caller-side
    filter. Keep this exception at the dependency boundary: never weaken the
    engine's candidate rules, never delete metadata from the retained cache,
    and retain .github/.gitignore, JSON, source and ordinary test fixtures.
    """
    result = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(n for n in directories if n != ".git")
        entries = files + [n for n in directories if (Path(directory) / n).is_symlink()]
        for name in sorted(entries):
            if name == ".git":
                continue
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            repo_path(relative)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                validate_link(relative, os.readlink(path))
            elif not stat.S_ISREG(mode):
                raise ContractError("non-regular Python dependency object: " + relative)
            result[relative] = path
    return result
