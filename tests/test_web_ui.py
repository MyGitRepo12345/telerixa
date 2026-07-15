import sqlite3
import tempfile
import unittest
from pathlib import Path

import web_ui


class WebUiDatabaseTests(unittest.TestCase):
    def test_ui_connection_closes_after_context_manager(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            connection = web_ui.connect_db(Path(temp_dir) / "ui-state.db")

            with connection as active_connection:
                active_connection.execute("CREATE TABLE sample (id INTEGER)")

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
