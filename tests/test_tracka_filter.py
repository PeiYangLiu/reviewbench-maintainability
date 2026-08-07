from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import trackA_filter  # noqa: E402


class TrackAFilterTests(unittest.TestCase):
    def test_testing_directory_is_a_test_path(self) -> None:
        self.assertTrue(trackA_filter.is_test_path("testing/test_assertion.py"))

    def test_uv_binary_prefers_path(self) -> None:
        with mock.patch("trackA_filter.shutil.which", return_value="/usr/bin/uv"):
            self.assertEqual(trackA_filter.uv_binary(), "/usr/bin/uv")

    def test_uv_binary_falls_back_to_user_install(self) -> None:
        with (
            mock.patch("trackA_filter.shutil.which", return_value=None),
            mock.patch("trackA_filter.Path.is_file", return_value=True),
            mock.patch("trackA_filter.os.access", return_value=True),
        ):
            self.assertTrue(
                trackA_filter.uv_binary().endswith("/.local/bin/uv")
            )

    def test_finds_nested_python_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            project = repo / "python"
            source = project / "ray" / "worker.py"
            source.parent.mkdir(parents=True)
            source.write_text("", encoding="utf-8")
            (project / "pyproject.toml").write_text(
                "[build-system]\nrequires=[]\n",
                encoding="utf-8",
            )

            found = trackA_filter.find_python_project_root(
                repo,
                "python/ray/worker.py",
            )

        self.assertEqual(found, project)

    def test_skips_subpackage_configuration_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "scipy" / "stats" / "_stats.py"
            source.parent.mkdir(parents=True)
            source.write_text("", encoding="utf-8")
            (repo / "pyproject.toml").write_text(
                "[build-system]\nrequires=[]\n",
                encoding="utf-8",
            )
            (source.parent / "setup.py").write_text(
                "def configuration(parent_package='', top_path=None): pass\n",
                encoding="utf-8",
            )

            found = trackA_filter.find_python_project_root(
                repo,
                "scipy/stats/_stats.py",
            )

        self.assertEqual(found, repo)

    def test_reads_commit_build_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "pyproject.toml").write_text(
                '[build-system]\nrequires=["hatchling", "setuptools<81"]\n',
                encoding="utf-8",
            )

            requirements = trackA_filter.python_build_requirements(project)

        self.assertEqual(requirements, ["hatchling", "setuptools<81"])

    def test_only_uses_declared_optional_extras(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "pyproject.toml").write_text(
                '[project]\nname="demo"\n'
                '[project.optional-dependencies]\ndev=["attrs"]\n',
                encoding="utf-8",
            )

            specs = trackA_filter.editable_install_specs(project)

        self.assertEqual(specs, [".[dev]", "."])

    def test_does_not_reinstall_existing_pytest(self) -> None:
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return (0, "")

        with mock.patch("trackA_filter.run", side_effect=fake_run):
            trackA_filter.ensure_test_tools(
                "/usr/bin/uv",
                "/venv/bin/python",
                Path("/repo"),
            )

        self.assertNotIn("pytest", calls[-1])
        self.assertIn("coverage", calls[-1])
        self.assertIn("--no-deps", calls[-1])


if __name__ == "__main__":
    unittest.main()
