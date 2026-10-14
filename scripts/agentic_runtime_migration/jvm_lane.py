"""Whole-candidate javac/Maven runtimes with private, unchanged JVM assertions."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import xml.etree.ElementTree as ET

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, file_lock
from reviewbench_maintainability.agentic.snapshots import git, tree_files

TOOLS = "/opt/reviewbench-assets/jvm-tools"
PRIVATE = "/opt/reviewbench-assets/jvm-tests"
DEPENDENCIES = "/opt/reviewbench-assets/maven-repository"
HOME = "/workspace/.reviewbench/jvm/home"
MAVEN_CACHE = "/opt/reviewbench-cache/maven"
RESULT_PATH = ".oracle/reviewbench-results.json"
MAVEN_OVERLAY = "/opt/reviewbench-assets/maven-repository-overlay"


def base_dependency_asset(ctx, task):
    package = ctx.config.get("migration", {}).get("maven_base_package")
    if not package:
        return None
    from reviewbench_maintainability.agentic.preparation import load_recipes
    if not Path(package).is_absolute():
        raise ContractError("base Maven runtime package must be absolute")
    recipe = load_recipes(Path(package) / "runtime-recipes.jsonl").get(task["id"])
    if recipe is None or (recipe.repo, recipe.start_sha) != (task["repo"], task["start_sha"]):
        raise ContractError("base Maven dependency recipe belongs to a different task/source")
    assets = [asset for asset in recipe.assets if asset["target"] == DEPENDENCIES]
    if len(assets) != 1 or assets[0].get("kind") != "archive" or assets[0]["visibility"] != "developer":
        raise ContractError("base Maven recipe has no unambiguous public dependency archive")
    result = dict(assets[0])
    path = Path(result["path"])
    if not path.is_absolute():
        result["path"] = str(Path(package) / path)
    return result


def migration_scratch_bytes(ctx, task):
    from reviewbench_maintainability.agentic.assets import asset_descriptor
    base = base_dependency_asset(ctx, task)
    return 4 * 1024**3 + (asset_descriptor(base, ctx.output)["archive"]["input_bytes"] if base else 0)


def validate_dependency_manifest(records, descriptor):
    if (not isinstance(records, list) or len(records) != descriptor["files"]
            or canonical_hash(records) != descriptor["tree_sha256"]):
        raise ContractError("cached Maven manifest differs from its immutable base archive")
    names = []
    for name, digest, executable in records:
        repo_path(name)
        # remote_maven_files only admits regular files; an unexpected link
        # must not be replayed as a dependency-cache deletion or overlay.
        if not isinstance(executable, bool) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ContractError("base Maven dependency manifest has a non-regular entry")
        names.append(name)
    if names != sorted(set(names)):
        raise ContractError("base Maven dependency manifest paths are not unique/sorted")
    return records


def dependency_manifest(ctx, asset):
    """Read a verified file index without repeatedly fetching the base tree."""
    from reviewbench_maintainability.agentic.assets import AssetPool, asset_descriptor
    descriptor = asset_descriptor(asset, ctx.output)
    if descriptor["source_kind"] != "dependency":
        raise ContractError("Maven base asset is not a dependency-only tree")
    path = ctx.output / "maven-base-manifests" / (asset["sha256"] + ".json")
    with file_lock(ctx.output / "locks" / ("maven-manifest-" + asset["sha256"])):
        if path.exists():
            return validate_dependency_manifest(json.loads(path.read_text()), descriptor)
        job = CURRENT_JOB.get()
        if not job:
            raise ContractError("Maven base indexing needs resource admission")
        pool = AssetPool(ctx.cache, ctx.scratch, job["lease"], ctx.output)
        try:
            root = pool.materialize(asset)
            records = [(name, file_hash(file), bool(file.stat().st_mode & 0o111))
                       for name, file in sorted(tree_files(root).items())]
            validate_dependency_manifest(records, descriptor)
            atomic_json(path, records)
        finally:
            pool.close()
    return records


def dependency_delta(repository, names, base_records):
    base = {name: (digest, executable) for name, digest, executable in base_records}
    changed = []
    for name in names:
        path = repository / repo_path(name)
        if path.is_symlink() or not path.is_file():
            raise ContractError("Maven overlay dependency is not a regular file")
        if base.get(name) != (file_hash(path), bool(path.stat().st_mode & 0o111)):
            changed.append(name)
    return sorted(changed), sorted(set(base) - set(names))


def maven_dependency_assets(ctx, task, repository, names):
    base = base_dependency_asset(ctx, task)
    if base is None:
        dependency = ctx.bundle(repository, names)
        return ([{**dependency, "kind": "archive", "visibility": "developer", "target": DEPENDENCIES}],
                ["cp -a " + DEPENDENCIES + "/. " + MAVEN_CACHE])
    from reviewbench_maintainability.agentic.assets import asset_descriptor
    records = dependency_manifest(ctx, base)
    changed, removed = dependency_delta(repository, names, records)
    assets, bootstrap = [base], ["cp -a " + DEPENDENCIES + "/. " + MAVEN_CACHE]
    overlay_bytes = 0
    if changed:
        overlay = ctx.bundle(repository, changed)
        assets.append({**overlay, "kind": "archive", "visibility": "developer", "target": MAVEN_OVERLAY})
        bootstrap.append("cp -a " + MAVEN_OVERLAY + "/. " + MAVEN_CACHE)
        overlay_bytes = asset_descriptor(overlay, ctx.output)["archive"]["input_bytes"]
    if removed:
        # Remove obsolete/now-excluded dependency files only inside this
        # lease's private Maven cache, never candidate source or the base tree.
        for offset in range(0, len(removed), 128):
            bootstrap.append("rm -f -- " + shlex.join(
                MAVEN_CACHE + "/" + repo_path(name) for name in removed[offset:offset + 128]))
    atomic_json(ctx.output / "maven-layering" / (component(task["id"]) + ".json"), {
        "id": task["id"], "base_asset": base, "base_reused": True, "base_files": len(records),
        "changed_files": len(changed), "removed_files": len(removed),
        "base_archive_input_bytes": asset_descriptor(base, ctx.output)["archive"]["input_bytes"],
        "new_archive_input_bytes": overlay_bytes,
    })
    return assets, bootstrap


def remote_maven_files(repository, excluded_coordinates=()):
    """Never expose locally installed task builds as developer dependencies."""
    excluded = set(excluded_coordinates)
    names = set()
    for marker in repository.rglob("_remote.repositories"):
        relative = marker.parent.relative_to(repository)
        parts = relative.parts
        if len(parts) < 3 or (".".join(parts[:-2]), parts[-2]) in excluded:
            continue
        origins = {}
        for line in marker.read_text().splitlines():
            match = re.fullmatch(r"([^#][^>]*)>([^=]*)=.*", line)
            if match:
                origins.setdefault(match[1], set()).add(match[2])
        valid = {name for name, values in origins.items()
                 if values and all(value and not re.search(r"(?i)local|oracle|reviewbench|session", value)
                                   for value in values)}
        if not valid:
            continue
        names.add(marker.relative_to(repository).as_posix())
        for name in valid:
            if "/" in name or "\\" in name or name in {".", ".."}:
                raise ContractError("invalid Maven dependency filename")
            for suffix in ("", ".sha1", ".sha256", ".md5"):
                path = marker.parent / (name + suffix)
                if path.is_file() and not path.is_symlink():
                    names.add(path.relative_to(repository).as_posix())
    # Closed version ranges and plugin prefixes still need remote metadata
    # when the exact JAR/POM is already cached and Maven is running offline.
    excluded_paths = [group.replace(".", "/") + "/" + artifact for group, artifact in excluded]
    for metadata in repository.rglob("maven-metadata-*.xml"):
        origin = metadata.name[len("maven-metadata-"):-len(".xml")]
        relative = metadata.relative_to(repository).as_posix()
        parent = metadata.parent.relative_to(repository).as_posix()
        if (metadata.is_symlink() or re.search(r"(?i)local|oracle|reviewbench|session", origin)
                or any(parent == prefix or parent.startswith(prefix + "/") for prefix in excluded_paths)):
            continue
        try:
            ET.parse(metadata)
        except ET.ParseError as exc:
            raise ContractError("invalid retained remote Maven metadata: " + relative) from exc
        names.add(relative)
        for suffix in (".sha1", ".sha256", ".md5"):
            checksum = Path(str(metadata) + suffix)
            if checksum.is_file() and not checksum.is_symlink():
                names.add(checksum.relative_to(repository).as_posix())
    if not names:
        raise ContractError("JVM retained remote-dependency manifest is empty")
    return sorted(names)


def dependency_repository(ctx, task):
    configured = ctx.config.get("migration", {}).get("maven_cache_root")
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise ContractError("prepared Maven cache root must be absolute")
        path = root / component(task["id"]) / "repository"
        if not path.is_dir():
            raise ContractError("prepared task-specific Maven cache is missing: " + str(path))
        return path
    return ctx.legacy / "jvm/cache/home/.m2/repository"


def project_coordinates(repository, start):
    paths = [name.decode() for name in git(repository, "ls-tree", "-r", "--name-only", "-z", start).split(b"\0")
             if name and (name == b"pom.xml" or name.endswith(b"/pom.xml"))]
    coordinates = set()
    root_group = None
    for name in sorted(paths, key=lambda value: (value.count("/"), value)):
        try:
            root = ET.fromstring(git(repository, "show", start + ":" + name))
        except ET.ParseError:
            continue  # Embedded POM test fixtures need not be valid projects.
        text = lambda path: root.findtext(path, namespaces={"m": "http://maven.apache.org/POM/4.0.0"})
        group = text("m:groupId") or text("m:parent/m:groupId") or root_group
        artifact = text("m:artifactId")
        if name == "pom.xml":
            root_group = group
        if group and artifact and "${" not in group + artifact:
            coordinates.add((group, artifact))
    return coordinates


def maven_tools(ctx):
    pointer = ctx.output / "jvm-assets/maven-tools.json"
    with file_lock(ctx.output / "locks/maven-tools"):
        if pointer.is_file():
            return json.loads(pointer.read_text())
        old = ctx.legacy / "jvm/cache/tools"
        with tempfile.TemporaryDirectory(prefix="jvm-tools-", dir=ctx.scratch) as temporary:
            root = Path(temporary)
            for name in ("maven", "java", "etc-maven"):
                shutil.copytree(old / name, root / name, symlinks=True)
            relocate_maven_links(root)
            # Offline builds do not need any authenticated Maven server.
            settings = root / "etc-maven/settings.xml"
            if settings.is_file():
                xml = ET.parse(settings)
                for server in xml.getroot().iter():
                    if server.tag.rsplit("}", 1)[-1] in {"password", "privateKey", "passphrase"} and server.text:
                        raise ContractError("JVM tool settings contain a non-public credential")
            result = ctx.bundle(root, role="toolchain")
        atomic_json(pointer, result)
        return result


def relocate_maven_links(root):
    """Translate Debian's two configuration links into the sealed tool tree."""
    for relative, suffix in (("maven/conf", ""), ("maven/bin/m2.conf", "m2.conf")):
        link = root / relative
        if not link.is_symlink():
            continue
        expected = "/etc/maven" + ("/" + suffix if suffix else "")
        if os.readlink(link) != expected:
            raise ContractError("unexpected absolute Maven configuration link")
        destination = root / "etc-maven" / suffix
        if not destination.exists():
            raise ContractError("retained Maven configuration target is absent")
        link.unlink()
        link.symlink_to(os.path.relpath(destination, link.parent))


def maven_compile_command(test_arguments, *, build_test_fixtures=False, build_plugin_descriptors=False):
    args = shlex.split(test_arguments)
    if not args or args[-1] != "test" or any(value in {"&&", ";", "|", "||"} for value in args):
        raise ContractError("JVM Maven shell sequence requires an explicit production build adapter")
    compile_args = [value for value in args[:-1] if not value.startswith("-Dtest=")]
    # Some reactors consume sibling test-jar artifacts even while compiling
    # production modules. Build those from this same candidate; do not borrow
    # previously installed artifacts from a different source revision.
    skip = "-DskipTests=true" if build_test_fixtures else "-Dmaven.test.skip=true"
    # A reactor's own Maven plugin must generate META-INF/maven/plugin.xml
    # before a downstream module can execute it. Stopping at compile leaves
    # that descriptor missing even though the plugin classes were built.
    goal = "package" if build_test_fixtures else "process-classes" if build_plugin_descriptors else "compile"
    return TOOLS + "/maven/bin/mvn " + shlex.join(
        ["-o", "-Dmaven.repo.local=" + MAVEN_CACHE, skip, *compile_args, goal])


def accepted_reference(ctx, task, oracle, directory):
    reference = ctx.golden(task, oracle, "jvm", directory)
    expected = oracle["source_only_patch"]["sha256"]
    if reference["sha256"] == expected:
        return reference
    paths = oracle.get("source_only_production_files")
    if not isinstance(paths, list) or not paths or any(not isinstance(name, str) for name in paths):
        raise ContractError("JVM accepted source-only reference digest differs")
    paths = [repo_path(name) for name in paths]
    rebuilt = git(ctx.repo(task), "diff", "--binary", task["start_sha"], task["target_sha"], "--", *paths)
    if hashlib.sha256(rebuilt).hexdigest() != expected:
        raise ContractError("reconstructed JVM reference does not match its frozen digest")
    output = ctx.output / "jvm-reference-patches" / (expected + ".patch")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(rebuilt)
    atomic_json(output.with_suffix(".json"), {
        "id": task["id"], "frozen_sha256": expected, "original_asset": reference,
        "source_paths": paths, "method": "git diff --binary start target -- frozen-production-paths",
    })
    return ctx.copy_asset(output, "golden")


def frozen_non_p2p_statuses(oracle):
    """Admit only deviations explicitly outside the original fixed P2P set."""
    required = set(oracle.get("pass_to_pass_tests", []))
    groups = [oracle.get("start_run_records", []), oracle.get("golden_run_records", [])]
    if not required or any(not isinstance(group, list) or len(group) != 3 for group in groups):
        return {}
    allowed = {}
    for row in [*groups[0], *groups[1]]:
        statuses = row.get("statuses", {})
        if any(statuses.get(name) != "PASSED" for name in required):
            return {}
        failed = {name for name, status in statuses.items() if status == "FAILED"}
        if failed & required or (row.get("exit_code") not in (None, 0) and not failed):
            return {}
        for name, status in statuses.items():
            if name not in required and status in {"FAILED", "SKIPPED"}:
                allowed.setdefault(name, set()).add(status)
    return {name: sorted(values) for name, values in sorted(allowed.items())}


def frozen_non_p2p_exit_codes(oracle):
    """Bind tolerated test-process failures to all six retained exit records."""
    if not frozen_non_p2p_statuses(oracle):
        return []
    rows = [*oracle["start_run_records"], *oracle["golden_run_records"]]
    codes = [row.get("exit_code") for row in rows]
    # A missing exit status, timeout, launcher error or termination signal is
    # not evidence of an ordinary test assertion failure.
    if any(type(code) is not int or not 0 <= code < 124 for code in codes):
        return []
    return sorted({code for code in codes if code != 0})


def toolchain_environment(environment):
    """Accept only prepared JDKs and the explicit fixed Node/SBT tool variants."""
    image_name = environment.get("container", "")
    node = image_name in {"oracle-java-tools:17-node", "oracle-java-tools:21-node"}
    if (not re.fullmatch(r"oracle-java-tools:(8|11|17|21)", image_name)
            and not (node and environment.get("build_system") == "sbt")):
        raise ContractError("JVM specialized emulator/Node/service toolchain adapter pending")
    return image_name


def historical_host_assumptions(command):
    """Recognize actual historical path roots, not Java package components."""
    return (
        "git diff --cached" in command
        or re.search(r"(?<![A-Za-z0-9_./\\-])/(?:data|home|oracle-tools)(?:/|$)", command) is not None
    )


def migrate(ctx, task, oracle):
    legacy = ctx.config.get("migration", {}).get("jvm_original_legacy_gradle_inputs", {}).get(task["id"])
    if legacy:
        from .jvm_legacy_gradle import migrate as migrate_original_gradle
        return migrate_original_gradle(ctx, task, oracle, legacy)
    transport = ctx.config.get("migration", {}).get("jvm_docker_lifecycle_inputs", {}).get(task["id"])
    if transport:
        from .jvm_testcontainers_services import migrate as migrate_original_services
        return migrate_original_services(ctx, task, oracle, transport)
    environment = oracle.get("environment", {})
    image_name = toolchain_environment(environment)
    command = oracle.get("command")
    if not isinstance(command, str):
        raise ContractError("JVM frozen command is absent")
    image, audit = ctx.image(image_name)
    directory = ctx.task_directory(task, oracle, "jvm")
    expected = oracle.get("pass_to_pass_tests")
    if not expected:
        raise ContractError("JVM frozen JUnit identities are absent")
    patch = accepted_reference(ctx, task, oracle, directory)
    assets, bootstrap, cache_mounts = [], ["mkdir -p " + HOME + " /workspace/.reviewbench/tmp"], {}
    grade_bootstrap = []
    managed_env, extra_scratch = {}, []
    build_cache = None
    original_cli = None
    original_sbt_cli = None
    memory, scratch = 4096, 4096
    if task["repo"] == "jszufa/java-console-game":
        public = ("mkdir -p /workspace/.reviewbench/jvm/public-classes && javac "
                  "-d /workspace/.reviewbench/jvm/public-classes $(find src -name '*.java' ! -name '*Test.java')")
    elif task["repo"] == "boyland/sasylf":
        javacc = Path("net/java/dev/javacc/javacc/4.0/javacc-4.0.jar")
        dependency_root = ctx.legacy / "jvm/cache/home/.m2/repository"
        if not (dependency_root / javacc).is_file():
            dependency_root = ctx.output / "recovered-dependencies"
            receipt = json.loads((dependency_root / javacc).with_suffix(".download.json").read_text())
            if (receipt["sha256"] != file_hash(dependency_root / javacc)
                    or receipt["url"] != "https://repo.maven.apache.org/maven2/" + javacc.as_posix()):
                raise ContractError("recovered pinned JavaCC dependency differs from its receipt")
        dependency = ctx.bundle(dependency_root, [javacc.as_posix()])
        assets.append({**dependency, "kind": "archive", "visibility": "developer", "target": DEPENDENCIES})
        command = command.replace("/cachehome/.m2/repository", DEPENDENCIES)
        public = ("mkdir -p bin && cd src/edu/cmu/cs/sasylf/parser && java -cp " +
                  DEPENDENCIES + "/" + javacc.as_posix() + " javacc parser.jj && "
                  "cd /workspace/src && javac -source 8 -target 8 -classpath ../bin:. "
                  "-d ../bin edu/cmu/cs/sasylf/Main.java")
    elif environment.get("build_system") == "maven" and command.count("/oracle-tools/maven/bin/mvn") == 1:
        if "/task/session-tests/" in command and "bash /task/session-tests/" in command:
            raise ContractError("JVM legacy partial-source recompile adapter needs a whole-project rebuild")
        suffix = command.split("/oracle-tools/maven/bin/mvn", 1)[1]
        needs_test_fixtures = task["repo"] == "apache/hive"
        public = maven_compile_command(suffix, build_test_fixtures=needs_test_fixtures,
                                       build_plugin_descriptors=task["repo"] == "apache/pinot")
        if needs_test_fixtures:
            grade_bootstrap.append(public)
        repository_cache = dependency_repository(ctx, task)
        names = remote_maven_files(repository_cache,
                                   project_coordinates(ctx.repo(task), task["start_sha"]))
        dependencies, dependency_bootstrap = maven_dependency_assets(ctx, task, repository_cache, names)
        assets += [*dependencies, {**maven_tools(ctx), "kind": "archive", "visibility": "developer", "target": TOOLS}]
        cache_mounts["maven"] = MAVEN_CACHE
        bootstrap.extend(dependency_bootstrap)
        command = command.replace("/oracle-tools/maven/bin/mvn",
                                  TOOLS + "/maven/bin/mvn -o -Dmaven.repo.local=" + MAVEN_CACHE)
        memory, scratch = 8192, 12288
    elif environment.get("build_system") == "maven" and task["repo"] == "cefriel/mapping-template":
        from .jvm_cli import runtime as original_cli_runtime
        original_cli = original_cli_runtime(ctx, task, oracle, directory)
        assets.extend(original_cli["assets"])
        bootstrap.extend(original_cli["bootstrap"])
        cache_mounts.update(original_cli["cache_mounts"])
        public, command = original_cli["public"], original_cli["command"]
        memory, scratch = original_cli["memory_mb"], original_cli["build_scratch_mb"]
    elif environment.get("build_system") == "maven" and task["repo"] == "TimefoldAI/timefold-solver":
        from .jvm_timefold import runtime as timefold_runtime
        wrapped = timefold_runtime(ctx, task, command)
        assets.extend(wrapped["assets"])
        bootstrap.extend(wrapped["bootstrap"])
        cache_mounts.update(wrapped["cache_mounts"])
        public, command = wrapped["public"], wrapped["command"]
        memory, scratch = wrapped["memory_mb"], wrapped["build_scratch_mb"]
    elif environment.get("build_system") in {"sbt", "gradle"}:
        if environment.get("build_system") == "sbt":
            from .jvm_sbt_cli import plan as original_sbt_cli_plan
            original_sbt_cli = original_sbt_cli_plan(task, oracle, directory)
        from .jvm_managed import managed_runtime
        managed = managed_runtime(ctx, task, oracle, command)
        assets.extend(managed["assets"])
        bootstrap.extend(managed["bootstrap"])
        cache_mounts.update(managed["cache_mounts"])
        managed_env.update(managed["env"])
        extra_scratch = managed["scratch_paths"]
        public, command = managed["public"], managed["command"]
        memory, scratch = managed["memory_mb"], managed["build_scratch_mb"]
        build_cache = managed.get("build_cache")
    else:
        raise ContractError("JVM Gradle/sbt/custom-build dependency adapter pending")
    command = command.replace("/task/", PRIVATE + "/").replace("/cachehome", HOME)
    if historical_host_assumptions(command):
        raise ContractError("JVM command still contains historical host/partial-build assumptions")
    with tempfile.TemporaryDirectory(prefix="jvm-harness-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        for name in ("session-tests", "session-setup"):
            if (directory / name).is_dir():
                shutil.copytree(directory / name, root / name, symlinks=False)
        runner = "jvm_sbt_cli_entry.py" if original_sbt_cli else "jvm_oracle_entry.py"
        shutil.copyfile(Path(__file__).with_name(runner), root / "runner.py")
        if original_cli:
            shutil.copyfile(Path(__file__).with_name("jvm_cli_entry.py"), root / "cli_entry.py")
            (root / "cli-spec.json").write_text(json.dumps(original_cli["spec"], indent=2) + "\n")
        (root / "spec.json").write_text(json.dumps({
            "command": command, "expected_tests": expected,
            "frozen_non_p2p_statuses": frozen_non_p2p_statuses(oracle),
            "frozen_non_p2p_exit_codes": frozen_non_p2p_exit_codes(oracle),
            **({"original_sbt_cli": original_sbt_cli} if original_sbt_cli else {}),
            **({"build_cache": build_cache} if build_cache else {}),
        }, indent=2))
        harness = ctx.bundle(root, role="grader")
    assets.append({**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE})
    from .jvm_platforms import runtime_inputs
    platform = runtime_inputs(ctx, task)
    assets.extend(platform["assets"])
    managed_env.update(platform["env"])
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="jvm",
        image=image, image_audit=audit, cpus=2, memory_mb=memory, build_scratch_mb=scratch,
        command_timeout_seconds=1800, submodules=ctx.modules(task), assets=assets, cache_mounts=cache_mounts,
        sidecars=platform["sidecars"],
        scratch_paths=[".oracle", ".oracle-build", *extra_scratch],
        env={"HOME": HOME, "TMPDIR": "/workspace/.reviewbench/tmp", "CI": "true",
             "JAVA_TOOL_OPTIONS": "-Duser.home=" + HOME + " -XX:ActiveProcessorCount=2 -Xmx3g",
             "MAVEN_OPTS": "-Xmx3g -XX:ActiveProcessorCount=2", **managed_env},
        bootstrap=bootstrap, dev_checks=[public],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": grade_bootstrap,
                 "commands": [{"command": "python3 " + PRIVATE + "/runner.py --spec " + PRIVATE +
                               "/spec.json --output /workspace/" + RESULT_PATH,
                               "parser": "json", "report_path": RESULT_PATH,
                               "expected_tests": expected}]},
    )
    recipe.validate()
    return recipe
