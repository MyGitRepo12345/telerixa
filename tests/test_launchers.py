import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_windows_launcher_installs_missing_dependencies(self):
        launcher = (ROOT_DIR / "run.bat").read_text(encoding="utf-8")

        self.assertIn("import aiohttp, telethon", launcher)
        self.assertIn("pip install --disable-pip-version-check", launcher)
        self.assertIn("-r requirements.txt", launcher)
        self.assertIn("pip uninstall -y static-ffmpeg", launcher)
        self.assertIn('rmdir /s /q "%OBSOLETE_STATIC_FFMPEG_DIR%"', launcher)

    def test_linux_launcher_migrates_obsolete_dependency_in_place(self):
        launcher = (ROOT_DIR / "run.sh").read_text(encoding="utf-8")

        self.assertIn('VENV_SCHEMA_VERSION="2"', launcher)
        self.assertIn('VENV_NEEDS_MIGRATION="1"', launcher)
        self.assertIn("pip uninstall -y static-ffmpeg", launcher)
        self.assertIn('rm -rf -- "$SITE_PACKAGES_DIR/static_ffmpeg"', launcher)
        self.assertIn('> "$VENV_SCHEMA_FILE"', launcher)

    def test_linux_launchers_open_konsole_when_started_without_terminal(self):
        for launcher_name in ("run.sh", "run_ui.sh"):
            with self.subTest(launcher=launcher_name):
                launcher = (ROOT_DIR / launcher_name).read_text(encoding="utf-8")
                self.assertIn('[ ! -t 0 ]', launcher)
                self.assertIn("TELERIXA_VISIBLE_TERMINAL", launcher)
                self.assertIn("command -v konsole", launcher)
                self.assertIn("--hold", launcher)


if __name__ == "__main__":
    unittest.main()
