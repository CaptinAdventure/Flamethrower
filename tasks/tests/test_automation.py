import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import AcpEndpoint, AgentRun, GateDecision, PermissionRequest, RunEvent, Task


class AutomationTests(TestCase):
    def setUp(self):
        self.endpoint = AcpEndpoint.objects.create(name="Test ACP", port=12345)
        self.task = Task.objects.create(
            title="Change", request="request", plan="plan",
            stage=Task.Stage.ARCHITECT,
        )

    def continue_workflow(self):
        return self.client.post(reverse("continue_task", args=[self.task.pk]), {
            "version": self.task.version,
            "stage_version": self.task.stage_version,
        })

    def report(self, result="passed", recommendation="advance"):
        data = {
            "result": result,
            "recommendation": recommendation,
            "summary": "Automated checks finished",
            "evidence": "The requested checks ran.",
            "untested": "Visual behavior",
            "reason": "The report describes the requested result.",
        }
        return "FLAMETHROWER_GATE_RECOMMENDATION\n" + json.dumps(data) + "\nEND_FLAMETHROWER_GATE_RECOMMENDATION"

    def completed_run(self, stage, report=None):
        run = AgentRun.objects.create(
            task=self.task, stage=stage, task_version=self.task.version,
            work_cycle=self.task.work_cycle, request_snapshot="request",
            plan_snapshot="plan", status=AgentRun.Status.COMPLETED,
        )
        if report:
            RunEvent.objects.create(run=run, kind="message", content=report)
        return run

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_plan_approval_starts_developer_by_default(self, worker):
        self.assertEqual(self.client.post(reverse("approve_request", args=[self.task.pk]), {
            "version": self.task.version,
            "stage_version": self.task.stage_version,
        }).status_code, 302)
        self.task.refresh_from_db()
        self.assertFalse(self.task.runs.exists())
        self.assertEqual(self.client.post(reverse("approve_plan", args=[self.task.pk]), {
            "version": self.task.version,
            "stage_version": self.task.stage_version,
        }).status_code, 302)
        self.task.refresh_from_db()
        run = self.task.runs.get()
        self.assertEqual(self.task.stage, Task.Stage.DEVELOPER)
        self.assertEqual(run.stage, Task.Stage.DEVELOPER)
        worker.assert_called_once_with()

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_approving_request_starts_architect_by_default(self, worker):
        self.task.plan = ""
        self.task.save()
        self.assertEqual(self.client.post(reverse("approve_request", args=[self.task.pk]), {
            "version": self.task.version, "stage_version": self.task.stage_version,
        }).status_code, 302)
        self.task.refresh_from_db()
        run = self.task.runs.get()
        self.assertEqual((self.task.state, self.task.stage, run.stage), (
            Task.State.SCOPE_APPROVED, Task.Stage.ARCHITECT, Task.Stage.ARCHITECT,
        ))
        worker.assert_called_once_with()

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_passing_testing_recommendation_advances_and_starts_review(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.TESTING
        self.task.save()
        run = self.completed_run(Task.Stage.TESTING, self.report())

        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)

        self.task.refresh_from_db()
        decision = self.task.gate_decisions.get()
        review_run = self.task.runs.first()
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        self.assertTrue(decision.automatic)
        self.assertTrue(decision.advanced)
        self.assertEqual(decision.source_run, run)
        self.assertEqual(review_run.stage, Task.Stage.REVIEW)
        worker.assert_called_once_with()

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_failed_gate_rework_pauses_for_human_decision(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.TESTING
        self.task.save()
        run = self.completed_run(Task.Stage.TESTING, self.report("failed", "rework"))

        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)

        self.task.refresh_from_db()
        decision = self.task.gate_decisions.get()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        self.assertEqual(self.task.work_cycle, 0)
        self.assertIn("manual decision", self.task.automatic_pause_reason)
        self.assertTrue(decision.automatic)
        self.assertFalse(decision.advanced)
        self.assertEqual(decision.source_run, run)
        self.assertEqual(self.task.runs.count(), 1)
        worker.assert_not_called()

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_unverified_gate_rework_pauses_for_human_decision(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.REVIEW
        self.task.save()
        run = self.completed_run(Task.Stage.REVIEW, self.report("unverified", "rework"))

        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)

        self.task.refresh_from_db()
        decision = self.task.gate_decisions.get()
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        self.assertIn("manual decision", self.task.automatic_pause_reason)
        self.assertEqual(decision.result, GateDecision.Result.UNVERIFIED)
        self.assertFalse(decision.advanced)
        self.assertEqual(decision.source_run, run)
        self.assertEqual(self.task.runs.count(), 1)
        worker.assert_not_called()

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_duplicate_completion_callback_does_not_pause_advanced_workflow(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.DEVELOPER
        self.task.save()
        run = self.completed_run(
            Task.Stage.DEVELOPER,
            "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE",
        )

        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)
        continue_automatic_run(run.pk)

        self.task.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        self.assertEqual(self.task.automatic_pause_reason, "")
        self.assertIsNotNone(run.automation_processed_at)
        self.assertEqual(self.task.runs.count(), 2)
        worker.assert_called_once_with()

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_invalid_gate_report_pauses_without_inventing_a_result(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.TESTING
        self.task.save()
        run = self.completed_run(Task.Stage.TESTING, "The report did not include a recommendation.")

        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)

        self.task.refresh_from_db()
        self.assertIn("missing or invalid", self.task.automatic_pause_reason)
        self.assertFalse(self.task.gate_decisions.exists())
        self.assertEqual(self.task.runs.count(), 1)
        worker.assert_not_called()

    @override_settings(ACP_PORT="12345")
    def test_review_pass_automatically_finishes_task(self):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.REVIEW
        self.task.save()
        run = self.completed_run(Task.Stage.REVIEW, self.report())

        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)

        self.task.refresh_from_db()
        decision = self.task.gate_decisions.get()
        self.assertEqual(self.task.stage, Task.Stage.FINISHED)
        self.assertTrue(self.task.finished)
        self.assertEqual(self.task.status, Task.Status.DONE)
        self.assertTrue(decision.automatic)
        self.assertTrue(decision.advanced)

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_pauses_on_third_return_to_testing(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.DEVELOPER
        self.task.work_cycle = 3
        self.task.automatic_gate_returns = {Task.Stage.TESTING: 2}
        self.task.save()
        run = self.completed_run(
            Task.Stage.DEVELOPER,
            "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE",
        )

        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)

        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        self.assertEqual(self.task.automatic_gate_returns[Task.Stage.TESTING], 3)
        self.assertIn("third return", self.task.automatic_pause_reason)
        self.assertEqual(self.task.runs.count(), 1)
        worker.assert_not_called()

    def test_permission_request_pauses_workflow(self):
        from asgiref.sync import async_to_sync
        from ..agent import create_permission

        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.DEVELOPER
        self.task.save()
        run = self.completed_run(Task.Stage.DEVELOPER)
        run.status = AgentRun.Status.RUNNING
        run.save(update_fields=["status"])

        permission_id = async_to_sync(create_permission)(
            run.pk, {"kind": "execute"}, [{"optionId": "allow", "kind": "allow_once"}],
        )

        self.task.refresh_from_db()
        self.assertIn("permission request", self.task.automatic_pause_reason)
        self.assertTrue(PermissionRequest.objects.filter(pk=permission_id, resolved=False).exists())

    def test_acknowledging_interruption_pauses_for_inspection(self):
        self.task.save()
        run = self.completed_run(Task.Stage.DEVELOPER)
        run.status = AgentRun.Status.INTERRUPTED
        run.save()

        response = self.client.post(reverse("acknowledge_run", args=[run.pk]))

        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertIn("Review it before continuing", self.task.automatic_pause_reason)

    def test_scope_edit_pauses_automation(self):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.TESTING
        self.task.save()

        response = self.client.post(reverse("task_edit", args=[self.task.pk]), {
            "version": self.task.version,
            "stage_version": self.task.stage_version,
            "title": self.task.title,
            "request": "A changed request",
            "plan": self.task.plan,
        })

        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertIn("Review and reapprove", self.task.automatic_pause_reason)
        self.assertEqual(self.task.stage, Task.Stage.DRAFT)

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_pending_gate_decision_blocks_continuation_until_gate_action(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.automatic_pause_reason = "Review the gate report manually."
        for stage in (Task.Stage.TESTING, Task.Stage.REVIEW):
            self.task.stage = stage
            self.task.save()
            for report in ("No structured recommendation.", self.report("unverified", "hold"), self.report()):
                with self.subTest(stage=stage, report=report):
                    self.completed_run(stage, report)
                    self.assertEqual(self.continue_workflow().status_code, 409)
                    self.task.refresh_from_db()
                    self.assertEqual(self.task.stage, stage)
                    self.assertEqual(self.task.automatic_pause_reason, "Review the gate report manually.")
                    self.assertFalse(self.task.gate_decisions.exists())
                    self.task.save()
        worker.assert_not_called()

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_recorded_failure_requires_gate_action_even_without_pause_reason(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.TESTING
        self.task.save()
        GateDecision.objects.create(
            task=self.task, stage=self.task.stage, task_version=self.task.version,
            work_cycle=self.task.work_cycle, result="failed", evidence="A failing check.",
        )
        self.assertEqual(self.continue_workflow().status_code, 409)
        worker.assert_not_called()

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_repeat_gate_checkpoint_requires_explicit_action(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.TESTING
        self.task.automatic_pause_reason = "Paused on the third return to Testing."
        self.task.automatic_gate_returns = {Task.Stage.TESTING: 3}
        self.task.save()
        self.assertEqual(self.continue_workflow().status_code, 409)
        self.assertFalse(self.task.runs.exists())
        worker.assert_not_called()

    def test_required_request_and_plan_approval_block_resume(self):
        self.task.automatic_pause_reason = "The task was edited. Review and reapprove."
        for state in (Task.State.DRAFT, Task.State.SCOPE_APPROVED):
            with self.subTest(state=state):
                self.task.state = state
                self.task.save()
                self.assertEqual(self.continue_workflow().status_code, 409)

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_continue_resumes_after_a_simple_pause(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.DEVELOPER
        self.task.automatic_pause_reason = "Task is Ready again. Continue the workflow when ready."
        self.task.save()
        self.assertEqual(self.continue_workflow().status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.automatic_pause_reason, "")
        worker.assert_called_once_with()

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_continue_processes_valid_report_left_idle(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.TESTING
        self.task.save()
        self.completed_run(Task.Stage.TESTING, self.report())
        self.assertEqual(self.continue_workflow().status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        worker.assert_called_once_with()

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_continue_advances_a_completed_developer_run(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.DEVELOPER
        self.task.save()
        self.completed_run(Task.Stage.DEVELOPER, "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE")
        self.assertEqual(self.continue_workflow().status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        worker.assert_called_once_with()

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_accepting_recommendation_clears_old_pause_and_starts_review(self, worker):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.TESTING
        self.task.automatic_pause_reason = "Review the completed report."
        self.task.save()
        run = self.completed_run(Task.Stage.TESTING, self.report())
        page = self.client.get(reverse("task_detail", args=[self.task.pk]))
        response = self.client.post(
            reverse("accept_recommendation", args=[self.task.pk]),
            page.context["recommendation_form"].initial,
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.stage, self.task.automatic_pause_reason), (Task.Stage.REVIEW, ""))
        self.assertEqual(self.task.gate_decisions.get().source_run, run)
        worker.assert_called_once_with()
