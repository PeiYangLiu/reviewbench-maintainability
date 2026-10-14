"""Original Anchor2488 async RPC oracle on the complete current candidate workspace."""
import hashlib
import json
from pathlib import Path
import re
import shlex

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, canonical_hash, file_hash,
)
from . import rust_image_entry as tree
from .rust_local_registry import graph_signature

TASK = "github__otter-sec__anchor__pull_2488__comment_1193854703__duplication"
OPTION = "rust_original_anchor_inputs"
SCHEMA = "reviewbench-original-anchor-runtime-inputs/v1"
START = "4f32cb5cae922f087870a28fc1683fafd8594bc3"
TASK_HASH = "83cc95312cc9620c5f7262517ee12c4f7a3a008461f9d0c259c25e97840d19a6"
ORACLE_HASH = "4fd1678165dc90d6eb4a4c0248cbd2bb2cadff31be0ac96e7af759d46db1ff73"
HARNESS = "b8744706bfd1b115231c623b0851efdc7be1a12155c304e2a14964ecebc1d767"
GOLDEN = "5ae99b21fc53a0faac5a939cd259044d4e1c2eb40b7a246963f2d15995e9c3a5"
LOCK = "697ad719700463c2046039dc0625f74b5d56e6fc89663c365d6acd13d6c4cb1c"
TESTS = ["async_account_fetches_and_deserializes_real_rpc_payload",
         "async_account_reports_missing_account"]
ORIGINAL = "cargo +1.70.0 test -p anchor-client --features async --test oracle_nonblocking -- --nocapture"
TEST_ARGV = ["test", "-p", "anchor-client", "--features", "async",
             "--test", "oracle_nonblocking", "--", "--nocapture"]
PUBLIC_ARGV = ["check", "-p", "anchor-client", "--features", "async"]
RUST = "/opt/reviewbench-assets/anchor-rust"
INPUTS = "/opt/reviewbench-assets/anchor-inputs"
GIT_SEED = "/opt/reviewbench-assets/anchor-git-seed"
PRIVATE = "/opt/reviewbench-assets/anchor-oracle"
SOURCES = "/opt/reviewbench-assets/anchor-sources.toml"
CARGO_HOME = "/opt/reviewbench-cache/anchor-cargo"
GIT_DATABASE = "program-8a040a0a4f62c15a"
GIT_EMPTY_DIRECTORIES = ("refs/heads", "refs/tags", "objects/info", "objects/pack")
GITLINKS = {
    "tests/auction-house": "b73b60e55a87bda1d2770c7c10db920db9cdb06b",
    "tests/cfo/deps/openbook-dex": "7d1d41538417aa8721aabea9503bf9d99eab7cc4",
    "tests/cfo/deps/stake": "fd78344ab5f34c36a91bdaf8b9edf2fbd8a93510",
    "tests/cfo/deps/swap": "24c6e5c9845f5f4cbfd51bd239fcbe6eaa2e5337",
    "tests/swap/deps/openbook-dex": "5420646709df6edaaea90162d4f7476328077293",
}


def require(condition, message):
    if not condition:
        raise ContractError("original Anchor: " + message)


def referenced(ref, *, document=True):
    require(isinstance(ref, dict) and isinstance(ref.get("path"), str)
            and re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", ""))), "missing independent input pin")
    path = Path(ref["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and file_hash(path) == ref["sha256"], "input absent, redirected or changed")
    return json.loads(path.read_text()) if document else path.read_bytes()


def requested(ctx, task):
    profiles = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(profiles, dict) and set(profiles) <= {TASK}, "foreign task profile")
    return task.get("id") == TASK and TASK in profiles


def checked_component(ctx, prepared, readback, *, status, role):
    require(prepared.get("status") == status, "component preparation incomplete")
    ref = prepared["asset"]
    descriptor = asset_descriptor(ref, ctx.output)
    require(descriptor["source_kind"] == role
            and readback.get("status") == "independent_Blob_component_readback_verified"
            and readback.get("asset") == ref
            and readback.get("tree_sha256") == descriptor.get("tree_sha256")
            and readback.get("all_paths_bytes_full_modes_verified") is True
            and readback.get("gzip_crc_and_eof_verified") is True,
            "component lacks matching independent Blob restoration")
    return ctx.copy_asset(Path(ref["path"]), "anchor-components")


def original_inputs(ctx, task, oracle):
    require(requested(ctx, task), "explicit original input profile required")
    require(canonical_hash(task) == TASK_HASH and canonical_hash(oracle) == ORACLE_HASH,
            "original task or oracle changed")
    require(task["start_sha"] == START and oracle["workspace_command"] == ORIGINAL
            and oracle["toolchain"] == "1.70.0" and oracle["pass_to_pass_tests"] == TESTS
            and oracle["pass_to_pass"] == 2 and not oracle.get("lane_results_source"),
            "original compiler/command/test identities differ")
    prepared = referenced(ctx.config["migration"][OPTION][TASK])
    require(prepared.get("schema_version") == SCHEMA and prepared.get("task_id") == TASK
            and prepared.get("full_scope") == 402, "input profile schema/scope differs")
    require(referenced(prepared["task"]) == task and referenced(prepared["oracle"]) == oracle,
            "captured release records differ")
    require(prepared["harness"]["sha256"] == HARNESS
            and hashlib.sha256(referenced(prepared["harness"], document=False)).hexdigest() == HARNESS,
            "original historical test bytes differ")
    require(prepared["golden"]["sha256"] == GOLDEN
            and hashlib.sha256(referenced(prepared["golden"], document=False)).hexdigest() == GOLDEN,
            "original three-file source golden changed")
    evidence = referenced(prepared["harness_recovery"])
    require(evidence["harness"]["sha256"] == HARNESS
            and evidence["independent_historical_hash_event"] == 45910
            and evidence["historical_shell_commands_executed"] is False,
            "harness lacks the independent original historical hash")
    sources = referenced(prepared["sources"])
    native, offline = referenced(prepared["native_graphs"]), referenced(prepared["offline_graphs"])
    require(native["status"] == "original_Anchor_native_graphs_resolved"
            and offline["status"] == "both_original_Anchor_offline_graphs_verified"
            and offline["complete_locked_registry_retained"] == 510
            and offline["no_platform_filter"] is True, "original full graph evidence incomplete")
    for variant in ("start", "golden"):
        root = Path(sources[variant]["source"])
        require(tree.snapshot(root) == sources[variant]["snapshot"]
                and file_hash(root / "Cargo.lock") == LOCK, "original parent source or committed lock changed")
        fresh = next(row for row in offline["observations"] if row["variant"] == variant)
        original = next(row for row in native["observations"] if row["variant"] == variant)
        require(fresh["exit_status"] == original["exit_status"] == 0 and fresh["full_edges_features_equal"]
                and fresh["source_unchanged"] and fresh["no_initial_registry_or_checkout"]
                and fresh["packages"] == fresh["nodes"] == 505 and fresh["workspace_members"] == 13,
                "fresh offline graph differs from original Cargo selection")
        require(graph_signature(referenced(fresh["stdout"])) == graph_signature(referenced(original["stdout"])),
                "native and offline graph identities/features differ")
        graph = referenced(fresh["stdout"])
        source_root = Path(native["sources"][variant])
        members = {p["id"]: p for p in graph["packages"] if p["id"] in graph["workspace_members"]}
        require(len(members) == 13 and all(
            not Path(p["manifest_path"]).relative_to(source_root).parts[0] == "tests"
            for p in members.values()), "selected original workspace depends on unhydrated fixture gitlinks")
    return prepared


def cargo_command(*, grading):
    return shlex.join([RUST + "/bin/cargo", "--config", SOURCES, *(TEST_ARGV if grading else PUBLIC_ARGV)])


def git_seed_commands(seed=GIT_SEED, home=CARGO_HOME):
    """Restore original native empty directories lost by file-only asset images."""
    quote = shlex.quote
    destination = Path(home) / "git/db" / GIT_DATABASE
    paths = [str(destination / name) for name in GIT_EMPTY_DIRECTORIES]
    return [
        "test ! -L " + quote(home) + " && test ! -L " + quote(home + "/git")
        + " && mkdir -p " + quote(home + "/git") + " && cp -a "
        + quote(seed + "/git/.") + " " + quote(home + "/git/"),
        "test -d " + quote(str(destination)) + " && test ! -L " + quote(str(destination))
        + " && test ! -L " + quote(str(destination.parent))
        + " && mkdir -p " + shlex.join(paths),
    ]


def submodule_policy(ctx, task):
    modules = ctx.modules(task)
    require({row["path"]: row["commit"] for row in modules} == GITLINKS,
            "original fixture gitlinks changed")
    return [{**row, "omit": True,
             "reason": "original fixture gitlink preserved; outside all13 original workspace members and the selected anchor-client async oracle; six original calibrations required"}
            for row in modules]


def migrate(ctx, task, oracle):
    from . import rust_lane

    prepared = original_inputs(ctx, task, oracle)
    compiler = referenced(prepared["compiler"])
    require([row["stdout"].split()[1] for row in compiler["versions"]] == ["1.70.0", "1.70.0"],
            "original executable versions changed")
    tool = checked_component(ctx, compiler, referenced(prepared["compiler_readback"]),
        status="original_Rust170_toolchain_reused_and_archived", role="toolchain")
    dependencies = referenced(prepared["dependencies"])
    require(dependencies["registry_packages"] == 510
            and dependencies["git_commit"] == "1be91f2863d8ecede32daaae7e768034e24bbc79",
            "locked registry or Git source changed")
    dep = checked_component(ctx, dependencies, referenced(prepared["dependency_readback"]),
        status="all_original_Anchor_locked_packages_and_git_archived", role="dependency")
    git = checked_component(ctx, referenced(prepared["git_seed"]), referenced(prepared["git_seed_readback"]),
        status="native_Anchor_Git_database_seed_archived", role="dependency")
    image, audit = ctx.image(rust_lane.IMAGE)
    config = ctx.output / "anchor-sources.toml"
    config.write_text('[source.crates-io]\nreplace-with="anchor-original-registry"\n'
                      '[source.anchor-original-registry]\nlocal-registry="' + INPUTS + '/local-registry"\n'
                      '[net]\noffline=true\nretry=0\n')
    config_asset = ctx.copy_asset(config, "anchor-config")
    harness = ctx.bundle(Path(prepared["harness"]["path"]).parent, ["round2_test.rs"], role="grader")
    assets = [
        {**tool, "kind": "archive", "visibility": "developer", "target": RUST},
        {**dep, "kind": "archive", "visibility": "developer", "target": INPUTS},
        {**git, "kind": "archive", "visibility": "developer", "target": GIT_SEED},
        {**config_asset, "visibility": "developer", "target": SOURCES},
        {**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE},
    ]
    recipe = RuntimeRecipe(
        task_id=TASK, repo=task["repo"], start_sha=START, lane="rust", image=image, image_audit=audit,
        workdir="/workspace", cpus=8, memory_mb=16384, build_scratch_mb=8192, pids_limit=512,
        command_timeout_seconds=3600, assets=assets, submodules=submodule_policy(ctx, task),
        cache_mounts={"anchor-cargo": CARGO_HOME, "target": "/opt/reviewbench-cache/target"},
        env={"PATH": RUST + "/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": RUST + "/bin/rustc", "RUSTDOC": RUST + "/bin/rustdoc",
             "CARGO_HOME": CARGO_HOME, "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target",
             "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0", "CARGO_BUILD_JOBS": "8",
             "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never",
             "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
             "TMPDIR": "/workspace/.reviewbench/tmp", "RUST_BACKTRACE": "1"},
        bootstrap=[
            "mkdir -p /workspace/.reviewbench/tmp",
            *git_seed_commands(),
            'test "$(' + RUST + '/bin/cargo -V)" = "cargo 1.70.0 (ec8a8a0ca 2023-04-25)"',
        ],
        dev_checks=[cargo_command(grading=False)],
        grading={
            "multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
            "golden_patch": ctx.copy_asset(Path(prepared["golden"]["path"]), "anchor-original-golden"),
            "bootstrap": ["mkdir -p /workspace/client/tests && cp " + PRIVATE
                          + "/round2_test.rs /workspace/client/tests/oracle_nonblocking.rs"],
            "commands": [{"command": cargo_command(grading=True), "parser": "rust-test", "expected_tests": TESTS}],
        },
    )
    recipe.validate()
    return recipe
