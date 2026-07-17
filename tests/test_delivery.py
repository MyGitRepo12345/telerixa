import logging
import unittest

from i18n import configure_language
from telerixa_core.delivery import (
    DeliverySettings,
    DeliveryStateActions,
    handle_pending_send_failure,
    prepare_pending_delivery,
)
from telerixa_core.models import SendResult


class FakeState:
    def __init__(self):
        self.pending = []
        self.boundaries = []
        self.processed = []
        self.failures = []
        self.deleted = []
        self.progress = []
        self.archived = []
        self.attempts_after = 1

    def actions(self):
        return DeliveryStateActions(
            add_pending_message=lambda *args: self.pending.append(args),
            archive_pending_failure=lambda *args: self.archived.append(args),
            advance_last_seen_id=lambda *args: self.boundaries.append(args),
            delete_pending_message=lambda *args: self.deleted.append(args),
            get_processed_group_message_ids=lambda channel, grouped_id: [10, 11],
            mark_pending_failed=self.mark_pending_failed,
            mark_processed_message=lambda *args: self.processed.append(args),
            update_pending_delivery_progress=lambda *args, **kwargs: self.progress.append(
                (args, kwargs)
            ),
        )

    def mark_pending_failed(self, *args, **kwargs):
        self.failures.append((args, kwargs))
        return self.attempts_after


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        configure_language("en")
        self.state = FakeState()
        self.logger = logging.getLogger("test.delivery")
        self.logger.disabled = True
        self.settings = DeliverySettings(
            max_queue_attempts=3,
            webhook_url="",
            alert_user_id="",
        )

    def test_prepare_pending_delivery_creates_outbox_before_send(self):
        boundary = prepare_pending_delivery(
            self.state.actions(),
            "demo",
            10,
            777,
            [10, 11],
            "delivery started",
        )

        self.assertEqual(boundary, 11)
        self.assertEqual(len(self.state.pending), 1)
        self.assertEqual(
            self.state.pending[0],
            ("demo", 10, 777, "delivery started", None),
        )
        self.assertEqual(self.state.boundaries, [("demo", 11)])
        self.assertEqual(
            self.state.processed,
            [
                ("demo", 10, 777, "queued"),
                ("demo", 11, 777, "queued"),
            ],
        )

    async def test_transient_failure_stays_in_outbox(self):
        kept, reason, attempts = await handle_pending_send_failure(
            self.state.actions(),
            self.logger,
            self.settings,
            "demo",
            10,
            None,
            [10],
            SendResult.transient_retry("network down"),
            "fallback",
        )

        self.assertTrue(kept)
        self.assertEqual(reason, "network down")
        self.assertEqual(attempts, 1)
        self.assertFalse(self.state.deleted)
        self.assertFalse(self.state.failures[0][1]["count_attempt"])

    async def test_terminal_failure_drops_entire_album(self):
        kept, reason, attempts = await handle_pending_send_failure(
            self.state.actions(),
            self.logger,
            self.settings,
            "demo",
            10,
            777,
            [10, 11],
            SendResult.terminal_failure("bad webhook"),
            "fallback",
        )

        self.assertFalse(kept)
        self.assertIn("bad webhook", reason)
        self.assertEqual(attempts, 1)
        self.assertFalse(self.state.deleted)
        self.assertEqual(len(self.state.archived), 1)
        archive_args = self.state.archived[0]
        self.assertEqual(archive_args[:4], ("demo", 10, 777, [10, 11]))
        self.assertEqual(archive_args[5:], ("terminal", "retry queue", 1))

    async def test_max_attempts_archives_message_with_distinct_reason(self):
        self.state.attempts_after = 3

        kept, reason, attempts = await handle_pending_send_failure(
            self.state.actions(),
            self.logger,
            self.settings,
            "demo",
            20,
            None,
            [20],
            SendResult.retry("Discord unavailable"),
            "fallback",
        )

        self.assertFalse(kept)
        self.assertEqual(attempts, 3)
        self.assertIn("3", reason)
        self.assertEqual(self.state.archived[0][5], "max_attempts")


if __name__ == "__main__":
    unittest.main()
