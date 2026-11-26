from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import AcpEndpoint, AgentRun, Approval, GateDecision, Task


class PlanningWorkflowTests(TestCase):
    def setUp(self):
        self.endpoint = AcpEndpoint.objects.create(name="Test ACP", port=12345, enabled=False)
        self.task = Task.objects.create(
            title="Add export", request="Export the report", plan="Add an endpoint",
            stage=Task.Stage.ARCHITECT, state=Task.State.SCOPE_APPROVED,
        )

    def approve(self, version):
        self.task.refresh_from_db()
        return self.client.post(reverse("approve_combined", args=[self.task.pk]), {"version": version, "stage_version": self.task.stage_version})

    def edit(self, version, **fields):
        self.task.refresh_from_db()
        values = {"version": version, "stage_version": self.task.stage_version, "title": self.task.title, "request": self.task.request, "plan": self.task.plan}
        values.update(fields)
        return self.client.post(reverse("task_edit", args=[self.task.pk]), values)

    def test_create_and_approve_request_and_plan_separately(self):
        response = self.client.post(reverse("task_create"), {
            "title": "Fix issue", "request": "Fix the problem", "plan": "Add a check",
        })
        self.assertEqual(response.status_code, 302)
        task = Task.objects.get(title="Fix issue")
        self.assertEqual(task.state, Task.State.DRAFT)
        self.assertEqual(task.stage, Task.Stage.DRAFT)
        self.assertEqual(self.client.post(reverse("approve_plan", args=[task.pk]), {"version": 0, "stage_version": 0}).status_code, 409)
        self.assertEqual(self.client.post(reverse("approve_request", args=[task.pk]), {"version": 0, "stage_version": 0}).status_code, 302)
        self.assertEqual(self.client.post(reverse("approve_plan", args=[task.pk]), {"version": 0, "stage_version": 1}).status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.PLAN_APPROVED)
        self.assertEqual(list(task.approvals.order_by("id").values_list("kind", flat=True)), ["scope", "plan"])
        self.assertIn("Fix the problem", task.approvals.get(kind="scope").content)
        self.assertIn("Add a check", task.approvals.get(kind="plan").content)


    def test_plan_edit_requires_reapproval_and_keeps_history(self):
        self.approve(0)
        self.assertEqual(self.edit(1, plan="Add a different endpoint").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, Task.State.SCOPE_APPROVED)
        self.assertIn("Add an endpoint", self.task.approvals.get(kind="plan").content)
        self.assertEqual(self.approve(2).status_code, 302)
        self.assertEqual(self.task.approvals.filter(kind="plan").count(), 2)

    def test_scope_edit_requires_request_approval_again(self):
        self.approve(0)
        self.assertEqual(self.edit(1, request="Export a different report").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, Task.State.DRAFT)
        self.assertEqual(self.approve(1).status_code, 409)

    def test_stale_forms_cannot_overwrite_or_approve(self):
        self.assertEqual(self.edit(0, plan="Revised plan").status_code, 302)
        self.assertEqual(self.edit(0, plan="Stale plan").status_code, 409)
        self.assertEqual(self.approve(0).status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.plan, "Revised plan")
        self.assertFalse(Approval.objects.exists())

    def test_cannot_approve_empty_plan_or_use_get(self):
        self.task.plan = ""
        self.task.save()
        self.assertEqual(self.approve(0).status_code, 400)
        self.assertEqual(self.client.get(reverse("approve_combined", args=[self.task.pk])).status_code, 405)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, Task.State.SCOPE_APPROVED)

    def test_next_action_shows_blocking_task_and_links_to_its_next_action(self):
        self.endpoint.enabled = True
        self.endpoint.session_limit = 1
        self.endpoint.save()
        blocking_task = Task.objects.create(title="Blocking task", request="Run first")
        AgentRun.objects.create(
            task=blocking_task, acp_endpoint=self.endpoint, task_version=blocking_task.version,
            request_snapshot="Run first", plan_snapshot="", status=AgentRun.Status.RUNNING,
        )

        response = self.client.get(reverse("task_detail", args=[self.task.pk]))

        self.assertContains(response, "Task is blocked")
        self.assertContains(response, "A reserved run is Running:")
        blocking_task_url = f'{reverse("task_detail", args=[blocking_task.pk])}#next-action'
        self.assertContains(response, f'href="{blocking_task_url}">{blocking_task.title}</a>')

    def test_next_action_reports_when_no_active_run_is_recorded(self):
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))

        self.assertContains(response, "No active agent run is recorded for this task.")

        AgentRun.objects.create(
            task=self.task, task_version=self.task.version, request_snapshot="Request",
            plan_snapshot="Plan", status=AgentRun.Status.RUNNING,
        )
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))

        self.assertNotContains(response, "No active agent run is recorded for this task.")

    def test_legacy_scope_approval_remains_valid_history(self):
        self.task.state = Task.State.SCOPE_APPROVED
        self.task.save()
        Approval.objects.create(task=self.task, kind=Approval.Kind.SCOPE, content="Earlier decision")
        self.assertEqual(self.approve(0).status_code, 302)
        self.assertEqual(self.task.approvals.count(), 2)

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent.start_worker")
    def test_refine_starts_trusted_architect_for_approved_request(self, worker):
        AcpEndpoint.objects.filter(pk=self.endpoint.pk).update(enabled=True)
        response = self.client.post(reverse("refine_plan", args=[self.task.pk]), {"version": 0, "stage_version": 0})
        self.assertEqual(response.status_code, 302)
        run = self.task.runs.get()
        self.assertEqual(run.stage, Task.Stage.ARCHITECT)
        self.assertEqual(run.request_snapshot, "Add export\n\nExport the report")
        worker.assert_called_once_with(run)

    def test_draft_requires_explicit_architect_transition(self):
        task = Task.objects.create(title="New", request="A request", plan="A plan")
        advance = reverse("advance_stage", args=[task.pk])
        approve = reverse("approve_combined", args=[task.pk])
        self.assertEqual(self.client.post(approve, {"version": 0, "stage_version": 0}).status_code, 409)
        self.assertEqual(self.client.post(advance, {"version": 0, "stage_version": 0}).status_code, 302)
        self.assertEqual(self.client.post(advance, {"version": 0, "stage_version": 0}).status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.stage, Task.Stage.ARCHITECT)
        self.assertEqual(self.client.post(approve, {"version": 0, "stage_version": 1}).status_code, 409)
        self.assertEqual(self.client.post(reverse("approve_request", args=[task.pk]), {"version": 0, "stage_version": 1}).status_code, 302)
        self.assertEqual(self.client.post(approve, {"version": 0, "stage_version": 2}).status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.stage, Task.Stage.DEVELOPER)

    def test_developer_cannot_advance_without_current_completed_run(self):
        self.approve(0)
        advance = reverse("advance_stage", args=[self.task.pk])
        values = {"version": 1, "stage_version": 1}
        self.assertEqual(self.client.post(advance, values).status_code, 409)
        AgentRun.objects.create(task=self.task, task_version=0, request_snapshot="r", plan_snapshot="p", status="completed")
        self.assertEqual(self.client.post(advance, values).status_code, 409)
        AgentRun.objects.create(task=self.task, task_version=1, request_snapshot="r", plan_snapshot="p", status="interrupted")
        self.assertEqual(self.client.post(advance, values).status_code, 409)

    def test_stale_gate_and_edit_actions_cannot_change_a_new_stage(self):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.TESTING
        self.task.automatic_pause_reason = "Review the recorded gate before continuing."
        self.task.save()
        gate = reverse("record_gate", args=[self.task.pk])
        data = {"version": 0, "stage_version": 0, "result": "passed", "evidence": "Checks ran", "action": "advance"}
        self.assertEqual(self.client.post(gate, data).status_code, 302)
        self.assertEqual(self.client.post(gate, data).status_code, 409)
        self.assertEqual(self.client.post(reverse("task_edit", args=[self.task.pk]), {
            "version": 0, "stage_version": 0, "title": self.task.title, "request": self.task.request, "plan": "Old edit",
        }).status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        self.assertEqual(self.task.gate_decisions.count(), 1)

    def test_plan_edit_preserves_gate_history_but_requires_new_approval(self):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.REVIEW
        self.task.save()
        GateDecision.objects.create(task=self.task, stage=Task.Stage.TESTING, task_version=0, result="passed", evidence="Old test")
        self.assertEqual(self.edit(0, plan="Revised plan").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.ARCHITECT)
        self.assertEqual(self.task.state, Task.State.SCOPE_APPROVED)
        self.assertEqual(self.task.version, 1)
        self.assertEqual(self.task.gate_decisions.get().task_version, 0)

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent.start_worker")
    def test_rework_preserves_history_and_requires_fresh_development_and_gates(self, worker):
        self.approve(0)
        AgentRun.objects.create(task=self.task, task_version=1, request_snapshot="r", plan_snapshot="p", status="completed")
        advance = reverse("advance_stage", args=[self.task.pk])
        gate = reverse("record_gate", args=[self.task.pk])
        self.assertEqual(self.client.post(advance, {"version": 1, "stage_version": 1}).status_code, 302)
        self.assertEqual(self.client.post(gate, {
            "version": 1, "stage_version": 2, "result": "failed", "evidence": "Regression failed", "action": "save",
        }).status_code, 302)
        rework = reverse("return_to_developer", args=[self.task.pk])
        values = {"version": 1, "stage_version": 3, "reason": "Fix the regression"}
        self.assertEqual(self.client.post(rework, values).status_code, 302)
        self.assertEqual(self.client.post(rework, values).status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual((self.task.stage, self.task.work_cycle, self.task.version), ("developer", 1, 1))
        self.assertEqual(self.client.post(advance, {"version": 1, "stage_version": 4}).status_code, 409)
        AcpEndpoint.objects.filter(pk=self.endpoint.pk).update(enabled=True)
        self.assertEqual(self.client.post(reverse("start_run", args=[self.task.pk]), {"version": 1, "stage_version": 4}).status_code, 302)
        AcpEndpoint.objects.filter(pk=self.endpoint.pk).update(enabled=False)
        run = self.task.runs.first()
        worker.assert_called_once()
        self.assertEqual(run.work_cycle, 1)
        self.assertIn("Fix the regression", run.context_snapshot)
        self.assertIn("Regression failed", run.context_snapshot)
        run.status = AgentRun.Status.COMPLETED
        run.save()
        Task.objects.filter(pk=self.task.pk).update(
            automatic_pause_reason="Review the completed Developer run before advancing.",
        )
        self.assertEqual(self.client.post(advance, {"version": 1, "stage_version": 4}).status_code, 302)
        self.assertEqual(self.client.post(gate, {
            "version": 1, "stage_version": 5, "result": "passed", "evidence": "New tests passed", "action": "advance",
        }).status_code, 302)
        AcpEndpoint.objects.filter(pk=self.endpoint.pk).update(enabled=True)
        self.assertEqual(self.client.post(reverse("start_run", args=[self.task.pk]), {"version": 1, "stage_version": 6}).status_code, 302)
        review = self.task.runs.first()
        self.assertEqual(review.work_cycle, 1)
        self.assertIn("New tests passed", review.context_snapshot)
        self.assertNotIn("Regression failed", review.context_snapshot)
        self.assertEqual(self.task.gate_decisions.count(), 2)

    def test_finish_requires_testing_and_review_gates_and_can_reopen(self):
        self.approve(0)
        finish = reverse("finish_task", args=[self.task.pk])
        self.assertEqual(self.client.post(finish, {"version": 1}).status_code, 409)
        AgentRun.objects.create(task=self.task, task_version=1, request_snapshot="r", plan_snapshot="p", status="completed")
        self.assertEqual(self.client.post(finish, {"version": 1}).status_code, 409)
        self.assertEqual(self.client.post(reverse("advance_stage", args=[self.task.pk]), {"version": 1, "stage_version": 1}).status_code, 302)
        gate = reverse("record_gate", args=[self.task.pk])
        failed = {"version": 1, "stage_version": 2, "result": "failed", "evidence": "Test failed", "action": "advance"}
        self.assertEqual(self.client.post(gate, failed).status_code, 400)
        self.assertFalse(GateDecision.objects.exists())
        self.assertEqual(self.client.post(gate, {**failed, "action": "save"}).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        self.assertEqual(self.client.post(gate, {**failed, "stage_version": 3, "waiver": "Known failure accepted"}).status_code, 302)
        self.assertEqual(self.client.post(gate, {
            "version": 1, "stage_version": 4, "result": "unverified", "evidence": "No independent review",
            "waiver": "Explicitly deferred", "action": "advance",
        }).status_code, 302)
        self.task.refresh_from_db()
        self.assertTrue(self.task.finished)
        self.assertEqual(self.task.stage, Task.Stage.FINISHED)
        self.assertEqual(self.client.post(reverse("reopen_task", args=[self.task.pk]), {"version": 1, "stage_version": 5}).status_code, 302)
        self.task.refresh_from_db()
        self.assertFalse(self.task.finished)
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        self.assertEqual(self.task.gate_decisions.count(), 3)
