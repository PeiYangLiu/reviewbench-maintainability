"""Read a completed original libsbp five-update preparation; never resolve here."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat

from reviewbench_maintainability.agentic.contracts import file_hash, repo_path
from reviewbench_maintainability.agentic.snapshots import tree_files

TASK = "github__swift-nav__libsbp__pull_1218__comment_970220101__naming"
LOCK_SHA = "acfd7d0222f8f41098fb3bdd4929e73c002a356564d6d0e8ae03115b44458475"
DECISION_SHA = "df82194261ef095dea938248f68b3fedf642b4f2f6e3a0d4548a744e474b1752"
IMAGE = "sha256:562c0e8695227d6010aee579057d9b46751ceb17825b99141b2fab919bbaf529"
CARGO_SHA = "6ef77ae23f53f7f106e915c63baf91ecf1b5745a3272a7287778970c11870114"
RUSTC_SHA = "b800bcf02a1ee8679fc182c7e7c55f7bb3d95b9eff89e8328e179094e120fc39"
PINS = (("swiftnav", "0.8.1"), ("swiftnav-sys", "0.8.1"), ("log", "0.4.17"),
        ("which", "4.2.5"), ("libc", "0.2.139"))
PREPARE = " && ".join("cargo +1.64.0 update -p " + name + " --precise " + version + " >/dev/null"
                      for name, version in PINS)
MODULES = {
    "c/cmake/common": "c5c4d80588d28695bd377cfe89d7df64980341d6",
    "c/third_party/check": "e05766540eba58ae46da2e2bfce4cc4212488c98",
    "c/third_party/googletest": "703bd9caab50b139428cea1aaff9974ebee5742e",
}


def verify_source(root, expected, *, lock=False):
    from .rust_first_resolution import require
    root = Path(root).resolve()
    files = tree_files(root)
    allowed = set(expected) | ({"Cargo.lock"} if lock else set())
    require(set(files) == allowed, "libsbp source set changed or a submodule was omitted")
    for name, row in expected.items():
        path = files[name]
        mode = path.lstat().st_mode
        if row["git_mode"] == "120000":
            require(stat.S_ISLNK(mode) and os.readlink(path) == row["link_target"],
                    "original libsbp source link changed")
            data = os.fsencode(os.readlink(path))
        else:
            require(stat.S_ISREG(mode), "original libsbp source file kind changed")
            data = path.read_bytes()
        require(len(data) == row["bytes"] and hashlib.sha256(data).hexdigest() == row["sha256"]
                and stat.S_IMODE(mode) == row["mode"]
                and hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == row["git_oid"],
                "libsbp original blob or mode changed")
    if lock:
        require(not files["Cargo.lock"].is_symlink() and file_hash(files["Cargo.lock"]) == LOCK_SHA,
                "libsbp first final root lock changed")


def prepared_original_sequence(envelope, profile):
    from .rust_first_resolution import BINDINGS, CRATES_IO, referenced, reference_packages, require, graph_summary
    from .rust_vendor import locked_packages
    from .rust_libsbp_entry import verify_blocks, metadata_warnings
    require(envelope["task_id"] == profile["task_id"] == TASK and profile["prepare_command"] == PREPARE
            and profile["repository_lib_tests"] is True, "foreign libsbp preparation")
    root = Path(envelope["root"])
    require(root.is_absolute() and root.is_dir(), "libsbp prepared input root absent")

    def read(name, *, json_data=True):
        name = repo_path(name)
        require(name in envelope["files"], "unsealed libsbp input: " + name)
        return referenced({"path": str(root / name), **envelope["files"][name]}, json_data=json_data)

    require(envelope["files"]["first-prepare-decision.json"]["sha256"] == DECISION_SHA,
            "libsbp first preparation decision changed")
    decision, projection = read("first-prepare-decision.json"), read("original-source-projection.json")
    receipt, first = read("resolution-receipt.json"), read("first-success/receipt.json")
    require(decision["task_id"] == projection["task_id"] == receipt["task_id"] == first["task_id"] == TASK
            and decision["original_plan_sha256"] == projection["plan_sha256"] == BINDINGS[TASK]["plan_sha256"]
            and projection["original_golden"]["sha256"] == profile["golden"]
            and decision["source_projection"]["sha256"] == envelope["files"]["original-source-projection.json"]["sha256"],
            "libsbp preparation lost original task/source/plan bindings")
    require(decision["original_prepare_command"] == PREPARE and decision["first_complete_prepare_attempts_max"] == 1
            and decision["no_generate_before_original_prepare"] is True
            and decision["freeze_only_after_all_five_successes"] is True
            and decision["no_repeated_prepare_or_update_after_success"] is True
            and decision["historical_lock_restoration"] is False
            and decision["original_workdir"] == "rust" and decision["toolchain"] == "1.64.0",
            "libsbp original initialization policy changed")
    require(receipt["first_prepare_attempts"] == first["prepare_calls"] == 1
            and receipt["formal_six_replay"] is False and receipt["registered"] is False
            and receipt["new_model_calls"] == receipt["new_completed_evaluations"] == 0
            and first["historical_lock_restoration"] is False
            and first["origin"] == "first final lock after entire original five-update prepare chain",
            "libsbp initialization was repeated or relabeled as historical")
    for binary, expected in (("cargo", CARGO_SHA), ("rustc", RUSTC_SHA)):
        record = decision["tools"][binary]
        require(record["sha256"] == expected and file_hash(Path(record["path"])) == expected,
                "libsbp original compiler changed")
    data = read("first-success/Cargo.lock", json_data=False)
    require(hashlib.sha256(data).hexdigest() == first["lock"]["sha256"] == LOCK_SHA,
            "libsbp first complete lock changed")
    lock = reference_packages(data)
    require(len(lock["package"]) == 154 and
            {name: [p["version"] for p in lock["package"] if p["name"] == name] for name, _ in PINS}
            == {name: [version] for name, version in PINS}, "original five libsbp pins changed")
    prepare = read("commands/prepare-full/result.json")
    require(prepare["argv"] == ["/bin/bash", "--noprofile", "--norc", "-c", PREPARE]
            and prepare["command_verbatim"] == PREPARE and prepare["cwd"] == str(root / "work/start/rust")
            and prepare["exit_status"] == 0 and prepare["natural_exit"] is True
            and prepare["source_before"]["root_lock"] is None and prepare["prepare_calls"] == 1
            and datetime.fromisoformat(decision["recorded_at"]) < datetime.fromisoformat(prepare["started_at"])
            <= datetime.fromisoformat(prepare["completed_at"]) <= datetime.fromisoformat(first["recorded_at"]),
            "original libsbp full prepare command/order/result changed")
    previous = None
    for name, version in PINS:
        label = "pin-" + name
        child = read("commands/" + label + "/result.json")
        require(child["requested_argv"] == ["cargo", "+1.64.0", "update", "-p", name, "--precise", version]
                and child["argv"][1:] == child["requested_argv"][1:] and Path(child["argv"][0]).name == "cargo"
                and child["exit_status"] == 0 and child["natural_exit"] is True
                and child["lock_before_sha256"] == previous, "libsbp original five-update lock chain changed")
        previous = child["lock_after_sha256"]
        for stream in ("stdout", "stderr"):
            raw = read("commands/" + label + "/" + stream, json_data=False)
            require(hashlib.sha256(raw).hexdigest() == child[stream]["sha256"], "original Cargo stream changed")
    require(previous == LOCK_SHA, "libsbp final root lock is not the original chain result")
    for stream in ("stdout", "stderr"):
        raw = read("commands/prepare-full/" + stream, json_data=False)
        require(hashlib.sha256(raw).hexdigest() == prepare[stream]["sha256"], "original full prepare stream changed")
    modules = projection["modules"]
    require({name: row["commit"] for name, row in modules.items()} == MODULES,
            "original libsbp gitlink closure changed")
    for name, module in modules.items():
        require(module["source"]["revision"] == MODULES[name] and not module["source"]["gitlinks"],
                "libsbp child module has an unbound commit or nested gitlink")
        verify_source(module["source"]["path"], module["source"]["files"])
    graphs, audits, locks = {}, {}, {}
    for variant in ("start", "golden"):
        source = root / "work" / variant
        original = projection["roots"][variant]
        require(original["entry_count"] == 2246 and
                {name: row["git_oid"] for name, row in original["gitlinks"].items()} == MODULES,
                "libsbp original root tree or Git pins changed")
        expected = dict(original["files"])
        for name, module in modules.items():
            expected.update({name + "/" + path: row for path, row in module["source"]["files"].items()})
        require(len(expected) == 2842, "libsbp expanded source was pruned")
        verify_source(source, expected, lock=True)
        verify_blocks(source, profile["test_blocks"])
        manifests = {name: row["sha256"] for name, row in original["files"].items() if name.endswith("Cargo.toml")}
        require(manifests == profile["defaults"][variant]["manifests"], "original libsbp manifests changed")
        measured = read("commands/metadata-" + variant + "/result.json")
        require(measured["argv"][1:] == ["+1.64.0", "metadata", "--format-version", "1", "--locked", "--all-features"]
                and Path(measured["argv"][0]).name == "cargo" and measured["cwd"] == str(source / "rust")
                and measured["environment"]["CARGO_NET_OFFLINE"] == "true" and measured["network_requests"] == 0
                and measured["exit_status"] == 0 and measured["natural_exit"] is True
                and measured["lock_sha256"] == LOCK_SHA
                and datetime.fromisoformat(first["recorded_at"]) < datetime.fromisoformat(measured["started_at"]),
                "original libsbp locked offline metadata changed")
        for stream in ("stdout", "stderr"):
            raw = read("commands/metadata-" + variant + "/" + stream, json_data=False)
            require(hashlib.sha256(raw).hexdigest() == measured[stream]["sha256"], "metadata stream changed")
        require(not metadata_warnings(read("commands/metadata-" + variant + "/stderr", json_data=False).decode(), source).strip(),
                "unrecognized libsbp metadata stderr")
        graph = json.loads(read("commands/metadata-" + variant + "/stdout", json_data=False))
        audits[variant] = graph_summary(graph, lock, source, manifests, local_packages=2, virtual=True)
        require(audits[variant]["packages"] == 154, "libsbp metadata omitted a package")
        graphs[variant] = graph
        locks[variant] = {"path": str(root / "first-success/Cargo.lock"), "sha256": LOCK_SHA, "data": lock}
    require(audits["start"]["semantic_graph_sha256"] == audits["golden"]["semantic_graph_sha256"],
            "identical original libsbp manifests produced different graphs")
    inventory = read("archive-hydration.json")
    require(not inventory["missing"] and inventory["registry_packages"] == 152, "libsbp archive set incomplete")
    expected = locked_packages([lock])
    archives = {}
    for row in inventory["rows"]:
        key = row["name"], row["version"], row["source"]
        require(key[2] == CRATES_IO and key not in archives and expected.get(key) == row["checksum"]
                and row["retained"]["sha256"] == row["checksum"], "libsbp archive identity changed")
        referenced(row["retained"], limit=256 * 1024**2)
        archives[key] = row["retained"]
    require(set(archives) == set(expected), "libsbp archive closure was pruned")
    native = read("native-image/result.json")
    image = read("native-image/image-inspect.json")
    packages = read("original-diagnostic/native-package-versions.json")
    require(native["exit_status"] == 0 and native["timeout"] is False and native["image_id"] == IMAGE
            and len(image) == 1 and image[0]["Id"] == IMAGE and image[0]["Architecture"] == "amd64"
            and packages["exit_status"] == 0 and packages["argv"][-2:] == ["libudev-dev", "libudev1"]
            and packages["stdout"] == "libudev-dev 249.11-0ubuntu3.22 amd64\nlibudev1 249.11-0ubuntu3.22 amd64\n",
            "libsbp native package/image binding changed")
    return {"envelope": envelope, "root": root, "profile": profile, "locks": locks, "archives": archives,
            "graphs": graphs, "graph_audits": audits, "receipt": receipt,
            "receipt_sha256": envelope["files"]["resolution-receipt.json"]["sha256"],
            "cargo_cache": root / "cargo-home", "git_packages": {}, "cargo_sha256": CARGO_SHA,
            "rustc_sha256": RUSTC_SHA, "native_image": IMAGE, "modules": modules}


def platform(ctx, spec):
    """Publish the complete fixed child trees; never use generic omit=True."""
    from .rust_first_resolution import prepared_inputs, require
    checked = prepared_inputs(spec["first_new_resolution_inputs"])
    result = []
    for name, module in sorted(checked["modules"].items()):
        asset = ctx.bundle(Path(module["source"]["path"]), role="submodule", git_commit=module["commit"])
        result.append({"path": name, "commit": module["commit"], "asset": asset})
    require({r["path"]: r["commit"] for r in result} == MODULES, "libsbp module assets changed")
    return checked["native_image"], result
