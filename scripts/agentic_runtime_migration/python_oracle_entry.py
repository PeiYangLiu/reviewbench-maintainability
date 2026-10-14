"""Run unchanged Python assertions with whole-candidate package resolution."""
from __future__ import annotations

import importlib.abc
import importlib.util
import builtins
import os
from pathlib import Path
import runpy
import sys
import threading
import types

# Load the frozen tracing/signature helper before candidate imports can shadow
# it. The containing directory is a grader-only mount.
import oracle_support  # noqa: F401


class CandidateNamespaceFinder(importlib.abc.MetaPathFinder):
    """A frozen external-boundary stub may have __path__=[].

    Preserve the stub's existing API but let newly extracted candidate helper
    modules be found below that package. No private test or reference directory
    is ever searched as a candidate production package.
    """

    def find_spec(self, fullname, path=None, target=None):
        root = Path(os.environ["SOURCE_ROOT"]).resolve()
        relative = Path(*fullname.split("."))
        roots = [root, root / "src", *[Path(p) for p in os.environ.get("CANDIDATE_PYTHON_ROOTS", "").split(":") if p]]
        for source_root in roots:
            if not source_root.resolve().is_relative_to(root):
                continue
            for name in (source_root / relative.with_suffix(".py"), source_root / relative / "__init__.py"):
                if name.is_file() and name.resolve().is_relative_to(root):
                    return importlib.util.spec_from_file_location(fullname, name)
        return None


_original_import = builtins.__import__
_import_state = threading.local()


def candidate_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Repair only synthetic package stubs, never replace their API members."""
    if getattr(_import_state, "repairing", False):
        return _original_import(name, globals, locals, fromlist, level)
    package = (globals or {}).get("__package__")
    full = importlib.util.resolve_name("." * level + name, package) if level and package else name
    parts = full.split(".")
    stubs = []
    for length in range(1, len(parts) + 1):
        module = sys.modules.get(".".join(parts[:length]))
        if isinstance(module, types.ModuleType) and module.__dict__.get("__spec__") is None:
            stubs.append((length, module))
    if not stubs:
        # pathlib itself imports ntpath; a tracing Oracle calls pathlib on
        # every traced line. Ordinary imports must not do filesystem scans.
        return _original_import(name, globals, locals, fromlist, level)
    _import_state.repairing = True
    try:
        root = Path(os.environ["SOURCE_ROOT"]).resolve()
        roots = [root, root / "src", *[Path(p) for p in os.environ.get("CANDIDATE_PYTHON_ROOTS", "").split(":") if p]]
        for length, module in stubs:
            candidates = [p.joinpath(*parts[:length]) for p in roots if p.resolve().is_relative_to(root)]
            paths = [str(p) for p in candidates if p.is_dir() and p.resolve().is_relative_to(root)]
            if paths:
                previous = module.__dict__.get("__path__", [])
                if not isinstance(previous, (list, tuple)):
                    previous = []
                module.__path__ = list(dict.fromkeys([*previous, *paths]))
    finally:
        _import_state.repairing = False
    return _original_import(name, globals, locals, fromlist, level)


if __name__ == "__main__":
    # PathFinder gets first refusal; this only repairs artificial empty
    # namespace paths in the unchanged historical boundary fixtures.
    sys.meta_path.append(CandidateNamespaceFinder())
    builtins.__import__ = candidate_import
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    logical_root = os.environ.get("ORACLE_ENTRY_WORKDIR")
    if logical_root:
        directory = Path(logical_root)
        directory.mkdir(parents=True, exist_ok=True)
        # Keep the code bytes and traceback filename pinned/read-only, while a
        # harness's __file__-relative scratch lives in this fresh workspace.
        code = compile(Path(script).read_bytes(), script, "exec")
        exec(code, {"__name__": "__main__", "__file__": str(directory / Path(script).name),
                    "__package__": None, "__builtins__": builtins})
    else:
        runpy.run_path(script, run_name="__main__")
