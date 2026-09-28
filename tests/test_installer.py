"""Release/install tests use temporary fixtures, never the user's Hermes directory."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("install_skill")
builder = load("build_release")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.skills = self.base / "skills"
        self.calls = []
        self.write("VERSION", "2.0.0\n")
        self.write("requirements.lock", "# fixture has no packages\n")
        self.write("scripts/video_kb.py", "print('fixture')\n")
        self.write("src/video_kb/__init__.py", "")
        self.write("skills/feishu-video-to-notion/SKILL.md", "fixture skill\n")
        self.write("skills/feishu-video-to-notion/references/help.md", "fixture reference\n")
        builder.build(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, text):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[1:3] == ["-m", "venv"]:
            path = Path(args[3]) / "bin" / "python"
            path.parent.mkdir(parents=True)
            path.write_text("fixture")
        return subprocess.CompletedProcess(args, 0)

    def install(self, **kwargs):
        return installer.install(self.source, self.skills, Path(sys.executable), runner=self.runner, **kwargs)

    def test_dry_run_has_no_mutations_or_subprocesses(self):
        result = self.install(dry_run=True)
        self.assertEqual(result["action"], "install_preview")
        self.assertFalse(self.skills.exists())
        self.assertEqual(self.calls, [])

    def test_unknown_existing_skill_is_preserved(self):
        target = self.skills / installer.SKILL_NAME
        target.mkdir(parents=True)
        (target / "notes.txt").write_text("user work")
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual((target / "notes.txt").read_text(), "user work")
        self.assertFalse(self.calls)

    def test_manifest_tamper_cannot_install(self):
        self.write("scripts/video_kb.py", "raise SystemExit('tampered')")
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertFalse(self.skills.exists())

    def test_manifest_path_escape_rejected(self):
        path = self.source / installer.MANIFEST
        manifest = json.loads(path.read_text())
        manifest["files"]["../outside"] = "a" * 64
        path.write_text(json.dumps(manifest))
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertFalse(self.skills.exists())

    def test_symlink_source_and_destination_rejected(self):
        outside = self.base / "outside"
        outside.write_text("private")
        p = self.source / "scripts" / "video_kb.py"
        p.unlink()
        p.symlink_to(outside)
        with self.assertRaises(installer.InstallError):
            self.install()
        p.unlink()
        self.write("scripts/video_kb.py", "print('fixture')\n")
        builder.build(self.source)
        real = self.base / "real-skills"
        real.mkdir()
        self.skills.symlink_to(real, target_is_directory=True)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(list(real.iterdir()), [])

    def test_install_is_scoped_pinned_and_repeatable(self):
        self.skills.mkdir()
        other = self.skills / "other-skill"
        other.mkdir()
        (other / "note").write_text("keep me")
        result = self.install()
        target = Path(result["install_path"])
        marker = json.loads((target / installer.MARKER).read_text())
        self.assertEqual(marker["release"], result["release"])
        self.assertTrue((target / "releases" / marker["release"] / ".venv" / "bin" / "python").is_file())
        pip = next(args for args in self.calls if "pip" in args)
        self.assertIn("--require-hashes", pip)
        self.assertIn("--isolated", pip)
        self.assertIn("https://pypi.org/simple", pip)
        self.assertEqual((other / "note").read_text(), "keep me")
        count = len(self.calls)
        self.assertTrue(self.install()["already_installed"])
        self.assertEqual(len(self.calls), count)

    def test_failed_upgrade_keeps_old_runtime_and_user_files(self):
        result = self.install()
        target = Path(result["install_path"])
        old_marker = (target / installer.MARKER).read_bytes()
        old_skill = (target / "SKILL.md").read_bytes()
        self.write("skills/feishu-video-to-notion/SKILL.md", "next version\n")
        builder.build(self.source)

        def fail(args, **kwargs):
            if "pip" in args:
                raise subprocess.CalledProcessError(1, args)
            return self.runner(args, **kwargs)

        with self.assertRaises(subprocess.CalledProcessError):
            installer.install(self.source, self.skills, Path(sys.executable), runner=fail)
        self.assertEqual((target / installer.MARKER).read_bytes(), old_marker)
        self.assertEqual((target / "SKILL.md").read_bytes(), old_skill)
        self.assertEqual([p.name for p in (target / "releases").iterdir()], [result["release"]])

    def test_successful_upgrade_retains_previous_version(self):
        first = self.install()
        self.write("skills/feishu-video-to-notion/SKILL.md", "next version\n")
        builder.build(self.source)
        second = self.install()
        target = Path(second["install_path"])
        marker = json.loads((target / installer.MARKER).read_text())
        self.assertNotEqual(first["release"], second["release"])
        self.assertEqual(marker["previous_release"], first["release"])
        self.assertTrue((target / "releases" / first["release"]).is_dir())
        self.assertEqual((target / "SKILL.md").read_text(), "next version\n")

    def test_user_modified_skill_never_overwritten(self):
        result = self.install()
        target = Path(result["install_path"])
        (target / "SKILL.md").write_text("my changes")
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual((target / "SKILL.md").read_text(), "my changes")

    def test_archive_excludes_local_data_and_is_deterministic(self):
        self.write(".env", "SECRET=do-not-ship")
        self.write(".venv/private.txt", "local")
        self.write("src/video_kb/__pycache__/private.py", "local")
        self.write("src/video_kb/token.json", "local")
        self.write("tests/test_fake.py", "test")
        self.write("IMPLEMENTATION_CONTRACT.md", "internal")
        first = self.base / "one.zip"
        second = self.base / "two.zip"
        builder.build(self.source, first)
        builder.build(self.source, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith("RELEASE_MANIFEST.json") for name in names))
            self.assertFalse(any(any(secret in name for secret in [".env", "private", "token.json", "tests/", "IMPLEMENTATION"]) for name in names))


if __name__ == "__main__":
    unittest.main()
