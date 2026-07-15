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

    def actions(self):
        return DeliveryStateActions(
            add_pending_message=lambda *args: self.pending.append(args),
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
        return 1


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
        self.assertEqual(self.state.deleted, [("demo", 10)])
        self.assertEqual(
            self.state.processed,
            [
                ("demo", 10, 777, "failed"),
                ("demo", 11, 777, "failed"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
