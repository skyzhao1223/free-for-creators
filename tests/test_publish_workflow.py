"""Local Git integration tests for publication ownership and race safety."""
import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("commit_generated", ROOT / "scripts/commit_generated.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


class GeneratedCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffc-publish-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.remote = self.base / "remote.git"
        self.repo = self.base / "repo"
        git(self.base, "init", "--bare", str(self.remote))
        git(self.base, "clone", str(self.remote), str(self.repo))
        git(self.repo, "switch", "-c", "main")
        git(self.repo, "config", "user.name", "test author")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("initial\n")
        (self.repo / "data").mkdir()
        (self.repo / "data/music.json").write_text("{}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "push", "origin", "HEAD:main")

    def concurrent_push(self):
        other = self.base / "other"
        git(self.base, "clone", "-b", "main", str(self.remote), str(other))
        git(other, "config", "user.name", "human")
        git(other, "config", "user.email", "human@example.com")
        git(other, "config", "commit.gpgsign", "false")
        (other / "human.txt").write_text("do not overwrite\n")
        git(other, "add", "human.txt")
        git(other, "commit", "-m", "human change")
        git(other, "push", "origin", "HEAD:main")
        return git(other, "rev-parse", "HEAD")

    def test_no_change_does_not_commit(self):
        before = git(self.repo, "rev-parse", "HEAD")
        self.assertFalse(module.commit_generated(self.repo))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)

    def test_generated_change_pushes_normally(self):
        (self.repo / "README.md").write_text("regenerated\n")
        self.assertTrue(module.commit_generated(self.repo))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"),
                         git(self.remote, "rev-parse", "refs/heads/main"))
        self.assertIn("[skip ci]", git(self.repo, "log", "-1", "--format=%s"))

    def test_main_advanced_refuses_stale_outputs(self):
        (self.repo / "README.md").write_text("old generated output\n")
        latest = self.concurrent_push()
        with self.assertRaisesRegex(RuntimeError, "main advanced"):
            module.commit_generated(self.repo)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), latest)
        self.assertEqual((self.repo / "README.md").read_text(), "old generated output\n")

    def test_unexpected_staged_file_is_refused(self):
        (self.repo / "unowned.txt").write_text("not a generator output\n")
        git(self.repo, "add", "unowned.txt")
        with self.assertRaisesRegex(RuntimeError, "unexpected staged"):
            module.commit_generated(self.repo)

    def test_push_race_cannot_overwrite_human_commit(self):
        (self.repo / "README.md").write_text("regenerated\n")
        original = module.git
        latest = []
        def racing_git(root, *args):
            if args[0] == "push":
                latest.append(self.concurrent_push())
            return original(root, *args)
        with patch.object(module, "git", racing_git):
            with self.assertRaises(module.MainAdvanced):
                module.commit_generated(self.repo)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), latest[0])

    def test_cli_marks_main_race_for_bounded_recovery(self):
        output = self.base / "github-output"
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), \
                patch.object(module, "commit_generated", side_effect=module.MainAdvanced("main advanced")), \
                patch("sys.argv", ["commit_generated.py", "--root", str(self.repo)]):
            self.assertEqual(module.main(), 2)
        self.assertEqual(output.read_text().splitlines()[-1], "main_advanced=true")

    def test_permission_failure_is_not_marked_as_a_main_race(self):
        output = self.base / "github-output"
        error = subprocess.CalledProcessError(128, ["git", "push"], stderr="permission denied")
        original = module.git
        def denied_push(root, *args):
            if args[0] == "push":
                raise error
            return original(root, *args)
        (self.repo / "README.md").write_text("regenerated\n")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), \
                patch.object(module, "git", denied_push), \
                patch("sys.argv", ["commit_generated.py", "--root", str(self.repo)]):
            with self.assertRaises(subprocess.CalledProcessError):
                module.main()
        self.assertNotIn("main_advanced=true", output.read_text())

    def test_ownership_excludes_issue_notes_and_source(self):
        (self.repo / "data/experiences").mkdir()
        (self.repo / "data/experiences/notes.json").write_text("{}")
        publish = module.owned_paths(self.repo, "publish")
        self.assertIn("data/music.json", publish)
        self.assertNotIn("data/experiences/notes.json", publish)
        self.assertNotIn("data/music.json", module.owned_paths(self.repo, "linkcheck"))


class PublicationWiringTests(unittest.TestCase):
    def test_single_owner_and_build_after_updates(self):
        workflow = (ROOT / ".github/workflows/pages.yml").read_text()
        self.assertIn("ref: main", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("scripts/commit_generated.py --kind publish", workflow)
        self.assertLess(workflow.index("scripts/sync_contributors.py --fetch --stamp"),
                        workflow.index("python3 scripts/build_site.py"))
        self.assertLess(workflow.index("scripts/render_banner.py --chrome"),
                        workflow.index("python3 scripts/build_site.py"))
        self.assertIn("if: steps.banner.outputs.needs_render == 'true'", workflow)
        self.assertFalse((ROOT / ".github/workflows/banner.yml").exists())
        self.assertFalse((ROOT / ".github/workflows/contributors.yml").exists())

    def test_publication_paths_cover_local_generator_imports(self):
        workflow = (ROOT / ".github/workflows/pages.yml").read_text()
        explicit = set()
        for line in workflow.splitlines():
            value = line.strip().split(" #", 1)[0]
            if value.startswith('- "') and value.endswith('"'):
                explicit.add(value[3:-1])
        pending = [ROOT / "scripts/build_site.py", ROOT / "scripts/build_llms.py"]
        visited = set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            relative = str(path.relative_to(ROOT))
            self.assertTrue(relative in explicit or
                            (path.name.startswith("build_") and "scripts/build_*.py" in explicit), relative)
            for node in ast.walk(ast.parse(path.read_text())):
                names = [n.name for n in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                for name in names:
                    local = ROOT / "scripts" / (name.split(".")[0] + ".py")
                    if local.is_file():
                        pending.append(local)

    def test_main_race_recovery_is_bounded_and_not_generic(self):
        workflow = (ROOT / ".github/workflows/pages.yml").read_text()
        self.assertIn("steps.generated.outputs.main_advanced == 'true'", workflow)
        self.assertIn('if [ "$retry" -ge 3 ]; then', workflow)
        self.assertIn('gh workflow run pages.yml --ref main -f race_retry=', workflow)
        self.assertIn("actions: write", workflow)
        self.assertIn("Stop rather than deploy stale or uncommitted outputs", workflow)

    def test_linkcheck_dispatches_even_for_broken_links(self):
        workflow = (ROOT / ".github/workflows/linkcheck.yml").read_text()
        self.assertIn("actions: write", workflow)
        self.assertIn("always() && steps.record.outcome == 'success'", workflow)
        self.assertIn("gh workflow run pages.yml --ref main", workflow)
        self.assertIn("scripts/commit_generated.py --kind linkcheck", workflow)
        self.assertNotIn("git pull --rebase", workflow)
        self.assertIn('test -f "$RUNNER_TEMP/linkcheck-report.md"', workflow)


if __name__ == "__main__":
    unittest.main()
