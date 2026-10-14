"""Offline sbt/Gradle builds from current candidate source and download-only caches."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.resources import file_lock
from reviewbench_maintainability.agentic.snapshots import git

TOOLS = "/opt/reviewbench-assets/jvm-build-tools"
SBT_BASE = "/opt/reviewbench-assets/sbt-downloads"
SBT_CACHE = "/opt/reviewbench-cache/sbt"
GRADLE_BASE = "/opt/reviewbench-assets/gradle-downloads"
GRADLE_CACHE = "/opt/reviewbench-cache/gradle"
BUILD_TOOLS = "/opt/reviewbench-assets/jvm-build-cache"
BUILD_CACHE = "/opt/reviewbench-cache/jvm-build"


def sbt_jvm_options():
    """Bound initial/max heap and compiler stack even with project JVM flags."""
    return "-Xms256m -Xmx3g -Xss4m -XX:ActiveProcessorCount=2"


def repository_mirrors(values):
    result = {}
    for value in values:
        before, separator, after = value.partition("=")
        urls = [urlparse(url) for url in (before, after)]
        if (not separator or any(url.scheme != "https" or not url.netloc or url.username or url.password
                                or url.query or url.fragment for url in urls)):
            raise ContractError("Gradle repository mirrors must be credential-free HTTPS base URLs")
        result[before.rstrip("/")] = after.rstrip("/")
    return result


def gradle_init_script(original, mirrors):
    if not mirrors:
        return original
    mappings = json.dumps(mirrors)
    return original + "\n" + (
        "def reviewbenchMirrors = new groovy.json.JsonSlurper().parseText(" + json.dumps(mappings) + ")\n"
        "gradle.projectsEvaluated {\n"
        "  allprojects { project ->\n"
        "    project.repositories.withType(org.gradle.api.artifacts.repositories.MavenArtifactRepository).all { repo ->\n"
        "      def source = repo.url.toString().replaceAll('/+$', '')\n"
        "      if (reviewbenchMirrors.containsKey(source)) { repo.setUrl(reviewbenchMirrors[source]) }\n"
        "    }\n"
        "  }\n"
        "}\n")


def gradle_test_dependency_resolver():
    return """
gradle.rootProject { root ->
  def paths = System.getProperty("reviewbench.test.targets", "").split(",").findAll { !it.isEmpty() }
  if (!paths.isEmpty()) {
    root.tasks.create("reviewbenchResolveTestDependencies") {
      doLast {
        paths.each { path ->
          def target = root.tasks.findByPath(path)
          if (!(target instanceof org.gradle.api.tasks.testing.Test)) {
            throw new GradleException("Expected a real Gradle Test task: " + path)
          }
          target.classpath.files.each { file -> file.exists() }
          ["CompileClasspath", "RuntimeClasspath", "AnnotationProcessor"].each { suffix ->
            def configuration = target.project.configurations.findByName(target.name + suffix)
            if (configuration != null && configuration.canBeResolved) { configuration.resolve() }
          }
        }
      }
    }
  }
}
"""


def gradle_forced_test_execution():
    """Reuse compilation, never replay cached Test task reports as measurements."""
    return """
gradle.projectsEvaluated {
  allprojects { project ->
    project.tasks.withType(org.gradle.api.tasks.testing.Test).configureEach { task ->
      task.outputs.upToDateWhen { false }
      task.outputs.cacheIf { false }
    }
  }
}
"""


def build_tools(ctx, mirrors, *, force_tests=False):
    root = ctx.legacy / "jvm/cache/tools"
    if not mirrors and not force_tests:
        return ctx.bundle(root, ["sbt", "oracle-gradle.init.gradle"], role="toolchain")
    with tempfile.TemporaryDirectory(prefix="jvm-public-tools-", dir=ctx.scratch) as temporary:
        target = Path(temporary)
        shutil.copy2(root / "sbt", target / "sbt")
        (target / "oracle-gradle.init.gradle").write_text(
            gradle_init_script((root / "oracle-gradle.init.gradle").read_text(), mirrors) +
            (gradle_forced_test_execution() if force_tests else ""))
        return ctx.bundle(target, role="toolchain")


def source_files(ctx, task):
    return [name.decode() for name in git(ctx.repo(task), "ls-tree", "-r", "--name-only", "-z",
                                         task["start_sha"]).split(b"\0") if name]


def public_download_path(relative):
    path = Path(relative)
    parts = path.parts[1:] if path.parts[:1] == ("v1",) else path.parts
    return (len(parts) >= 3 and parts[0] in {"http", "https"}
            and "@" not in parts[1] and "%40" not in parts[1].lower()
            and not path.name.endswith((".lock", ".part", ".tmp", ".error"))
            and not any(token in path.name.lower() for token in ("credential", "password", "authorization")))


def pom_only_negative_cache(path, relative):
    """Keep Coursier's empty not-found marker only for a pinned POM-only module.

    This is metadata for an absent optional JAR, never an empty replacement JAR
    or a way to suppress an unavailable required binary.
    """
    relative = Path(relative)
    match = re.fullmatch(r"\.(.+)\.jar(?:\.sha1)?\.error", relative.name)
    if (not match or path.is_symlink() or not path.is_file() or path.stat().st_size != 0
            or not public_download_path(relative.with_name(match[1] + ".jar"))):
        return False
    pom = path.parent / (match[1] + ".pom")
    if not pom.is_file() or pom.is_symlink():
        return False
    try:
        project = ET.parse(pom).getroot()
    except ET.ParseError:
        return False
    ns = "{http://maven.apache.org/POM/4.0.0}" if project.tag.startswith("{") else ""
    artifact = project.findtext(ns + "artifactId")
    version = project.findtext(ns + "version") or project.findtext(ns + "parent/" + ns + "version")
    return (project.findtext(ns + "packaging") == "pom" and artifact is not None and version is not None
            and match[1] == artifact + "-" + version and path.parent.name == version)


def coursier_files(root):
    """Keep remote downloads, never local Ivy publications or project outputs."""
    names = [path.relative_to(root).as_posix() for path in root.rglob("*")
             if path.is_file() and not path.is_symlink() and public_download_path(path.relative_to(root))]
    if not names:
        raise ContractError("retained remote Coursier dependency cache is empty")
    return sorted(names)


def sbt_cache_files(home):
    names = [".cache/coursier/" + name for name in coursier_files(home / ".cache/coursier")]
    for subdir in (".sbt/boot", ".sbt/launchers"):
        for path in (home / subdir).rglob("*"):
            if (path.is_file() and not path.is_symlink()
                    and path.suffix in {".jar", ".properties", ".xml", ".sha1", ".md5"}):
                names.append(path.relative_to(home).as_posix())
    if not any(name.startswith(".sbt/launchers/") and name.endswith(".jar") for name in names):
        raise ContractError("retained versioned sbt launcher binaries are absent")
    # In particular .ivy2/local, .sbt/1.0/staging and project target outputs
    # are not developer dependencies.
    return sorted(set(names))


def sbt_boot_files(home):
    return sorted(path.relative_to(home).as_posix()
                  for prefix in (".sbt/boot", ".sbt/launchers")
                  for path in (home / prefix).rglob("*")
                  if path.is_file() and not path.is_symlink()
                  and path.suffix in {".jar", ".properties", ".xml", ".sha1", ".md5"})


def gradle_cache_files(root):
    names = []
    for prefix in ("caches/modules-2", "wrapper/dists"):
        for path in (root / prefix).rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if path.name.endswith((".lock", ".lck", ".part", ".tmp")) or path.name == "gc.properties":
                continue
            names.append(relative)
    if not any(name.startswith("wrapper/dists/") and "/bin/gradle" in name for name in names):
        raise ContractError("retained Gradle distribution cache is absent")
    return sorted(names)


def shared_archive(ctx, kind, source, names, *, revision=None):
    configured = ctx.config.get("migration", {}).get("jvm_shared_assets")
    shared = Path(configured) if configured else ctx.output.parent / "jvm-shared-assets"
    if not shared.is_absolute():
        raise ContractError("shared JVM asset index must be local and absolute")
    shared.mkdir(parents=True, exist_ok=True)
    identity = {"policy": "jvm-download-only/v1", "kind": kind, "source": str(source.resolve())}
    if revision:
        identity["revision"] = revision
    key = canonical_hash(identity)
    pointer = shared / (key + ".json")
    with file_lock(shared / (key + ".lock")):
        if pointer.exists():
            record = json.loads(pointer.read_text())
            asset = record["asset"]
            if file_hash(Path(asset["path"])) != asset["sha256"]:
                raise ContractError("shared JVM dependency descriptor changed")
            return asset
        # Directory enumeration of a large download cache can itself be
        # expensive. Reused content-addressed descriptors need no new scan.
        names = names() if callable(names) else names
        asset = ctx.bundle(source, names)
        atomic_json(pointer, {"kind": kind, "source_root": str(source), "policy": "jvm-download-only/v1",
                              "asset": asset, "files": len(names)})
        return asset


def seed_once(asset, target, digest):
    marker = target + "/.reviewbench-seed-" + digest
    return ("if [ ! -f " + shlex.quote(marker) + " ]; then cp -a " + shlex.quote(asset + "/.") +
            " " + shlex.quote(target) + " && touch " + shlex.quote(marker) + "; fi")


def sbt_prefix():
    # Explicit arguments remain effective when the project has .sbtopts,
    # which sbt-extras prefers over its SBT_OPTS environment variable.
    return shlex.join([
        TOOLS + "/sbt", "-sbt-launch-dir", SBT_CACHE + "/.sbt/launchers",
        "-Dsbt.boot.directory=" + SBT_CACHE + "/.sbt/boot",
        "-Dsbt.global.base=" + SBT_CACHE + "/global", "-Dsbt.ivy.home=" + SBT_CACHE + "/ivy",
        "-Dsbt.task.cpus=2", "-Dsbt.log.noformat=true", "-Dsbt.supershell=false",
        "-Dsbt.server.autostart=false",
    ])


def bind_scala_classpath(command, cache_roots):
    """Bind historical ad-hoc Java launchers to their compiled Scala version."""
    if "find /cachehome/.cache/coursier -path" not in command:
        return command, None
    versions = set(re.findall(r"/target/scala-(3\.\d+\.\d+)/classes", command))
    if len(versions) != 1:
        raise ContractError("ad-hoc Scala classpath has no unique compiled-version binding")
    version = versions.pop()
    scala2 = set()
    poms = []
    pattern = f"**/org/scala-lang/scala3-library_3/{version}/scala3-library_3-{version}.pom"
    for root in cache_roots:
        for path in root.glob(pattern):
            if not path.is_file() or path.is_symlink():
                continue
            xml = ET.parse(path)
            ns = {"m": "http://maven.apache.org/POM/4.0.0"}
            for dependency in xml.findall(".//m:dependencies/m:dependency", ns):
                if dependency.findtext("m:artifactId", namespaces=ns) == "scala-library":
                    scala2.add(dependency.findtext("m:version", namespaces=ns))
            poms.append({"path": str(path), "sha256": file_hash(path)})
    if len(scala2) != 1 or not re.fullmatch(r"2\.\d+\.\d+", next(iter(scala2), "") or ""):
        raise ContractError("compiled Scala runtime POM has no unique Scala-library dependency")
    dependency_version = scala2.pop()
    for artifact, selected in [("scala3-library_3", version), ("scala-library", dependency_version)]:
        original = f"*/org/scala-lang/{artifact}/*/*.jar"
        replacement = f"*/org/scala-lang/{artifact}/{selected}/{artifact}-{selected}.jar"
        if command.count(original) != 1:
            raise ContractError("Scala runtime lookup differs from its frozen classpath shape")
        command = command.replace(original, replacement)
    return command, {"scala3": version, "scala_library": dependency_version, "dependency_poms": poms}


def command_arguments(command, executable):
    words = shlex.split(command)
    if words.count(executable) != 1:
        raise ContractError("JVM command needs one explicit build-tool invocation")
    result = []
    for word in words[words.index(executable) + 1:]:
        if word in {"&&", ";", "||", "|", "&"}:
            break
        result.append(word)
    return result


def sbt_compile_arguments(command):
    from .jvm_sbt_cli import public_arguments
    original_cli = public_arguments(command_arguments(command, "/oracle-tools/sbt"))
    if original_cli is not None:
        return original_cli
    result, build = [], []
    for token in command_arguments(command, "/oracle-tools/sbt"):
        if token in {"-batch", "-no-colors"} or token.startswith(("-D", "-J", "++")):
            result.append(token)
            continue
        if token.startswith("-"):
            raise ContractError("sbt public build needs an explicit option adapter: " + token)
        if token == "set Global / enableWasmEverywhere := true":
            if build or token in result:
                raise ContractError("sbt WebAssembly setting must occur once before compilation targets")
            result.append(token)
            continue
        head = token.split()[0]
        if head in {"test", "testOnly", "testQuick"}:
            build.append("compile")
        elif re.fullmatch(r"[\w.-]+/(?:Test/)?(?:test|testOnly|testQuick)", head):
            build.append(head.split("/", 1)[0] + "/Compile/compile")
        elif re.fullmatch(r"(?:[\w.-]+/)?(?:Compile/)?compile", head):
            build.append(head)
        else:
            raise ContractError("sbt custom/runMain command needs a whole-candidate build adapter")
    if not build:
        raise ContractError("sbt command has no public compilation target")
    return [*result, *dict.fromkeys(build)]


def sbt_test_dependency_arguments(command):
    """Resolve the original test configurations without running test selectors."""
    public = sbt_compile_arguments(command)
    options = [arg for arg in public if arg.startswith(("-", "++"))
               or arg == "set Global / enableWasmEverywhere := true"]
    targets = []
    for token in command_arguments(command, "/oracle-tools/sbt"):
        head = token.split()[0]
        if head in {"test", "testOnly", "testQuick"}:
            targets.append("Test/update")
        elif re.fullmatch(r"[\w.-]+/(?:Test/)?(?:test|testOnly|testQuick)", head):
            targets.append(head.split("/", 1)[0] + "/Test/update")
    return [*options, *dict.fromkeys(targets)] if targets else []


def gradle_compile_arguments(command, executable):
    arguments = command_arguments(command, executable)
    result, targets = [], []
    index = 0
    valued = {"--init-script", "-I", "-x", "--exclude-task", "--project-dir", "-p", "--project-cache-dir"}
    while index < len(arguments):
        token = arguments[index]
        if token == "--tests":
            if index + 1 >= len(arguments):
                raise ContractError("Gradle test selector has no value")
            index += 2
            continue
        if token in valued:
            if index + 1 >= len(arguments):
                raise ContractError("Gradle option has no value")
            result.extend(arguments[index:index + 2])
            index += 2
            continue
        if token.startswith("-"):
            result.append(token)
        else:
            prefix, separator, goal = token.rpartition(":")
            if goal in {"test", "testClasses", "integrationTest", "acceptanceTest", "oracleSessionTest"}:
                targets.append((prefix + separator if separator else "") + "classes")
            elif goal in {"clean", "cleanTest", "cleanAcceptanceTest"}:
                pass
            else:
                raise ContractError("Gradle specialized task needs a production build mapping: " + token)
        index += 1
    if not targets:
        raise ContractError("Gradle command has no production build target")
    return [*result, *dict.fromkeys(targets)]


def gradle_test_targets(command, executable):
    arguments = command_arguments(command, executable)
    targets, index = [], 0
    valued = {"--tests", "--init-script", "-I", "-x", "--exclude-task", "--project-dir", "-p", "--project-cache-dir"}
    while index < len(arguments):
        token = arguments[index]
        if token in valued:
            index += 2
            continue
        if not token.startswith("-") and token.rsplit(":", 1)[-1] in {
                "test", "integrationTest", "acceptanceTest", "oracleSessionTest"}:
            targets.append(":" + token.lstrip(":"))
        index += 1
    if not targets:
        raise ContractError("Gradle preparation has no test classpath target")
    return list(dict.fromkeys(targets))


def scratch_directories(names, build_system):
    candidates = {".gradle"} if build_system == "gradle" else {".bsp", ".sbt"}
    for name in names:
        basename = Path(name).name
        if ((build_system == "gradle" and basename in {"build.gradle", "build.gradle.kts"})
                or (build_system == "sbt" and basename == "build.sbt")):
            candidates.add((Path(name).parent / ("build" if build_system == "gradle" else "target")).as_posix())
    return sorted(path for path in candidates
                  if not any(name == path or name.startswith(path + "/") for name in names))


def managed_runtime(ctx, task, oracle, command):
    system = oracle.get("environment", {}).get("build_system")
    if "/task/session-setup/" in command or "git diff --cached" in command or "emulator" in command:
        raise ContractError("JVM source replacement/partial build/emulator requires a candidate-aware adapter")
    names = source_files(ctx, task)
    home = ctx.legacy / "jvm/cache/home"
    mirrors = {}
    prepared = ctx.config.get("migration", {}).get("gradle_cache_root")
    if system == "gradle" and prepared:
        receipt = Path(prepared) / component(task["id"]) / "preparation.json"
        if not receipt.is_file():
            raise ContractError("prepared Gradle cache has no task binding")
        record = json.loads(receipt.read_text())
        if (record.get("id") != task["id"] or record.get("start_sha") != task["start_sha"]
                or record.get("oracle_fingerprint") != canonical_hash(oracle)):
            raise ContractError("prepared Gradle cache belongs to another frozen task")
        mirrors = repository_mirrors([key + "=" + value for key, value in record.get("repository_mirrors", {}).items()])
    from .jvm_platforms import configuration
    platform, _ = configuration(ctx)
    use_build_cache = platform.get(task["id"], {}).get("build_cache", False)
    tools = build_tools(ctx, mirrors, force_tests=use_build_cache and system == "gradle")
    assets = [{**tools, "kind": "archive", "visibility": "developer", "target": TOOLS}]
    environment = {
        "TERM": "dumb", "USER": "reviewbench", "LOGNAME": "reviewbench",
        "SPARK_USER": "reviewbench", "HADOOP_USER_NAME": "reviewbench",
    }
    if system == "sbt":
        if "project/build.properties" not in names:
            raise ContractError("sbt project does not declare its launcher version")
        properties = git(ctx.repo(task), "show", task["start_sha"] + ":project/build.properties").decode()
        match = re.search(r"(?m)^sbt\.version\s*=\s*(\S+)\s*$", properties)
        if not match:
            raise ContractError("sbt start launcher version is not declared")
        boot_extra = None
        if not (home / ".sbt/launchers" / match[1] / "sbt-launch.jar").is_file():
            configured = ctx.config.get("migration", {}).get("sbt_boot_root")
            location = Path(configured) / match[1] if configured else None
            receipt = location / "preparation.json" if location else None
            if receipt and receipt.is_file():
                record = json.loads(receipt.read_text())
                launcher = location / "home/.sbt/launchers" / match[1] / "sbt-launch.jar"
                if (record.get("status") != "prepared" or record.get("version") != match[1]
                        or file_hash(launcher) != record.get("launcher_sha256")):
                    raise ContractError("prepared sbt launcher binding changed")
                boot_extra = ctx.bundle(location / "home", sbt_boot_files(location / "home"), role="toolchain")
            else:
                raise ContractError("sbt pinned start launcher is not retained")
        dependency = shared_archive(ctx, "sbt", home, lambda: sbt_cache_files(home))
        assets.append({**dependency, "kind": "archive", "visibility": "developer", "target": SBT_BASE})
        prefix = sbt_prefix()
        public = prefix + " " + shlex.join(sbt_compile_arguments(command))
        roots = [home / ".cache/coursier"]
        prepared = ctx.config.get("migration", {}).get("sbt_dependency_root")
        if prepared:
            roots.append(Path(prepared) / component(task["id"]) / "overlay/.cache/coursier")
        command, classpath = bind_scala_classpath(command, roots)
        if classpath:
            atomic_json(ctx.output / "sbt-classpath-bindings" / (component(task["id"]) + ".json"), classpath)
        command = command.replace("/oracle-tools/sbt", prefix)
        command = command.replace("/cachehome/.cache/coursier", SBT_CACHE + "/.cache/coursier")
        environment.update({
            "COURSIER_CACHE": SBT_CACHE + "/.cache/coursier",
            "COURSIER_PARALLEL_DOWNLOAD_COUNT": "2",
            "JVM_OPTS": sbt_jvm_options(),
            "_JAVA_OPTIONS": sbt_jvm_options(),
            "SBT_OPTS": " ".join([
                "-Dsbt.boot.directory=" + SBT_CACHE + "/.sbt/boot",
                "-Dsbt.global.base=" + SBT_CACHE + "/global",
                "-Dsbt.ivy.home=" + SBT_CACHE + "/ivy",
                "-Dsbt.log.noformat=true", "-Dsbt.supershell=false", "-Dsbt.server.autostart=false",
                "-Dsbt.task.cpus=2", "-Dsbt.coursier.parallel-downloads=2", "-Dsbt.offline=true",
            ]),
        })
        bootstrap = [seed_once(SBT_BASE, SBT_CACHE, dependency["sha256"])]
        if boot_extra:
            target = "/opt/reviewbench-assets/sbt-boot-extra"
            assets.append({**boot_extra, "kind": "archive", "visibility": "developer", "target": target})
            bootstrap.append(seed_once(target, SBT_CACHE, boot_extra["sha256"]))
        prepared = ctx.config.get("migration", {}).get("sbt_dependency_root")
        if prepared:
            location = Path(prepared) / component(task["id"])
            receipt = location / "preparation.json"
            if receipt.exists():
                record = json.loads(receipt.read_text())
                if (record.get("status") != "prepared" or record.get("id") != task["id"]
                        or record.get("start_sha") != task["start_sha"]
                        or record.get("oracle_fingerprint") != canonical_hash(oracle)
                        or record.get("base_asset") != dependency):
                    raise ContractError("prepared sbt dependency overlay has a different task/base binding")
                overlay = location / "overlay"
                manifest = record["overlay_files"]
                if any(file_hash(overlay / repo_path(name)) != digest for name, digest in manifest.items()):
                    raise ContractError("prepared sbt dependency overlay bytes changed")
                from .jvm_ivy import validate_remote_ivy_overlay
                validate_remote_ivy_overlay(overlay, manifest)
                if manifest:
                    delta = ctx.bundle(overlay, sorted(manifest))
                    target = "/opt/reviewbench-assets/sbt-dependency-overlay"
                    assets.append({**delta, "kind": "archive", "visibility": "developer", "target": target})
                    bootstrap.append(seed_once(target, SBT_CACHE, delta["sha256"]))
                for name in record.get("removed_files", []):
                    bootstrap.append("rm -f -- " + shlex.quote(SBT_CACHE + "/" + repo_path(name)))
        caches = {"sbt": SBT_CACHE}
    elif system == "gradle":
        words = shlex.split(command)
        wrappers = [word for word in words if word in {"gradlew", "./gradlew"}]
        if len(wrappers) != 1:
            raise ContractError("Gradle frozen command has no unambiguous checked-in wrapper")
        wrapper = wrappers[0]
        repository = (Path(prepared) / component(task["id"]) / "gradle-home" if prepared else
                      home / "gradle-repos" / re.sub(r"[^A-Za-z0-9_.-]+", "__", task["repo"]).strip("_"))
        if not repository.is_dir():
            raise ContractError("task-specific Gradle download cache is not retained: " + str(repository))
        dependency = shared_archive(ctx, "gradle", repository, lambda: gradle_cache_files(repository),
                                    revision=file_hash(receipt) if prepared else None)
        assets.append({**dependency, "kind": "archive", "visibility": "developer", "target": GRADLE_BASE})
        arguments = gradle_compile_arguments(command, wrapper)
        public = shlex.join(["bash", wrapper, "--offline", "--max-workers=2", *arguments])
        command = command.replace("bash " + wrapper, "bash " + wrapper + " --offline --max-workers=2", 1)
        environment.update({
            "GRADLE_USER_HOME": GRADLE_CACHE,
            "GRADLE_OPTS": "-Dorg.gradle.workers.max=2 -Dorg.gradle.parallel=false -Dorg.gradle.vfs.watch=false",
        })
        bootstrap = [seed_once(GRADLE_BASE, GRADLE_CACHE, dependency["sha256"])]
        caches = {"gradle": GRADLE_CACHE}
    else:
        raise ContractError("unsupported managed JVM build system")
    command = command.replace("/oracle-tools/oracle-gradle.init.gradle", TOOLS + "/oracle-gradle.init.gradle")
    public = public.replace("/oracle-tools/oracle-gradle.init.gradle", TOOLS + "/oracle-gradle.init.gradle")
    build_cache = None
    if use_build_cache:
        if "/workspace" in command or "/workspace" in public:
            raise ContractError("cached JVM build has absolute workspace references needing an explicit relocation adapter")
        helper = ctx.bundle(Path(__file__).parent, ["jvm_build_entry.py"], role="toolchain")
        assets.append({**helper, "kind": "archive", "visibility": "developer", "target": BUILD_TOOLS})
        caches["jvm-build"] = BUILD_CACHE
        build_cache = {"helper": BUILD_TOOLS + "/jvm_build_entry.py", "cache": BUILD_CACHE}
        public = shlex.join([
            "python3", build_cache["helper"], "--workspace", "/workspace", "--cache", BUILD_CACHE,
            "--mode", "developer", "--command", "bash", "-e", "-o", "pipefail", "-c", public,
        ])
    return {"assets": assets, "bootstrap": bootstrap, "env": environment, "cache_mounts": caches,
            "public": public, "command": command, "scratch_paths": scratch_directories(names, system),
            "memory_mb": 8192, "build_scratch_mb": 16384, "build_cache": build_cache}
