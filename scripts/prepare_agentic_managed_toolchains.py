#!/usr/bin/env python3
"""Freeze managed-language toolchains, with bounded builds and no task sources."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import atomic_json, canonical_hash, file_hash
from reviewbench_maintainability.agentic.environment import docker
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import GiB, ResourceRequest
from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock

DOTNET = {"rb-csharp-sdk8-cov": ("8.0", "8.0"),
          "rb-csharp-sdk9-cov": ("9.0", "9.0"),
          "rb-csharp-sdk7-cov": ("7.0", "7.0"),
          "rb-csharp-sdk6.0.400-cov": ("6.0.400", "6.0")}
PHP = {"rb-scripted-php" + version + "-cov": "Dockerfile.php" + version for version in ("74", "82", "84")}
RUBY = {"ruby:3.2-bookworm", "ruby:3.3-bookworm", "ruby:3.4-bookworm"}
JAVA = {"oracle-java-tools:" + str(version): version for version in (8, 11, 17, 21)}
JAVA_NODE = {"oracle-java-tools:" + str(version) + "-node": version for version in (17, 21)}
JAVA.update(JAVA_NODE)
NODE_IMAGE = "node:22.14.0-bookworm-slim"
NODE_PACKAGES = {"express": "4.19.2", "jsdom": "16.7.0", "jszip": "3.8.0", "source-map-support": "0.5.19"}
SYNTAX_SOURCE = """using System;
using System.IO;
using System.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

if (args.Length == 0) { Console.Error.WriteLine("usage: CSharpSyntax FILE..."); return 2; }
var failed = false;
foreach (var file in args) {
    var tree = CSharpSyntaxTree.ParseText(File.ReadAllText(file),
        new CSharpParseOptions(LanguageVersion.Preview), path: file);
    foreach (var diagnostic in tree.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error)) {
        Console.Error.WriteLine(diagnostic.ToString());
        failed = true;
    }
}
if (failed) return 1;
Console.WriteLine("Public C# syntax: PASS");
return 0;
"""


def syntax_project(framework):
    return f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType><TargetFramework>net{framework}</TargetFramework>
    <EnableNETAnalyzers>false</EnableNETAnalyzers><NuGetAudit>false</NuGetAudit>
  </PropertyGroup>
  <ItemGroup>
    <Reference Include="Microsoft.CodeAnalysis">
      <HintPath>$(MSBuildSDKsPath)/../Roslyn/bincore/Microsoft.CodeAnalysis.dll</HintPath>
    </Reference>
    <Reference Include="Microsoft.CodeAnalysis.CSharp">
      <HintPath>$(MSBuildSDKsPath)/../Roslyn/bincore/Microsoft.CodeAnalysis.CSharp.dll</HintPath>
    </Reference>
  </ItemGroup>
</Project>
"""


def pin_debian_snapshot(text, timestamp):
    from datetime import datetime
    if not re.fullmatch(r"\d{8}T\d{6}Z", timestamp):
        raise ValueError("Debian snapshot must be an exact UTC timestamp")
    datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ")
    if text.count("RUN apt-get update") != 1:
        raise ValueError("snapshot toolchain requires one curated Debian apt step")
    # Timestamped snapshots intentionally retain expired Release dates.
    # Scope this setting to these signed, immutable snapshot sources only.
    configure = (
        "RUN . /etc/os-release && test \"$ID\" = debian && "
        "printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/" + timestamp +
        "/ %s main\\ndeb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/" + timestamp +
        "/ %s-security main\\n' \"$VERSION_CODENAME\" \"$VERSION_CODENAME\" > /etc/apt/sources.list "
        "&& rm -f /etc/apt/sources.list.d/debian.sources\n")
    return text.replace("RUN apt-get update", configure + "RUN apt-get update", 1)


def managed_source(name, legacy, *, debian_snapshot=None, java_native_build_tools=False, java_mxml=False):
    if java_native_build_tools and name not in JAVA:
        raise ValueError("native Java build tools require a Java toolchain")
    if java_mxml and (name not in JAVA or not java_native_build_tools):
        raise ValueError("Mini-XML source requires a native Java build toolchain")
    if name in RUBY:
        return f"FROM {name}\n", {}, "ruby --version"
    if name in JAVA:
        packages = "git python3 unzip"
        check = "java -version 2>&1 && javac -version"
        if java_native_build_tools:
            packages += " build-essential pkg-config"
            check += " && cc --version && make --version"
        text = (f"FROM eclipse-temurin:{JAVA[name]}-jdk\n"
                "RUN apt-get update && apt-get install -y --no-install-recommends " + packages +
                " && rm -rf /var/lib/apt/lists/*\n")
        if name in JAVA_NODE:
            # The public Node tool distribution is copied from an independently
            # digest-pinned image, never from a historical benchmark workspace.
            # Both symlinked npm entrypoints and their package tree are retained.
            text += (
                f"COPY --from={NODE_IMAGE} /usr/local/ /usr/local/\n"
                "ENV NODE_PATH=/usr/local/lib/node_modules\n"
                "RUN npm install --global --no-audit --no-fund " +
                " ".join(package + "@" + version for package, version in NODE_PACKAGES.items()) +
                " && npm cache clean --force\n")
            probe = ("const expected=" + json.dumps(NODE_PACKAGES) + ";"
                     "for (const [name,version] of Object.entries(expected)) {"
                     "if(require(name+'/package.json').version!==version) throw Error(name+' version mismatch');"
                     "require(name); } console.log('Pinned Node tooling: PASS');")
            check += " && test \"$(node --version)\" = v22.14.0 && node -e " + shlex.quote(probe)
        if java_mxml:
            text += (
                "COPY mxml-source.tar.gz /tmp/reviewbench-mxml.tar.gz\n"
                "RUN mkdir /tmp/reviewbench-mxml && "
                "tar -xzf /tmp/reviewbench-mxml.tar.gz --strip-components=1 -C /tmp/reviewbench-mxml && "
                "cd /tmp/reviewbench-mxml && CC=gcc CFLAGS='-O2 -std=gnu11' ./configure --prefix=/usr/local && "
                "make -j2 && make install && ldconfig && "
                "rm -rf /tmp/reviewbench-mxml /tmp/reviewbench-mxml.tar.gz\n")
            check += (
                " && printf '#include <mxml.h>\\nint main(void) { mxml_node_t *n = mxmlNewXML(\"1.0\"); "
                "mxmlDelete(n); return 0; }\\n' > /tmp/reviewbench-mxml-check.c"
                " && cc /tmp/reviewbench-mxml-check.c -lmxml -o /tmp/reviewbench-mxml-check"
                " && /tmp/reviewbench-mxml-check && pkg-config --modversion mxml")
        return text, {}, check
    if name in PHP:
        source = legacy.parent / "strict_screen_20260726/oracle_recovery/scripted/tools" / PHP[name]
        text = source.read_text()
        copies = re.findall(r"^(?:COPY|ADD)\s+.*$", text, re.MULTILINE)
        if copies != ["COPY --from=composer:2 /usr/bin/composer /usr/local/bin/composer"]:
            raise ValueError("PHP toolchain has an uncurated build context")
        if debian_snapshot:
            text = pin_debian_snapshot(text, debian_snapshot)
        return text, {}, "php --version && composer --version"
    version, framework = DOTNET[name]
    text = (f"FROM mcr.microsoft.com/dotnet/sdk:{version}\n"
            "ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1\n"
            "COPY Program.cs CSharpSyntax.csproj /tmp/reviewbench-syntax/\n"
            "RUN mkdir -p /tmp/empty-feed && "
            "dotnet build /tmp/reviewbench-syntax/CSharpSyntax.csproj -c Release "
            "-o /opt/reviewbench-tools/csharp-syntax -m:1 -p:UseSharedCompilation=false "
            "-p:RestoreSources=/tmp/empty-feed -p:NuGetAudit=false --nologo "
            "&& rm -rf /tmp/reviewbench-syntax /tmp/empty-feed\n")
    return text, {"Program.cs": SYNTAX_SOURCE, "CSharpSyntax.csproj": syntax_project(framework)}, "dotnet --info"


def pin_parents(text):
    """Pin both FROM and the explicitly curated composer tool-only COPY."""
    references = list(dict.fromkeys(re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE) +
                                    re.findall(r"^COPY --from=(\S+)", text, re.MULTILINE)))
    parents = []
    for reference in references:
        docker("pull", reference, timeout=900)
        data = json.loads(docker("image", "inspect", reference))[0]
        pin = data["RepoDigests"][0]
        parents.append({"reference": reference, "image_id": data["Id"], "digest": pin})
        text = text.replace("FROM " + reference + "\n", "FROM " + pin + "\n")
        text = text.replace("COPY --from=" + reference + " ", "COPY --from=" + pin + " ")
    return text, parents


def managed_build_profile(name, *, java_mxml=False):
    """Curated Java tool preparation is separate from full repository builds."""
    if name in RUBY:
        return {"kind": "ruby-sdk-image", "cpus": 1, "memory_gib": 1,
                "scratch_gib": 4, "heavy": False}
    if name in JAVA:
        # Java SDKs are supplied by the pinned parent. The only optional source
        # build is the explicitly supplied small Mini-XML archive with make -j2.
        return {"kind": ("java-native-dependency" if java_mxml else
                         "java-node-tools" if name in JAVA_NODE else "java-sdk-packages"),
                "cpus": 2, "memory_gib": 4, "scratch_gib": 8, "heavy": False}
    return {"kind": "managed-source-build", "cpus": 4, "memory_gib": 8,
            "scratch_gib": 12, "heavy": True}


def bounded_mxml_source(path):
    if path.stat().st_size > 10 * 1024**2:
        raise ValueError("Mini-XML source archive exceeds the curated tool-input bound")
    total, count = 0, 0
    with tarfile.open(path) as archive:
        for member in archive:
            count += 1
            total += member.size
            if (count > 10000 or total > 64 * 1024**2
                    or member.name.startswith("/") or ".." in Path(member.name).parts
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Mini-XML source has an unsupported member or expanded size")
    return {"members": count, "expanded_bytes": total}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--image", action="append", required=True, choices=sorted(set(DOTNET) | set(PHP) | RUBY | set(JAVA)))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--debian-snapshot",
                        help="exact YYYYMMDDTHHMMSSZ apt snapshot for legacy PHP toolchains")
    parser.add_argument("--java-native-build-tools", action="store_true",
                        help="include C/C++ compiler, binutils, make and pkg-config for Java native modules")
    parser.add_argument("--java-mxml-source", type=Path, help="explicit fixed Mini-XML source archive")
    parser.add_argument("--java-mxml-sha256", help="independent SHA256 pin for --java-mxml-source")
    args = parser.parse_args()
    if args.java_native_build_tools and any(name not in JAVA for name in args.image):
        parser.error("--java-native-build-tools requires only Java --image selections")
    if bool(args.java_mxml_source) != bool(args.java_mxml_sha256):
        parser.error("--java-mxml-source and --java-mxml-sha256 must be supplied together")
    if args.java_mxml_source:
        if (not args.java_native_build_tools or any(name not in JAVA for name in args.image)
                or args.java_mxml_source.is_symlink() or not args.java_mxml_source.is_file()
                or not re.fullmatch(r"[0-9a-f]{64}", args.java_mxml_sha256)
                or file_hash(args.java_mxml_source) != args.java_mxml_sha256):
            parser.error("Mini-XML source must be hash-bound and used with a native Java toolchain")
        bounded_mxml_source(args.java_mxml_source)
    services = ExecutionServices(load_config(args.config))
    output = args.out_dir.resolve()
    with output_lock(output):
        registry = json.loads(args.registry.read_text()) if args.registry.is_file() else {}

        def build(name):
            previous = registry.get(name)
            if (previous and previous.get("status") == "ready"
                    and (name not in PHP or previous.get("debian_snapshot") == args.debian_snapshot)
                    and (name not in JAVA or previous.get("java_native_build_tools", False)
                         == args.java_native_build_tools)
                    and previous.get("java_mxml_sha256") == args.java_mxml_sha256):
                try:
                    docker("image", "inspect", previous["image"])
                    return previous
                except Exception:
                    pass
            result = {"legacy_image": name, "status": "failed"}
            try:
                profile = managed_build_profile(name, java_mxml=args.java_mxml_source is not None)
                result["resource_profile"] = profile
                with services.gate.acquire(ResourceRequest(
                        "managed-image:" + name, cpus=profile["cpus"],
                        memory_bytes=profile["memory_gib"] * GiB,
                        scratch_bytes=profile["scratch_gib"] * GiB, heavy=profile["heavy"])):
                    text, files, check = managed_source(
                        name, args.legacy_root, debian_snapshot=args.debian_snapshot,
                        java_native_build_tools=args.java_native_build_tools,
                        java_mxml=args.java_mxml_source is not None,
                    )
                    text, parents = pin_parents(text)
                    binary_inputs = {"mxml-source.tar.gz": args.java_mxml_sha256} if args.java_mxml_source else {}
                    key = canonical_hash({"dockerfile": text, "files": files, "binary_inputs": binary_inputs,
                                          "resource_profile": profile})[:24]
                    directory = output / key
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / "Dockerfile").write_text(text)
                    for filename, content in files.items():
                        (directory / filename).write_text(content)
                    if args.java_mxml_source:
                        if file_hash(args.java_mxml_source) != args.java_mxml_sha256:
                            raise ValueError("fixed Mini-XML input changed before staging")
                        shutil.copy2(args.java_mxml_source, directory / "mxml-source.tar.gz")
                        if file_hash(directory / "mxml-source.tar.gz") != args.java_mxml_sha256:
                            raise ValueError("staged Mini-XML input differs from its pin")
                    image_file, log = directory / "image.id", directory / "build.log"
                    tag = "reviewbench-agentic-managed:" + key
                    command = ["docker", "build", "--resource", "cpu-quota=" + str(profile["cpus"] * 100000),
                               "--resource", "memory=" + str(profile["memory_gib"]) + "g",
                               "--progress=plain", "--iidfile", str(image_file), "-t", tag, str(directory)]
                    started = time.monotonic()
                    with log.open("w") as stream:
                        done = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=3600)
                    result.update(command=shlex.join(command), log=str(log), exit_status=done.returncode,
                                  parents=parents, seconds=round(time.monotonic() - started, 2),
                                  dockerfile_sha256=file_hash(directory / "Dockerfile"),
                                  debian_snapshot=args.debian_snapshot if name in PHP else None,
                                  java_native_build_tools=args.java_native_build_tools if name in JAVA else False,
                                  java_mxml_sha256=args.java_mxml_sha256,
                                  java_mxml_source=str(args.java_mxml_source.resolve()) if args.java_mxml_source else None)
                    if done.returncode:
                        raise RuntimeError("managed toolchain build failed: " + log.read_text()[-2000:])
                    image = image_file.read_text().strip()
                    assert json.loads(docker("image", "inspect", image))[0]["Id"] == image
                    versions = docker("run", "--rm", "--network", "none", "--cpus", "1", "--memory", "1g",
                                      "--entrypoint", "bash", image, "-c", check, timeout=120)
                    result.update(status="ready", image=image, tag=tag, version_command=check, versions=versions)
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            return result

        for name, result in completed_jobs(build, args.image, args.workers):
            registry[name] = result
            atomic_json(args.registry, registry)
            print(json.dumps(result, indent=2), flush=True)
        atomic_json(output / "resource-usage.json", services.gate.status())
    return 0 if all(registry[name]["status"] == "ready" for name in args.image) else 2


if __name__ == "__main__":
    raise SystemExit(main())
