import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import AcpEndpoint, AgentRun, GateDecision, RunEvent, Task


@override_settings(ACP_PORT="12345")
class GuidedActionTests(TestCase):
    def setUp(self):
        AcpEndpoint.objects.create(name="Test ACP", port=12345)
        self.task = Task.objects.create(
            title="Guided task", request="Requested change", plan="Approved plan",
            stage=Task.Stage.TESTING, state=Task.State.PLAN_APPROVED, version=2,
            automatic_pause_reason="Review the completed Testing run.",
        )
        self.run = AgentRun.objects.create(
            task=self.task, stage=Task.Stage.TESTING, status=AgentRun.Status.COMPLETED,
            task_version=2, request_snapshot="Requested change", plan_snapshot="Approved plan",
        )
        self.url = reverse("task_detail", args=[self.task.pk])


    def save_gate(self, intent, **overrides):
        self.task.refresh_from_db()
        result, action = {
            "pass": ("passed", "advance"), "fail": ("failed", "save"),
            "hold": ("unverified", "save"), "waiver": ("unverified", "advance"),
        }[intent]
        values = {
            "version": self.task.version, "stage_version": self.task.stage_version,
            "gate_action": intent, "result": result, "action": action,
            "evidence": "Actual check output", **overrides,
        }
        return self.client.post(reverse("record_gate", args=[self.task.pk]), values)

    @patch("tasks.agent.start_worker")
    def test_selecting_and_cancelling_actions_does_not_record_or_launch(self, worker):
        for stage in (Task.Stage.TESTING, Task.Stage.REVIEW):
            Task.objects.filter(pk=self.task.pk).update(stage=stage)
            for action in ("pass", "fail", "hold", "waiver", "rework", "run"):
                with self.subTest(stage=stage, action=action):
                    self.assertEqual(self.client.get(self.url, {"next_action": action}).status_code, 200)
                    self.assertEqual(self.client.get(self.url).status_code, 200)
                    self.task.refresh_from_db()
                    self.assertEqual((self.task.stage, self.task.stage_version, self.task.work_cycle), (stage, 0, 0))
                    self.assertFalse(self.task.finished)
                    self.assertFalse(self.task.gate_decisions.exists())
                    self.assertFalse(self.task.approvals.exists())
                    self.assertEqual(self.task.runs.count(), 1)
        worker.assert_not_called()


    @patch("tasks.agent.start_worker")
    def test_pass_advances_only_on_save_and_rejects_duplicate_save(self, worker):
        self.assertEqual(self.save_gate("pass").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        decision = self.task.gate_decisions.get()
        self.assertEqual((decision.result, decision.advanced), ("passed", True))
        stale = {
            "version": 2, "stage_version": 0, "gate_action": "pass",
            "result": "passed", "evidence": "Actual check output", "action": "advance",
        }
        self.assertEqual(self.client.post(reverse("record_gate", args=[self.task.pk]), stale).status_code, 409)
        self.assertEqual(self.task.gate_decisions.count(), 1)
        worker.assert_called_once_with(self.task.runs.first())

    def test_review_pass_finishes_on_save(self):
        Task.objects.filter(pk=self.task.pk).update(stage=Task.Stage.REVIEW)
        self.assertEqual(self.save_gate("pass").status_code, 302)
        self.task.refresh_from_db()
        self.assertTrue(self.task.finished)

    def test_fail_and_hold_save_without_advancing(self):
        for intent, result in (("fail", "failed"), ("hold", "unverified")):
            with self.subTest(intent=intent):
                self.assertEqual(self.save_gate(intent).status_code, 302)
                self.task.refresh_from_db()
                self.assertEqual(self.task.stage, Task.Stage.TESTING)
                decision = self.task.gate_decisions.first()
                self.assertEqual((decision.result, decision.advanced), (result, False))

    @patch("tasks.agent.start_worker")
    def test_waiver_requires_reason(self, worker):
        response = self.save_gate("waiver", waiver="")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.task.gate_decisions.exists())
        self.assertEqual(self.save_gate("waiver", waiver="Accept the documented risk").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        self.assertEqual(self.task.gate_decisions.get().waiver, "Accept the documented risk")
        worker.assert_called_once_with(self.task.runs.first())

    def test_missing_evidence_and_mismatched_intent_do_not_save(self):
        for overrides in ({"evidence": " "}, {"result": "failed"}, {"action": "save"}):
            self.assertEqual(self.save_gate("pass", **overrides).status_code, 400)
        self.assertFalse(self.task.gate_decisions.exists())

    @patch("tasks.agent.start_worker")
    def test_rerun_starts_only_after_save(self, worker):
        self.assertEqual(self.client.get(self.url, {"next_action": "run"}).status_code, 200)
        worker.assert_not_called()
        response = self.client.post(reverse("start_run", args=[self.task.pk]), {"version": 2, "stage_version": 0})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.task.runs.count(), 2)
        worker.assert_called_once_with(self.task.runs.first())

    @patch("tasks.agent.start_worker")
    def test_rework_requires_reason_then_returns_on_save(self, worker):
        self.assertEqual(self.client.get(self.url, {"next_action": "rework"}).status_code, 200)
        url = reverse("return_to_developer", args=[self.task.pk])
        values = {"version": 2, "stage_version": 0, "reason": ""}
        response = self.client.post(url, values)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.task.approvals.exists())
        self.assertEqual(self.client.post(url, {**values, "reason": "Fix the failing check"}).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.stage, self.task.work_cycle), (Task.Stage.DEVELOPER, 1))
        worker.assert_called_once_with(self.task.runs.first())

    @patch("tasks.agent.start_worker")
    def test_developer_decisions_also_require_saving(self, worker):
        Task.objects.filter(pk=self.task.pk).update(stage=Task.Stage.DEVELOPER)
        AgentRun.objects.filter(pk=self.run.pk).update(stage=Task.Stage.DEVELOPER)
        for action in ("approve", "refuse"):
            self.assertEqual(self.client.get(self.url, {"next_action": action}).status_code, 200)
            self.client.get(self.url)
            self.task.refresh_from_db()
            self.assertEqual((self.task.stage, self.task.stage_version), (Task.Stage.DEVELOPER, 0))
            self.assertFalse(self.task.approvals.exists())
            self.assertEqual(self.task.runs.count(), 1)
        worker.assert_not_called()
        self.assertEqual(self.client.post(
            reverse("advance_stage", args=[self.task.pk]), {"version": 2, "stage_version": 0},
        ).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)


    @patch("tasks.agent.start_worker")
    def test_passing_paused_testing_resumes_automatic_review_and_confirms_save(self, worker):
        Task.objects.filter(pk=self.task.pk).update(
            automatic_pause_reason="The agent run did not complete normally.",
        )
        AgentRun.objects.filter(pk=self.run.pk).update(status=AgentRun.Status.ACKNOWLEDGED)
        self.assertEqual(self.save_gate("pass").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        self.assertEqual(self.task.automatic_pause_reason, "")
        review = self.task.runs.first()
        self.assertEqual(review.stage, Task.Stage.REVIEW)
        self.assertIn("Actual check output", review.context_snapshot)
        worker.assert_called_once_with(review)

    @patch("tasks.agent.start_worker")
    def test_fail_and_hold_do_not_clear_pause_or_restart_automation(self, worker):
        Task.objects.filter(pk=self.task.pk).update(automatic_pause_reason="Assess the report.")
        for action in ("fail", "hold"):
            self.assertEqual(self.save_gate(action).status_code, 302)
            self.task.refresh_from_db()
            self.assertEqual(self.task.stage, Task.Stage.TESTING)
            self.assertEqual(self.task.automatic_pause_reason, "Assess the report.")
        worker.assert_not_called()

    @patch("tasks.agent.start_worker")
    def test_manual_pass_confirms_transition_and_starts_review(self, worker):
        self.assertEqual(self.save_gate("pass").status_code, 302)
        self.assertEqual(self.task.runs.count(), 2)
        worker.assert_called_once_with(self.task.runs.first())

    @patch("tasks.agent.start_worker")
    def test_waiver_advances_and_resumes_enabled_automation(self, worker):
        Task.objects.filter(pk=self.task.pk).update(automatic_pause_reason="Assess the report.")
        self.assertEqual(self.save_gate("waiver", waiver="Accept the documented risk").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.stage, self.task.automatic_pause_reason), (Task.Stage.REVIEW, ""))
        worker.assert_called_once_with(self.task.runs.first())

    @patch("tasks.agent.start_worker")
    def test_review_pass_finishes_without_starting_another_run(self, worker):
        Task.objects.filter(pk=self.task.pk).update(
            stage=Task.Stage.REVIEW, automatic_pause_reason="Review required.",
        )
        self.assertEqual(self.save_gate("pass").status_code, 302)
        self.task.refresh_from_db()
        self.assertTrue(self.task.finished)
        self.assertEqual(self.task.automatic_pause_reason, "")
        worker.assert_not_called()

    @patch("tasks.agent.start_worker", side_effect=RuntimeError("Worker unavailable"))
    def test_failure_to_start_next_stage_records_a_new_pause(self, worker):
        Task.objects.filter(pk=self.task.pk).update(automatic_pause_reason="Old gate pause.")
        self.assertEqual(self.save_gate("pass").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        self.assertIn("worker could not start", self.task.automatic_pause_reason)
        review = self.task.runs.first()
        self.assertEqual(review.status, AgentRun.Status.INTERRUPTED)
        self.assertIn("Worker unavailable", review.error)
        worker.assert_called_once_with(review)


class WorkflowActionAvailabilityTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(
            title="Gate awaiting assessment", request="Request", plan="Plan",
            stage=Task.Stage.TESTING, state=Task.State.PLAN_APPROVED,
            relates_indexing=Task.IndexingStatus.DONE,
        )
        self.run = AgentRun.objects.create(
            task=self.task, stage=self.task.stage, task_version=self.task.version,
            request_snapshot=self.task.request, plan_snapshot=self.task.plan,
            status=AgentRun.Status.COMPLETED,
        )
        self.url = reverse("task_detail", args=[self.task.pk])

    def hold_report(self, result):
        report = {
            "result": result, "recommendation": "hold", "summary": "Manual assessment needed",
            "evidence": "Recorded checks", "untested": "Browser behavior", "reason": "Inspect the evidence",
        }
        RunEvent.objects.update_or_create(
            run=self.run, kind="message",
            defaults={"content": f"FLAMETHROWER_GATE_RECOMMENDATION\n{json.dumps(report)}\nEND_FLAMETHROWER_GATE_RECOMMENDATION"},
        )

    def test_manual_actions_wait_until_the_server_allows_them(self):
        for result in (None, "passed", "failed", "unverified"):
            with self.subTest(result=result):
                if result:
                    self.hold_report(result)
                page = self.client.get(self.url, {"next_action": "pass"})
                self.assertTrue(page.context["workflow_continue_block_reason"])
                response = self.client.post(reverse("record_gate", args=[self.task.pk]), {
                    "version": self.task.version, "stage_version": self.task.stage_version,
                    "gate_action": "pass", "action": "advance", "result": "passed", "evidence": "Actual checks",
                })
                self.assertEqual(response.status_code, 409)
        self.assertFalse(self.task.gate_decisions.exists())

    @patch("tasks.agent.start_worker")
    def test_paused_gate_offers_actions_that_can_be_saved(self, worker):
        self.hold_report("failed")
        Task.objects.filter(pk=self.task.pk).update(automatic_pause_reason="Review the gate evidence.")
        response = self.client.post(reverse("record_gate", args=[self.task.pk]), {
            "version": self.task.version, "stage_version": self.task.stage_version,
            "gate_action": "hold", "result": "unverified", "action": "save",
            "evidence": "More testing is needed",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.task.gate_decisions.get().result, GateDecision.Result.UNVERIFIED)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        self.assertEqual(self.task.automatic_pause_reason, "Review the gate evidence.")
        worker.assert_not_called()
