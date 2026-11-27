from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse

from ..automation import continue_automatic_task
from ..models import AcpEndpoint, AgentRun, Task
from ..views import lane_order_token


class TaskMetadataTests(TestCase):
    def setUp(self):
        AcpEndpoint.objects.create(name="Test ACP", port=12345)
        self.task = Task.objects.create(title="Task", request="Request")

    def metadata(self, **fields):
        self.task.refresh_from_db()
        data = {
            "version": self.task.version, "stage_version": self.task.stage_version,
            "status": self.task.status, "severity": self.task.severity, "blocked_reason": self.task.blocked_reason,
        }
        data.update(fields)
        return self.client.post(reverse("set_task_metadata", args=[self.task.pk]), data)

    def test_human_task_defaults_and_editable_severity(self):
        self.assertEqual(self.task.status, Task.Status.READY)
        self.assertEqual(self.task.severity, Task.Severity.MAJOR)
        self.assertGreater(self.task.pk, 0)
        self.assertEqual(self.metadata(severity="blocker").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.severity, Task.Severity.BLOCKER)


    def test_severity_levels_save(self):
        for severity in Task.Severity.values:
            with self.subTest(severity=severity):
                self.assertEqual(self.metadata(severity=severity).status_code, 302)
                self.task.refresh_from_db()
                self.assertEqual(self.task.severity, severity)

    def test_old_severity_names_are_rejected_without_changing_the_task(self):
        for severity in ("high", "medium", "low"):
            with self.subTest(severity=severity):
                response = self.metadata(severity=severity)
                self.assertEqual(response.status_code, 400)
                self.task.refresh_from_db()
                self.assertEqual(self.task.severity, Task.Severity.MAJOR)


    def test_nonaccepted_requires_explicit_acceptance_before_advancing(self):
        Task.objects.filter(pk=self.task.pk).update(status=Task.Status.NON_ACCEPTED)
        advance = reverse("advance_stage", args=[self.task.pk])
        self.assertEqual(self.client.post(advance, {"version": 0, "stage_version": 0}).status_code, 409)
        self.assertEqual(self.metadata(status="ready").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.client.post(advance, {"version": self.task.version, "stage_version": self.task.stage_version}).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.ARCHITECT)


    def test_blocked_needs_reason_and_blocks_advancement_and_runs(self):
        response = self.metadata(status="blocked", blocked_reason="")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.metadata(status="blocked", blocked_reason="Waiting for access").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.blocked_reason, "Waiting for access")
        self.assertEqual(self.client.post(reverse("advance_stage", args=[self.task.pk]), {
            "version": self.task.version, "stage_version": self.task.stage_version,
        }).status_code, 409)
        self.assertEqual(self.metadata(status="ready").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.blocked_reason, "")

    def test_non_ready_status_transitions_preserve_metadata(self):
        Task.objects.filter(pk=self.task.pk).update(status=Task.Status.NON_ACCEPTED)
        severity, work_type_id = self.task.severity, self.task.work_type_id
        self.assertEqual(self.metadata(status="blocked", blocked_reason="Waiting for access").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.blocked_reason), (Task.Status.BLOCKED, "Waiting for access"))
        self.assertEqual(self.metadata(status="abandoned").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.blocked_reason), (Task.Status.ABANDONED, ""))
        self.assertEqual(self.metadata(status="ready").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.severity, self.task.work_type_id), (Task.Status.READY, severity, work_type_id))

    @override_settings(ACP_PORT="12345")
    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_blocked_task_cannot_start_or_automatically_advance(self, worker):
        Task.objects.filter(pk=self.task.pk).update(
            status="blocked", blocked_reason="Waiting", state="plan_approved",
            stage="developer",
        )
        response = self.client.post(reverse("start_run", args=[self.task.pk]), {"version": 0, "stage_version": 0})
        self.assertEqual(response.status_code, 409)
        continue_automatic_task(self.task.pk)
        self.assertFalse(AgentRun.objects.exists())
        worker.assert_not_called()

    def test_active_run_prevents_blocking_or_abandoning(self):
        AgentRun.objects.create(task=self.task, task_version=0, request_snapshot="r", plan_snapshot="p", status="running")
        self.assertEqual(self.metadata(status="blocked", blocked_reason="Waiting").status_code, 409)
        self.assertEqual(self.metadata(status="abandoned").status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "ready")
        self.assertEqual(self.metadata(severity="critical").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.severity, "critical")

    def test_abandoned_and_stale_changes_are_rejected(self):
        self.assertEqual(self.metadata(status="abandoned").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.client.post(reverse("advance_stage", args=[self.task.pk]), {
            "version": self.task.version, "stage_version": self.task.stage_version,
        }).status_code, 409)
        response = self.client.post(reverse("set_task_metadata", args=[self.task.pk]), {
            "version": self.task.version, "stage_version": 0, "status": "ready", "severity": "major",
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.metadata(status="ready").status_code, 302)

    def test_status_pause_requires_explicit_workflow_continuation(self):
        self.assertEqual(self.metadata(status="blocked", blocked_reason="Dependency").status_code, 302)
        self.task.refresh_from_db()
        self.assertIn("status prevents advancement", self.task.automatic_pause_reason)
        self.assertEqual(self.metadata(status="ready").status_code, 302)
        self.task.refresh_from_db()
        self.assertTrue(self.task.automatic_pause_reason)

    def test_gate_finishing_and_reopening_synchronize_done_status(self):
        Task.objects.filter(pk=self.task.pk).update(
            stage="review", state="plan_approved",
            automatic_pause_reason="Review the current gate before continuing.",
        )
        response = self.client.post(reverse("record_gate", args=[self.task.pk]), {
            "version": 0, "stage_version": 0, "result": "passed", "evidence": "Verified", "action": "advance",
        })
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.stage, self.task.finished), ("done", "finished", True))
        response = self.client.post(reverse("reopen_task", args=[self.task.pk]), {
            "version": self.task.version, "stage_version": self.task.stage_version,
        })
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.stage, self.task.finished), ("ready", "review", False))

    def test_blocked_task_cannot_advance_gate_or_request_rework(self):
        Task.objects.filter(pk=self.task.pk).update(
            stage="review", state="plan_approved", status="blocked", blocked_reason="Waiting",
        )
        gate = self.client.post(reverse("record_gate", args=[self.task.pk]), {
            "version": 0, "stage_version": 0, "result": "passed", "evidence": "Verified", "action": "advance",
        })
        rework = self.client.post(reverse("return_to_developer", args=[self.task.pk]), {
            "version": 0, "stage_version": 0, "reason": "Fix issue",
        })
        self.assertEqual(gate.status_code, 409)
        self.assertEqual(rework.status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, "review")

    def test_manual_done_and_invalid_version_are_rejected(self):
        self.assertEqual(self.metadata(status="done").status_code, 400)
        response = self.client.post(reverse("set_task_metadata", args=[self.task.pk]), {
            "version": "bad", "stage_version": 0, "status": "ready", "severity": "major",
        })
        self.assertEqual(response.status_code, 400)


class PriorityTests(TestCase):
    def setUp(self):
        self.tasks = [Task.objects.create(title=f"Task {index}", request="r") for index in range(3)]
        self.other = Task.objects.create(title="Other lane", request="r", stage=Task.Stage.ARCHITECT)

    def ordered(self):
        return list(Task.objects.filter(stage=Task.Stage.DRAFT).order_by("priority", "pk"))

    def reorder(self, task, **fields):
        return self.client.post(reverse("reorder_task", args=[task.pk]), {
            "order_token": lane_order_token(self.ordered()), **fields,
        })

    def test_buttons_and_drag_reorder_per_lane(self):
        self.assertEqual([task.priority for task in self.ordered()], [1, 2, 3])
        self.assertEqual(self.reorder(self.tasks[2], direction="up").status_code, 302)
        self.assertEqual([task.pk for task in self.ordered()], [self.tasks[0].pk, self.tasks[2].pk, self.tasks[1].pk])
        self.assertEqual(self.reorder(self.tasks[0], direction="after", target=self.tasks[1].pk).status_code, 302)
        self.assertEqual([task.pk for task in self.ordered()], [self.tasks[2].pk, self.tasks[1].pk, self.tasks[0].pk])
        self.other.refresh_from_db()
        self.assertEqual(self.other.priority, 1)

    def test_stale_and_cross_lane_moves_do_not_change_order(self):
        before = [task.pk for task in self.ordered()]
        self.assertEqual(self.client.post(reverse("reorder_task", args=[self.tasks[0].pk]), {
            "order_token": "stale", "direction": "down",
        }).status_code, 409)
        self.assertEqual(self.reorder(self.tasks[0], direction="before", target=self.other.pk).status_code, 400)
        self.assertEqual([task.pk for task in self.ordered()], before)

    def test_stage_changes_append(self):
        self.assertEqual(self.client.post(reverse("advance_stage", args=[self.tasks[0].pk]), {
            "version": 0, "stage_version": 0,
        }).status_code, 302)
        self.tasks[0].refresh_from_db()
        self.assertEqual(self.tasks[0].priority, 2)
