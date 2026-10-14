"""Exact original BSMPT4/LAPIS16 Conan inputs and complete candidate builds."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import tempfile

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash
from reviewbench_maintainability.agentic.snapshots import git
from .common import relocate

TOOLS = "/opt/reviewbench-assets/native-conan"
COMPILER = TOOLS + "/compiler/lapis_coverage_g++"
SEED = "/opt/reviewbench-assets/conan-original-input"
PLUGINS = "/opt/reviewbench-assets/conan-original-plugins"
PLUGIN_HASHES = {
    "extensions/plugins/profile.py": "1d7e475b83e61e2586dfc5e68947743426b8df56fcce92a8e863c9f33bb6b10b",
    "extensions/plugins/compatibility/compatibility.py": "618d3b20c38b1f9283d5757541f010706f72751dfa6caac8f18d4df82382a697",
    "extensions/plugins/compatibility/cppstd_compat.py": "0868185e464d65dbc41d8b75da4b59595290f89b1b834bbce77501921f810dca",
}
HOME = "/workspace/.reviewbench/conan2"
PROFILES = {
    "bsmpt4": {
        "index": 4, "repo": "BSMPT/BSMPT",
        "id": "github__BSMPT__BSMPT__pull_172__comment_2160485808__modularity",
        "start": "c17a651fa461dc1825b0a2822ab95b216db91d2f",
        "plan": "b90c98b977bd0d13496ae849525b28d6b53ddf35a0e656b021be44823f276fa7",
        "declaration": "ec414b1ffc274ba6ce0d4e55b1c3535e2724ccee8ba86242a53a3d73a1c300b5",
        "recipes": 10, "packages": 9,
        "harnesses": {
            "configure_bsmpt_c2hdm.sh": "7151a0041b13dd514161f8b6fc943c28dc6cc5aa51316b997f1fd11a6778a69c",
            "build_bsmpt_c2hdm.sh": "f2eff4678af333124d378d54b6a214af2b01de8961526eea9eb1315c5880cd08",
            "bsmpt_c2hdm_masses.cpp": "29129e136123a552dcb1c141c8ff76077d34b7231b2613a0faeb75647e448167",
        },
    },
    "lapis16": {
        "index": 16, "repo": "GenSpectrum/LAPIS-SILO",
        "id": "github__GenSpectrum__LAPIS-SILO__pull_215__comment_1415315312__readability",
        "start": "b256bcf3ab2b5943a583dfcae57d05bff9dd4788",
        "plan": "2e847620df7f27907435744771d01d474ae4ec5a5ef19cea1efe826ca5e815e1",
        "declaration": "b23852f67e131981eb05fad942b6d7d0a8ce1582c7415f424793f8bf637226e3",
        "recipes": 20, "packages": 17,
        "harnesses": {
            "configure_lapis_preprocessor.sh": "e52613da225d3405043aa7a94a70f0f20fa4d33883b22cb6337ece32a63ea0db",
            "run_lapis_preprocessor.sh": "b8be33719d09479c35b9c8dc393813c672600078254b37cab07e8e4e9dd64502",
            "lapis_coverage_g++": "96f12baf34884f03abde514065df751182efaf08bebbe3002c43265d2b20b802",
        },
    },
}


def complete_plan(plan, legacy):
    """Bind every original field; normalize only the relocatable legacy prefix."""
    def visit(value):
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, str):
            return value.replace(str(legacy), "{legacy}")
        return value
    return visit(plan)


def bound_profile(ctx, task, oracle, plan):
    name = next((name for name, row in PROFILES.items()
                 if (task.get("repo"), oracle.get("input_index")) == (row["repo"], row["index"])), None)
    if name is None:
        return None
    profile = PROFILES[name]
    if (task.get("id") != profile["id"] or task.get("start_sha") != profile["start"]
            or canonical_hash(complete_plan(plan, ctx.legacy)) != profile["plan"]):
        raise ContractError("Conan adapter requires the complete original task/build/test plan")
    for filename, expected in profile["harnesses"].items():
        path = ctx.legacy / "native/harnesses" / filename
        if not path.is_file() or path.is_symlink() or file_hash(path) != expected:
            raise ContractError("original Conan build/test helper changed: " + filename)
    if hashlib.sha256(git(ctx.repo(task), "show", task["start_sha"] + ":conanfile.py")).hexdigest() != profile["declaration"]:
        raise ContractError("original Conan recipe declaration changed")
    return name, profile


def checked_receipt(value, label, limit=16 * 1024**2):
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ContractError("Conan input receipt missing: " + label)
    path = Path(value["path"])
    if (not path.is_absolute() or not path.is_file() or path.is_symlink()
            or path.stat().st_size > limit or file_hash(path) != value["sha256"]):
        raise ContractError("Conan input receipt changed: " + label)
    return json.loads(path.read_text())


def original_inputs(ctx, task, name, profile):
    inputs = ctx.config.get("migration", {}).get("native_conan_inputs", {}).get(name, {})
    original = checked_receipt(inputs.get("input"), name + " input")
    rb = checked_receipt(inputs.get("readback"), name + " independent readback")
    if (original.get("status") != "original_Conan_hot_dependencies_archived"
            or original.get("id") != task["id"] or original.get("conan_version") != "2.0.14"
            or original.get("recipes") != profile["recipes"] or original.get("packages") != profile["packages"]
            or not original.get("original_recipe_and_package_identities_unchanged")):
        raise ContractError("Conan receipt differs from the original dependency identities")
    if (rb.get("status") != "original_Conan_Blob_inputs_independently_verified"
            or rb.get("asset") != original["asset"] or rb.get("id") != task["id"]
            or rb.get("manifest") != original["manifest"]
            or not rb.get("all_original_bytes_full_archive_modes_and_links_verified")
            or not rb.get("frozen_hydration_policy_verified") or not rb.get("gzip_crc_and_eof_verified")):
        raise ContractError("Conan inputs lack complete independent recovery")
    manifest = checked_receipt(original["manifest"], name + " original payload")
    if (len(manifest.get("records", {}).get("recipes", [])) != profile["recipes"]
            or len(manifest["records"].get("packages", [])) != profile["packages"]
            or manifest["files"]["p/cache.sqlite3"]["sha256"] != original["original_database_sha256"]):
        raise ContractError("Conan package identity database differs")
    descriptor = asset_descriptor(original["asset"], ctx.output)
    if (descriptor["source_kind"] != "dependency" or descriptor["files"] > 30000
            or descriptor["archive"]["input_bytes"] > 768 * 1024**2
            or descriptor["files"] != len(manifest["files"]) + len(manifest["links"])):
        raise ContractError("Conan complete export/binary input bounds differ")
    plugins = checked_receipt(inputs.get("plugins"), name + " original configuration plugins")
    if (plugins.get("status") != "original_Conan_plugins_archived_and_independently_verified"
            or task["id"] not in plugins.get("originals", {})
            or plugins.get("conan_version") != "2.0.14"
            or not plugins.get("original_bytes_modes_and_frozen_hydration_verified")
            or not plugins.get("gzip_crc_and_eof_verified")
            or {key: row["sha256"] for key, row in plugins.get("files", {}).items()} != PLUGIN_HASHES):
        raise ContractError("original Conan configuration plugins lack complete original recovery")
    plugin_descriptor = asset_descriptor(plugins["asset"], ctx.output)
    if (plugin_descriptor["source_kind"] != "dependency" or plugin_descriptor["files"] != 3
            or plugin_descriptor["archive"]["input_bytes"] > 64 * 1024):
        raise ContractError("original Conan plugin layer bounds differ")
    return original, inputs, plugins


def public_commands(name, configure, build):
    # Strip only comments; the original option values/commands stay literal.
    body = "\n".join(line for line in configure.splitlines()
                     if line.strip() and not line.lstrip().startswith("#")) + "\n"
    if name == "bsmpt4":
        if "sed -i 's#nlopt/2.9.1#nlopt/2.7.1#' conanfile.py" not in body:
            raise ContractError("original BSMPT bootstrap substitution is absent")
        source_line = "source build-oracle/build/Debug/generators/conanbuild.sh"
        build_line = 'cmake --build build-oracle --target Models -j"${CMAKE_BUILD_PARALLEL_LEVEL:-12}"'
        if source_line not in build or build_line not in build:
            raise ContractError("original BSMPT production graph boundary differs")
        public = "set -e\n" + source_line + "\n" + build_line
    elif name == "lapis16":
        assignment = "oracle_root=$1\n"
        compiler = '"$oracle_root/harnesses/lapis_coverage_g++"'
        if assignment not in body or compiler not in body:
            raise ContractError("original LAPIS compiler wrapper boundary differs")
        body = body.replace(assignment, "", 1).replace(compiler, shlex.quote(COMPILER), 1)
        # Compile the original full repository target, but execute no assertion.
        # This also follows candidate CMake target/source-graph changes.
        public = build
    else:
        raise ContractError("unknown original Conan profile")
    if "/harnesses/" in body or "/harnesses/" in public:
        raise ContractError("private harness appeared in public Conan commands")
    if body.count("conan install .") != 1:
        raise ContractError("original Conan dependency command boundary differs")
    # All original package IDs/revisions and original compatibility plugins are
    # already present. Do not contact a remote before trying those local binaries.
    body = body.replace("conan install .", "conan install . --no-remote", 1)
    return body, public


def compiler_asset(ctx):
    source = ctx.legacy / "native/harnesses/lapis_coverage_g++"
    if (not source.is_file() or source.is_symlink() or not source.stat().st_mode & 0o111
            or file_hash(source) != PROFILES["lapis16"]["harnesses"]["lapis_coverage_g++"]):
        raise ContractError("original LAPIS compiler bytes or executable mode changed")
    # copy_asset is byte-only and intentionally does not preserve file modes.
    # An archive retains the original compiler's executable bit during hydration.
    return {**ctx.bundle(source.parent, [source.name], role="toolchain"),
            "kind": "archive", "visibility": "developer", "target": TOOLS + "/compiler"}


def migrate(ctx, task, oracle):
    if (task.get("repo"), oracle.get("input_index")) not in {
            (row["repo"], row["index"]) for row in PROFILES.values()}:
        return None
    from .native_lane import completed_signature, limit_jobs
    plans = {row["input_index"]: row for row in map(json.loads, (ctx.legacy / "native/plans.jsonl").read_text().splitlines())}
    plan = plans[oracle["input_index"]]
    name, profile = bound_profile(ctx, task, oracle, plan)
    original, inputs, plugins = original_inputs(ctx, task, name, profile)
    image, audit = ctx.image(plan["docker_image"])
    directory = ctx.task_directory(task, oracle, "native")
    lane = ctx.legacy / "native"
    configure_path = lane / "harnesses" / ("configure_bsmpt_c2hdm.sh" if name == "bsmpt4" else "configure_lapis_preprocessor.sh")
    build_text = ((lane / "harnesses/build_bsmpt_c2hdm.sh").read_text()
                  if name == "bsmpt4" else plan["build_command"])
    configure, public = public_commands(name, configure_path.read_text(), build_text)
    spec = {
        "schema_version": "reviewbench-original-conan/v1", "profile": name, "source": SEED,
        "manifest": TOOLS + "/manifest.json", "manifest_sha256": original["manifest"]["sha256"],
        "plugin_source": PLUGINS, "plugin_files": plugins["files"],
        "configure": limit_jobs(configure), "build": limit_jobs(public),
    }
    spec_file = ctx.output / "native-conan" / (component(task["id"]) + "-public.json")
    atomic_json(spec_file, spec)
    assets = [
        {**original["asset"], "kind": "archive", "visibility": "developer", "target": SEED},
        {**plugins["asset"], "kind": "archive", "visibility": "developer", "target": PLUGINS},
        {**ctx.copy_asset(Path(original["manifest"]["path"]), "conan-inputs"),
         "kind": "file", "visibility": "developer", "target": TOOLS + "/manifest.json"},
        {**ctx.copy_asset(Path(__file__).with_name("native_conan_entry.py"), "conan-inputs"),
         "kind": "file", "visibility": "developer", "target": TOOLS + "/entry.py"},
        {**ctx.copy_asset(spec_file, "conan-inputs"), "kind": "file", "visibility": "developer", "target": TOOLS + "/spec.json"},
    ]
    if name == "lapis16":
        assets.append(compiler_asset(ctx))
    runtime_root = "/workspace/.reviewbench/native"
    context = {"root": runtime_root, "artifact": runtime_root + "/artifacts/task",
               "cache": runtime_root + "/cache", "variant": "candidate"}
    normalize = lambda value: limit_jobs(relocate(value.replace(str(lane), runtime_root), context))
    with tempfile.TemporaryDirectory(prefix="conan-original-harness-", dir=ctx.scratch) as temporary:
        curated = Path(temporary)
        for filename in profile["harnesses"]:
            target = curated / "harnesses" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lane / "harnesses" / filename, target)
            if filename.endswith(".sh"):
                target.write_text(normalize(target.read_text()))
        harness = ctx.bundle(curated, role="grader")
    assets.append({**harness, "kind": "archive", "visibility": "grader", "target": "/opt/reviewbench-assets/native"})
    command = shlex.join(["python3", TOOLS + "/entry.py", "--spec", TOOLS + "/spec.json", "--workspace", "/workspace"])
    expected = oracle.get("pass_to_pass_tests") or plan["pass_to_pass_tests"]
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="native",
        image=image, image_audit=audit, cpus=4, memory_mb=8192, build_scratch_mb=8192,
        scratch_paths=["build-oracle"], submodules=ctx.modules(task),
        cache_mounts={"ccache": "/opt/reviewbench-cache/ccache"}, command_timeout_seconds=plan["timeout_seconds"],
        env={"ORACLE_ROOT": runtime_root, "ORACLE_ARTIFACT_DIR": runtime_root + "/artifacts/task", "ORACLE_VARIANT": "candidate",
             "TMPDIR": "/workspace/.reviewbench/tmp", "HOME": "/workspace/.reviewbench/home", "CONAN_HOME": HOME,
             "CMAKE_BUILD_PARALLEL_LEVEL": "4", "CCACHE_DIR": "/opt/reviewbench-cache/ccache",
             "CCACHE_BASEDIR": "/workspace", "CCACHE_COMPRESS": "1", "CCACHE_MAXSIZE": "1G",
             "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "SOURCE_DATE_EPOCH": "0"},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp " + runtime_root + "/artifacts/task",
                   command + " --mode configure"],
        dev_checks=[command + " --mode check"], assets=assets,
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": ctx.golden(task, oracle, "native", directory),
                 "bootstrap": ["ln -s /opt/reviewbench-assets/native/harnesses " + runtime_root + "/harnesses",
                               normalize(plan["build_command"])],
                 "commands": [{"command": normalize(plan["test_command"]), "parser": "regex",
                               "expected_tests": expected, "success_pattern": completed_signature(ctx, oracle, plan)}]},
    )
    recipe.validate()
    atomic_json(ctx.output / "native-conan" / (component(task["id"]) + ".json"), {
        "id": task["id"], "original_plan_sha256": profile["plan"], "original_harnesses": profile["harnesses"],
        "input_receipts": inputs, "original_configure_command": plan["configure_command"],
        "original_build_command": plan["build_command"], "original_test_command": plan["test_command"],
        "public_configure": spec["configure"], "public_build": spec["build"],
        "candidate_recipe_restored_after_original_bootstrap_substitution": name == "bsmpt4",
        "source_changes": [], "test_changes": [], "golden_changes": [],
    })
    return recipe
