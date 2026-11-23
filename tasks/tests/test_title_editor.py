from django.test import Client, TestCase
from django.urls import reverse

from ..models import AgentRun, Approval, RunEvent, Task


class InlineTaskTitleTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(title="Original title", request="Requested change")
        self.url = reverse("update_task_title", args=[self.task.pk])

    def post_title(self, title, *, version=None, stage_version=None):
        return self.client.post(self.url, {
            "title": title,
            "version": self.task.version if version is None else version,
            "stage_version": self.task.stage_version if stage_version is None else stage_version,
        })

    def test_title_update_returns_canonical_title_and_preserves_edit_semantics(self):
        self.task.state = Task.State.PLAN_APPROVED
        self.task.stage = Task.Stage.DEVELOPER
        self.task.plan = "Approved plan"
        self.task.save(update_fields=["state", "stage", "plan"])
        approval = Approval.objects.create(
            task=self.task, kind=Approval.Kind.PLAN, content="Original title and approved plan",
        )
        run = AgentRun.objects.create(
            task=self.task, task_version=self.task.version,
            request_snapshot="Original title\n\nRequested change", plan_snapshot="Approved plan",
            status=AgentRun.Status.COMPLETED,
        )
        self.task.embedding = [0.1, 0.2]
        self.task.indexing_error = "Previous indexing error"
        self.task.relates_indexing = Task.IndexingStatus.FAILED
        self.task.save(update_fields=["embedding", "indexing_error", "relates_indexing"])

        response = self.post_title("  Revised title  ")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "title": "Revised title", "version": 0, "stage_version": 1,
        })
        self.task.refresh_from_db()
        self.assertEqual((self.task.state, self.task.stage), (Task.State.PLAN_APPROVED, Task.Stage.DEVELOPER))
        self.assertEqual(self.task.plan, "Approved plan")
        self.assertEqual(self.task.version, run.task_version)
        self.assertEqual(self.task.title, "Revised title")
        self.assertEqual(self.task.indexing_revision, 1)
        self.assertEqual(self.task.relates_indexing, Task.IndexingStatus.PENDING)
        self.assertIsNone(self.task.embedding)
        self.assertEqual(self.task.indexing_error, "")
        self.assertEqual(Approval.objects.get(pk=approval.pk).content, "Original title and approved plan")
        self.assertEqual(run.request_snapshot, "Original title\n\nRequested change")
        self.assertEqual(run.plan_snapshot, "Approved plan")

    def test_title_update_keeps_current_architect_proposal_eligible(self):
        from ..views.planning import current_architect_proposal

        self.task.state = Task.State.SCOPE_APPROVED
        self.task.stage = Task.Stage.ARCHITECT
        self.task.save(update_fields=["state", "stage"])
        run = AgentRun.objects.create(
            task=self.task, stage=Task.Stage.ARCHITECT, task_version=self.task.version,
            request_snapshot=self.task.request, plan_snapshot="",
            status=AgentRun.Status.COMPLETED,
        )
        RunEvent.objects.create(
            run=run, kind="message",
            content="FLAMETHROWER_ARCHITECT_PLAN\nProposed plan\nEND_FLAMETHROWER_ARCHITECT_PLAN",
        )

        response = self.post_title("Revised title")

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(current_architect_proposal(self.task)["plan"], "Proposed plan")

    def test_unchanged_title_does_not_advance_versions(self):
        self.task.embedding = [0.1, 0.2]
        self.task.indexing_error = "Previous indexing error"
        self.task.save(update_fields=["embedding", "indexing_error"])

        response = self.post_title("Original title")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], 0)
        self.assertEqual(response.json()["stage_version"], 0)
        self.task.refresh_from_db()
        self.assertEqual((self.task.version, self.task.stage_version), (0, 0))
        self.assertEqual(self.task.indexing_revision, 0)
        self.assertEqual(self.task.embedding, [0.1, 0.2])
        self.assertEqual(self.task.indexing_error, "Previous indexing error")

    def test_invalid_title_or_versions_return_json_validation_errors(self):
        for values in (
            {"title": ""},
            {"title": " "},
            {"title": "x" * 201},
            {"title": "Changed", "version": "bad"},
        ):
            with self.subTest(values=values):
                response = self.client.post(self.url, {
                    "version": 0, "stage_version": 0, **values,
                })
                self.assertEqual(response.status_code, 400)
                self.assertIn("errors", response.json())
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Original title")

    def test_stale_versions_return_conflict_without_overwriting(self):
        response = self.post_title("Changed", version=1)

        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Original title")

    def test_active_run_rejects_title_updates(self):
        AgentRun.objects.create(
            task=self.task, task_version=self.task.version,
            request_snapshot=self.task.request, plan_snapshot="",
            status=AgentRun.Status.RUNNING,
        )

        response = self.post_title("Changed")

        self.assertEqual(response.status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Original title")

    def test_endpoint_requires_post_and_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)

        response = csrf_client.post(self.url, {
            "title": "Changed", "version": 0, "stage_version": 0,
        })

        self.assertEqual(response.status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Original title")

    def test_inline_editor_is_only_rendered_on_task_detail(self):
        detail = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertContains(detail, 'data-title-editor')
        self.assertContains(detail, 'data-title-input')
        self.assertContains(detail, 'aria-label="Save title"')
        self.assertContains(detail, 'aria-label="Cancel title edit"')
        self.assertContains(detail, "tasks/title-editor.js")

        run = AgentRun.objects.create(
            task=self.task, task_version=self.task.version,
            request_snapshot=self.task.request, plan_snapshot="",
            status=AgentRun.Status.COMPLETED,
        )
        activity = self.client.get(reverse("run_activity", args=[self.task.pk, run.pk]))
        self.assertNotContains(activity, 'data-title-editor')
