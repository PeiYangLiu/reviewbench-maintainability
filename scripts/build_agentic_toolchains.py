#!/usr/bin/env python3
"""Build pinned toolchain images with shared admission and bounded BuildKit resources."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import atomic_json, canonical_hash, file_hash
from reviewbench_maintainability.agentic.environment import docker
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import GiB, ResourceRequest
from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock

NATIVE_FILES = {
    "oracle-rippled-modern:real": "Dockerfile.rippled-modern",
    "reviewbench-hard-native-absl:20260804": "docker/Dockerfile.absl-protobuf",
    "reviewbench-hard-native-cxx23:20260804": "Dockerfile.cxx23",
    "reviewbench-hard-native-autopas:20260804": "docker/Dockerfile.autopas",
    "reviewbench-hard-native-lmms:20260804": "docker/Dockerfile.lmms",
    "reviewbench-hard-native-hyrise:20260804": "docker/Dockerfile.hyrise",
    "reviewbench-hard-native-lapis:20260804": "Dockerfile.lapis",
    "reviewbench-hard-native-t8code:20260804": "Dockerfile.t8code",
    "reviewbench-hard-native-mariadb:20260804": "Dockerfile.mariadb",
    "reviewbench-hard-native-diskann:20260804": "Dockerfile.diskann",
    "reviewbench-hard-native-gmgpolar:20260804": "Dockerfile.gmgpolar",
    "reviewbench-hard-native-qlever:20260804": "Dockerfile.qlever",
    "reviewbench-hard-native-arrow:20260804": "Dockerfile.arrow",
    "reviewbench-hard-native-endless-sky:20260804": "Dockerfile.endless-sky",
    "reviewbench-hard-native-flutter-tizen:20260804": "Dockerfile.flutter-tizen",
    "reviewbench-hard-native-ginkgo:20260804": "Dockerfile.ginkgo",
    "reviewbench-hard-native-google-cloud:20260804": "Dockerfile.google-cloud",
    "reviewbench-hard-native-halide:20260804": "Dockerfile.halide",
    "reviewbench-hard-native-iree-compiler:20260804": "Dockerfile.iree-compiler",
    "reviewbench-hard-native-iree:20260804": "Dockerfile.iree",
    "reviewbench-hard-native-luanti:20260804": "Dockerfile.luanti",
    "reviewbench-hard-native-openvino:20260804": "Dockerfile.openvino",
    "reviewbench-hard-native-nmos:20260804": "Dockerfile.nmos",
}


def build_profile(text):
    """Reserve compilation slots for actual builds, not small metadata/APT layers.

    Classification is deliberately narrow. Unknown shell syntax, source
    downloads and compiler invocations use the original conservative profile.
    Only literal system-library compatibility links and exact-version Python
    build tools extend the small package-install profile.
    """
    instructions = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending += (" " if pending else "") + line.rstrip("\\").strip()
        if line.endswith("\\"):
            continue
        instruction, _, body = pending.partition(" ")
        instructions.append((instruction.upper(), body))
        pending = ""
    if pending:
        instructions.append(("UNKNOWN", pending))
    metadata = {"FROM", "ARG", "ENV", "LABEL"}
    if instructions and all(name in metadata for name, _ in instructions):
        return {"kind": "metadata-only", "cpus": 1, "memory_gib": 1, "build_memory_gib": 1,
                "scratch_gib": 1, "heavy": False}
    package_install = bool(instructions) and all(name in metadata | {"RUN"} for name, _ in instructions)
    saw_install = False
    tool_install = False
    compatibility_link = False
    compatibility_directories = set()
    tool_venvs = set()
    for name, body in instructions:
        if name != "RUN":
            continue
        if any(token in body for token in (";", "|", "`", "$(", "\n", "\r", "||")):
            package_install = False
            break
        for part in body.split("&&"):
            try:
                words = shlex.split(part)
            except ValueError:
                package_install = False
                break
            if words[:1] == ["DEBIAN_FRONTEND=noninteractive"]:
                words = words[1:]
            if words == ["apt-get", "update"] or words == ["rm", "-rf", "/var/lib/apt/lists/*"]:
                continue
            if (len(words) == 3 and words[:2] == ["mkdir", "-p"]
                    and re.fullmatch(r"/opt/[A-Za-z0-9][A-Za-z0-9_.-]*", words[2])):
                compatibility_directories.add(words[2])
                continue
            if (len(words) == 4 and words[:2] == ["ln", "-s"]
                    and re.fullmatch(r"/usr/lib/x86_64-linux-gnu/lib[A-Za-z0-9_+.-]+\.so(?:\.\d+)*", words[2])
                    and Path(words[3]).parent.as_posix() in compatibility_directories
                    and re.fullmatch(r"lib[A-Za-z0-9_+.-]+\.so(?:\.\d+)*", Path(words[3]).name)):
                compatibility_link = True
                continue
            if (len(words) == 4 and words[:3] == ["python3", "-m", "venv"]
                    and words[3] in {"/opt/conan", "/opt/cmake"}):
                tool_venvs.add(words[3])
                continue
            if len(words) == 4 and words[1:3] == ["install", "--no-cache-dir"]:
                pin = re.fullmatch(r"(conan|cmake)==[0-9]+(?:\.[0-9]+){1,3}", words[3])
                if pin and (words[0] == "pip3" or (
                        words[0] == "/opt/" + pin[1] + "/bin/pip" and "/opt/" + pin[1] in tool_venvs)):
                    tool_install = True
                    continue
            if words[:2] != ["apt-get", "install"] or len(words) < 3:
                package_install = False
                break
            packages = [word for word in words[2:] if word not in {"-y", "--no-install-recommends"}]
            if not packages or any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?:=[A-Za-z0-9:~+._-]+)?", p)
                                   for p in packages):
                package_install = False
                break
            saw_install = True
    if package_install and (saw_install or tool_install):
        kind = ("pinned-python-tool" if tool_install else
                "apt-compat-libraries" if compatibility_link else "apt-install")
        return {"kind": kind, "cpus": 2, "memory_gib": 4, "build_memory_gib": 4,
                "scratch_gib": 8 if tool_install or compatibility_link else 6, "heavy": False}
    return {"kind": "source-build", "cpus": 4, "memory_gib": 12, "build_memory_gib": 8,
            "scratch_gib": 24, "heavy": True}


def limit_build_jobs(text, cpus):
    """Enforce the cap without increasing original serial/two-job builds."""
    if type(cpus) is not int or cpus < 1:
        raise ValueError("toolchain CPU cap must be a positive integer")
    return re.sub(r"-j\s*(\d+)", lambda match: "-j" + str(min(int(match[1]) or cpus, cpus)), text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--image", action="append", required=True, choices=list(NATIVE_FILES))
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    services = ExecutionServices(load_config(args.config))
    output = args.out_dir.resolve()
    with output_lock(output):
        registry = json.loads(args.registry.read_text()) if args.registry.is_file() else {}
        def build(name):
            old = registry.get(name)
            if old and old.get("status") == "ready":
                try:
                    docker("image", "inspect", old["image"])
                    return old
                except Exception:
                    pass
            source = args.legacy_root / "native" / NATIVE_FILES[name]
            record = {"legacy_image": name, "source_dockerfile": str(source), "status": "failed"}
            try:
                text = source.read_text()
                if re.search(r"^(COPY|ADD)\s", text, re.M):
                    raise ValueError("toolchain Dockerfile needs a curated COPY context")
                profile = build_profile(text)
                record["resource_profile"] = profile
                with services.gate.acquire(ResourceRequest(
                        "image:" + name, cpus=profile["cpus"], memory_bytes=profile["memory_gib"] * GiB,
                        scratch_bytes=profile["scratch_gib"] * GiB, heavy=profile["heavy"])):
                    # Resolve every parent once, then build from its immutable
                    # ID. No mutable legacy image tag is overwritten.
                    parents = []
                    for match in list(re.finditer(r"^FROM\s+(\S+)", text, re.M)):
                        parent = match[1]
                        if parent.startswith(("reviewbench-", "oracle-")):
                            resolved = registry.get(parent, {}).get("image", parent)
                            details = json.loads(docker("image", "inspect", resolved))[0]
                        else:
                            docker("pull", parent, timeout=900)
                            details = json.loads(docker("image", "inspect", parent))[0]
                        parents.append({"reference": parent, "image_id": details["Id"],
                                        "repo_digests": details.get("RepoDigests", [])})
                        if details.get("RepoDigests"):
                            pin = details["RepoDigests"][0]
                        else:
                            # BuildKit interprets FROM sha256:... as a registry
                            # repository named "sha256", not a local image ID.
                            # Give the already-verified ID a unique content tag.
                            pin = "reviewbench-agentic-parent:" + details["Id"].split(":")[1]
                            docker("tag", details["Id"], pin)
                            assert json.loads(docker("image", "inspect", pin))[0]["Id"] == details["Id"]
                        text = text.replace("FROM " + parent, "FROM " + pin, 1)
                    # Limit explicit project build flags as well as cgroup CPU.
                    text = limit_build_jobs(text, profile["cpus"])
                    key = canonical_hash({"text": text, "resources": profile})[:24]
                    directory = output / key
                    directory.mkdir(parents=True, exist_ok=True)
                    dockerfile = directory / "Dockerfile"
                    dockerfile.write_text(text)
                    image_file, log = directory / "image.id", directory / "build.log"
                    tag = "reviewbench-agentic-toolchain:" + key
                    command = ["docker", "build", "--resource", "cpu-quota=" + str(profile["cpus"] * 100000),
                               "--resource", "memory=" + str(profile["build_memory_gib"]) + "g",
                               "--progress=plain", "--iidfile", str(image_file), "-t", tag, str(directory)]
                    started = time.monotonic()
                    with log.open("w") as stream:
                        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=7200)
                    record.update(command=shlex.join(command), log=str(log), exit_status=result.returncode,
                                  seconds=round(time.monotonic()-started,2), parents=parents,
                                  dockerfile_sha256=file_hash(dockerfile))
                    if result.returncode:
                        raise RuntimeError("bounded toolchain build failed: " + log.read_text()[-1200:])
                    image = image_file.read_text().strip()
                    assert json.loads(docker("image", "inspect", image))[0]["Id"] == image
                    record.update(status="ready", image=image, tag=tag)
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
            return record
        for name, result in completed_jobs(build, args.image, args.workers):
            registry[name] = result
            atomic_json(args.registry, registry)
            print(json.dumps(result, indent=2), flush=True)
        atomic_json(output / "resources.json", services.gate.status())
    return 0 if all(registry[n]["status"] == "ready" for n in args.image) else 2


if __name__ == "__main__":
    raise SystemExit(main())
