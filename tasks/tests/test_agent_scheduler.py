from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch

from django.db import OperationalError, close_old_connections
from django.http import HttpResponse
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from ..acp import AcpUnavailable, create_run
from ..agent_scheduler import claim_next_run, recover_interrupted_runs
from ..models import AcpEndpoint, AgentRun, Task


class PendingRunTests(TestCase):
    def setUp(self):
        self.primary = AcpEndpoint.objects.create(name="Primary", port=12345, session_limit=1)
        self.backup = AcpEndpoint.objects.create(name="Backup", port=12346, priority=2, session_limit=1)

    def task(self, title, endpoint=None):
        values = {}
        if endpoint:
            values = {"acp_selection": Task.AcpSelection.EXPLICIT, "selected_acp_endpoint": endpoint}
        return Task.objects.create(
            title=title, request=f"{title} request", stage=Task.Stage.DEVELOPER,
            state=Task.State.PLAN_APPROVED, **values,
        )

    def prepare(self, task):
        return create_run(
            task, stage=task.stage, task_version=task.version, work_cycle=task.work_cycle,
            request_snapshot=task.request, plan_snapshot="Approved plan", context_snapshot="Recorded context",
        )

    def test_prepared_prompt_is_persistent_and_second_run_is_rejected(self):
        task = self.task("One")
        run = self.prepare(task)
        self.assertEqual(run.status, AgentRun.Status.PENDING)
        self.assertIsNone(run.acp_endpoint)
        self.assertIn("One request", run.initial_prompt)
        self.assertIn("Approved plan", run.initial_prompt)
        self.assertIn("Recorded context", run.initial_prompt)
        self.assertIn("FLAMETHROWER_DEVELOPER_COMPLETE", run.initial_prompt)
        prompt = run.initial_prompt
        task.request = "Changed later"
        task.save(update_fields=["request"])
        run.refresh_from_db()
        self.assertEqual(run.initial_prompt, prompt)
        with self.assertRaisesMessage(AcpUnavailable, "already has an active run"):
            self.prepare(task)

    def test_architect_prompt_snapshots_task_context_at_preparation(self):
        task = Task.objects.create(
            title="Architect", request="Design request", stage=Task.Stage.ARCHITECT,
            state=Task.State.SCOPE_APPROVED,
        )
        other = self.task("Related")
        run = self.prepare(task)
        self.assertIn(f'"id": {other.pk}, "title": "Related"', run.initial_prompt)
        self.assertIn("FLAMETHROWER_TASKQUERY", run.initial_prompt)
        self.assertIn("FLAMETHROWER_ARCHITECT_PLAN", run.initial_prompt)
        prompt = run.initial_prompt
        other.title = "Renamed"
        other.save(update_fields=["title"])
        run.refresh_from_db()
        self.assertEqual(run.initial_prompt, prompt)

    def test_full_explicit_endpoint_does_not_block_automatic_dispatch(self):
        first = self.prepare(self.task("First", self.primary))
        waiting = self.prepare(self.task("Waiting", self.primary))
        automatic = self.prepare(self.task("Automatic"))
        self.assertEqual(claim_next_run().pk, first.pk)
        chosen = claim_next_run()
        self.assertEqual((chosen.pk, chosen.acp_endpoint_id), (automatic.pk, self.backup.pk))
        self.assertIsNone(claim_next_run())
        first.status = AgentRun.Status.INTERRUPTED
        first.save(update_fields=["status"])
        self.assertIsNone(claim_next_run())
        first.status = AgentRun.Status.ACKNOWLEDGED
        first.save(update_fields=["status"])
        self.assertEqual(claim_next_run().pk, waiting.pk)

    def test_disabled_or_unavailable_endpoint_waits_until_usable(self):
        run = self.prepare(self.task("Fixed", self.primary))
        self.primary.enabled = False
        self.primary.save(update_fields=["enabled"])
        self.assertIsNone(claim_next_run())
        self.primary.enabled = True
        self.primary.last_error = "Connection failed"
        self.primary.save(update_fields=["enabled", "last_error"])
        self.assertIsNone(claim_next_run())
        self.primary.last_error = ""
        self.primary.save(update_fields=["last_error"])
        self.assertEqual(claim_next_run().pk, run.pk)

    def test_paused_task_is_not_dispatched(self):
        task = self.task("Paused")
        run = self.prepare(task)
        task.automatic_pause_reason = "Review needed"
        task.save(update_fields=["automatic_pause_reason"])
        self.assertIsNone(claim_next_run())
        task.automatic_pause_reason = ""
        task.save(update_fields=["automatic_pause_reason"])
        self.assertEqual(claim_next_run().pk, run.pk)

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_refine_plan_resumes_after_cancelled_or_acknowledged_run(self, wake):
        for status in (AgentRun.Status.CANCELLED, AgentRun.Status.ACKNOWLEDGED):
            with self.subTest(status=status):
                task = Task.objects.create(
                    title=status, request="Design", stage=Task.Stage.ARCHITECT,
                    state=Task.State.SCOPE_APPROVED, automatic_pause_reason="Inspect the previous run.",
                )
                previous = AgentRun.objects.create(
                    task=task, stage=task.stage, status=status, task_version=task.version,
                    request_snapshot=task.request, plan_snapshot="",
                )
                response = self.client.post(reverse("refine_plan", args=[task.pk]), {
                    "version": task.version, "stage_version": task.stage_version,
                })
                self.assertEqual(response.status_code, 302)
                task.refresh_from_db()
                self.assertEqual(task.automatic_pause_reason, "")
                run = task.runs.exclude(pk=previous.pk).get()
                self.assertEqual(run.status, AgentRun.Status.PENDING)
                wake.assert_called_once_with()
                wake.reset_mock()
                self.assertEqual(claim_next_run().pk, run.pk)

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_refine_plan_failure_preserves_pause_and_does_not_queue(self, wake):
        task = Task.objects.create(
            title="Architect", request="Design", stage=Task.Stage.ARCHITECT,
            state=Task.State.SCOPE_APPROVED, automatic_pause_reason="Inspect the previous run.",
        )
        with patch("tasks.acp.build_initial_prompt", side_effect=RuntimeError("Prompt preparation failed")):
            with self.assertRaisesMessage(RuntimeError, "Prompt preparation failed"):
                self.client.post(reverse("refine_plan", args=[task.pk]), {
                    "version": task.version, "stage_version": task.stage_version,
                })
        task.refresh_from_db()
        self.assertEqual(task.automatic_pause_reason, "Inspect the previous run.")
        self.assertFalse(task.runs.exists())
        wake.assert_not_called()

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_refine_plan_rejects_unacknowledged_interruption_without_clearing_pause(self, wake):
        task = Task.objects.create(
            title="Architect", request="Design", stage=Task.Stage.ARCHITECT,
            state=Task.State.SCOPE_APPROVED, automatic_pause_reason="Inspect the interrupted run.",
        )
        AgentRun.objects.create(
            task=task, stage=task.stage, status=AgentRun.Status.INTERRUPTED,
            task_version=task.version, request_snapshot=task.request, plan_snapshot="",
        )
        with patch("tasks.views.planning.render", side_effect=lambda request, template, context, status: HttpResponse(status=status)):
            response = self.client.post(reverse("refine_plan", args=[task.pk]), {
                "version": task.version, "stage_version": task.stage_version,
            })
        self.assertEqual(response.status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.automatic_pause_reason, "Inspect the interrupted run.")
        self.assertEqual(task.runs.count(), 1)
        wake.assert_not_called()

    def test_pending_cancel_never_reserves_or_dispatches(self):
        task = self.task("Cancelled", self.primary)
        run = self.prepare(task)
        self.assertEqual(self.client.post(reverse("cancel_run", args=[run.pk])).status_code, 302)
        run.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.CANCELLED)
        self.assertTrue(run.cancel_requested)
        self.assertIn("cancelled", task.automatic_pause_reason)
        self.assertTrue(run.events.filter(content__contains="before dispatch").exists())
        self.assertIsNone(claim_next_run())

    def test_stale_or_unrecorded_prompt_is_not_dispatched(self):
        task = self.task("Stale")
        run = self.prepare(task)
        task.version += 1
        task.save(update_fields=["version"])
        self.assertIsNone(claim_next_run())
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.CANCELLED)
        self.assertIn("no longer valid", run.error)
        task = self.task("Missing")
        run = self.prepare(task)
        AgentRun.objects.filter(pk=run.pk).update(initial_prompt=None)
        self.assertIsNone(claim_next_run())
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.CANCELLED)

    def test_restart_preserves_pending_but_interrupts_dispatched_runs(self):
        running = self.prepare(self.task("Running"))
        pending = self.prepare(self.task("Pending"))
        self.assertEqual(claim_next_run().pk, running.pk)
        recover_interrupted_runs()
        running.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(running.status, AgentRun.Status.INTERRUPTED)
        self.assertEqual(pending.status, AgentRun.Status.PENDING)
        self.assertEqual(claim_next_run().pk, pending.pk)


class ConcurrentClaimsTests(TransactionTestCase):
    def test_worker_executes_recorded_prompt_and_continues_workflow(self):
        from ..agent_scheduler import execute_run

        endpoint = AcpEndpoint.objects.create(name="Only", port=12345)
        task = Task.objects.create(
            title="Run", request="r", stage=Task.Stage.DEVELOPER, state=Task.State.PLAN_APPROVED,
        )
        create_run(task, stage=task.stage, task_version=task.version, request_snapshot="r", plan_snapshot="p")
        run = claim_next_run()
        with patch("tasks.agent_scheduler.run_agent", new_callable=Mock) as agent, patch(
            "tasks.agent_scheduler.asyncio.run",
        ) as run_async, patch("tasks.automation.continue_automatic_run") as continuation:
            thread = execute_run(run)
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        agent.assert_called_once_with(
            run.pk, run.initial_prompt, endpoint.pk, endpoint.host, endpoint.port, run.stage,
        )
        run_async.assert_called_once_with(agent.return_value)
        continuation.assert_called_once_with(run.pk)

    def test_worker_failure_is_recorded_and_reserves_the_session(self):
        from ..agent_scheduler import execute_run

        endpoint = AcpEndpoint.objects.create(name="Only", port=12345, session_limit=1)
        task = Task.objects.create(
            title="Failure", request="r", stage=Task.Stage.DEVELOPER, state=Task.State.PLAN_APPROVED,
        )
        run = create_run(task, stage=task.stage, task_version=task.version, request_snapshot="r", plan_snapshot="p")
        run = claim_next_run()
        with patch("tasks.agent_scheduler.run_agent", new=lambda *args: None), patch(
            "tasks.agent_scheduler.asyncio.run", side_effect=RuntimeError("worker failed"),
        ), self.assertLogs("tasks.agent_scheduler", level="ERROR"):
            thread = execute_run(run)
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        run.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.INTERRUPTED)
        self.assertIn("worker failed", run.error)
        self.assertIn("worker failed", task.automatic_pause_reason)
        self.assertEqual(run.acp_endpoint, endpoint)

    def test_concurrent_claims_never_exceed_session_limit(self):
        endpoint = AcpEndpoint.objects.create(name="Only", port=12345, session_limit=1)
        for index in range(2):
            task = Task.objects.create(
                title=str(index), request="r", stage=Task.Stage.DEVELOPER, state=Task.State.PLAN_APPROVED,
            )
            create_run(task, stage=task.stage, task_version=task.version, request_snapshot="r", plan_snapshot="p")
        barrier = Barrier(2)

        def claim():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    run = claim_next_run()
                    return run.pk if run else None
                except OperationalError:
                    return None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: claim(), range(2)))
        self.assertLessEqual(sum(item is not None for item in claimed), 1)
        self.assertLessEqual(AgentRun.objects.filter(acp_endpoint=endpoint, status=AgentRun.Status.RUNNING).count(), 1)
        if all(item is None for item in claimed):
            self.assertIsNotNone(claim_next_run())
        self.assertIsNone(claim_next_run())
