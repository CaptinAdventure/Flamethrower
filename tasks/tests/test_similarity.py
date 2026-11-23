from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import LinkType, SimilaritySettings, Task, TaskLink, WorkType
from ..similarity import (
    process_one, process_work_type_suggestion, recover_interrupted, settings_instance,
    suggestions, work_type_suggestion,
)


def vector(first, second=0):
    return [first, second] + [0.0] * 382


class SimilarityTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(title="First", request="request")
        self.other = Task.objects.create(title="Second", request="request")

    def done(self, task, embedding):
        Task.objects.filter(pk=task.pk).update(relates_indexing=Task.IndexingStatus.DONE, embedding=embedding)
        task.refresh_from_db()

    def review(self, action, task=None, candidate=None):
        task = task or self.task
        candidate = candidate or self.other
        return self.client.post(reverse("review_suggestion", args=[task.pk, candidate.pk]), {
            "action": action, "task_revision": task.indexing_revision, "candidate_revision": candidate.indexing_revision,
        })

    def test_worker_claims_and_indexes_pending_task(self):
        model = Mock()
        model.encode.return_value = vector(1)
        self.assertEqual(self.task.relates_indexing, Task.IndexingStatus.PENDING)
        self.assertTrue(process_one(lambda: model))
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, Task.IndexingStatus.DONE)
        self.assertEqual(self.task.embedding, vector(1))
        model.encode.assert_called_once_with("First\n\nrequest", normalize_embeddings=True)
        self.assertEqual(model.max_seq_length, 512)

    def test_failure_can_be_retried(self):
        with patch("tasks.similarity.logger.exception"):
            process_one(Mock(side_effect=RuntimeError("model unavailable")))
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, Task.IndexingStatus.FAILED)
        self.client.post(reverse("retry_indexing", args=[self.task.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, Task.IndexingStatus.PENDING)
        self.assertEqual(self.task.indexing_revision, 1)
        self.assertEqual(self.task.indexing_error, "")

    def test_recovery_requeues_interrupted_tasks(self):
        Task.objects.filter(pk=self.task.pk).update(relates_indexing=Task.IndexingStatus.IN_PROGRESS)
        recover_interrupted()
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, Task.IndexingStatus.PENDING)
        self.assertEqual(self.task.indexing_revision, 1)

    def test_stale_worker_result_is_discarded(self):
        def encode(*args, **kwargs):
            Task.objects.filter(pk=self.task.pk).update(
                title="Changed", indexing_revision=1, relates_indexing=Task.IndexingStatus.PENDING,
            )
            return vector(1)
        model = Mock()
        model.encode.side_effect = encode
        process_one(lambda: model)
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, Task.IndexingStatus.PENDING)
        self.assertIsNone(self.task.embedding)

    def test_ranking_threshold_and_limit_and_existing_links(self):
        self.done(self.task, vector(1))
        self.done(self.other, vector(0.9, 0.43589))
        third = Task.objects.create(title="Third", request="request", relates_indexing="DONE", embedding=vector(0.8, 0.6))
        fourth = Task.objects.create(title="Fourth", request="request", relates_indexing="DONE", embedding=vector(0.7, 0.714))
        config = settings_instance()
        config.max_results = 1
        self.assertEqual([x["task"].pk for x in suggestions(self.task, config)], [self.other.pk])
        config.max_results = 5
        self.assertEqual([x["task"].pk for x in suggestions(self.task, config)], [self.other.pk, third.pk])
        TaskLink.objects.create(link_type=LinkType.objects.get(name="Blocks"), source=third, target=self.task)
        self.assertEqual([x["task"].pk for x in suggestions(self.task, config)], [self.other.pk])
        self.assertNotIn(fourth.pk, [x["task"].pk for x in suggestions(self.task, config)])

    def test_dismissal_expires_on_candidate_change_and_accept_creates_link(self):
        self.done(self.task, vector(1))
        self.done(self.other, vector(1))
        self.assertEqual(self.review("dismiss").status_code, 302)
        self.assertEqual(suggestions(self.task, settings_instance()), [])
        Task.objects.filter(pk=self.other.pk).update(indexing_revision=1)
        self.other.refresh_from_db()
        self.assertEqual(len(suggestions(self.task, settings_instance())), 1)
        self.assertEqual(self.review("accept").status_code, 302)
        link = TaskLink.objects.get()
        self.assertEqual(link.link_type.name, "Relates")
        self.assertEqual((link.source_id, link.target_id), (self.task.pk, self.other.pk))
        self.assertEqual(self.review("accept").status_code, 409)


    def test_stale_suggestion_cannot_be_accepted(self):
        self.done(self.task, vector(1))
        self.done(self.other, vector(1))
        Task.objects.filter(pk=self.other.pk).update(indexing_revision=1, relates_indexing="PENDING", embedding=None)
        self.assertEqual(self.review("accept").status_code, 409)
        self.assertFalse(TaskLink.objects.exists())

    def test_edit_reindexes_only_when_title_or_request_changes(self):
        self.done(self.task, vector(1))
        url = reverse("task_edit", args=[self.task.pk])
        self.client.post(url, {"version": 0, "stage_version": 0, "title": "First", "request": "request", "plan": "A plan"})
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, "DONE")
        self.client.post(url, {"version": self.task.version, "stage_version": self.task.stage_version, "title": "Changed", "request": "request", "plan": "A plan"})
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, "PENDING")
        self.assertEqual(self.task.indexing_revision, 1)
        self.assertIsNone(self.task.embedding)

    def test_settings_validate_and_reindex_only_for_token_changes(self):
        self.done(self.task, vector(1))
        self.client.force_login(get_user_model().objects.create_superuser(username="admin", password="test"))
        url = reverse("admin:tasks_similaritysettings_change", args=[settings_instance().pk])
        self.assertEqual(self.client.post(url, {"threshold": 0.8, "max_results": 3, "max_tokens": 512}).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, "DONE")
        self.assertEqual(self.client.post(url, {"threshold": 0.8, "max_results": 3, "max_tokens": 513}).status_code, 200)
        self.assertEqual(settings_instance().max_tokens, 512)
        self.assertEqual(self.client.post(url, {"threshold": 0.8, "max_results": 3, "max_tokens": 128}).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, "PENDING")
        self.assertEqual(self.task.indexing_revision, 1)
        self.assertIsNone(self.task.embedding)

    def test_settings_admin_requires_permission_and_manages_singleton(self):
        list_url = reverse("admin:tasks_similaritysettings_changelist")
        self.assertEqual(self.client.get(list_url).status_code, 302)
        self.assertFalse(SimilaritySettings.objects.exists())
        staff = get_user_model().objects.create_user(username="staff", password="test", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(list_url).status_code, 403)
        self.assertFalse(SimilaritySettings.objects.exists())
        admin = get_user_model().objects.create_superuser(username="admin", password="test")
        self.client.force_login(admin)
        self.assertEqual(self.client.get(list_url).status_code, 200)
        self.assertEqual(SimilaritySettings.objects.count(), 1)
        self.assertEqual(self.client.get(reverse("admin:tasks_similaritysettings_add")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:tasks_similaritysettings_delete", args=[1]), {"post": "yes"}).status_code, 403)
        self.assertEqual(SimilaritySettings.objects.count(), 1)

    def test_work_type_suggestion_uses_descriptions_and_reuses_vectors(self):
        self.done(self.task, vector(1))
        self.done(self.other, vector(1))
        bug = WorkType.objects.get(title="Bug")
        model = Mock()
        model.encode.side_effect = lambda texts, **kwargs: [
            vector(1) if text == f"{bug.title}\n\n{bug.description}" else vector(0, 1)
            for text in texts
        ]
        cache = {}
        self.assertTrue(process_work_type_suggestion(lambda: model, cache))
        self.task.refresh_from_db()
        self.assertEqual(work_type_suggestion(self.task)["work_type"], bug)
        self.assertNotEqual(self.task.work_type_id, bug.pk)
        self.assertTrue(process_work_type_suggestion(lambda: model, cache))
        self.assertEqual(model.encode.call_count, 1)
        self.other.refresh_from_db()
        self.assertEqual(work_type_suggestion(self.other)["work_type"], bug)
        self.assertFalse(process_work_type_suggestion(lambda: model, cache))

    def test_changed_type_invalidates_and_recomputes_suggestion(self):
        self.done(self.task, vector(1))
        model = Mock()
        model.encode.side_effect = lambda texts, **kwargs: [vector(1) for _ in texts]
        cache = {}
        process_work_type_suggestion(lambda: model, cache)
        self.task.refresh_from_db()
        self.assertEqual(work_type_suggestion(self.task)["status"], "ready")
        bug = WorkType.objects.get(title="Bug")
        bug.description = "A revised definition."
        bug.save(update_fields=["description"])
        self.assertEqual(work_type_suggestion(self.task)["status"], "pending")
        process_work_type_suggestion(lambda: model, cache)
        self.task.refresh_from_db()
        self.assertEqual(work_type_suggestion(self.task)["status"], "ready")
        self.assertEqual(model.encode.call_count, 2)

    def test_work_type_suggestion_failure_is_visible_and_retryable(self):
        self.done(self.task, vector(1))
        model = Mock()
        model.encode.side_effect = RuntimeError("model unavailable")
        with patch("tasks.similarity.logger.exception"):
            self.assertTrue(process_work_type_suggestion(lambda: model, {}))
        self.task.refresh_from_db()
        self.assertIn("model unavailable", work_type_suggestion(self.task)["error"])
        self.assertFalse(process_work_type_suggestion(lambda: model, {}))
        self.assertEqual(self.client.post(reverse("retry_work_type_suggestion", args=[self.task.pk])).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(work_type_suggestion(self.task)["status"], "pending")

    def test_missing_task_embedding_is_reported_as_failure(self):
        Task.objects.filter(pk=self.task.pk).update(relates_indexing=Task.IndexingStatus.DONE, embedding=None)
        with patch("tasks.similarity.logger.exception"):
            self.assertTrue(process_work_type_suggestion(Mock(), {}))
        self.task.refresh_from_db()
        self.assertIn("task embedding is invalid", work_type_suggestion(self.task)["error"])
        self.assertEqual(self.client.post(reverse("retry_work_type_suggestion", args=[self.task.pk])).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.relates_indexing, Task.IndexingStatus.PENDING)
        self.assertEqual(self.task.indexing_revision, 1)

    def test_reindexing_invalidates_existing_work_type_suggestion(self):
        self.done(self.task, vector(1))
        model = Mock()
        model.encode.side_effect = lambda texts, **kwargs: [vector(1) for _ in texts]
        process_work_type_suggestion(lambda: model, {})
        self.task.refresh_from_db()
        self.assertEqual(work_type_suggestion(self.task)["status"], "ready")
        Task.objects.filter(pk=self.task.pk).update(relates_indexing=Task.IndexingStatus.PENDING)
        model.encode.side_effect = None
        model.encode.return_value = vector(1)
        process_one(lambda: model)
        self.task.refresh_from_db()
        self.assertEqual(work_type_suggestion(self.task)["status"], "pending")

    def test_work_type_suggestion_discards_changed_task_and_type(self):
        self.done(self.task, vector(1))
        model = Mock()
        def encode(texts, **kwargs):
            Task.objects.filter(pk=self.task.pk).update(indexing_revision=1, relates_indexing="PENDING")
            return [vector(1) for _ in texts]
        model.encode.side_effect = encode
        process_work_type_suggestion(lambda: model, {})
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_type_suggestion_signature, "")
        self.assertIsNone(self.task.suggested_work_type)

        self.done(self.task, vector(1))
        def change_type(texts, **kwargs):
            WorkType.objects.filter(title="Bug").update(description="Changed")
            return [vector(1) for _ in texts]
        model.encode.side_effect = change_type
        process_work_type_suggestion(lambda: model, {})
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_type_suggestion_signature, "")
        self.assertEqual(work_type_suggestion(self.task)["status"], "pending")
