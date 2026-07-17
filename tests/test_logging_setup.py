import logging
import unittest

from telerixa_core import logging_setup


class LoggingSetupTests(unittest.TestCase):
    def test_color_formatter_marks_success_without_changing_plain_output(self):
        record = logging.LogRecord(
            "telerixa.test",
            logging_setup.SUCCESS_LEVEL,
            __file__,
            1,
            "Delivered",
            (),
            None,
        )
        plain = logging_setup.ConsoleColorFormatter(
            "%(levelname)s:%(message)s",
            use_color=False,
        ).format(record)
        colored = logging_setup.ConsoleColorFormatter(
            "%(levelname)s:%(message)s",
            use_color=True,
        ).format(record)

        self.assertEqual(plain, "SUCCESS:Delivered")
        self.assertEqual(
            colored,
            "\x1b[32mSUCCESS:Delivered\x1b[0m",
        )

    def test_log_success_uses_dedicated_level(self):
        logger = logging.getLogger("telerixa.success-test")
        with self.assertLogs(logger, level=logging_setup.SUCCESS_LEVEL) as captured:
            logging_setup.log_success(logger, "Message delivered")

        self.assertEqual(
            captured.output,
            ["SUCCESS:telerixa.success-test:Message delivered"],
        )


if __name__ == "__main__":
    unittest.main()
