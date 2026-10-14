"""No external dependencies, containers, model calls or test-result cache."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import jvm_build_entry as entry
from agentic_runtime_migration.jvm_oracle_entry import read_reports, report_files

SCRIPT = Path(entry.__file__).resolve()
ERRORS = (entry.BuildMirrorError, OSError)


def put(root, name, content=b"input\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def snapshot(root):
    """Byte/permission/link evidence, without following links or hashing atime."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            info = path.lstat()
            result[path.relative_to(root).as_posix()] = (
                stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                os.readlink(path) if path.is_symlink() else
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "",
            )
    return result


@pytest.fixture
def trees(tmp_path):
    source, cache = tmp_path / "workspace", tmp_path / "cache"
    source.mkdir()
    return source, cache


def sync(trees, **kwargs):
    return entry.synchronize_candidate(*trees, **kwargs)


def manifest(cache, mode="developer", name="inputs.json"):
    return json.loads((cache / mode / name).read_text())


def cli(source, cache, *args, mode="developer", **kwargs):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--workspace", str(source), "--cache", str(cache),
         "--mode", mode, *args], text=True, capture_output=True, timeout=20, **kwargs,
    )


def git(root, *args):
    env = entry._git_environment(root)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(["git", "-C", str(root), *args], env=env,
                            capture_output=True, check=True)
    return result.stdout


def init_git(source):
    git(source, "init", "-q", "--initial-branch=main")
    git(source, "config", "user.name", "Snapshot")
    git(source, "config", "user.email", "snapshot@example.invalid")
    git(source, "config", "core.autocrlf", "false")
    git(source, "config", "gc.auto", "0")
    git(source, "config", "maintenance.auto", "false")
    git(source, "add", "--all", "--force")
    git(source, "commit", "-qm", "isolated task start")


def test_whole_candidate_keeps_mtime_and_outputs_without_mutating_input(trees):
    source, cache = trees
    for name in ("anchor/Main.java", "other/Helper.java", "build.gradle", ".hidden/config",
                 ".reviewbench/private/fixed-test.java", "build/source-fixture.txt"):
        put(source, name, "current " + name)
    (source / "empty").mkdir()
    put(source, "gradlew", "#!/bin/sh\n").chmod(0o751)
    (source / "helper-link").symlink_to("other/Helper.java")
    before = snapshot(source)
    mirror = sync(trees)
    assert snapshot(mirror) == before
    metadata = {name: (mirror / name).lstat().st_mtime_ns for name in before}
    put(mirror, "build/classes/Helper.class", b"compiler output")
    put(mirror, ".gradle/8.7/executionHistory/history.bin", b"incremental state")
    # A new workspace timestamp is not a content change.
    for name, value in before.items():
        if value[0] == stat.S_IFREG:
            os.utime(source / name, ns=(1_000_000_000, 1_000_000_000))
    assert sync(trees) == mirror
    assert all((mirror / name).lstat().st_mtime_ns == value
               for name, value in metadata.items() if before[name][0] != stat.S_IFDIR)
    assert (mirror / "build/classes/Helper.class").read_bytes() == b"compiler output"
    assert (mirror / ".gradle/8.7/executionHistory/history.bin").read_bytes() == b"incremental state"
    assert "build/classes/Helper.class" not in manifest(cache)["inputs"]
    assert snapshot(source) == before


def test_add_modify_delete_rename_and_removed_input_directory_keep_only_outputs(trees):
    source, cache = trees
    put(source, "src/Main.java", "main")
    put(source, "src/Helper.java", "one")
    put(source, "old/input.txt", "remove")
    mirror = sync(trees)
    put(mirror, "old/generated.class", b"keep")
    put(mirror, "build/Helper.class", b"keep compile state")
    info = (source / "src/Helper.java").stat()
    put(source, "src/Helper.java", "two")
    os.utime(source / "src/Helper.java", ns=(info.st_atime_ns, info.st_mtime_ns))
    (source / "src/Main.java").rename(source / "src/Renamed.java")
    shutil.rmtree(source / "old")
    put(source, "src/Added.java", "new helper")
    assert sync(trees) == mirror
    assert (mirror / "src/Helper.java").read_text() == "two"
    assert not (mirror / "src/Main.java").exists()
    assert not (mirror / "old/input.txt").exists()
    assert (mirror / "src/Renamed.java").read_text() == "main"
    assert (mirror / "src/Added.java").read_text() == "new helper"
    assert (mirror / "old/generated.class").read_bytes() == b"keep"
    assert (mirror / "build/Helper.class").read_bytes() == b"keep compile state"
    assert "old" not in manifest(cache)["inputs"]
    sync(trees)
    assert "old/generated.class" not in manifest(cache)["inputs"]
    assert not (mirror / "old/input.txt").exists()


def test_same_size_timestamp_edits_and_build_modified_source_are_not_stat_hits(trees):
    source, _ = trees
    put(source, "Helper.java", "one")
    mirror = sync(trees)
    original = (mirror / "Helper.java").stat()
    put(source, "Helper.java", "two")
    os.utime(source / "Helper.java", ns=(original.st_atime_ns, original.st_mtime_ns))
    sync(trees)
    assert (mirror / "Helper.java").read_text() == "two"
    assert (mirror / "Helper.java").stat().st_mtime_ns != original.st_mtime_ns
    info = (mirror / "Helper.java").stat()
    put(mirror, "Helper.java", "bad")
    os.utime(mirror / "Helper.java", ns=(info.st_atime_ns, info.st_mtime_ns))
    sync(trees)
    assert (mirror / "Helper.java").read_text() == "two"
    assert (source / "Helper.java").read_text() == "two"


def test_copy_is_independent_even_if_workspace_uses_hardlinks(trees):
    source, _ = trees
    put(source, "one", "original")
    os.link(source / "one", source / "two")
    mirror = sync(trees)
    assert (mirror / "one").stat().st_ino != (source / "one").stat().st_ino
    assert (mirror / "one").stat().st_nlink == 1
    put(mirror, "one", "build edit")
    assert (source / "two").read_text() == "original"
    assert (mirror / "two").read_text() == "original"


def test_relative_directory_dangling_parent_and_retargeted_links(trees):
    source, _ = trees
    put(source, "dir/file", "hello")
    (source / "dir/up").symlink_to("../dir/file")
    (source / "alias").symlink_to("dir", target_is_directory=True)
    (source / "dangling").symlink_to("build/not-yet-generated")
    mirror = sync(trees)
    assert (mirror / "alias/file").read_text() == "hello"
    assert os.readlink(mirror / "dir/up") == "../dir/file"
    assert os.readlink(mirror / "dangling") == "build/not-yet-generated"
    (source / "alias").unlink()
    (source / "alias").symlink_to("dir/file")
    sync(trees)
    assert os.readlink(mirror / "alias") == "dir/file"


@pytest.mark.parametrize("old,new", [
    ("file", "dir"), ("dir", "file"), ("link", "dir"),
    ("dir", "link"), ("file", "link"), ("link", "file"),
])
def test_input_kind_changes_supersede_colliding_generated_outputs(trees, old, new):
    source, _ = trees
    put(source, "real", "link target")
    def install(kind):
        if kind == "file":
            put(source, "item", "plain")
        elif kind == "dir":
            put(source, "item/helper", "helper")
        else:
            (source / "item").symlink_to("real")
    install(old)
    mirror = sync(trees)
    if old == "dir":
        put(mirror, "item/generated.class", "obsolete conflicting output")
        shutil.rmtree(source / "item")
    else:
        (source / "item").unlink()
    install(new)
    sync(trees)
    assert snapshot(mirror) == snapshot(source)


def test_candidate_names_cannot_collide_with_external_cache_metadata(trees):
    source, cache = trees
    for name in ("inputs.json", "pending.json", "build.lock", "staging/one",
                 "developer/mirror/config", "grader/config", ".jvm-build-cache/meta"):
        put(source, name, "candidate data")
    mirror = sync(trees)
    assert snapshot(mirror) == snapshot(source)
    assert (mirror / "inputs.json").read_text() == "candidate data"
    assert manifest(cache)["schema"] == entry.SCHEMA


@pytest.mark.parametrize("target", ["/tmp/outside", "../outside", "x/../../outside",
                                   r"..\outside", "cycle", ""])
def test_invalid_source_links_fail_before_touching_mirror(trees, target):
    source, _ = trees
    put(source, "keep", "first")
    mirror = sync(trees)
    before = snapshot(mirror)
    if target:
        (source / "cycle").symlink_to(target)
    else:
        # The filesystem rejects empty links; exercise the validator directly.
        with pytest.raises(entry.BuildMirrorError):
            entry._validate_links({"cycle": {"kind": "link", "target": ""}})
        return
    with pytest.raises(ERRORS):
        sync(trees)
    assert snapshot(mirror) == before


def test_symlink_component_resolution_detects_normpath_only_escape(trees):
    source, _ = trees
    (source / "alias").symlink_to(".")
    (source / "escape").symlink_to("alias/../outside")
    with pytest.raises(entry.BuildMirrorError, match="escapes"):
        sync(trees)


@pytest.mark.parametrize("kind", ["fifo", "socket", "setuid"])
def test_special_candidate_files_fail_without_execution(trees, kind):
    source, cache = trees
    held = None
    if kind == "fifo":
        os.mkfifo(source / "special")
    elif kind == "socket":
        held = socket.socket(socket.AF_UNIX)
        # Keep the actual socket inside the candidate even when pytest's
        # absolute worktree path exceeds AF_UNIX's short sockaddr limit.
        descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
        try:
            held.bind(f"/proc/self/fd/{descriptor}/special")
        finally:
            os.close(descriptor)
    else:
        put(source, "special").chmod(0o4644)
    try:
        result = cli(source, cache, "--command", sys.executable, "-c", "print('must not run')")
        assert result.returncode == 2
        assert result.stdout == ""
        assert "special" in result.stderr
    finally:
        if held:
            held.close()


@pytest.mark.parametrize("case", ["same", "cache-in-source", "source-in-cache",
                                 "source-link", "cache-link", "cache-ancestor-link"])
def test_roots_must_be_disjoint_real_directories(trees, tmp_path, case):
    source, cache = trees
    put(source, "sentinel", "unchanged")
    if case == "same":
        cache = source
    elif case == "cache-in-source":
        cache = source / "cache"
    elif case == "source-in-cache":
        cache = source.parent
    elif case == "source-link":
        alias = tmp_path / "source-alias"
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif case == "cache-link":
        target = tmp_path / "elsewhere"
        target.mkdir()
        cache.symlink_to(target, target_is_directory=True)
    else:
        target = tmp_path / "elsewhere"
        target.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)
        cache = alias / "cache"
    with pytest.raises(ERRORS):
        entry.synchronize_candidate(source, cache)
    assert (source / "sentinel").read_text() == "unchanged"


@pytest.mark.parametrize("name", ["build.lock", "developer", "developer/inputs.json",
                                 "developer/pending.json", "developer/mirror", "developer/staging"])
def test_cache_metadata_symlinks_do_not_redirect_writes(trees, tmp_path, name):
    source, cache = trees
    put(source, "Main.java")
    sync(trees)
    outside = tmp_path / "outside"
    outside.mkdir()
    put(outside, "sentinel", "unchanged")
    path = cache / name
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ERRORS):
        sync(trees)
    assert snapshot(outside) == {"sentinel": (stat.S_IFREG, 0o644,
                                            hashlib.sha256(b"unchanged").hexdigest())}


@pytest.mark.parametrize("name", ["build.lock", "developer/inputs.json", "developer/mirror/Main.java"])
def test_cache_hardlinks_are_rejected_before_writing_external_inode(trees, tmp_path, name):
    source, cache = trees
    put(source, "Main.java", "one")
    sync(trees)
    outside = tmp_path / "external-copy"
    os.link(cache / name, outside)
    before = outside.read_bytes()
    put(source, "Main.java", "two")
    with pytest.raises(ERRORS):
        sync(trees)
    assert outside.read_bytes() == before


@pytest.mark.parametrize("value", [
    {"schema": entry.SCHEMA, "mode": "developer", "inputs": ["../outside"]},
    {"schema": entry.SCHEMA, "mode": "developer", "inputs": ["/outside"]},
    {"schema": entry.SCHEMA, "mode": "developer", "inputs": ["a//b"]},
    {"schema": entry.SCHEMA, "mode": "developer", "inputs": ["a/./b"]},
    {"schema": entry.SCHEMA, "mode": "developer", "inputs": ["a", "a"]},
    {"schema": entry.SCHEMA, "mode": "developer", "inputs": [1]},
    {"schema": entry.SCHEMA, "mode": "grader", "inputs": []},
    {"schema": "other/v1", "mode": "developer", "inputs": []},
    [], "broken",
])
def test_corrupt_or_wrong_mode_manifest_is_not_used_for_deletion(trees, value):
    source, cache = trees
    put(source, "Main.java")
    mirror = sync(trees)
    before = snapshot(mirror)
    (cache / "developer/inputs.json").write_text(json.dumps(value))
    with pytest.raises(entry.BuildMirrorError):
        sync(trees)
    assert snapshot(mirror) == before


def test_duplicate_json_keys_are_rejected(trees):
    source, cache = trees
    put(source, "Main.java")
    sync(trees)
    (cache / "developer/inputs.json").write_text(
        '{"schema":"%s","mode":"developer","inputs":[],"inputs":["Main.java"]}' % entry.SCHEMA)
    with pytest.raises(entry.BuildMirrorError):
        sync(trees)


def test_unowned_nonempty_mirror_is_not_a_build_seed(trees):
    source, cache = trees
    put(source, "Main.java", "current")
    put(cache, "developer/mirror/Main.java", "old reference")
    put(cache, "developer/mirror/build/Main.class", "old output")
    before = snapshot(cache / "developer/mirror")
    with pytest.raises(entry.BuildMirrorError, match="no previous input manifest"):
        sync(trees)
    assert snapshot(cache / "developer/mirror") == before


@pytest.mark.parametrize("name", ["unexpected", "developer/unexpected", "developer/staging/unexpected"])
def test_cache_namespace_collisions_fail_closed(trees, name):
    source, cache = trees
    put(source, "Main.java")
    sync(trees)
    put(cache, name, "not owned")
    with pytest.raises(entry.BuildMirrorError, match="collision"):
        sync(trees)
    assert (cache / name).read_text() == "not owned"


def test_generated_symlink_cannot_redirect_current_candidate_or_cleanup(trees, tmp_path):
    source, _ = trees
    put(source, "src/Main.java", "candidate")
    mirror = sync(trees)
    external = put(tmp_path, "external/Main.java", "external unchanged")
    shutil.rmtree(mirror / "src")
    (mirror / "src").symlink_to(external.parent, target_is_directory=True)
    with pytest.raises(entry.BuildMirrorError):
        sync(trees)
    assert external.read_text() == "external unchanged"
    assert (source / "src/Main.java").read_text() == "candidate"


def test_surviving_generated_link_is_revalidated_against_new_input_graph(trees):
    source, _ = trees
    put(source, "dir/file")
    mirror = sync(trees)
    (mirror / "generated-link").symlink_to("dir/../Main.java")
    shutil.rmtree(source / "dir")
    (source / "dir").symlink_to(".")
    with pytest.raises(entry.BuildMirrorError, match="escapes"):
        sync(trees)
    assert (mirror / "dir").is_dir() and not (mirror / "dir").is_symlink()


def test_interrupted_copy_journal_prevents_adopting_partial_sources_as_outputs(trees, monkeypatch):
    source, cache = trees
    put(source, "old.java", "old")
    mirror = sync(trees)
    put(mirror, "build/keep.class", "keep")
    put(source, "added.java", "partial input")
    original = entry._install_file
    def interrupted(*args):
        original(*args)
        if args[3] == "added.java":
            raise OSError("injected interrupted sync")
    monkeypatch.setattr(entry, "_install_file", interrupted)
    with pytest.raises(OSError, match="injected"):
        sync(trees)
    assert "added.java" in manifest(cache, name="pending.json")["inputs"]
    assert (mirror / "added.java").exists()
    put(cache, "developer/staging/" + "a" * 32, "unfinished source bytes")
    (source / "added.java").unlink()
    monkeypatch.setattr(entry, "_install_file", original)
    sync(trees)
    assert not (mirror / "added.java").exists()
    assert (mirror / "build/keep.class").read_text() == "keep"
    assert not (cache / "developer/pending.json").exists()
    assert not list((cache / "developer/staging").iterdir())


def test_separate_modes_purge_inactive_private_files_and_outputs(trees):
    source, cache = trees
    put(source, "Main.java", "public")
    developer = sync(trees)
    put(developer, "build/developer.class", "dev output")
    put(source, ".reviewbench/Private.java", "fixed grader")
    grader = sync(trees, mode="grader")
    assert grader != developer and not developer.exists()
    assert (grader / ".reviewbench/Private.java").read_text() == "fixed grader"
    put(grader, "build/Private.class", "private compiled output")
    assert sync(trees, mode="grader") == grader
    assert (grader / "build/Private.class").exists()
    shutil.rmtree(source / ".reviewbench")
    assert sync(trees) == developer
    assert not grader.exists()
    assert not (developer / ".reviewbench").exists()
    assert not (developer / "build").exists()


def test_same_mode_next_workspace_does_not_reuse_stale_inputs(trees, tmp_path):
    source, cache = trees
    put(source, "Main.java", "one")
    put(source, "Helper.java", "remove")
    mirror = sync(trees, mode="grader")
    put(mirror, "build/state.bin", "compiler state")
    second = tmp_path / "next-replay"
    put(second, "Main.java", "two")
    assert entry.synchronize_candidate(second, cache, mode="grader") == mirror
    assert (mirror / "Main.java").read_text() == "two"
    assert not (mirror / "Helper.java").exists()
    assert (mirror / "build/state.bin").read_text() == "compiler state"


def test_git_snapshot_keeps_head_branch_index_tags_and_dirty_bytes_without_old_refs(trees):
    source, _ = trees
    put(source, "Main.java", "committed\n")
    put(source, ".gitignore", "build/\n")
    init_git(source)
    git(source, "tag", "workspace-tag")
    put(source, "Main.java", "candidate edit\n")
    before = snapshot(source)
    mirror = sync(trees)
    for args in (("rev-parse", "HEAD"), ("symbolic-ref", "HEAD"), ("show-ref",),
                 ("ls-files", "--stage"), ("diff", "--no-ext-diff"),
                 ("describe", "--always", "--dirty", "--tags")):
        assert git(mirror, *args) == git(source, *args)
    put(mirror, "build/Main.class", "old compiler output")
    git(mirror, "add", "--all", "--force")
    git(mirror, "commit", "-qm", "build must not persist this history")
    old_build_head = git(mirror, "rev-parse", "HEAD").strip()
    git(mirror, "tag", "old-build-tag")
    put(mirror, ".git/extra-build-metadata", "drop")
    sync(trees)
    assert git(mirror, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert git(mirror, "show-ref") == git(source, "show-ref")
    assert git(mirror, "ls-files", "--stage") == git(source, "ls-files", "--stage")
    assert old_build_head not in git(mirror, "rev-list", "--all")
    assert set(snapshot(mirror / ".git")) == set(snapshot(source / ".git"))
    assert not (mirror / ".git/extra-build-metadata").exists()
    assert (mirror / "build/Main.class").read_text() == "old compiler output"
    assert (mirror / "Main.java").read_text() == "candidate edit\n"
    assert snapshot(source) == before


def test_absent_git_never_gets_synthesized_and_previous_git_is_removed(trees):
    source, _ = trees
    put(source, "Main.java", "current")
    mirror = sync(trees)
    assert not (mirror / ".git").exists()
    init_git(source)
    sync(trees)
    assert (mirror / ".git/HEAD").exists()
    shutil.rmtree(source / ".git")
    sync(trees)
    assert not (mirror / ".git").exists()
    # A build-created Git repository is not an input either.
    init_git(mirror)
    sync(trees)
    assert not (mirror / ".git").exists()


@pytest.mark.parametrize("name", [
    ".git/commondir", ".git/gitdir", ".git/objects/info/alternates",
    ".git/objects/info/http-alternates", ".git/info/grafts",
    ".git/config.worktree", ".git/worktrees/old/gitdir", ".git/modules/sub/config",
    ".git/refs/replace/" + "a" * 40,
])
def test_git_indirection_is_not_imported(trees, name):
    source, _ = trees
    put(source, "Main.java")
    init_git(source)
    put(source, name, "/outside")
    with pytest.raises(entry.BuildMirrorError, match="SCM indirection"):
        sync(trees)


@pytest.mark.parametrize("name,kind", [(".git", "file"), (".git", "link"),
                                     ("sub/.git", "file"), (".git/HEAD", "link")])
def test_gitfiles_and_scm_links_are_rejected(trees, name, kind):
    source, _ = trees
    put(source, "dummy", "ref: refs/heads/main")
    if kind == "file":
        put(source, name, "gitdir: /old/repository/.git")
    else:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).symlink_to("dummy")
    with pytest.raises(entry.BuildMirrorError, match="SCM"):
        sync(trees)


@pytest.mark.parametrize("key", [
    "core.worktree", "core.hooksPath", "core.fsmonitor", "core.alternateRefsCommand",
    "include.path", "includeIf.gitdir:*/.path", "extensions.worktreeConfig",
])
def test_external_git_configuration_is_rejected_without_following_includes(trees, key):
    source, _ = trees
    put(source, "Main.java")
    init_git(source)
    git(source, "config", "--file", str(source / ".git/config"), key, "/outside")
    with pytest.raises(entry.BuildMirrorError, match="SCM config"):
        sync(trees)


def test_run_sanitizes_inherited_git_paths_and_never_discovers_parent_repository(trees, monkeypatch):
    source, cache = trees
    put(source, "Main.java")
    init_git(source)
    expected = git(source, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", "/unrelated/.git")
    monkeypatch.setenv("GIT_WORK_TREE", str(source))
    monkeypatch.setenv("GIT_INDEX_FILE", str(source / ".git/index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(source))
    result = cli(source, cache, "--command", "git", "rev-parse", "HEAD")
    assert result.returncode == 0 and result.stdout.encode() == expected
    # A real parent repository must not supply SCM when workspace .git is absent.
    shutil.rmtree(source / ".git")
    init_git(cache.parent)
    result = cli(source, cache, "--command", "git", "rev-parse", "--show-toplevel")
    assert result.returncode == 128


def test_real_commands_always_run_and_junit_bytes_and_exit_status_are_original(trees):
    source, cache = trees
    put(source, "Main.java", "current")
    xml = '<testsuite><testcase classname="Fixed" name="behavior"><failure message="original"/></testcase></testsuite>'
    command = [sys.executable, "-c",
               "from pathlib import Path; import sys; "
               "p=Path('build/test-results/test/TEST-fixed.xml'); p.parent.mkdir(parents=True, exist_ok=True); "
               f"p.write_text({xml!r}); "
               "n=Path('build/executions'); n.write_text(str(int(n.read_text())+1) if n.exists() else '1'); "
               "print('literal failure'); sys.exit(7)"]
    before = snapshot(source)
    for number in (1, 2):
        result = cli(source, cache, "--command", *command, mode="grader")
        assert result.returncode == 7 and result.stdout == "literal failure\n"
        mirror = cache / "grader/mirror"
        assert (mirror / "build/executions").read_text() == str(number)
        reports = report_files(mirror)
        assert len(reports) == 1 and reports[0].read_text() == xml
        assert read_reports(reports) == {"Fixed#behavior": False}
    assert snapshot(source) == before


def test_cli_and_python_api_return_stable_path_and_use_mirror_cwd(trees):
    source, cache = trees
    put(source, "module/input", "here")
    result = cli(source, cache, "--sync-only")
    mirror = cache / "developer/mirror"
    assert result.returncode == 0 and result.stdout == str(mirror) + "\n"
    returned, status = entry.run_candidate(
        source, cache, cwd="module",
        command=[sys.executable, "-c",
                 "from pathlib import Path; import os; "
                 "assert Path.cwd() == Path(os.environ['SOURCE_ROOT']) / 'module'; "
                 "assert Path('input').read_text() == 'here'; "
                 "Path('built').write_text(os.environ['REVIEWBENCH_JVM_BUILD_MODE'])"],
    )
    assert returned == mirror and status == 0
    assert (mirror / "module/built").read_text() == "developer"
    assert not (source / "module/built").exists()


@pytest.mark.parametrize("cwd", ["../workspace", "/workspace", "module/../module", "alias"])
def test_command_cwd_never_escapes_or_follows_a_link(trees, cwd):
    source, cache = trees
    put(source, "module/file")
    (source / "alias").symlink_to("module")
    result = cli(source, cache, "--cwd", cwd, "--command", sys.executable, "-c", "print('bad')")
    assert result.returncode == 2 and not result.stdout


@pytest.mark.parametrize("command", [[], "", "echo implicit-shell", [1], ["a\0b"]])
def test_api_rejects_invalid_or_implicit_shell_commands(trees, command):
    with pytest.raises(entry.BuildMirrorError):
        entry.run_candidate(*trees, command=command)


@pytest.mark.parametrize("mode", ["../grader", "other", ""])
def test_invalid_mode_cannot_select_an_arbitrary_cache_directory(trees, mode):
    with pytest.raises(entry.BuildMirrorError):
        sync(trees, mode=mode)


def wait_for(path, process=None):
    deadline = time.monotonic() + 10
    while not path.exists():
        if process and process.poll() is not None:
            raise AssertionError("worker exited before marker: " + str(process.returncode))
        if time.monotonic() >= deadline:
            raise AssertionError("worker did not reach marker")
        time.sleep(0.01)


@contextmanager
def worker(source, cache, code, *, mode="developer"):
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--workspace", str(source), "--cache", str(cache),
         "--mode", mode, "--command", sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)


@pytest.mark.parametrize("second_mode", ["developer", "grader"])
def test_cache_lock_covers_synchronization_real_execution_and_mode_switch(trees, tmp_path, second_mode):
    source, cache = trees
    put(source, "input", "first")
    second_source = tmp_path / "next"
    put(second_source, "input", "second")
    entered, release, second_entered = (tmp_path / name for name in ("entered", "release", "second"))
    first_code = (
        "from pathlib import Path; import time; "
        f"Path({str(entered)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "assert Path('input').read_text() == 'first'\n"
        "print('first locked')\n"
    )
    second_code = (f"from pathlib import Path; Path({str(second_entered)!r}).touch(); "
                   "assert Path('input').read_text() == 'second'; print('second locked')")
    with worker(source, cache, first_code) as first:
        wait_for(entered, first)
        with worker(second_source, cache, second_code, mode=second_mode) as second:
            time.sleep(0.25)
            assert second.poll() is None and not second_entered.exists()
            assert (cache / "developer/mirror/input").read_text() == "first"
            release.touch()
            first_out, _ = first.communicate(timeout=10)
            second_out, _ = second.communicate(timeout=10)
            assert first.returncode == second.returncode == 0
            assert first_out == "first locked\n" and second_out == "second locked\n"


def test_cli_sigterm_stops_foreground_child_before_releasing_cache_lock(trees, tmp_path):
    source, cache = trees
    put(source, "input")
    started, pid_file = tmp_path / "started", tmp_path / "child-pid"
    code = (f"from pathlib import Path; import os,time; Path({str(pid_file)!r}).write_text(str(os.getpid())); "
            f"Path({str(started)!r}).touch(); time.sleep(60)")
    with worker(source, cache, code) as process:
        wait_for(started, process)
        child = int(pid_file.read_text())
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10)
        assert process.returncode == 143
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
    assert cli(source, cache, "--command", sys.executable, "-c", "print('released')").stdout == "released\n"
