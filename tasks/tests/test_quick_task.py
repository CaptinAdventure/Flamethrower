from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from ..models import Task
from ..title_generation import UnusableTitleError, generate_title


class QuickTaskTests(TestCase):
    def test_quick_entry_prefills_form_without_creating_task(self):
        request_text = 'Add a "save" button <safely>'
        with patch("tasks.views.editing.generate_title", return_value="Add a save button") as generate:
            response = self.client.post(reverse("quick_task"), {"request": request_text, "action": "edit"})
        generate.assert_called_once_with(request_text)
        self.assertEqual(Task.objects.count(), 0)
        self.assertEqual(response.context["form"].initial["request"], request_text)
        self.assertEqual(response.context["form"].initial["title"], "Add a save button")

        response = self.client.post(reverse("task_create"), {
            "title": "Add a save button", "request": request_text, "plan": "Plan",
        })
        task = Task.objects.get()
        self.assertRedirects(response, reverse("task_detail", args=[task.pk]))
        self.assertEqual((task.request, task.status, task.relates_indexing), (
            request_text, Task.Status.READY, Task.IndexingStatus.PENDING,
        ))

    def test_enter_creates_task_and_returns_to_board(self):
        with patch("tasks.views.editing.generate_title", return_value="Add a save button"):
            response = self.client.post(reverse("quick_task"), {"request": "Add a save button", "action": "create"})
        self.assertRedirects(response, reverse("task_list"))
        task = Task.objects.get()
        self.assertEqual((task.title, task.request, task.status, task.relates_indexing), (
            "Add a save button", "Add a save button", Task.Status.READY, Task.IndexingStatus.PENDING,
        ))

    def test_empty_request_is_rejected_without_calling_model(self):
        with patch("tasks.views.editing.generate_title") as generate:
            response = self.client.post(reverse("quick_task"), {"request": "  "})
        self.assertEqual(response.status_code, 400)
        generate.assert_not_called()
        self.assertFalse(Task.objects.exists())

    def test_repetitive_title_uses_request_and_warns_for_both_actions(self):
        for action in ("create", "edit"):
            with self.subTest(action=action):
                with patch("tasks.views.editing.generate_title", side_effect=UnusableTitleError("repeated")):
                    with self.assertLogs("tasks.views.editing", level="WARNING"):
                        response = self.client.post(reverse("quick_task"), {
                            "request": "Make the task quick entry submit on enter", "action": action,
                        }, follow=True)
                if action == "create":
                    task = Task.objects.get()
                    self.assertEqual(task.title, "Make the task quick entry submit on enter")
                    task.delete()
                else:
                    self.assertEqual(response.context["form"].initial["title"], "Make the task quick entry submit on enter")
                    self.assertFalse(Task.objects.exists())

    def test_generation_failure_preserves_request_for_manual_entry(self):
        with patch("tasks.views.editing.generate_title", side_effect=OSError("model unavailable")):
            with self.assertLogs("tasks.views.editing", level="ERROR"):
                response = self.client.post(reverse("quick_task"), {"request": "Do something"})
        self.assertEqual(response.context["form"].initial, {"request": "Do something"})
        self.assertFalse(Task.objects.exists())


class TitleGenerationTests(SimpleTestCase):
    def test_empty_model_output_is_an_error(self):
        tokenizer = Mock()
        tokenizer.return_value = {}
        tokenizer.decode.return_value = "   "
        model = Mock()
        model.generate.return_value = [[1]]
        with patch("tasks.title_generation.load_model", return_value=(tokenizer, model)):
            with self.assertRaisesMessage(ValueError, "empty title"):
                generate_title("Add a task")

    def test_repeated_words_and_phrases_are_rejected(self):
        tokenizer = Mock()
        tokenizer.return_value = {}
        model = Mock()
        model.generate.return_value = [[1]]
        for title in ("Standard: Standard: Standrard:", "A task: a task", "..."):
            with self.subTest(title=title):
                tokenizer.decode.return_value = title
                with patch("tasks.title_generation.load_model", return_value=(tokenizer, model)):
                    with self.assertRaises(UnusableTitleError):
                        generate_title("Add a task")
