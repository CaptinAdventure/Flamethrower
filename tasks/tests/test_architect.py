import hashlib
import threading
import time
from unittest.mock import call, patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from ..models import AcpEndpoint, AgentRun, Approval, DeferredTask, PermissionRequest, RunEvent, Task
from .test_agent import FakeACPServer


@override_settings(ACP_PORT="12345")
class ArchitectWorkflowTests(TestCase):
    def setUp(self):
        self.endpoint = AcpEndpoint.objects.create(name="Test ACP", port=12345)
        self.task = Task.objects.create(title="Feature", request="Add the feature")

    def post(self, name, **values):
        return self.client.post(reverse(name, args=[self.task.pk]), values)

    def approve_request(self):
        return self.post("approve_request", version=0, stage_version=0)

    @patch("tasks.agent.start_worker")
    def test_request_approval_is_required_and_invalidated_by_scope_edit(self, worker):
        self.assertEqual(self.post("refine_plan", version=0, stage_version=0).status_code, 409)
        self.assertEqual(self.post("approve_plan", version=0, stage_version=0).status_code, 400)
        self.assertEqual(self.approve_request().status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.state, self.task.stage), (Task.State.SCOPE_APPROVED, Task.Stage.ARCHITECT))
        self.assertEqual(self.task.approvals.get().kind, Approval.Kind.SCOPE)
        run = self.task.runs.get()
        run.status = AgentRun.Status.ACKNOWLEDGED
        run.save(update_fields=["status"])
        worker.assert_called_once_with(run)
        self.assertEqual(self.approve_request().status_code, 409)
        response = self.client.post(reverse("task_edit", args=[self.task.pk]), {
            "version": self.task.version, "stage_version": self.task.stage_version,
            "title": self.task.title, "request": "Different request", "plan": "",
        })
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.state, self.task.stage), (Task.State.DRAFT, Task.Stage.DRAFT))
        self.assertEqual(self.post("refine_plan", version=self.task.version, stage_version=self.task.stage_version).status_code, 409)

    @patch("tasks.agent.start_worker")
    def test_changing_approved_request_pauses_workflow(self, worker):
        self.assertEqual(self.approve_request().status_code, 302)
        self.task.refresh_from_db()
        run = self.task.runs.get()
        run.status = AgentRun.Status.ACKNOWLEDGED
        run.save(update_fields=["status"])
        response = self.client.post(reverse("task_edit", args=[self.task.pk]), {
            "version": self.task.version, "stage_version": self.task.stage_version,
            "title": self.task.title, "request": "Different request", "plan": "",
        })
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, Task.State.DRAFT)
        self.assertIn("changed", self.task.automatic_pause_reason)
        worker.assert_called_once()

    @override_settings(ACP_PORT="")
    def test_existing_plan_waits_for_approval_without_an_acp_connection(self):
        self.endpoint.delete()
        Task.objects.filter(pk=self.task.pk).update(plan="Manual plan")
        self.assertEqual(self.approve_request().status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, Task.State.SCOPE_APPROVED)
        self.assertEqual(self.task.automatic_pause_reason, "")
        self.assertFalse(self.task.runs.exists())

    @patch("tasks.agent.start_worker")
    def test_refine_rejects_stale_requests_and_active_runs(self, worker):
        self.approve_request()
        self.task.refresh_from_db()
        self.assertEqual(self.post("refine_plan", version=0, stage_version=0).status_code, 409)
        self.assertEqual(self.task.runs.count(), 1)
        worker.assert_called_once_with(self.task.runs.get())

    @patch("tasks.agent.start_worker")
    def test_automatic_workflow_starts_architect_and_waits_for_human(self, worker):
        self.assertEqual(self.approve_request().status_code, 302)
        self.task.refresh_from_db()
        run = self.task.runs.get()
        self.assertEqual(run.stage, Task.Stage.ARCHITECT)
        worker.assert_called_once_with(run)
        run.status = AgentRun.Status.COMPLETED
        run.save()
        RunEvent.objects.create(run=run, kind="message", content="FLAMETHROWER_ARCHITECT_PLAN\nProposed design\nEND_FLAMETHROWER_ARCHITECT_PLAN")
        from ..automation import continue_automatic_run, continue_automatic_task

        continue_automatic_run(run.pk)
        continue_automatic_task(self.task.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, Task.State.SCOPE_APPROVED)
        self.assertEqual(self.task.plan, "")
        self.assertEqual(self.task.runs.count(), 1)

    @patch("tasks.agent.start_worker")
    def test_accepting_current_proposal_approves_plan_and_advances(self, worker):
        self.approve_request()
        self.task.refresh_from_db()
        run = self.task.runs.get()
        run.status = AgentRun.Status.COMPLETED
        run.save(update_fields=["status"])
        RunEvent.objects.create(run=run, kind="message", content="FLAMETHROWER_ARCHITECT_PLAN\nProposed design\nEND_FLAMETHROWER_ARCHITECT_PLAN")
        page = self.client.get(reverse("task_detail", args=[self.task.pk]))
        form = page.context["architect_form"].initial
        self.assertEqual(self.post("use_architect_plan", **{**form, "report_hash": "0" * 64}).status_code, 409)
        self.assertEqual(self.post("use_architect_plan", **form).status_code, 302)
        self.assertEqual(self.post("use_architect_plan", **form).status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.plan, "Proposed design")
        self.assertEqual(self.task.state, Task.State.PLAN_APPROVED)
        self.assertEqual(self.task.stage, Task.Stage.DEVELOPER)
        self.assertEqual(list(self.task.approvals.order_by("id").values_list("kind", flat=True)), ["scope", "plan"])
        worker.assert_has_calls([call(run), call(self.task.runs.first())])

    @patch("tasks.agent.start_worker")
    def test_stale_and_interrupted_proposals_cannot_be_applied(self, worker):
        self.approve_request()
        self.task.refresh_from_db()
        run = self.task.runs.get()
        run.status = AgentRun.Status.INTERRUPTED
        run.save(update_fields=["status"])
        RunEvent.objects.create(run=run, kind="message", content="Draft")
        data = {
            "version": self.task.version, "stage_version": self.task.stage_version, "run_id": run.pk,
            "report_hash": hashlib.sha256(b"Draft").hexdigest(),
        }
        self.assertEqual(self.post("use_architect_plan", **data).status_code, 409)
        run.status = AgentRun.Status.COMPLETED
        run.save()
        Task.objects.filter(pk=self.task.pk).update(version=self.task.version + 1)
        self.assertEqual(self.post("use_architect_plan", **data).status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.plan, "")
        worker.assert_called_once_with(run)

    @patch("tasks.agent.start_worker")
    def test_unstructured_or_unresolved_report_is_not_a_plan(self, worker):
        self.approve_request()
        self.task.refresh_from_db()
        run = self.task.runs.get()
        run.status = AgentRun.Status.COMPLETED
        run.save(update_fields=["status"])
        RunEvent.objects.create(run=run, kind="message", content="A decision is needed before planning.")
        page = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertIsNone(page.context["architect_form"])
        self.assertEqual(self.post(
            "use_architect_plan", version=self.task.version, stage_version=self.task.stage_version,
            run_id=run.pk, report_hash=hashlib.sha256(b"A decision is needed before planning.").hexdigest(),
        ).status_code, 409)
        worker.assert_called_once_with(run)

    def proposal(self):
        self.assertEqual(self.approve_request().status_code, 302)
        self.task.refresh_from_db()
        run = self.task.runs.first()
        if run is None:
            run = AgentRun.objects.create(
                task=self.task, stage=Task.Stage.ARCHITECT, task_version=self.task.version,
                request_snapshot=self.task.request, plan_snapshot="", status=AgentRun.Status.COMPLETED,
            )
        else:
            run.status = AgentRun.Status.COMPLETED
            run.save(update_fields=["status"])
        RunEvent.objects.create(run=run, kind="message", content="FLAMETHROWER_ARCHITECT_PLAN\nProposed design\nEND_FLAMETHROWER_ARCHITECT_PLAN")
        page = self.client.get(reverse("task_detail", args=[self.task.pk]))
        return run, page.context["architect_form"].initial


    @patch("tasks.agent.start_worker")
    def test_accepting_proposal_starts_developer_automatically(self, worker):
        run, values = self.proposal()
        Task.objects.filter(pk=self.task.pk).update(automatic_pause_reason="Review this proposal.")
        self.assertEqual(self.post("use_architect_plan", **values).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.stage, self.task.state, self.task.automatic_pause_reason), (Task.Stage.DEVELOPER, Task.State.PLAN_APPROVED, ""))
        developer = self.task.runs.first()
        self.assertEqual(developer.stage, Task.Stage.DEVELOPER)
        self.assertEqual(developer.plan_snapshot, "Proposed design")
        worker.assert_has_calls([call(run), call(developer)])
        self.assertIn(f"Architect run #{run.pk}", self.task.approvals.get(kind=Approval.Kind.PLAN).content)

    @patch("tasks.agent.start_worker")
    def test_refinement_requires_feedback_and_sends_the_proposal_to_architect(self, worker):
        previous, values = self.proposal()
        self.assertEqual(self.post("refine_architect_plan", **values, feedback=" ").status_code, 400)
        self.assertEqual(self.task.runs.count(), 1)
        self.assertEqual(self.post("refine_architect_plan", **values, feedback="Use a single service boundary.").status_code, 302)
        self.task.refresh_from_db()
        run = self.task.runs.first()
        self.assertEqual((self.task.stage, self.task.state, self.task.plan), (Task.Stage.ARCHITECT, Task.State.SCOPE_APPROVED, ""))
        self.assertEqual(run.plan_snapshot, "Proposed design")
        self.assertIn("Use a single service boundary.", run.context_snapshot)
        self.assertIn(f"Architect run #{previous.pk}", run.context_snapshot)
        self.assertEqual(run.stage, Task.Stage.ARCHITECT)
        self.assertFalse(self.task.approvals.filter(kind=Approval.Kind.PLAN).exists())
        worker.assert_has_calls([call(previous), call(run)])
        self.assertEqual(self.post("refine_architect_plan", **values, feedback="Again").status_code, 409)
        self.assertEqual(self.task.runs.count(), 2)

    @override_settings(ACP_PORT="")
    def test_refinement_without_acp_keeps_feedback_and_does_not_modify_task(self):
        self.endpoint.delete()
        run, values = self.proposal()
        response = self.post("refine_architect_plan", **values, feedback="Reduce the scope.")
        self.assertEqual(response.status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage_version, values["stage_version"])
        self.assertEqual(self.task.runs.count(), 1)

    @patch("tasks.agent.start_worker")
    def test_abandoning_keeps_history(self, worker):
        run, values = self.proposal()
        Task.objects.filter(pk=self.task.pk).update(automatic_pause_reason="Review this proposal.")
        self.assertEqual(self.post("abandon_architect_plan", **values).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.ABANDONED)
        self.assertEqual(self.task.request, "Add the feature")
        self.assertEqual(self.task.runs.get().pk, run.pk)
        self.assertEqual(self.task.approvals.count(), 2)
        self.assertFalse(self.task.approvals.filter(kind=Approval.Kind.PLAN).exists())
        self.assertEqual(self.post("abandon_architect_plan", **values).status_code, 409)
        worker.assert_called_once_with(run)

    @patch("tasks.agent.start_worker")
    def test_all_proposal_actions_reject_stale_or_tampered_forms(self, worker):
        run, values = self.proposal()
        for action in ("use_architect_plan", "refine_architect_plan", "abandon_architect_plan"):
            for changes in (
                {"run_id": run.pk + 1}, {"report_hash": "0" * 64},
                {"version": self.task.version + 1}, {"stage_version": self.task.stage_version + 1},
            ):
                with self.subTest(action=action, changes=changes):
                    self.assertEqual(self.post(action, **{**values, **changes, "feedback": "Change it."}).status_code, 409)
            self.assertEqual(self.client.get(reverse(action, args=[self.task.pk])).status_code, 405)
        self.task.refresh_from_db()
        self.assertEqual((self.task.stage, self.task.status, self.task.plan), (Task.Stage.ARCHITECT, Task.Status.READY, ""))
        self.assertEqual(self.task.runs.count(), 1)
        worker.assert_called_once_with(run)


class ArchitectAcpIntegrationTests(TransactionTestCase):
    def wait_for_worker(self, run):
        worker = next(
            (thread for thread in threading.enumerate() if thread.name == f"agent-run-{run.pk}"),
            None,
        )
        if worker:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())

    def endpoint(self, port):
        return AcpEndpoint.objects.create(name="Integration ACP", port=port)

    def test_acp_proposal_can_be_reviewed_and_approved(self):
        message = "FLAMETHROWER_ARCHITECT_PLAN\nImplement the approved request.\nEND_FLAMETHROWER_ARCHITECT_PLAN"
        server = FakeACPServer(message=message)
        server.start()
        self.endpoint(server.port)
        task = Task.objects.create(title="Change", request="A request")
        with override_settings(ACP_PORT=str(server.port)):
            self.assertEqual(self.client.post(reverse("approve_request", args=[task.pk]), {
                "version": 0, "stage_version": 0,
            }).status_code, 302)
            deadline = time.monotonic() + 10
            while not PermissionRequest.objects.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            permission = PermissionRequest.objects.get()
            self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "no"}).status_code, 302)
            while not AgentRun.objects.filter(status=AgentRun.Status.COMPLETED).exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(AgentRun.objects.filter(status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_worker(task.runs.first())
            page = self.client.get(reverse("task_detail", args=[task.pk]))
            AcpEndpoint.objects.filter(port=server.port).update(enabled=False)
            self.assertEqual(self.client.post(reverse("use_architect_plan", args=[task.pk]), page.context["architect_form"].initial).status_code, 302)
            task.refresh_from_db()
            self.assertEqual(task.plan, "Implement the approved request.")
            self.assertEqual(task.stage, Task.Stage.DEVELOPER)
            self.assertEqual(task.state, Task.State.PLAN_APPROVED)
            self.wait_for_worker(task.runs.first())
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        task.refresh_from_db()
        self.assertEqual((task.state, task.stage), (Task.State.PLAN_APPROVED, Task.Stage.DEVELOPER))

    def test_architect_session_returns_proposal_without_creating_tasks(self):
        message = (
            'FLAMETHROWER_DEFERRED_TASKS\n{"tasks":[{"title":"Unexpected","request":"No"}]}\n'
            'END_FLAMETHROWER_DEFERRED_TASKS\n\n'
            'FLAMETHROWER_ARCHITECT_PLAN\nProposed design\nEND_FLAMETHROWER_ARCHITECT_PLAN'
        )
        server = FakeACPServer(message=message)
        server.start()
        self.endpoint(server.port)
        task = Task.objects.create(title="Change", request="A request", state=Task.State.SCOPE_APPROVED, stage=Task.Stage.ARCHITECT)
        with override_settings(ACP_PORT=str(server.port)):
            self.assertEqual(self.client.post(reverse("refine_plan", args=[task.pk]), {"version": 0, "stage_version": 0}).status_code, 302)
            deadline = time.monotonic() + 10
            while not PermissionRequest.objects.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            permission = PermissionRequest.objects.get()
            self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "no"}).status_code, 302)
            while not AgentRun.objects.filter(status=AgentRun.Status.COMPLETED).exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(AgentRun.objects.filter(status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_worker(task.runs.first())
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertIn("Act as Architect", server.prompt)
        self.assertIn("Do not implement", server.prompt)
        self.assertNotIn("\n\nAPPROVED PLAN:\n", server.prompt)
        self.assertFalse(DeferredTask.objects.exists())
        task.refresh_from_db()
        self.assertEqual(task.plan, "")
