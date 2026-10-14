"""Shared prerequisites for opt-in, hash-bound historical integration inputs."""
import os
from pathlib import Path

import pytest


@pytest.fixture
def external_artifact_root():
    def required_directory(variable):
        value = os.environ.get(variable)
        if not value:
            pytest.skip(f"set {variable} to the original archived integration inputs")
        path = Path(value)
        if not path.is_dir():
            pytest.fail(f"configured {variable} is not a directory: {path}")
        return path

    return required_directory
