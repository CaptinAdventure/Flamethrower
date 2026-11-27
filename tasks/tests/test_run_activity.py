from datetime import datetime, timedelta, timezone
from decimal import Decimal

from acp.schema import AgentThoughtChunk
from asgiref.sync import async_to_sync
from django.test import TestCase
from django.urls import reverse

from ..models import ACPUsageSnapshot, AgentRun, RunEvent, Task
from ..views.activity import format_activity


class RunActivityPageTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(title="Task with sessions", request="Request", plan="Plan")
        self.run = AgentRun.objects.create(
            task=self.task, stage=Task.Stage.DEVELOPER, task_version=self.task.version,
            request_snapshot="Snapshot request", plan_snapshot="## Snapshot plan",
            status=AgentRun.Status.RUNNING, session_id="session-identifier",
        )
        RunEvent.objects.create(run=self.run, kind="message", content="Unique **session activity**.")
        self.detail_url = reverse("task_detail", args=[self.task.pk])
        self.activity_url = reverse("run_activity", args=[self.task.pk, self.run.pk])


    def test_activity_preserves_recorded_timestamps_and_message_merging(self):
        first = self.run.events.get()
        second = RunEvent.objects.create(run=self.run, kind="message", content=" Continued message.")
        tool = RunEvent.objects.create(run=self.run, kind="tool", content="Unformatted tool activity")
        permission = RunEvent.objects.create(run=self.run, kind="permission decision", content="{}")
        other = RunEvent.objects.create(run=self.run, kind="status", content="Status update")
        start = datetime(2026, 11, 25, 5, 37, 15, tzinfo=timezone.utc)
        for index, event in enumerate((first, second, tool, permission, other)):
            RunEvent.objects.filter(pk=event.pk).update(created_at=start + timedelta(seconds=index))

        events = format_activity(list(self.run.events.all()))
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0]["content"], first.content + second.content)
        for item, index in zip(events, (0, 2, 3, 4)):
            timestamp = start + timedelta(seconds=index)
            self.assertEqual(item["created_at"], timestamp)


    def test_activity_run_must_belong_to_the_task(self):
        other = Task.objects.create(title="Other task", request="Other request")
        self.assertEqual(self.client.get(reverse("run_activity", args=[other.pk, self.run.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("run_activity", args=[self.task.pk, self.run.pk + 1])).status_code, 404)

    def test_old_run_links_redirect_and_reject_invalid_selections(self):
        self.assertRedirects(self.client.get(self.detail_url, {"run": self.run.pk}), self.activity_url)
        for selection in ("invalid", self.run.pk + 1):
            self.assertEqual(self.client.get(self.detail_url, {"run": selection}).status_code, 404)

    def test_historical_session_is_not_replaced_by_latest_session(self):
        AgentRun.objects.create(
            task=self.task, task_version=self.task.version, request_snapshot="Latest request",
            plan_snapshot="Latest plan", status=AgentRun.Status.COMPLETED,
        )
        page = self.client.get(self.activity_url)
        self.assertEqual(page.context["run"].pk, self.run.pk)

    def test_activity_page_renders_formatted_and_unformatted_tool_events(self):
        RunEvent.objects.create(
            run=self.run, kind="tool",
            content='{"kind":"execute","rawInput":{"command":"echo safe"}}',
        )
        RunEvent.objects.create(run=self.run, kind="tool", content="<script>unsafe()</script>")

        response = self.client.get(self.activity_url)

        self.assertContains(response, "<h2>Tool activity</h2>", html=True)
        self.assertContains(response, "<strong>Command:</strong>", html=True)
        self.assertContains(response, "echo safe")
        self.assertContains(response, "Unable to format tool activity")
        self.assertContains(response, "&lt;script&gt;unsafe()&lt;/script&gt;")

    def test_activity_page_shows_reported_usage_and_marks_missing_values(self):
        response = self.client.get(self.activity_url)
        self.assertContains(response, "ACP-reported usage")
        self.assertContains(response, "Not reported")
        self.assertContains(response, "Not reported")

        ACPUsageSnapshot.objects.create(
            run=self.run, cost_amount=Decimal("0.125"), cost_currency="USD",
            context_used=40, context_size=100, total_tokens=90, input_tokens=60, output_tokens=30,
        )
        response = self.client.get(self.activity_url)
        self.assertContains(response, "0.125 USD")
        self.assertContains(response, "40")
        self.assertContains(response, "90 total")
        self.assertContains(response, "Not reported")

    def test_markdown_messages_remain_ordered_around_tool_events(self):
        RunEvent.objects.create(run=self.run, kind="message", content="**Before**")
        RunEvent.objects.create(
            run=self.run, kind="tool",
            content='{"kind":"execute","rawInput":{"command":"echo middle"}}',
        )
        RunEvent.objects.create(run=self.run, kind="message", content="*After*")

        response = self.client.get(self.activity_url)
        content = response.content.decode()

        before = content.index("<strong>Before</strong>")
        tool = content.index("echo middle")
        after = content.index("<em>After</em>")
        self.assertLess(before, tool)
        self.assertLess(tool, after)

    def test_acp_thought_updates_are_persisted_and_visible_in_activity(self):
        from ..agent import RunClient

        client = RunClient(self.run.pk)
        for content in (
            {"type": "text", "text": "First **thought**"},
            {"type": "text", "text": " continued."},
            {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="},
        ):
            update = AgentThoughtChunk.model_validate({"sessionUpdate": "agent_thought_chunk", "content": content})
            async_to_sync(client.session_update)("session-identifier", update)
        thoughts = list(self.run.events.filter(kind__startswith="thought").values_list("kind", "content"))
        self.assertEqual(thoughts[:2], [("thought", "First **thought**"), ("thought", " continued.")])
        self.assertEqual(thoughts[2][0], "thought content")
        self.assertIn('"data": "aGVsbG8="', thoughts[2][1])
        events = format_activity(list(self.run.events.all()))
        self.assertEqual(events[1]["content"], "First **thought** continued.")
        page = self.client.get(self.activity_url)
        self.assertContains(page, "Agent thinking")
        self.assertContains(page, "First <strong>thought</strong> continued.")
        self.assertContains(page, "Agent thinking attachment")
        self.assertContains(page, "image/png")
