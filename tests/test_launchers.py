import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
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
