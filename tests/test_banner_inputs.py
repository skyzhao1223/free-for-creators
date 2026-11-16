"""Pure/temp-fixture banner input gate tests; never invoke Chrome. License: CC0."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import banner_inputs
import render_banner


class FingerprintTests(unittest.TestCase):
    def test_stable_and_independent_of_mapping_order(self):
        self.assertEqual(banner_inputs.fingerprint({"a": b"a", "b": b"b"}),
                         banner_inputs.fingerprint({"b": b"b", "a": b"a"}))

    def test_file_names_and_bytes_are_both_inputs(self):
        original = banner_inputs.fingerprint({"a": b"same"})
        self.assertNotEqual(original, banner_inputs.fingerprint({"b": b"same"}))
        self.assertNotEqual(original, banner_inputs.fingerprint({"a": b"different"}))

    def test_render_config_change_invalidates(self):
        self.assertNotEqual(banner_inputs.fingerprint({"a": b"a"}, "v1"),
                            banner_inputs.fingerprint({"a": b"a"}, "v2"))

    def test_force_overrides_identical_inputs(self):
        current = dict(banner_inputs.fingerprint({"a": b"a"}),
                       images={name: "fixture-hash" for name in banner_inputs.IMAGES})
        self.assertEqual(banner_inputs.evaluate(current, current), (False, []))
        self.assertEqual(banner_inputs.evaluate(current, current, force=True), (True, ["forced"]))


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in banner_inputs.INPUTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<style>body{color:red}</style>221 resources" if name.endswith(".html")
                            else "# original checker or renderer", encoding="utf-8")
        for name in banner_inputs.IMAGES:
            (self.root / name).write_bytes(b"fixture image; not a real render")

    def stamp(self):
        return banner_inputs.stamp(self.root)

    def needed(self):
        return banner_inputs.check(self.root)[0]

    def test_missing_manifest_needs_render(self):
        self.assertTrue(self.needed())
        self.assertFalse((self.root / banner_inputs.MANIFEST).exists())

    def test_same_inputs_after_stamp_skip_render(self):
        self.stamp()
        self.assertFalse(self.needed())

    def test_data_change_with_identical_count_html_does_not_trigger(self):
        self.stamp()
        data = self.root / "data"
        data.mkdir()
        file = data / "music.json"
        file.write_text('{"entries":[{"name":"before","url":"https://old.example"}]}')
        self.assertFalse(self.needed())
        file.write_text('{"entries":[{"name":"after","url":"https://new.example"}]}')
        self.assertFalse(self.needed())

    def test_html_style_change_triggers(self):
        self.stamp()
        (self.root / banner_inputs.INPUTS[0]).write_text("<style>body{color:blue}</style>221 resources")
        self.assertTrue(self.needed())

    def test_html_count_change_triggers(self):
        self.stamp()
        (self.root / banner_inputs.INPUTS[0]).write_text("<style>body{color:red}</style>222 resources")
        self.assertTrue(self.needed())

    def test_renderer_change_triggers(self):
        self.stamp()
        (self.root / "scripts/render_banner.py").write_text("# changed renderer")
        self.assertTrue(self.needed())

    def test_checker_change_triggers(self):
        self.stamp()
        (self.root / "scripts/check_banner.py").write_text("# changed geometry thresholds")
        self.assertTrue(self.needed())

    def test_publisher_recipe_change_triggers(self):
        original = self.stamp()
        path = self.root / ".github/workflows/pages.yml"
        path.write_text("# changed font install or Chrome recipe")
        needed, reasons, current = banner_inputs.check(self.root)
        self.assertTrue(needed)
        self.assertIn("render inputs changed", reasons)
        self.assertNotEqual(current["fingerprint"], original["fingerprint"])

    def test_every_image_content_change_triggers_without_changing_input_fingerprint(self):
        original = self.stamp()
        for name in banner_inputs.IMAGES:
            with self.subTest(name=name):
                path = self.root / name
                content = path.read_bytes()
                path.write_bytes(b"hand-edited or corrupted output")
                needed, reasons, current = banner_inputs.check(self.root)
                self.assertTrue(needed)
                self.assertIn("rendered image contents changed", reasons)
                self.assertEqual(current["fingerprint"], original["fingerprint"])
                self.assertNotEqual(current["images"][name], original["images"][name])
                path.write_bytes(content)
        self.assertFalse(self.needed())

    def test_legacy_manifest_without_images_needs_first_render(self):
        current = banner_inputs.current_inputs(self.root)
        (self.root / banner_inputs.MANIFEST).write_text(json.dumps(current))
        needed, reasons, _ = banner_inputs.check(self.root)
        self.assertTrue(needed)
        self.assertIn("missing or invalid image hashes in manifest", reasons)

    def test_incomplete_or_wrong_image_hashes_fail_closed(self):
        for images in (None, [], {}, {banner_inputs.IMAGES[0]: "partial"},
                       {name: "wrong" for name in banner_inputs.IMAGES}):
            with self.subTest(images=images):
                manifest = dict(banner_inputs.current_inputs(self.root), images=images)
                (self.root / banner_inputs.MANIFEST).write_text(json.dumps(manifest))
                self.assertTrue(self.needed())

    def test_stamp_contains_sha256_for_all_outputs(self):
        import hashlib
        manifest = self.stamp()
        self.assertEqual(manifest["images"], {
            name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
            for name in banner_inputs.IMAGES})
        pure_inputs = banner_inputs.current_inputs(self.root)
        self.assertNotIn("images", pure_inputs)
        self.assertEqual(manifest["fingerprint"], pure_inputs["fingerprint"])

    def test_every_missing_image_triggers(self):
        self.stamp()
        for name in banner_inputs.IMAGES:
            with self.subTest(name=name):
                path = self.root / name
                content = path.read_bytes()
                path.unlink()
                self.assertTrue(self.needed())
                path.write_bytes(content)
        self.assertFalse(self.needed())

    def test_empty_image_triggers_and_cannot_be_stamped(self):
        self.stamp()
        path = self.root / banner_inputs.IMAGES[0]
        path.write_bytes(b"")
        self.assertTrue(self.needed())
        with self.assertRaisesRegex(ValueError, "Cannot stamp"):
            self.stamp()

    def test_invalid_manifest_fails_closed(self):
        for text in ("not json", "[]", "null", "{}", '{"fingerprint":"stale"}'):
            with self.subTest(text=text):
                (self.root / banner_inputs.MANIFEST).write_text(text)
                self.assertTrue(self.needed())

    def test_stamp_is_idempotent_without_timestamp_or_rewrite(self):
        first = self.stamp()
        path = self.root / banner_inputs.MANIFEST
        original = path.read_bytes()
        os.utime(path, ns=(1234567890000000000, 1234567890000000000))
        mtime = path.stat().st_mtime_ns
        self.assertEqual(first, self.stamp())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(json.loads(original), first)

    def test_check_cli_emits_github_output_and_does_not_stamp(self):
        output = self.root / "github-output.txt"
        output.write_text("previous=value\n")
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = banner_inputs.main(["--root", str(self.root), "--check", "--github-output", str(output)])
        self.assertEqual(code, 0)
        self.assertIn("needs_render=true", stdout.getvalue())
        self.assertEqual(output.read_text(), "previous=value\nneeds_render=true\n")
        self.assertFalse((self.root / banner_inputs.MANIFEST).exists())

    def test_check_cli_false_then_force_true(self):
        self.stamp()
        for force, expected in (([], "false"), (["--force"], "true")):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = banner_inputs.main(["--root", str(self.root), "--check", *force])
            self.assertEqual(code, 0)
            self.assertIn("needs_render=" + expected, stdout.getvalue())

    def test_github_output_can_use_environment(self):
        output = self.root / "github-output.txt"
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(banner_inputs.main(["--root", str(self.root), "--check", "--github-output"]), 0)
        self.assertEqual(output.read_text(), "needs_render=true\n")

    def test_stamp_cli_missing_image_fails_without_manifest(self):
        (self.root / banner_inputs.IMAGES[0]).unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(banner_inputs.main(["--root", str(self.root), "--stamp"]), 1)
        self.assertFalse((self.root / banner_inputs.MANIFEST).exists())


class RendererContractTests(unittest.TestCase):
    def test_linux_generic_font_assumption_is_not_forced_on_other_platforms(self):
        with patch.object(render_banner.sys, "platform", "darwin"):
            with patch.object(render_banner.subprocess, "run") as run:
                render_banner.check_linux_fonts()
        run.assert_not_called()

    def test_linux_rejects_changed_generic_font(self):
        with patch.object(render_banner.sys, "platform", "linux"):
            with patch.object(render_banner.subprocess, "run") as run:
                run.return_value.stdout = "Unexpected Sans"
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, "DejaVu Sans"):
                        render_banner.check_linux_fonts()

    def test_linux_requires_emoji_and_cjk_fonts(self):
        with patch.object(render_banner.sys, "platform", "linux"):
            with patch.object(render_banner.subprocess, "run") as run:
                run.return_value.stdout = "DejaVu Sans"
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, "Missing noto color emoji"):
                        render_banner.check_linux_fonts()

    def test_render_recipe_checks_pixels_and_geometry_before_copying(self):
        # Synthetic image/subprocess adapters verify the recipe without Chrome,
        # cwebp, Pillow or numpy, and without touching repository assets.
        from types import ModuleType
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "assets").mkdir()
            (root / "assets/social-preview.html").write_text("<body>fixture</body>")
            fake_pil = ModuleType("PIL")
            fake_image = MagicMock()
            fake_image.LANCZOS = 1
            opened = fake_image.open.return_value.__enter__.return_value
            opened.size = (2560, 1280)
            opened.mode = "RGB"
            opened.resize.return_value.save.side_effect = lambda path, **kwargs: path.write_bytes(b"preview")
            fake_pil.Image = fake_image
            calls = []

            def command(argv, **kwargs):
                calls.append(argv)
                for flag in argv:
                    if flag.startswith("--screenshot="):
                        Path(flag.split("=", 1)[1]).write_bytes(b"screenshot")
                if "-o" in argv:
                    Path(argv[argv.index("-o") + 1]).write_bytes(b"webp")

            import check_banner
            with patch.dict(sys.modules, {"PIL": fake_pil}):
                with patch.object(render_banner, "check_linux_fonts") as fonts, \
                        patch.object(render_banner.subprocess, "run", side_effect=command), \
                        patch.object(render_banner, "check_pixels") as pixels, \
                        patch.object(check_banner, "category_count", return_value=11), \
                        patch.object(check_banner, "measure", return_value={}) as measure, \
                        patch.object(check_banner, "evaluate", return_value=([], [])) as evaluate:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(render_banner.render("chrome-fixture", root), 0)
                    fonts.assert_called_once()
                    pixels.assert_called_once()
                    measure.assert_called_once_with(root / "assets/social-preview.html", "chrome-fixture")
                    evaluate.assert_called_once_with({}, 11)
                    self.assertEqual((root / "assets/banner.png").read_bytes(), b"screenshot")
                    self.assertEqual((root / "assets/banner.webp").read_bytes(), b"webp")
                    self.assertFalse((root / banner_inputs.MANIFEST).exists())
                    # A subsequent geometry failure must not replace approved images.
                    (root / "assets/banner.png").write_bytes(b"approved")
                    evaluate.return_value = (["overlap"], [])
                    with contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(ValueError, "overlap"):
                            render_banner.render("chrome-fixture", root)
                    self.assertEqual((root / "assets/banner.png").read_bytes(), b"approved")
            screenshot = next(argv for argv in calls if "--headless=new" in argv)
            for flag in ("--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                         "--window-size=1280,640", "--virtual-time-budget=8000"):
                self.assertIn(flag, screenshot)
            encoder = next(argv for argv in calls if "-sharp_yuv" in argv)
            self.assertEqual(encoder[:6], ["cwebp", "-quiet", "-q", "90", "-sharp_yuv", encoder[5]])
            opened.resize.assert_any_call((1280, 640), fake_image.LANCZOS)

    def test_cli_passes_chrome_and_root_without_implicit_stamp(self):
        with patch.object(render_banner, "render", return_value=0) as render:
            self.assertEqual(render_banner.main(["--chrome", "chrome-fixture", "--root", "/fixture"]), 0)
        render.assert_called_once_with("chrome-fixture", Path("/fixture"))


if __name__ == "__main__":
    unittest.main()
