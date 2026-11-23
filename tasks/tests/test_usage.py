from decimal import Decimal

from asgiref.sync import async_to_sync
from django.test import TestCase

from ..agent import RunClient
from ..models import ACPUsageSnapshot, AgentRun, Task


class ACPUsagePersistenceTests(TestCase):
    def setUp(self):
        task = Task.objects.create(title="Usage tracking", request="Request")
        self.run = AgentRun.objects.create(
            task=task, task_version=task.version, request_snapshot="Request", plan_snapshot="Plan",
        )
        self.client = RunClient(self.run.pk)

    def update(self, payload):
        class FakeUpdate:
            def model_dump(self, **kwargs):
                return payload

        async_to_sync(self.client.session_update)("session", FakeUpdate())

    def test_partial_updates_preserve_existing_metrics_and_invalid_reports_are_ignored(self):
        self.update({
            "sessionUpdate": "usage_update", "used": 4, "size": 20,
            "cost": {"amount": 0.5, "currency": "USD"},
        })
        snapshot = ACPUsageSnapshot.objects.get(run=self.run)
        first_context_timestamp = snapshot.context_reported_at
        self.assertEqual((snapshot.cost_amount, snapshot.cost_currency), (Decimal("0.5"), "USD"))
        self.assertIsNone(snapshot.total_tokens)

        with self.assertLogs("tasks.agent", level="WARNING"):
            self.update({
                "sessionUpdate": "usage_update", "used": 5, "size": 20,
                "cost": {"amount": -1, "currency": "USD"},
            })
        snapshot.refresh_from_db()
        self.assertEqual((snapshot.context_used, snapshot.cost_amount), (4, Decimal("0.5")))
        self.assertEqual(snapshot.context_reported_at, first_context_timestamp)

        with self.assertLogs("tasks.agent", level="WARNING"):
            self.update({"sessionUpdate": "usage_update", "used": 25, "size": 20})
        snapshot.refresh_from_db()
        self.assertEqual((snapshot.context_used, snapshot.context_size), (4, 20))

        self.update({"sessionUpdate": "usage_update", "used": 8, "size": 24})
        snapshot.refresh_from_db()
        self.assertEqual((snapshot.context_used, snapshot.context_size), (8, 24))
        self.assertEqual((snapshot.cost_amount, snapshot.cost_currency), (Decimal("0.5"), "USD"))

    def test_malformed_and_absent_token_metrics_do_not_replace_valid_values(self):
        with self.assertLogs("tasks.agent", level="WARNING"):
            self.update({"sessionUpdate": "usage_update", "used": -1, "size": 20})
        self.assertFalse(ACPUsageSnapshot.objects.filter(run=self.run).exists())

        from ..agent import record_token_usage

        async_to_sync(record_token_usage)(self.run.pk, {
            "totalTokens": 100, "inputTokens": 70, "outputTokens": 30,
        })
        with self.assertLogs("tasks.agent", level="WARNING"):
            async_to_sync(record_token_usage)(self.run.pk, {
                "totalTokens": 50, "inputTokens": -1, "outputTokens": 51,
            })
        snapshot = ACPUsageSnapshot.objects.get(run=self.run)
        self.assertEqual((snapshot.total_tokens, snapshot.input_tokens, snapshot.output_tokens), (100, 70, 30))
        self.assertIsNone(snapshot.thought_tokens)
