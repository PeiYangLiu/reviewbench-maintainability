"""Optional real, offline Gradle regressions for the private compile adapter."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_runner(source):
    path = source / "scripts/agentic_runtime_migration/jvm_legacy_gradle_entry.py"
    spec = importlib.util.spec_from_file_location("compiler_fixture_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_original_compiler_provider_is_forwarded_lazily():
    text = load_runner(ROOT).fresh_compile_init("/workspace/original.java")
    assert "task.javaCompiler.set(originalCompile.javaCompiler)" in text
    assert "originalCompile.javaCompiler.get()" not in text


def run_real_fixture(source, root, mode, distribution, image):
    """Execute the generated init script, including fresh recompilation.

    An unrelated broken test must remain excluded. The private fixture inlines
    a public constant, so its changed bytecode proves that it was recompiled
    after the public implementation changed, not copied from an older run.
    """
    assert mode in {"unset", "explicit", "deferred"}
    assert distribution.name == "gradle-7.6.1"
    assert (distribution / "bin/gradle").is_file()
    assert image.startswith("sha256:") and len(image) == 71
    root.mkdir(parents=True, exist_ok=False)
    for name in ["home", "tmp", "src/main/java/example", "src/test/java/oracle",
                 "src/test/java/unrelated"]:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "settings.gradle").write_text("rootProject.name = 'compiler-provider-fixture'\n")
    configuration = """
plugins { id 'java' }
sourceCompatibility = JavaVersion.VERSION_1_8
targetCompatibility = JavaVersion.VERSION_1_8
tasks.named('compileTestJava', JavaCompile) {
    options.encoding = 'UTF-8'
}
"""
    if mode == "deferred":
        configuration += """
// Realize the helper before selecting the original compiler.
tasks.named('reviewbenchCompileOriginalOracle').get()
"""
    if mode != "unset":
        configuration += """
tasks.named('compileTestJava', JavaCompile) {
    javaCompiler.set(javaToolchains.compilerFor {
        languageVersion = JavaLanguageVersion.of(17)
    })
}
"""
    configuration += """
gradle.projectsEvaluated {
    def original = tasks.named('compileTestJava', JavaCompile).get()
    def fresh = tasks.named('reviewbenchCompileOriginalOracle', JavaCompile).get()
    fresh.doFirst {
        assert fresh.javaCompiler.isPresent() == original.javaCompiler.isPresent()
        if (original.javaCompiler.isPresent()) {
            assert fresh.javaCompiler.get().executablePath.asFile ==
                   original.javaCompiler.get().executablePath.asFile
        }
        assert fresh.sourceCompatibility == original.sourceCompatibility
        assert fresh.targetCompatibility == original.targetCompatibility
        assert fresh.options.encoding == original.options.encoding
        println 'COMPILER_SELECTION_PRESERVED=true'
        println 'ORIGINAL_TARGET_MAJOR=' + JavaVersion.toVersion(original.targetCompatibility).majorVersion
    }
}
"""
    (root / "build.gradle").write_text(configuration)
    main = root / "src/main/java/example/Value.java"
    fixture = root / "src/test/java/oracle/OriginalFixture.java"
    fixture.write_text(
        "package oracle;\npublic class OriginalFixture {\n"
        " public static void main(String[] args) {\n"
        '  System.out.println("FRESH_VALUE=" + example.Value.CURRENT);\n'
        " }\n}\n")
    (root / "src/test/java/unrelated/Broken.java").write_text("not valid Java\n")
    (root / "fresh.init.gradle").write_text(
        load_runner(source).fresh_compile_init("/workspace/src/test/java/oracle/OriginalFixture.java"))
    prefix = [
        "docker", "run", "--rm", "--network=none", "--read-only",
        "--cpus=2", "--memory=1536m", "--memory-swap=1536m", "--pids-limit=128",
        "--user", str(os.getuid()) + ":" + str(os.getgid()),
        "--mount", "type=bind,source=" + str(root) + ",target=/workspace",
        "--mount", "type=bind,source=" + str(root / "tmp") + ",target=/tmp",
        "--mount", "type=bind,source=" + str(distribution) + ",target=/opt/gradle,readonly",
        "--workdir", "/workspace",
        "--env", "HOME=/workspace/home",
        "--env", "GRADLE_USER_HOME=/workspace/home/gradle",
        "--env", "JAVA_TOOL_OPTIONS=-XX:ActiveProcessorCount=2 -Duser.home=/workspace/home",
        "--env", "GRADLE_OPTS=-Dorg.gradle.workers.max=2 -Dorg.gradle.java.installations.auto-download=false",
        image,
    ]
    command = [
        *prefix, "bash", "-e", "-o", "pipefail", "-c",
        "/opt/gradle/bin/gradle --version && "
        "/opt/gradle/bin/gradle --no-daemon --console=plain --offline "
        "-I /workspace/fresh.init.gradle reviewbenchCompileOriginalOracle && "
        "java -cp build/classes/java/main:build/classes/java/test oracle.OriginalFixture",
    ]
    records = []
    bytecode = []
    for value in [7, 8]:
        main.write_text("package example;\npublic class Value {\n"
                        " public static final int CURRENT = " + str(value) + ";\n}\n")
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        record = {"command": command, "input": {"mode": mode, "public_constant": value},
                  "stdout": result.stdout, "stderr": result.stderr, "exit_status": result.returncode}
        records.append(record)
        (root / ("command-" + str(value) + ".json")).write_text(json.dumps(record, indent=2) + "\n")
        if result.returncode:
            return {"status": "failed", "mode": mode, "commands": records, "bytecode": bytecode}
        assert "Gradle 7.6.1" in result.stdout
        assert "COMPILER_SELECTION_PRESERVED=true" in result.stdout
        assert "FRESH_VALUE=" + str(value) in result.stdout
        blob = (root / "build/classes/java/test/oracle/OriginalFixture.class").read_bytes()
        targets = re.findall(r"^ORIGINAL_TARGET_MAJOR=(\d+)$", result.stdout, re.MULTILINE)
        assert len(targets) == 1
        assert blob[:4] == b"\xca\xfe\xba\xbe"
        assert int.from_bytes(blob[6:8], "big") == 44 + int(targets[0])
        assert not (root / "build/classes/java/test/unrelated/Broken.class").exists()
        bytecode.append(hashlib.sha256(blob).hexdigest())
    assert bytecode[0] != bytecode[1]
    return {"status": "passed", "mode": mode, "commands": records, "bytecode": bytecode,
            "fresh_values": [7, 8], "compiler_selection_preserved": True,
            "unrelated_tests_not_compiled": True, "network": "none"}


@pytest.mark.parametrize("mode", ["unset", "explicit", "deferred"])
def test_real_gradle_compiler_selection_and_fresh_bytecode(tmp_path, mode):
    distribution = os.environ.get("LEGACY_GRADLE_TEST_DISTRIBUTION")
    image = os.environ.get("LEGACY_GRADLE_TEST_IMAGE")
    if not distribution or not image:
        pytest.skip("requires retained Gradle 7.6.1 and the digest-pinned JDK17 image")
    result = run_real_fixture(ROOT, tmp_path / "real-gradle", mode, Path(distribution), image)
    assert result["status"] == "passed", result
