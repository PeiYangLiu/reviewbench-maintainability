"""Explicit SDK components and isolated hostname resolution for JVM tasks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import zipfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, component, file_hash, repo_path,
)

SCHEMA = "reviewbench-jvm-platform-inputs/v1"
SDK_TARGET = "/opt/reviewbench-assets/android-sdk"
COMPONENT = re.compile(r"(?:platforms/android-\d+(?:-ext\d+)?|build-tools/\d+\.\d+\.\d+(?:-[\w.-]+)?|licenses)\Z")


def configuration(ctx):
    declared = ctx.config.get("migration", {}).get("jvm_platforms")
    if not declared:
        return {}, None
    path = Path(declared)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ContractError("JVM platform inputs require an explicit regular local configuration")
    digest = file_hash(path)
    previous = getattr(ctx, "_jvm_platform_inputs", None)
    if previous:
        if previous["path"] != str(path) or previous["sha256"] != digest:
            raise ContractError("JVM platform configuration changed during migration")
        return previous["tasks"], {"path": str(path), "sha256": digest}
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or set(data) != {"schema_version", "tasks"}
            or data["schema_version"] != SCHEMA or not isinstance(data["tasks"], dict)
            or not set(data["tasks"]) <= ctx.tasks.keys()):
        raise ContractError("JVM platform configuration has a different schema or task scope")
    for row in data["tasks"].values():
        if not isinstance(row, dict) or set(row) - {"android_sdk", "internal_network", "build_cache"}:
            raise ContractError("JVM platform inputs may only supply SDK components, an internal network and build caching")
        if "build_cache" in row and type(row["build_cache"]) is not bool:
            raise ContractError("JVM build-cache activation must be an explicit boolean")
    ctx._jvm_platform_inputs = {"path": str(path), "sha256": digest, "tasks": data["tasks"]}
    return data["tasks"], {"path": str(path), "sha256": digest}


def sdk_files(settings):
    if (not isinstance(settings, dict)
            or set(settings) != {"root", "components", "properties_sha256"}
            or not isinstance(settings["root"], str) or not Path(settings["root"]).is_absolute()):
        raise ContractError("Android SDK needs an explicit root, component list and metadata pins")
    root = Path(settings["root"]).resolve(strict=True)
    components = settings["components"]
    pins = settings["properties_sha256"]
    if (not isinstance(components, list) or not components
            or any(not isinstance(name, str) or not COMPONENT.fullmatch(name) for name in components)
            or len(set(components)) != len(components) or not isinstance(pins, dict)):
        raise ContractError("Android SDK component selection is absent or ambiguous")
    metadata = {name + "/source.properties" for name in components if name != "licenses"}
    if set(pins) != metadata:
        raise ContractError("each selected Android SDK platform/build-tool component needs a metadata pin")
    for name in components:
        current = root
        for part in name.split("/"):
            current /= part
            if current.is_symlink() or not current.is_dir():
                raise ContractError("Android SDK component is absent or linked outside the selected closure: " + name)
    for name, expected in pins.items():
        path = root / name
        if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or not path.is_file() or path.is_symlink() or file_hash(path) != expected):
            raise ContractError("Android SDK component metadata differs from its pin: " + name)
    files = []
    versions = {}
    for name in sorted(components):
        source = root / repo_path(name)
        if not source.is_dir() or source.is_symlink():
            raise ContractError("Android SDK component is absent or linked outside the selected closure: " + name)
        for directory, directories, filenames in os.walk(source, followlinks=False):
            if any((Path(directory) / child).is_symlink() for child in directories):
                raise ContractError("Android SDK component contains a linked directory")
            for child in filenames:
                path = Path(directory) / child
                if path.is_symlink() or not path.is_file():
                    raise ContractError("Android SDK component contains a linked or special file")
                files.append(path.relative_to(root).as_posix())
        if name.startswith("platforms/"):
            values = dict(line.split("=", 1) for line in (source / "source.properties").read_text().splitlines()
                          if "=" in line and not line.startswith("#"))
            expected_api = re.fullmatch(r"platforms/android-(\d+)(?:-ext\d+)?", name)[1]
            if values.get("AndroidVersion.ApiLevel") != expected_api:
                raise ContractError("Android SDK platform directory and API metadata differ")
            jar = source / "android.jar"
            if not jar.is_file():
                raise ContractError("Android SDK platform has no real android.jar")
            try:
                with zipfile.ZipFile(jar) as archive:
                    if not {"android/app/Activity.class", "android/os/Bundle.class"} <= set(archive.namelist()):
                        raise ContractError("Android SDK platform JAR lacks its public Android API")
            except zipfile.BadZipFile as exc:
                raise ContractError("Android SDK platform JAR is invalid") from exc
            versions[name] = values
    if not any(name.startswith("platforms/") for name in components):
        raise ContractError("Android SDK runtime needs at least one actual platform")
    return root, sorted(files), versions


def runtime_inputs(ctx, task):
    tasks, source_config = configuration(ctx)
    settings = tasks.get(task["id"], {})
    result = {"assets": [], "env": {}, "sidecars": []}
    if not settings:
        return result
    evidence = {"id": task["id"], "start_sha": task["start_sha"],
                "configuration": source_config, "scoring_result": False}
    if "android_sdk" in settings:
        root, names, versions = sdk_files(settings["android_sdk"])
        asset = ctx.bundle(root, names, role="toolchain")
        # Check pinned metadata again after potentially slow hashing/upload.
        for name, digest in settings["android_sdk"]["properties_sha256"].items():
            if file_hash(root / name) != digest:
                raise ContractError("Android SDK metadata changed during packaging")
        result["assets"].append({**asset, "kind": "archive", "visibility": "developer", "target": SDK_TARGET})
        result["env"].update(ANDROID_HOME=SDK_TARGET, ANDROID_SDK_ROOT=SDK_TARGET)
        evidence["android_sdk"] = {"asset": asset, "root": str(root), "files": len(names),
                                   "components": settings["android_sdk"]["components"], "platforms": versions}
    if "internal_network" in settings:
        network = settings["internal_network"]
        if (not isinstance(network, dict) or set(network) != {"anchor_image"}
                or not isinstance(network["anchor_image"], str)
                or not re.fullmatch(r"(?:[^@\s]+@)?sha256:[0-9a-f]{64}", network["anchor_image"])):
            raise ContractError("internal JVM network needs an explicit immutable anchor image")
        image, audit = ctx.image(network["anchor_image"])
        result["sidecars"].append({"name": "network-anchor", "image": image, "command": ["sleep", "7200"]})
        evidence["internal_network"] = {"image": image, "image_audit": audit,
                                        "network_mode": "engine-created internal network",
                                        "resource_accounting": "normal per-sidecar lease reservation"}
    atomic_json(ctx.output / "jvm-platform-inputs" / (component(task["id"]) + ".json"), evidence)
    return result
