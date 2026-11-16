"""CI's skip decisions are an allowlist, never a reason to bypass the build gate.

Includes real git-object graphs (no checkout or worktree commits) to reproduce the
PR merge-ref checkout and a base branch that advanced after the PR forked. CC0.
"""
import contextlib
import fnmatch
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ci_changes  # noqa: E402

A, B = "a" * 40, "b" * 40


def pr_event(base=A, head=B):
    return {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}


class PathTests(unittest.TestCase):
    def test_page_payload_and_transitive_dependencies_need_chrome(self):
        for path in (
            "data/fonts.json", "data/experiences/notes.json", "data/experiences/skipped.json",
            "schema/entry.schema.json", "site/index.html", "site/404.html", "site/new.css",
            "contributors.json", *sorted(ci_changes.RESPONSIVE_SCRIPTS),
        ):
            with self.subTest(path=path):
                self.assertTrue(ci_changes.classify_paths([path]).responsive_needed)

    def test_known_non_page_changes_skip_only_chrome(self):
        for path in (
            "README.md", "README.zh-CN.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
            "LICENSE", "docs/guide.md", "PROMO-DRAFTS.md", ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/ISSUE_TEMPLATE/suggest-resource.yml", "tests/test_ci_efficiency.py",
            "linkcheck-titles.json", *sorted(ci_changes.NON_RESPONSIVE_SCRIPTS),
        ):
            with self.subTest(path=path):
                self.assertFalse(ci_changes.classify_paths([path]).responsive_needed)

    def test_unknown_or_unsafe_paths_fail_closed(self):
        for path in (
            "scripts/new_generator.py", "package.json", ".github/workflows/ci.yml",
            "assets/social-preview.png", "README.other.md", "", "../README.md",
            "/README.md", "site\\index.html",
        ):
            with self.subTest(path=path):
                self.assertTrue(ci_changes.classify_paths([path]).responsive_needed)

    def test_mixed_changes_do_not_skip_chrome(self):
        self.assertTrue(ci_changes.classify_paths(["README.md", "data/fonts.json"]).responsive_needed)
        self.assertTrue(ci_changes.classify_paths(["docs/guide.md", "unknown"]).responsive_needed)

    def test_known_empty_diff_skips_chrome(self):
        self.assertFalse(ci_changes.classify_paths([]).responsive_needed)


class EventTests(unittest.TestCase):
    def test_pr_uses_event_head_and_merge_base_not_checkout_head(self):
        fork = "c" * 40
        with patch.object(ci_changes, "_git", side_effect=[
            (fork + "\n").encode(), b"README.md\0",
        ]) as git:
            result = ci_changes.classify_event("pull_request", pr_event())
        self.assertFalse(result.responsive_needed)
        self.assertEqual(git.call_args_list[0].args[1:], ("merge-base", "--all", A, B))
        self.assertEqual(git.call_args_list[1].args[1:],
                         ("diff", "--name-only", "--no-renames", "-z", fork, B, "--"))

    def test_push_uses_full_before_after_range(self):
        with patch.object(ci_changes, "_git", return_value=b"README.md\0") as git:
            result = ci_changes.classify_event("push", {"before": A, "after": B})
        self.assertFalse(result.responsive_needed)
        self.assertEqual(git.call_args.args[1:],
                         ("diff", "--name-only", "--no-renames", "-z", A, B, "--"))

    def test_initial_push_deleted_branch_dispatch_and_unknown_event_run_full(self):
        events = [
            ("push", {"before": "0" * 40, "after": B}),
            ("push", {"before": A, "after": "0" * 40}),
            ("workflow_dispatch", {}), ("pull_request_target", pr_event()),
            ("push", {}), ("pull_request", {}), ("pull_request", None),
            ("pull_request", {"pull_request": {"base": None}}),
            ("push", {"before": "--help", "after": B}),
            ("push", {"before": [], "after": B}),
        ]
        for name, event in events:
            with self.subTest(name=name, event=event), patch.object(ci_changes, "_git") as git:
                self.assertTrue(ci_changes.classify_event(name, event).responsive_needed)
                git.assert_not_called()

    def test_diff_failure_timeout_missing_history_or_non_utf8_runs_full(self):
        failures = [
            OSError("no git"), subprocess.CalledProcessError(128, ["git", "diff"]),
            subprocess.TimeoutExpired(["git", "diff"], 30), UnicodeError("bad path"),
        ]
        for failure in failures:
            with self.subTest(failure=failure), patch.object(ci_changes, "_git", side_effect=failure):
                self.assertTrue(ci_changes.classify_event("push", {"before": A, "after": B}).responsive_needed)
        for raw in (b"\xff\0", b"README.md"):
            with self.subTest(raw=raw), patch.object(ci_changes, "_git", return_value=raw):
                self.assertTrue(ci_changes.classify_event("push", {"before": A, "after": B}).responsive_needed)

    def test_ambiguous_or_missing_pr_merge_base_runs_full(self):
        for raw in (b"", f"{A}\n{B}\n".encode()):
            with self.subTest(raw=raw), patch.object(ci_changes, "_git", return_value=raw):
                self.assertTrue(ci_changes.classify_event("pull_request", pr_event()).responsive_needed)

    def test_cli_malformed_missing_event_outputs_true(self):
        with tempfile.TemporaryDirectory() as directory:
            event, output = Path(directory) / "event.json", Path(directory) / "output"
            for content in ("{broken", "null", "[]"):
                event.write_text(content, encoding="utf-8")
                with self.subTest(content=content), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(ci_changes.main([
                        "--event-name", "push", "--event-path", str(event), "--output", str(output),
                    ]), 0)
            with contextlib.redirect_stdout(io.StringIO()):
                ci_changes.main(["--event-path", str(Path(directory) / "missing"), "--output", str(output)])
            self.assertEqual(output.read_text(), "responsive_needed=true\n" * 4)

    def test_cli_valid_event_appends_literal_false_to_github_output(self):
        with tempfile.TemporaryDirectory() as directory:
            event, output = Path(directory) / "event.json", Path(directory) / "output"
            event.write_text(json.dumps({"before": A, "after": B}), encoding="utf-8")
            output.write_text("other=value\n", encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event),
                                         "GITHUB_OUTPUT": str(output)}), \
                    patch.object(ci_changes, "_git", return_value=b"README.md\0"), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ci_changes.main([]), 0)
            self.assertEqual(output.read_text(), "other=value\nresponsive_needed=false\n")


class RealGitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "--quiet")

    def git(self, *args, data=None):
        env = dict(os.environ, GIT_AUTHOR_NAME="CI test", GIT_AUTHOR_EMAIL="test@example.invalid",
                   GIT_COMMITTER_NAME="CI test", GIT_COMMITTER_EMAIL="test@example.invalid")
        return subprocess.run(["git", *args], cwd=self.root, env=env, input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout

    def commit_object(self, files, parents=()):
        records = []
        for name, text in sorted(files.items()):
            blob = self.git("hash-object", "-w", "--stdin", data=text.encode()).strip()
            records.append(b"100644 blob " + blob + b"\t" + name.encode() + b"\0")
        tree = self.git("mktree", "-z", data=b"".join(records)).decode().strip()
        args = ["commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        return self.git(*args, data=b"synthetic test graph\n").decode().strip()

    def test_default_pr_merge_head_and_advanced_base_use_only_pr_changes(self):
        original = self.commit_object({"README.md": "old"})
        head = self.commit_object({"README.md": "new"}, [original])
        base = self.commit_object({"README.md": "old", "page.css": "base work"}, [original])
        merge = self.commit_object({"README.md": "new", "page.css": "base work"}, [base, head])
        # Mimic the default PR merge-ref HEAD without checking out contributor files.
        self.git("update-ref", "HEAD", merge)
        result = ci_changes.classify_event("pull_request", pr_event(base, head), self.root)
        self.assertFalse(result.responsive_needed)
        self.assertEqual(result.paths, ("README.md",))
        self.assertIn(b"page.css", self.git("diff", "--name-only", original, "HEAD"))

    def test_push_spans_multiple_commits_not_just_last_commit(self):
        before = self.commit_object({"README.md": "old"})
        first = self.commit_object({"README.md": "old", "page.css": "new"}, [before])
        after = self.commit_object({"README.md": "new", "page.css": "new"}, [first])
        result = ci_changes.classify_event("push", {"before": before, "after": after}, self.root)
        self.assertTrue(result.responsive_needed)
        self.assertEqual(set(result.paths), {"README.md", "page.css"})

    def test_forced_push_uses_two_endpoints_without_merge_base(self):
        before = self.commit_object({"README.md": "old"})
        after = self.commit_object({"README.md": "new"})
        result = ci_changes.classify_event("push", {"before": before, "after": after}, self.root)
        self.assertFalse(result.responsive_needed)
        self.assertEqual(result.paths, ("README.md",))

    def test_rename_checks_deleted_source_and_new_destination(self):
        before = self.commit_object({"README.md": "same"})
        after = self.commit_object({"page.css": "same"}, [before])
        result = ci_changes.classify_event("push", {"before": before, "after": after}, self.root)
        self.assertTrue(result.responsive_needed)
        self.assertEqual(set(result.paths), {"README.md", "page.css"})

    def test_nul_diff_preserves_spaces_newlines_and_unicode(self):
        before = self.commit_object({"README.md": "old"})
        names = ("a space.css", "a\nnewline.css", "中文.css")
        after = self.commit_object({"README.md": "old", **{name: "x" for name in names}}, [before])
        paths = ci_changes.changed_paths("push", {"before": before, "after": after}, self.root)
        self.assertEqual(set(paths), set(names))

    def test_missing_event_commit_runs_full(self):
        present = self.commit_object({"README.md": "old"})
        result = ci_changes.classify_event("push", {"before": present, "after": A}, self.root)
        self.assertTrue(result.responsive_needed)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    def test_paths_ignore_docs_but_never_readme_or_contract_template(self):
        for event in ("pull_request", "push"):
            block = self.workflow.split(f"  {event}:\n", 1)[1].split("\n  # Dispatchable", 1)[0]
            if event == "pull_request":
                block = block.split("\n  push:", 1)[0]
            ignored = [line.strip()[3:-1] for line in block.splitlines() if line.strip().startswith("- '")]
            self.assertTrue(any(fnmatch.fnmatchcase("docs/guide.md", pattern) for pattern in ignored))
            for protected in ("README.md", "README.zh-CN.md", ".github/PULL_REQUEST_TEMPLATE.md"):
                self.assertFalse(any(fnmatch.fnmatchcase(protected, pattern) for pattern in ignored), protected)

    def test_build_gate_still_runs_every_business_check(self):
        build = self.workflow.split("\n  build:\n", 1)[1].split("\n  responsive:\n", 1)[0]
        for command in (
            "python3 -m unittest discover -s tests -t .", "python scripts/build_readme.py --validate-only",
            "python scripts/experiences.py --check", "python scripts/sync_experiences.py --check",
            "python scripts/sync_contributors.py --check", "python scripts/check_entry.py --all --offline",
            "python scripts/build_readme.py --check", "python scripts/build_site.py --out",
        ):
            self.assertIn(command, build)
        self.assertNotIn("\n    if:", build)
        self.assertNotIn("sync-description:", self.workflow)

    def test_responsive_requires_successful_build_and_explicit_false_to_skip(self):
        self.assertIn("responsive_needed: ${{ steps.changes.outputs.responsive_needed }}", self.workflow)
        self.assertIn("run: python3 scripts/ci_changes.py", self.workflow)
        responsive = self.workflow.split("\n  responsive:\n", 1)[1]
        self.assertIn("needs: build", responsive)
        self.assertIn("if: needs.build.outputs.responsive_needed != 'false'", responsive)
        self.assertIn('python3 scripts/check_responsive.py --chrome "${{ steps.chrome.outputs.path }}"', responsive)

    def test_pr_cancellation_and_read_only_default_checkout(self):
        self.assertIn("group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}", self.workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", self.workflow)
        self.assertIn("contents: read", self.workflow)
        self.assertNotIn("pull_request_target:", self.workflow)
        self.assertNotIn("ref:", self.workflow)
        uses = [line.split("uses:", 1)[1].strip() for line in self.workflow.splitlines() if "uses:" in line]
        self.assertTrue(all(action.startswith("actions/") for action in uses))


if __name__ == "__main__":
    unittest.main()
