from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe
from .common import relocate
from .python_dependencies import dependency_tree_files

PYTHON_IMAGE = "python@sha256:c7220863385ee39fb6d822da81f4469d0cd33ff893d92ce94105e5c3f4b95fe2"


def migrate(ctx, task, oracle):
    if task["inferred_language"].lower() != "python":
        raise ContractError("Go runtime adapter is pending")
    # The released QDK wrapper compiles protobufs and then executes its frozen
    # Oracle. Preserve that order using only current candidate source and an
    # explicitly prepared dependency-only site, not its old editable install.
    from .python_legacy import QDK, qdk_migrate
    if task["id"] == QDK:
        return qdk_migrate(ctx, task, oracle)
    directory = ctx.task_directory(task, oracle, "gopy")
    spec_file = directory / "session-spec.json"
    if not spec_file.is_file():
        from .python_automatic import migrate as migrate_automatic
        return migrate_automatic(ctx, task, oracle)
    spec = json.loads(spec_file.read_text())
    command = spec["command"]
    if not command or Path(command[0]).name not in {"python3.11", "python3", "python"}:
        raise ContractError("task-specific Python/shell toolchain needs its pinned environment adapter")
    if spec.get("inject_files"):
        raise ContractError("Python Oracle injection needs explicit immutable test destinations")
    site = None
    if "/" in command[0]:
        executable = Path(relocate(command[0], {"task_dir": str(directory), "root": str(ctx.legacy/"gopy")}))
        venv = executable.parent.parent
        candidates = sorted((venv/"lib").glob("python*/site-packages"))
        if len(candidates) != 1:
            raise ContractError("pinned virtualenv site-packages are missing/ambiguous")
        site = candidates[0]
        version = site.parent.name.removeprefix("python")
        config = (venv/"pyvenv.cfg").read_text()
        match = re.search(r"^version\s*=\s*(\S+)", config, re.M)
        if not match:
            raise ContractError("pinned virtualenv interpreter version is missing")
        reference = PYTHON_IMAGE if version == "3.11" else "python:" + match[1] + "-bookworm"
    else:
        reference = PYTHON_IMAGE
    image, audit = ctx.image(reference)
    files = ["oracle_support.py"]
    # Preserve all test-side scripts/fixtures, but never copy historical source
    # trees, predictions, reference patches, virtualenvs or result logs.
    allowed = {".py", ".json", ".csv", ".txt", ".yaml", ".yml", ".toml", ".png", ".sh"}
    excluded = {"task.json", "session-spec.json", "inventory.json", "decision.json",
                "automatic.json", "recovery-result.json", "local-golden-probe.json"}
    for path in sorted(directory.iterdir()):
        if (path.is_file() and not path.is_symlink() and path.suffix in allowed
                and path.name not in excluded and not any(k in path.name for k in ("coverage", "golden", "start-"))):
            files.append(path.relative_to(ctx.legacy / "gopy").as_posix())
    harness = ctx.bundle(ctx.legacy / "gopy", files, role="grader")
    entry = ctx.copy_asset(Path(__file__).with_name("python_oracle_entry.py"), "grader")
    # This retained site is the external numerical dependency only, not the
    # evaluated repository. Images contain neither task sources nor assertions.
    numpy_site = site or ctx.legacy / "wave-001/correctness/gopy-runtime/python3.11-site"
    if site:
        own = {"BoothGroup/Vayesta": {"vayesta"}, "rapidsai/cudf": {"cudf", "dask_cudf"},
               "spack/spack": {"spack"}, "python-poetry/poetry": {"poetry"}}.get(task["repo"], set())
        names = []
        for name in dependency_tree_files(site):
            parts = Path(name).parts
            if name.endswith((".pth", ".egg-link")) or parts[0].startswith("__editable__"):
                continue
            if parts[0] in own and not (task["repo"] == "python-poetry/poetry" and name.startswith("poetry/core/")):
                continue
            names.append(name)
        dependencies = ctx.bundle(site, names)
    else:
        dependencies = ctx.bundle(numpy_site, list(dependency_tree_files(numpy_site)))
    task_root = "/opt/reviewbench-assets/gopy/" + directory.relative_to(ctx.legacy / "gopy").as_posix()
    context = {"repo": "/workspace", "root": "/opt/reviewbench-assets/gopy", "task_dir": task_root,
               "coverage_out": "/workspace/.reviewbench/coverage.json", "reviewed_path": task["locality"]["path"]}
    pythonpath = ["/workspace", "/workspace/src", "/opt/reviewbench-assets/python-site",
                  "/opt/reviewbench-assets/gopy", relocate(spec.get("pythonpath", ""), context)]
    env = {"SOURCE_ROOT": "/workspace", "PYTHONPATH": ":".join(p for p in pythonpath if p),
           "CANDIDATE_PYTHON_ROOTS": ":".join(p for p in pythonpath if p.startswith("/workspace")),
           "COVERAGE_OUT": "/workspace/.reviewbench/coverage.json", "REVIEWED_PATH": task["locality"]["path"],
           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
           "TMPDIR": "/workspace/.reviewbench/tmp"}
    env_context = {**context, "task_dir": "/workspace/.reviewbench/oracle-work"}
    env.update({k: relocate(v, env_context) for k, v in spec.get("environment", {}).items()})
    if task["repo"] == "jodavis/AdaptiveRemote":
        env["ORACLE_ENTRY_WORKDIR"] = "/workspace/.reviewbench/oracle-work"
    original = [relocate(str(arg), context) for arg in command[1:]]
    execute = shlex.join(["python3", "/opt/reviewbench-assets/python_oracle_entry.py", *original])
    signatures = [row["signature"] for row in oracle.get("runs", {}).get("start", []) if "signature" in row]
    if not signatures or len(set(signatures)) != 1:
        raise ContractError("frozen Python behavior signature is missing or unstable")
    expected = oracle.get("pass_to_pass_tests") or spec.get("pass_to_pass_tests")
    if not expected:
        raise ContractError("frozen Python PASS_TO_PASS identities are absent")
    anchor = task["locality"]["path"]
    compile_command = ("python3 -c " + shlex.quote(
        f"from pathlib import Path; p=Path({anchor!r}); compile(p.read_bytes(),str(p),'exec'); "
        "print('Public production-module compilation: PASS')"))
    assets = [{**harness, "kind": "archive", "target": "/opt/reviewbench-assets/gopy", "visibility": "grader"},
              {**entry, "target": "/opt/reviewbench-assets/python_oracle_entry.py", "visibility": "grader"},
              {**dependencies, "kind": "archive", "target": "/opt/reviewbench-assets/python-site", "visibility": "developer"}]
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="gopy",
        image=image, image_audit=audit, cpus=2, memory_mb=2048,
        command_timeout_seconds=max(120, spec.get("timeout", 120)), build_scratch_mb=2048,
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/oracle-work"], dev_checks=[compile_command],
        env=env, assets=assets, submodules=ctx.modules(task),
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 # This frozen patch was serialized through universal-newline
                 # text IO. Keep its original hash, and apply the documented
                 # historical whitespace mode to reference verification only.
                 "golden_apply_ignore_space_change": task["repo"] == "openfoodfacts/taxonomy-editor",
                 "golden_patch": ctx.golden(task, oracle, "gopy", directory),
                 "commands": [{"command": execute, "parser": "regex", "expected_tests": expected,
                               "success_pattern": "^P2P_SIGNATURE=" + re.escape(signatures[0]) + "$"}]},
    )
    recipe.validate()
    return recipe
