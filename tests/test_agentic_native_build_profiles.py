from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_agentic_toolchains import NATIVE_FILES, build_profile, limit_build_jobs
from prepare_agentic_managed_toolchains import managed_build_profile, bounded_mxml_source


def test_metadata_only_image_has_small_bounded_normal_admission():
    profile = build_profile("FROM pinned-image\nENV CC=gcc-11\nENV CXX=g++-11\n")
    assert profile == {"kind": "metadata-only", "cpus": 1, "memory_gib": 1,
                       "build_memory_gib": 1, "scratch_gib": 1, "heavy": False}


def test_literal_apt_layers_keep_global_limits_without_waiting_for_compilation_slots():
    profile = build_profile("""FROM pinned-image
RUN apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
 libeigen3-dev libspdlog-dev \\
 && rm -rf /var/lib/apt/lists/*
""")
    assert profile["kind"] == "apt-install" and not profile["heavy"]
    assert profile["cpus"] == 2 and profile["memory_gib"] == 4 and profile["scratch_gib"] == 6
    assert profile["build_memory_gib"] <= profile["memory_gib"]


@pytest.mark.parametrize("tool", ["conan==2.0.14", "cmake==3.31.6"])
def test_exact_python_build_tool_install_has_bounded_normal_admission(tool):
    profile = build_profile(f"""FROM pinned-image
RUN apt-get update && apt-get install -y python3-pip \\
 && pip3 install --no-cache-dir {tool} && rm -rf /var/lib/apt/lists/*
""")
    assert profile == {"kind": "pinned-python-tool", "cpus": 2, "memory_gib": 4,
                       "build_memory_gib": 4, "scratch_gib": 8, "heavy": False}


def test_rippled_tool_image_uses_only_its_original_toolchain_dockerfile():
    assert NATIVE_FILES["oracle-rippled-modern:real"] == "Dockerfile.rippled-modern"
    profile = build_profile("""FROM ubuntu:24.04
ENV PATH=/opt/conan/bin:${PATH}
RUN apt-get update && apt-get install -y python3-venv python3-pip
RUN python3 -m venv /opt/conan \\
 && /opt/conan/bin/pip install --no-cache-dir conan==2.17.1
""")
    assert profile["kind"] == "pinned-python-tool"
    assert not profile["heavy"] and profile["scratch_gib"] == 8


def test_literal_apt_library_compatibility_links_are_tool_preparation_not_compilation():
    profile = build_profile("""FROM pinned-image
RUN apt-get update && apt-get install -y --no-install-recommends libmumps-seq-dev \\
 && mkdir -p /opt/gmgpolar-mumps-seq \\
 && ln -s /usr/lib/x86_64-linux-gnu/libdmumps_seq.so /opt/gmgpolar-mumps-seq/libdmumps.so \\
 && ln -s /usr/lib/x86_64-linux-gnu/libmpiseq_seq.so /opt/gmgpolar-mumps-seq/libmpiseq.so \\
 && rm -rf /var/lib/apt/lists/*
""")
    assert profile["kind"] == "apt-compat-libraries"
    assert not profile["heavy"] and profile["cpus"] == 2
    assert profile["memory_gib"] == profile["build_memory_gib"] == 4
    assert profile["scratch_gib"] == 8


@pytest.mark.parametrize("extra", [
    "pip3 install --no-cache-dir conan",
    "pip3 install --no-cache-dir conan>=2.0.14",
    "pip3 install --no-cache-dir conan==2.0.14 --extra-index-url https://example.invalid",
    "pip3 install --no-cache-dir private-project==1.2.3",
    "pip3 install --no-cache-dir conan==2.0.14 && conan install .",
    "mkdir -p /opt/compat && ln -s /workspace/answer.so /opt/compat/libanswer.so",
    "mkdir -p /opt/compat && ln -s /usr/lib/x86_64-linux-gnu/libmumps.so /etc/ld.so.preload",
    "ln -s /usr/lib/x86_64-linux-gnu/libmumps.so /opt/missing/libmumps.so",
    "mkdir -p /opt/compat/../../workspace",
    "mkdir -p /opt/compat && ln -sf /usr/lib/x86_64-linux-gnu/libmumps.so /opt/compat/libmumps.so",
    "mkdir -p /opt/compat && ln -s /usr/lib/x86_64-linux-gnu/../../workspace/libmumps.so /opt/compat/libmumps.so",
    "mkdir -p /opt/compat && ln -s /usr/lib/x86_64-linux-gnu/libmumps.so /opt/compat/../libmumps.so",
    "/opt/conan/bin/pip install --no-cache-dir conan==2.17.1",
    "python3 -m venv /workspace/tool && /workspace/tool/bin/pip install --no-cache-dir conan==2.17.1",
    "python3 -m venv /opt/conan && /opt/conan/bin/pip install --no-cache-dir cmake==3.31.6",
    "python3 -m venv /opt/conan --system-site-packages",
])
def test_extended_small_profile_rejects_dynamic_installs_and_nonliteral_link_targets(extra):
    assert build_profile("FROM pinned-image\nRUN apt-get install -y cmake && " + extra)["heavy"]


@pytest.mark.parametrize("jobs,cap,expected", [(1, 4, 1), (2, 4, 2), (8, 4, 4), (0, 2, 2)])
def test_toolchain_job_clamp_never_increases_requested_parallelism(jobs, cap, expected):
    assert limit_build_jobs("make -j " + str(jobs), cap) == "make -j" + str(expected)


@pytest.mark.parametrize("cap", [0, -1, True, 1.5])
def test_toolchain_job_clamp_requires_positive_integer_cap(cap):
    with pytest.raises(ValueError):
        limit_build_jobs("make -j2", cap)


@pytest.mark.parametrize("body", [
    "RUN git clone https://example.org/project && make -j40",
    "RUN apt-get update && apt-get install -y cmake && make",
    "RUN apt-get update && apt-get install -y cmake; make",
    "RUN apt-get update && apt-get install -y $(cat packages)",
    "RUN apt-get update && apt-get install -y cmake | tee log",
    "RUN pip3 install conan==2.0.14",
    "RUN apt-get install -y --allow-unauthenticated cmake",
    "RUN apt-get install -y /tmp/package.deb",
    'SHELL ["/usr/local/bin/custom", "-c"]\nRUN apt-get install -y cmake',
    "ONBUILD RUN make",
    "COPY source /src",
    "RUN apt-get update &&",
])
def test_unknown_or_source_build_steps_keep_conservative_heavy_classification(body):
    profile = build_profile("FROM pinned-image\n" + body + "\n")
    assert profile["kind"] == "source-build" and profile["heavy"]
    assert profile["cpus"] == 4 and profile["memory_gib"] == 12 and profile["scratch_gib"] == 24


def test_curated_java_tool_preparation_retains_hard_shared_resource_limits():
    for native in (False, True):
        profile = managed_build_profile("oracle-java-tools:21", java_mxml=native)
        assert profile["cpus"] == 2 and profile["memory_gib"] == 4 and profile["scratch_gib"] == 8
        assert not profile["heavy"]
    assert managed_build_profile("rb-scripted-php74-cov")["heavy"]
    assert managed_build_profile("rb-csharp-sdk8-cov")["heavy"]


def test_small_java_source_profile_rejects_non_source_archive_members(tmp_path):
    import io
    import tarfile
    archive = tmp_path / "tool.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("tool/source.c")
        info.size = 3
        output.addfile(info, io.BytesIO(b"src"))
    assert bounded_mxml_source(archive) == {"members": 1, "expanded_bytes": 3}
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("tool/escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "/outside"
        output.addfile(info)
    with pytest.raises(ValueError, match="unsupported member"):
        bounded_mxml_source(archive)
