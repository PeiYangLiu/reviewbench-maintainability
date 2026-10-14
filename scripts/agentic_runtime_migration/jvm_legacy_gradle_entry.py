"""Execute original Gradle assertions against freshly compiled candidate code."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def original_environment():
    value = dict(os.environ)
    value.pop("CI", None)
    value.pop("ORACLE_JCENTER_MIRROR", None)
    return value


def mapped_original_init(original, agent, mirror, execution):
    changes = {
        "'/oracle-cache/jacocoagent.jar'": repr(str(agent)),
        "'/oracle-cache/m2-mirror'": repr(str(mirror)),
        "'/task/start.exec'": repr(str(execution)),
    }
    value = original
    for old, new in changes.items():
        if value.count(old) != 1:
            raise ValueError("original Gradle init path binding changed")
        value = value.replace(old, new)
    for old, new in reversed(list(changes.items())):
        value = value.replace(new, old)
    if value != original:
        raise ValueError("Gradle init reconstruction changed executable logic")
    value = original
    for old, new in changes.items():
        value = value.replace(old, new)
    return value


def fresh_compile_init(source):
    """Compile the one original fixture, never reuse pre-candidate test bytecode."""
    return """allprojects { project ->
    project.pluginManager.withPlugin('java') {
        if (project.tasks.findByName('reviewbenchCompileOriginalOracle') != null) {
            throw new GradleException('original private compile task name conflict')
        }
        def originalCompile = project.tasks.named('compileTestJava', JavaCompile).get()
        def freshCompile = project.tasks.register('reviewbenchCompileOriginalOracle', JavaCompile) { task ->
            task.dependsOn(project.tasks.named('classes'), project.tasks.named('processTestResources'))
            task.source(new File(%s))
            task.classpath = project.sourceSets.test.compileClasspath
            task.destinationDirectory.set(originalCompile.destinationDirectory)
            // Preserve the provider: Gradle 7 permits an unset compiler.
            task.javaCompiler.set(originalCompile.javaCompiler)
            task.outputs.upToDateWhen { false }
        }
        // Compatibility getters can finalize the compiler provider. Read them
        // only after project configuration has selected its actual compiler.
        project.gradle.projectsEvaluated {
            freshCompile.configure { task ->
                task.sourceCompatibility = originalCompile.sourceCompatibility
                task.targetCompatibility = originalCompile.targetCompatibility
                task.options.encoding = originalCompile.options.encoding
                task.options.compilerArgs.addAll(originalCompile.options.compilerArgs)
            }
        }
    }
}
""" % json.dumps(str(source))


def install_fixture(root, spec, directory):
    source = directory / spec["original_fixture"]
    if source.is_symlink() or digest(source) != spec["fixture_sha256"]:
        raise ValueError("original private Java fixture changed")
    relative = Path(spec["source_relative_path"])
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".java":
        raise ValueError("invalid original fixture target")
    target = root / relative
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("original fixture target traverses a symlink")
    if target.exists() and target.read_bytes() != source.read_bytes():
        raise ValueError("candidate shadows the original private fixture")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return target


def execute(spec, directory, root, parser, *, run=subprocess.run):
    source = install_fixture(root, spec, directory)
    original = directory / "oracle.init.gradle"
    agent = directory / "jacocoagent.jar"
    if digest(original) != spec["original_init_sha256"] or digest(agent) != spec["jacoco_agent_sha256"]:
        raise ValueError("original Gradle instrumentation changed")
    private = root / ".oracle/legacy-gradle"
    private.mkdir(parents=True, exist_ok=True)
    mapped = private / "original.init.gradle"
    mapped.write_text(mapped_original_init(original.read_text(), agent, spec["mirror"], private / "start.exec"))
    for path in parser.report_files(root):
        path.unlink()
    environment = original_environment()
    commands = []
    if spec["fresh_private_compile"]:
        helper = private / "fresh-compile.init.gradle"
        helper.write_text(fresh_compile_init(source))
        command = ["bash", "./gradlew", "--no-daemon", "--console=plain", "--offline",
                   "-I", str(mapped), "-I", str(helper), "reviewbenchCompileOriginalOracle"]
        result = run(command, cwd=root, env=environment)
        commands.append({"stage": "fresh_original_private_compile", "command": command, "exit_status": result.returncode})
        if result.returncode:
            return result.returncode, {}, commands
    for path in parser.report_files(root):
        path.unlink()
    command = spec["original_command"]
    if command.count("/oracle-cache/oracle.init.gradle") != 1 or command.count("bash ./gradlew") != 1:
        raise ValueError("original Gradle command binding changed")
    command = command.replace("/oracle-cache/oracle.init.gradle", shlex.quote(str(mapped)))
    command = command.replace("bash ./gradlew", "bash ./gradlew --offline", 1)
    argv = ["bash", "-e", "-o", "pipefail", "-c", command]
    result = run(argv, cwd=root, env=environment)
    commands.append({"stage": "original_scored_command", "command": argv, "exit_status": result.returncode})
    observed = parser.read_statuses(parser.report_files(root))
    return result.returncode, observed, commands


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    if spec["schema_version"] != "reviewbench-original-legacy-gradle-execution/v1":
        raise ValueError("unknown original Gradle execution schema")
    loader = importlib.util.spec_from_file_location("original_junit_reports", args.spec.parent / "reports.py")
    reports = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(reports)
    code, observed, commands = execute(spec, args.spec.parent, Path.cwd(), reports)
    expected = spec["expected_tests"]
    complete = code == 0 and set(observed) == set(expected)
    tests = [{"id": name, "status": "passed" if complete and observed.get(name) == "PASSED" else "failed"}
             for name in expected]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "schema_version": "reviewbench-exec-test-results/v1", "tests": tests,
        "original_exit_code": code, "observed_statuses": observed,
        "original_fixture_sha256": spec["fixture_sha256"],
        "fresh_private_compilation": spec["fresh_private_compile"], "commands": commands,
    }, indent=2) + "\n")
    passed = sum(row["status"] == "passed" for row in tests)
    print("Fixed original legacy Gradle tests: " + str(passed) + "/" + str(len(expected)), flush=True)
    return 0 if tests and passed == len(expected) else (code or 1)


if __name__ == "__main__":
    raise SystemExit(main())
