"""Pinned Node execution of the frozen JavaScript/TypeScript assertion suites."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.resources import file_lock
from reviewbench_maintainability.agentic.snapshots import git, gitlinks

IMAGE = "sha256:8268d755008211c1e35be7b2387dca7498d460008febae21e6af1ce0394094b5"
NODE_CACHE_TARGET = "/opt/reviewbench-cache/node_modules"


def node_worker_environment(workers=2):
    """Cap Vitest's internal pools, independently of the outer task scheduler.

    CPU quota does not restrict os.availableParallelism() on every Node/host
    combination. Both pool types need bounds; the frozen test command and
    selected test identities remain unchanged.
    """
    if type(workers) is not int or workers < 1:
        raise ContractError("Node worker count must be a positive integer")
    return {
        "VITEST_MAX_THREADS": str(workers), "VITEST_MIN_THREADS": "1",
        "VITEST_MAX_FORKS": str(workers), "VITEST_MIN_FORKS": "1",
    }


def success_pattern(signature):
    framework, values = signature.split(":", 1)
    counts = dict(part.split("=") for part in values.split(","))
    n = counts.get("tests", counts.get("passed"))
    if not n or int(n) < 1:
        raise ContractError("frozen JS test count is missing")
    if framework == "node-tap":
        return rf"(?s)(?=.*^#\s+tests\s+{n}\s*$)(?=.*^#\s+pass\s+{n}\s*$)(?=.*^#\s+fail\s+0\s*$).*"
    if framework == "jest":
        return rf"(?s)(?=.*Tests:[^\n]*\b{n}\s+passed)(?!.*Tests:[^\n]*\d+\s+failed).*"
    if framework == "vitest":
        # Retain raw logs, but allow SGR decoration between summary tokens.
        # Require the fixed total as well as the passed count; skipped/failed
        # or additional test cases must not masquerade as the frozen suite.
        space = r"(?:[ \t\r]|\x1b\[[0-9;]*m)"
        tests = rf"^{space}*Tests{space}+{n}{space}+passed{space}*\({n}\){space}*$"
        files = (rf"(?=.*^{space}*Test{space}+Files{space}+{counts['files']}{space}+passed"
                 rf"{space}*\({counts['files']}\){space}*$)") if "files" in counts else ""
        return rf"(?s)(?=.*{tests}){files}.*"
    if framework == "mocha":
        return rf"(?s)(?=.*\b{n}\s+passing(?:\s|\())(?!.*\b[1-9]\d*\s+failing).*"
    if framework == "jasmine":
        return rf"(?s)(?=.*\b{n}\s+specs?,\s+0\s+failures?\b)(?!.*\b[1-9]\d*\s+failures?\b).*"
    raise ContractError("JS framework completion parser needs an adapter: "+framework)


def babel_public_check(anchor):
    return "node -e " + shlex.quote(
        "const fs=require('fs'),parser=require('@babel/parser');"
        f"const file={json.dumps('/workspace/'+anchor)};"
        "const text=fs.readFileSync(file,'utf8');"
        "const plugins=['jsx','decorators-legacy'];"
        "if(/@flow\\b/.test(text))plugins.push('flow');"
        "parser.parse(text,{sourceType:'unambiguous',sourceFilename:file,plugins});"
        "console.log('Public JavaScript/JSX syntax: PASS');")


def typescript_public_check(anchor):
    return "node -e "+shlex.quote(
        "const fs=require('fs'),ts=require('typescript');"
        f"const p={json.dumps('/workspace/'+anchor)};"
        "const fileName=p.endsWith('.js')?p.slice(0,-3)+'.jsx':p;"
        "const r=ts.transpileModule(fs.readFileSync(p,'utf8'),{fileName,reportDiagnostics:true,"
        "compilerOptions:{allowJs:true,target:ts.ScriptTarget.ES2020,module:ts.ModuleKind.CommonJS,"
        "experimentalDecorators:true,jsx:ts.JsxEmit.React}});"
        "const e=(r.diagnostics||[]).filter(x=>x.category===ts.DiagnosticCategory.Error);"
        "if(e.length){console.error(e);process.exit(1)};console.log('Public TypeScript/JSX transpilation: PASS');")


def node_asset(ctx, version):
    root = ctx.legacy.parent/"strict_screen_20260726/oracle_recovery/tsjs/_runtimes"/("node-v"+version+"-linux-x64")
    if not (root/"bin/node").is_file():
        raise ContractError("pinned Node distribution is missing: "+version)
    observed = subprocess.check_output([str(root/"bin/node"), "--version"], text=True, timeout=10).strip()
    if observed != "v"+version:
        raise ContractError("Node version differs from frozen case")
    pointer = ctx.output/"node-assets"/(version+".json")
    with file_lock(ctx.output/"locks"/("node-"+version)):
        if pointer.exists():
            return json.loads(pointer.read_text())
        result = ctx.bundle(root, role="toolchain")
        atomic_json(pointer, result)
        return result


def prepared_dependencies(ctx, task, case):
    configured = ctx.config.get("migration", {}).get("node_dependency_root")
    if not configured:
        return None
    root = Path(configured)
    if not root.is_absolute():
        raise ContractError("prepared Node dependency root must be absolute")
    directory = root/component(task["id"])
    manifest = directory/"preparation.json"
    if not manifest.is_file():
        return None
    record = json.loads(manifest.read_text())
    if (record.get("status") != "prepared" or record.get("id") != task["id"]
            or record.get("case_fingerprint") != canonical_hash(case)
            or record.get("node_version") != case["node"]
            or file_hash(directory/"package-lock.json") != record.get("package_lock_sha256")):
        raise ContractError("prepared Node dependency snapshot differs from frozen case/lock bindings")
    if record.get("kind") not in {"minimal", "npm-ci"}:
        raise ContractError("unsupported prepared Node dependency layout")
    if not (directory/"node_modules").is_dir():
        raise ContractError("prepared Node dependency directory is missing")
    return directory/"node_modules", record


def reference_patch(ctx, task, oracle):
    # Reproduce the existing lane's source_only_checkout: target tree, with
    # every recorded changed test artifact restored to exact-start bytes.
    restored = oracle.get("source_only_restored_tests", [])
    if not isinstance(restored, list):
        raise ContractError("JS frozen restored-test manifest is missing")
    excluded = [repo_path(row["path"]) for row in restored]
    # The executable snapshot's explicit optional gitlinks are empty checkout
    # components, not tracked source files. Project the reference onto the same
    # task layout, rather than attempting a gitlink update in the private Git.
    excluded += [m["path"] for m in gitlinks(ctx.repo(task), task["start_sha"])]
    excluded += [m["path"] for m in gitlinks(ctx.repo(task), task["target_sha"])]
    arguments = ["diff", "--binary", "--full-index", task["start_sha"], task["target_sha"], "--", "."]
    arguments += [":(exclude)"+name for name in excluded]
    patch = git(ctx.repo(task), *arguments)
    if not patch:
        raise ContractError("empty reconstructed source-only JS reference")
    # Two review tasks may share commits but restore different test paths.
    # Never overwrite a reference already bound to another runtime recipe.
    path = ctx.output/"js-reference-patches"/(hashlib.sha256(patch).hexdigest()+".patch")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(patch)
    return {"path": str(path), "sha256": file_hash(path)}


def migrate(ctx, task, oracle):
    docker_inputs = ctx.config.get("migration", {}).get("tsjs_docker_lifecycle_inputs", {}).get(task["id"])
    if docker_inputs is not None:
        from .tsjs_testcontainers_postgres import migrate as migrate_original_docker
        return migrate_original_docker(ctx, task, oracle, docker_inputs)
    legacy_yarn = ctx.config.get("migration", {}).get("legacy_yarn_two_graph_inputs", {}).get(task["id"])
    if legacy_yarn is not None:
        from .tsjs_legacy_yarn import migrate as migrate_legacy_yarn
        return migrate_legacy_yarn(ctx, task, oracle, legacy_yarn)
    legacy = ctx.config.get("migration", {}).get("legacy_js_session_inputs", {}).get(task["id"])
    if legacy is not None:
        from .tsjs_legacy_session import migrate as migrate_legacy_session
        return migrate_legacy_session(ctx, task, oracle, legacy)
    case = json.loads((ctx.legacy/"tsjs/cases.json").read_text())["cases"][task["id"]]
    from .tsjs_renovate55 import migrate as migrate_original_renovate
    original = migrate_original_renovate(ctx, task, oracle, case)
    if original is not None:
        return original
    if case.get("status") != "ready":
        raise ContractError("historical JS case is not executable")
    if case.get("build") not in (None, "SKIP"):
        raise ContractError("JS case requires a curated production build/dependency adapter")
    yarn = ctx.config.get("migration", {}).get("tsjs_yarn1_dependencies", {}).get(task["id"])
    if yarn is not None:
        from .tsjs_yarn1 import migrate as migrate_yarn1
        return migrate_yarn1(ctx, task, oracle, case, yarn)
    pnpm9 = ctx.config.get("migration", {}).get("tsjs_pnpm9_dependencies", {}).get(task["id"])
    if pnpm9 is not None:
        from .tsjs_pnpm9 import migrate as migrate_pnpm9
        return migrate_pnpm9(ctx, task, oracle, case, pnpm9)
    pnpm = ctx.config.get("migration", {}).get("tsjs_pnpm_dependencies", {}).get(task["id"])
    if pnpm is not None:
        from .tsjs_pnpm import migrate as migrate_pnpm
        return migrate_pnpm(ctx, task, oracle, case, pnpm)
    node = node_asset(ctx, case["node"])
    image, audit = ctx.image(IMAGE)
    cwd = repo_path(case.get("workspace", "."), allow_root=True)
    workdir = "/workspace" + ("/"+cwd if cwd != "." else "")
    assets = [{**node, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/node"}]
    env = {"PATH": "/opt/reviewbench-assets/node/bin:/usr/local/bin:/usr/bin:/bin",
           "CI": "true", "HUSKY": "0", "FORCE_COLOR": "0",
           "TMPDIR": "/workspace/.reviewbench/tmp", "COREPACK_ENABLE_PROJECT_SPEC": "0",
           "SOURCE_ROOT": "/workspace", **node_worker_environment(2)}
    old_dependency = ctx.legacy/"tsjs/_session_deps"/str(oracle["index"]).zfill(2)/"node_modules"
    prepared = prepared_dependencies(ctx, task, case)
    dependency = prepared[0] if prepared else old_dependency
    dependency_bootstrap, cache_mounts, scratch_paths = [], {}, []
    if case.get("install") != "SKIP":
        if not dependency.is_dir():
            raise ContractError("JS case needs its pinned external node_modules snapshot")
        deps = ctx.bundle(dependency)
        assets.append({**deps, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/node_modules"})
        env["NODE_PATH"] = "/opt/reviewbench-assets/node_modules"
        if prepared:
            record = prepared[1]
            # ESM ignores NODE_PATH and resolves siblings through directories
            # literally named node_modules. Preserve that layout after mount.
            cache = NODE_CACHE_TARGET
            cache_mounts["node-dependencies"] = cache
            modules = workdir+"/node_modules"
            destination = modules + ("/.oracle-deps/node_modules" if record["kind"] == "minimal" else "")
            dependency_bootstrap += [
                "cp -a /opt/reviewbench-assets/node_modules/. " + cache,
                "mkdir -p " + shlex.quote(str(Path(destination).parent)) + " && ln -s " +
                shlex.quote(cache) + " " + shlex.quote(destination),
            ]
            for link in record.get("workspace_links", []):
                path = cache + "/" + repo_path(link["path"])
                target = "/workspace/" + repo_path(link["workspace_target"])
                dependency_bootstrap.append(
                    "mkdir -p " + shlex.quote(str(Path(path).parent)) + " && ln -s " +
                    shlex.quote(target) + " " + shlex.quote(path))
            scratch_paths.append((cwd + "/" if cwd != "." else "") + "node_modules")
            env["NODE_PATH"] = cache
            env["PATH"] = "/opt/reviewbench-assets/node/bin:" + cache + "/.bin:/usr/local/bin:/usr/bin:/bin"
    expected = oracle.get("pass_to_pass_tests") or [task["id"]+"::frozen-"+oracle["start_signature"]]
    signature = oracle.get("start_signature")
    if not signature or oracle.get("golden_signature", signature) != signature:
        raise ContractError("frozen JS start/golden signature is unstable")
    inject = case.get("session_files", [])
    bootstrap = ["mkdir -p /workspace/.reviewbench/tmp", *dependency_bootstrap]
    grade = []
    if inject:
        harness = ctx.bundle(ctx.legacy/"tsjs", [repo_path(v["source"]) for v in inject], role="grader")
        assets.append({**harness, "kind": "archive", "visibility": "grader", "target": "/opt/reviewbench-assets/js-tests"})
        for item in inject:
            source = "/opt/reviewbench-assets/js-tests/"+repo_path(item["source"])
            destination = "/workspace/"+repo_path(item["destination"])
            grade.append("mkdir -p "+shlex.quote(str(Path(destination).parent))+" && cp "
                         +shlex.quote(source)+" "+shlex.quote(destination))
    raw = case["test"]
    if isinstance(raw, list) and raw[:2] == ["bash", "-lc"]:
        command = raw[2]
    elif isinstance(raw, list):
        command = shlex.join(raw)
    else:
        command = raw
    command = command.replace(str(old_dependency), "/opt/reviewbench-assets/node_modules")
    if "/data/" in command or "/home/" in command:
        raise ContractError("JS test command still contains a host-only runtime path")
    anchor = task["locality"]["path"]
    if anchor.endswith((".js", ".jsx")) and (dependency/"@babel/parser").is_dir():
        public = babel_public_check(anchor)
    elif anchor.endswith((".js", ".jsx", ".ts", ".tsx")) and (dependency/"typescript").is_dir():
        public = typescript_public_check(anchor)
    elif anchor.endswith((".js", ".mjs")):
        public = "node --input-type=module --check < "+shlex.quote("/workspace/"+anchor)
    elif anchor.endswith(".cjs"):
        public = "node --check "+shlex.quote("/workspace/"+anchor)
    else:
        raise ContractError("JS case needs its public TypeScript compiler dependency")
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="tsjs",
        image=image, image_audit=audit, workdir=workdir, cpus=2, memory_mb=2048,
        build_scratch_mb=4096, command_timeout_seconds=case.get("test_timeout") or 600,
        env=env, assets=assets, bootstrap=bootstrap, dev_checks=[public], submodules=ctx.modules(task),
        cache_mounts=cache_mounts, scratch_paths=scratch_paths,
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_apply_ignore_space_change": True,
                 "golden_patch": reference_patch(ctx, task, oracle), "bootstrap": grade,
                 "commands": [{"command": command, "parser": "regex", "expected_tests": expected,
                               "success_pattern": success_pattern(signature)}]},
    )
    recipe.validate()
    return recipe
