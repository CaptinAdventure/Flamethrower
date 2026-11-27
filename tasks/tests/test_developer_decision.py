from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import AcpEndpoint, AgentRun, Approval, RunEvent, Task


@override_settings(ACP_PORT="12345")
class DeveloperDecisionTests(TestCase):
    def setUp(self):
        self.endpoint = AcpEndpoint.objects.create(name="Test ACP", port=12345)
        self.task = Task.objects.create(
            title="Change", request="A request", plan="A plan", stage=Task.Stage.DEVELOPER,
            state=Task.State.PLAN_APPROVED, version=2,
            automatic_pause_reason="Paused for an ACP permission request.",
        )
        self.run = AgentRun.objects.create(
            task=self.task, stage=Task.Stage.DEVELOPER, status=AgentRun.Status.COMPLETED,
            task_version=2, request_snapshot="A request", plan_snapshot="A plan",
        )
        self.values = {"version": 2, "stage_version": 0}

    def post(self, name, **values):
        return self.client.post(reverse(name, args=[self.task.pk]), {**self.values, **values})

    def test_completed_paused_run_requires_explicit_decision(self):
        self.assertEqual(self.post("continue_task").status_code, 409)
        self.assertEqual(self.post("start_run").status_code, 409)
        self.assertEqual(self.client.post(f"/tasks/{self.task.pk}/automatic-mode/").status_code, 404)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.DEVELOPER)

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_approval_advances_and_restarts_automation(self, worker):
        self.assertEqual(self.post("advance_stage").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        self.assertEqual(self.task.automatic_pause_reason, "")
        self.assertIn(f"approved Developer run #{self.run.pk}", self.task.approvals.get(kind=Approval.Kind.STAGE).content)
        testing = self.task.runs.first()
        self.assertEqual(testing.stage, Task.Stage.TESTING)
        worker.assert_called_once_with()
        self.assertEqual(self.post("advance_stage").status_code, 409)

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_refusal_requires_feedback_and_starts_fresh_run(self, worker):
        self.assertEqual(self.post("refuse_developer", reason="  ").status_code, 400)
        self.assertEqual(self.task.runs.count(), 1)
        self.assertEqual(self.post("refuse_developer", reason="Fix the contrast").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.stage, self.task.work_cycle, self.task.automatic_pause_reason), (Task.Stage.DEVELOPER, 1, ""))
        run = self.task.runs.first()
        self.assertEqual((run.stage, run.work_cycle, run.task_version), (Task.Stage.DEVELOPER, 1, 2))
        self.assertIn("Fix the contrast", run.context_snapshot)
        self.assertIn(f"refused Developer run #{self.run.pk}", self.task.approvals.get(kind=Approval.Kind.STAGE).content)
        worker.assert_called_once_with()
        self.assertEqual(self.post("refuse_developer", reason="Again").status_code, 409)
        self.assertEqual(self.task.runs.count(), 2)

    @override_settings(ACP_PORT="")
    def test_refusal_without_acp_preserves_feedback_and_does_not_change_task(self):
        self.endpoint.delete()
        response = self.post("refuse_developer", reason="Fix the contrast")
        self.assertEqual(response.status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_cycle, 0)
        self.assertEqual(self.task.runs.count(), 1)

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_refusal_starts_rework_run(self, worker):
        self.assertEqual(self.post("refuse_developer", reason="Fix the contrast").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_cycle, 1)
        worker.assert_called_once_with()

    def test_cannot_continue_paused_active_run(self):
        self.run.status = AgentRun.Status.RUNNING
        self.run.save()
        self.assertEqual(self.post("continue_task").status_code, 409)
        self.task.refresh_from_db()
        self.assertIn("permission", self.task.automatic_pause_reason)

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_unpaused_workflow_advances_automatically(self, worker):
        Task.objects.filter(pk=self.task.pk).update(automatic_pause_reason="")
        RunEvent.objects.create(
            run=self.run, kind="message",
            content="FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE",
        )
        from ..automation import continue_automatic_run

        continue_automatic_run(self.run.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        worker.assert_called_once()
