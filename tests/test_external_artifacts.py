"""Unavailable optional archives are skipped, misconfigured archives fail."""
import pytest


@pytest.mark.parametrize("value", [None, ""])
def test_unset_archive_is_explicitly_optional(external_artifact_root, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("REVIEWBENCH_TEST_ARCHIVE", raising=False)
    else:
        monkeypatch.setenv("REVIEWBENCH_TEST_ARCHIVE", value)
    with pytest.raises(pytest.skip.Exception, match="original archived integration inputs"):
        external_artifact_root("REVIEWBENCH_TEST_ARCHIVE")


def test_configured_missing_archive_fails(external_artifact_root, monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEWBENCH_TEST_ARCHIVE", str(tmp_path / "missing"))
    with pytest.raises(pytest.fail.Exception, match="is not a directory"):
        external_artifact_root("REVIEWBENCH_TEST_ARCHIVE")


def test_configured_archive_is_used_without_replacement(external_artifact_root, monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEWBENCH_TEST_ARCHIVE", str(tmp_path))
    assert external_artifact_root("REVIEWBENCH_TEST_ARCHIVE") == tmp_path
