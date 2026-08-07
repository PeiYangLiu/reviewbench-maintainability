from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import trackA_go  # noqa: E402


class TrackAGoTests(unittest.TestCase):
    def test_builds_docker_go_command(self) -> None:
        command = trackA_go.build_go_command(
            Path("/tmp/repo"),
            ["test", "./pkg/"],
            use_docker=True,
        )

        self.assertEqual(command[:3], ["docker", "run", "--rm"])
        self.assertIn("/tmp/repo:/repo", command)
        self.assertEqual(command[-3:], ["go", "test", "./pkg/"])

    def test_builds_host_go_command(self) -> None:
        command = trackA_go.build_go_command(
            Path("/tmp/repo"),
            ["test", "./pkg/"],
            use_docker=False,
        )

        self.assertEqual(command, ["go", "test", "./pkg/"])

    def test_finds_nearest_nested_go_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            module = repo / "constraint"
            source = module / "pkg" / "client" / "driver.go"
            source.parent.mkdir(parents=True)
            source.write_text("package client\n", encoding="utf-8")
            (module / "go.mod").write_text(
                "module example.com/constraint\n",
                encoding="utf-8",
            )

            go_dir, target = trackA_go.module_context(
                repo,
                "constraint/pkg/client/driver.go",
            )

        self.assertEqual(go_dir, module)
        self.assertEqual(target, "./pkg/client/")


if __name__ == "__main__":
    unittest.main()
