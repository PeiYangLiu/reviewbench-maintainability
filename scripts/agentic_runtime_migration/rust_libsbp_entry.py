"""Original repository-library test identity checks, not production selectors."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

if __package__:
    from .rust_wash_compat_entry import masked_rust, top_level_item
    from .rust_build_entry import OverlayError, candidate_files
else:
    from rust_wash_compat_entry import masked_rust, top_level_item
    from build import OverlayError, candidate_files


def verify_blocks(root, bindings):
    """Allow production edits and file moves; keep the original test items.

    Marker locations must be real top-level Rust items, not comments, strings,
    macros, or conditionally nested copies. Runtime Cargo test IDs separately
    prove that these are selected by the actual library test target.
    """
    found = {row["sha256"]: [] for row in bindings}
    for name, path in candidate_files(root).items():
        if not name.endswith(".rs") or path.is_symlink():
            continue
        data = path.read_bytes()
        relevant = [row for row in bindings if row["marker"].encode() in data]
        if not relevant:
            continue
        mask = masked_rust(data)
        for row in relevant:
            marker = row["marker"].encode()
            for match in re.finditer(re.escape(masked_rust(marker)), mask):
                start, end = match.start(), match.start() + row["bytes"]
                if hashlib.sha256(data[start:end]).hexdigest() != row["sha256"]:
                    continue
                if not top_level_item(mask, start) or not top_level_item(mask, end):
                    raise OverlayError("original libsbp test item gained an enclosing/conditional wrapper")
                found[row["sha256"]].append({"path": name, "start": start, "end": end})
    if any(len(matches) != 1 for matches in found.values()):
        raise OverlayError("original libsbp inline test item missing, changed, hidden or duplicated")
    return found


def metadata_warnings(text, root):
    """Recognize only Cargo's exact non-root-profile warning, retaining raw text."""
    lines = text.splitlines()
    remainder = []
    index = 0
    prefix = "warning: profiles for the non root package will be ignored, specify profiles at the workspace root:"
    root = Path(root).resolve()
    while index < len(lines):
        if lines[index] != prefix:
            remainder.append(lines[index])
            index += 1
            continue
        if index + 2 >= len(lines) or not lines[index + 1].startswith("package:   ") or (
                lines[index + 2] != "workspace: " + str(root / "Cargo.toml")):
            raise OverlayError("libsbp metadata warning changed its workspace binding")
        manifest = Path(lines[index + 1][len("package:   "):])
        if (not manifest.is_absolute() or manifest.name != "Cargo.toml"
                or not manifest.resolve().is_relative_to(root) or not manifest.is_file()
                or manifest.is_symlink()
                or any(p.is_symlink() for p in manifest.parents if p.is_relative_to(root))):
            raise OverlayError("libsbp metadata warning points outside the current source")
        index += 3
    return "\n".join(remainder)
