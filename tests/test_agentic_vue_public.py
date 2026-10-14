"""Native compiler controls on small fixtures, never benchmark-task repetitions."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

CHECK = Path(__file__).resolve().parents[1] / "scripts/agentic_runtime_migration/vue_public_check.mjs"


@pytest.fixture(scope="session")
def compiler(tmp_path_factory):
    node = Path(os.environ.get("REVIEWBENCH_TEST_NODE", "/nonexistent"))
    modules = Path(os.environ.get("REVIEWBENCH_TEST_VUE_MODULES", "/nonexistent"))
    if not node.is_file() or not (modules / "@vue/compiler-sfc/package.json").is_file():
        pytest.skip("set REVIEWBENCH_TEST_NODE and REVIEWBENCH_TEST_VUE_MODULES to retained native inputs")
    assert subprocess.check_output([str(node), "--version"], text=True) == "v25.6.1\n"
    # Archive roots can have arbitrary host names. Native Node resolution,
    # like the real readonly runtime mount, requires the node_modules layout.
    restored = tmp_path_factory.mktemp("native-vue") / "node_modules"
    shutil.copytree(modules, restored, symlinks=True)
    return node, restored


def run(compiler, source, flags=()):
    node, modules = compiler
    return subprocess.run([str(node), str(CHECK), str(source), str(modules), *flags],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("source", [
    "<template><div>{{ answer }}</div></template><script>export default {data(){return {answer:42}}}</script>",
    '<script setup lang="ts">const answer: number = 42</script><template><div>{{ answer }}</div></template>',
    "<template><p>Hello</p></template>",
    '<script lang="ts">import Vue from "vue"; export default Vue.extend({})</script><template><div/></template>',
])
def test_real_sfc_compiler_accepts_native_options_setup_template_and_legacy_extend(tmp_path, compiler, source):
    file = tmp_path / "Example.vue"
    file.write_text(source)
    before = file.read_bytes()
    result = run(compiler, file)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"VUE_SFC_COMPILE_OK {file}\n"
    assert file.read_bytes() == before


def test_real_compiler_reports_current_candidate_edit_not_a_saved_start(tmp_path, compiler):
    file = tmp_path / "Example.vue"
    file.write_text("<template><div/></template><script>export default {}</script>")
    assert run(compiler, file).returncode == 0
    file.write_text("<template><div/></template><script>export default {broken: }</script>")
    observed = run(compiler, file)
    assert observed.returncode != 0 and "VUE_SFC_COMPILE_OK" not in observed.stdout
    assert "Unexpected token" in observed.stderr


def test_script_only_preserves_original_non_rendering_compiler_boundary(tmp_path, compiler):
    file = tmp_path / "Example.vue"
    file.write_text('<script>export default {}</script><template><div v-if="(">text</div></template>')
    assert run(compiler, file).returncode != 0
    assert run(compiler, file, ["--script-only"]).returncode == 0


def test_public_check_does_not_execute_current_candidate_script(tmp_path, compiler):
    marker = tmp_path / "side-effect"
    file = tmp_path / "Example.vue"
    file.write_text(
        f'<script>import fs from "node:fs"; fs.writeFileSync({str(marker)!r}, "executed"); '
        "export default {}</script><template><div/></template>")
    assert run(compiler, file).returncode == 0
    assert not marker.exists()
