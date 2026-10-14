from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_agentic_managed_toolchains import RUBY, managed_source, managed_build_profile


def test_retained_ruby_32_contract_does_not_silently_upgrade_to_another_ruby(tmp_path):
    assert "ruby:3.2-bookworm" in RUBY
    dockerfile, files, check = managed_source("ruby:3.2-bookworm", tmp_path)
    assert dockerfile == "FROM ruby:3.2-bookworm\n"
    assert files == {}
    assert check == "ruby --version"
    assert managed_build_profile("ruby:3.2-bookworm") == {
        "kind": "ruby-sdk-image", "cpus": 1, "memory_gib": 1, "scratch_gib": 4, "heavy": False}
