from django.test import Client, TestCase
from django.urls import reverse

from ..models import AgentRun, Approval, Task


class InlineTaskRequestTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(title="Title", request="Original request", plan="Approved plan")
        self.url = reverse("update_task_request", args=[self.task.pk])

    def save_request(self, value, **versions):
        return self.client.post(self.url, {
            "request": value,
            "version": versions.get("version", self.task.version),
            "stage_version": versions.get("stage_version", self.task.stage_version),
        })

    def test_edit_invalidates_approval_and_reindexes_without_changing_history(self):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.DEVELOPER
        self.task.embedding = [0.1, 0.2]
        self.task.indexing_error = "Old error"
        self.task.automatic_gate_returns = {"testing": 2}
        self.task.save()
        approval = Approval.objects.create(task=self.task, kind=Approval.Kind.PLAN, content="Original approval")
        run = AgentRun.objects.create(
            task=self.task, task_version=0, request_snapshot="Original request",
            plan_snapshot="Approved plan", status=AgentRun.Status.COMPLETED,
        )

        response = self.save_request("  Revised request\nSecond line  ")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "request": "Revised request\nSecond line", "version": 1, "stage_version": 1,
        })
        self.task.refresh_from_db()
        self.assertEqual((self.task.state, self.task.stage), (Task.State.DRAFT, Task.Stage.DRAFT))
        self.assertEqual((self.task.title, self.task.plan), ("Title", "Approved plan"))
        self.assertIn("reapprove", self.task.automatic_pause_reason)
        self.assertEqual(self.task.automatic_gate_returns, {})
        self.assertEqual(self.task.relates_indexing, Task.IndexingStatus.PENDING)
        self.assertEqual(self.task.indexing_revision, 1)
        self.assertIsNone(self.task.embedding)
        self.assertEqual(self.task.indexing_error, "")
        self.assertEqual(Approval.objects.get(pk=approval.pk).content, "Original approval")
        run.refresh_from_db()
        self.assertEqual(run.request_snapshot, "Original request")

    def test_unchanged_request_does_not_change_versions_or_index(self):
        self.task.embedding = [0.1, 0.2]
        self.task.save(update_fields=["embedding"])
        response = self.save_request("Original request")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["version"], response.json()["stage_version"]), (0, 0))
        self.task.refresh_from_db()
        self.assertEqual(self.task.indexing_revision, 0)
        self.assertEqual(self.task.embedding, [0.1, 0.2])

    def test_invalid_or_stale_submissions_do_not_change_request(self):
        for value, versions, expected in (
            ("   ", {}, 400),
            ("Changed", {"version": "bad"}, 400),
            ("Changed", {"version": 1}, 409),
            ("Changed", {"stage_version": 1}, 409),
        ):
            with self.subTest(value=value, versions=versions):
                response = self.save_request(value, **versions)
                self.assertEqual(response.status_code, expected)
        self.task.refresh_from_db()
        self.assertEqual(self.task.request, "Original request")

    def test_active_runs_prevent_edits(self):
        for run_status in AgentRun.ACTIVE:
            with self.subTest(status=run_status):
                run = AgentRun.objects.create(
                    task=self.task, task_version=0, request_snapshot="Original request",
                    plan_snapshot="", status=run_status,
                )
                response = self.save_request("Changed")
                self.assertEqual(response.status_code, 409)
                run.delete()
        self.task.refresh_from_db()
        self.assertEqual(self.task.request, "Original request")

    def test_requires_post_and_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        response = Client(enforce_csrf_checks=True).post(self.url, {
            "request": "Changed", "version": 0, "stage_version": 0,
        })
        self.assertEqual(response.status_code, 403)
