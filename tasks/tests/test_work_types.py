from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import Task, WorkType


class WorkTypeTests(TestCase):
    def setUp(self):
        self.media = self.enterContext(TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=self.media))
        self.client.force_login(get_user_model().objects.create_user(
            username="admin", is_staff=True, is_superuser=True,
        ))
        self.task = Task.objects.create(title="Existing task", request="Request")

    def create_type(self, **values):
        data = {"title": "Spike", "icon": "enabler", "description": "A time-boxed investigation."}
        data.update(values)
        return self.client.post(reverse("admin:tasks_worktype_add"), data)

    def metadata(self, **values):
        self.task.refresh_from_db()
        data = {
            "version": self.task.version, "stage_version": self.task.stage_version,
            "status": self.task.status, "severity": self.task.severity,
            "work_type": self.task.work_type_id, "blocked_reason": self.task.blocked_reason,
        }
        data.update(values)
        return self.client.post(reverse("set_task_metadata", args=[self.task.pk]), data)

    def png(self):
        data = BytesIO()
        Image.new("RGB", (16, 16), "green").save(data, format="PNG")
        return data.getvalue()


    def test_admin_custom_type_can_be_selected_on_creation_and_changed_as_metadata(self):
        self.assertEqual(self.create_type().status_code, 302)
        custom = WorkType.objects.get(title="Spike")
        response = self.client.post(reverse("task_create"), {
            "title": "Investigate", "request": "Investigate the behavior", "plan": "",
            "work_type": custom.pk,
        })
        self.assertEqual(response.status_code, 302)
        created = Task.objects.get(title="Investigate")
        self.assertEqual(created.work_type, custom)
        Task.objects.filter(pk=self.task.pk).update(state=Task.State.PLAN_APPROVED, stage=Task.Stage.DEVELOPER)
        self.assertEqual(self.metadata(work_type=custom.pk).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_type, custom)
        self.assertEqual(self.task.version, 0)
        self.assertEqual(self.task.stage_version, 1)
        self.assertEqual(self.task.state, Task.State.PLAN_APPROVED)

    def test_default_type_survives_renaming_and_omitted_fields_preserve_it(self):
        default = self.task.work_type
        self.assertEqual(default.title, "Task")
        response = self.client.post(reverse("admin:tasks_worktype_change", args=[default.pk]), {
            "title": "General work", "icon": "task", "description": default.description,
        })
        self.assertEqual(response.status_code, 302)
        created = Task.objects.create(title="Automatic task", request="r")
        self.assertEqual(created.work_type_id, default.pk)
        response = self.client.post(reverse("task_create"), {"title": "Human task", "request": "r", "plan": ""})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Task.objects.get(title="Human task").work_type_id, default.pk)
        data = {"version": 0, "stage_version": 0, "status": "ready", "severity": "major"}
        self.assertEqual(self.client.post(reverse("set_task_metadata", args=[self.task.pk]), data).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_type_id, default.pk)

    def test_uploaded_icon_is_validated_and_served_from_private_storage(self):
        png = self.png()
        self.assertEqual(self.create_type(icon_image=SimpleUploadedFile("spike.png", png, content_type="image/png")).status_code, 302)
        custom = WorkType.objects.get(title="Spike")
        self.assertTrue(Path(custom.icon_image.path).is_relative_to(Path(self.media)))
        self.assertEqual(self.metadata(work_type=custom.pk).status_code, 302)
        image = self.client.get(custom.icon_image.url)
        try:
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image["Content-Type"], "image/png")
            self.assertEqual(b"".join(image.streaming_content), png)
        finally:
            image.close()
        (Path(self.media) / "outside.png").write_bytes(png)
        self.assertEqual(self.client.get("/media/work_types/../outside.png").status_code, 400)
        response = self.client.post(reverse("admin:tasks_worktype_change", args=[custom.pk]), {
            "title": custom.title, "icon": "story", "description": custom.description, "icon_image-clear": "on",
        })
        self.assertEqual(response.status_code, 302)
        response = self.create_type(title="Invalid", icon_image=SimpleUploadedFile("invalid.png", b"not an image", content_type="image/png"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WorkType.objects.filter(title="Invalid").exists())

    def test_default_and_assigned_types_cannot_be_deleted_but_unused_types_can(self):
        default = self.task.work_type
        self.task.delete()
        self.assertEqual(self.client.post(reverse("admin:tasks_worktype_delete", args=[default.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:tasks_worktype_changelist"), {
            "action": "delete_selected", "_selected_action": [default.pk], "post": "yes",
        }).status_code, 403)
        self.assertTrue(WorkType.objects.filter(pk=default.pk).exists())
        self.assertEqual(self.create_type().status_code, 302)
        custom = WorkType.objects.get(title="Spike")
        task = Task.objects.create(title="Assigned", request="r", work_type=custom)
        response = self.client.post(reverse("admin:tasks_worktype_delete", args=[custom.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(WorkType.objects.filter(pk=custom.pk).exists())
        task.delete()
        self.assertEqual(self.client.post(reverse("admin:tasks_worktype_delete", args=[custom.pk]), {"post": "yes"}).status_code, 302)
        self.assertFalse(WorkType.objects.filter(pk=custom.pk).exists())

    def test_invalid_or_blank_type_is_rejected_and_stale_metadata_does_not_save(self):
        original = self.task.work_type_id
        for value in ("", "999999"):
            with self.subTest(value=value):
                self.assertEqual(self.metadata(work_type=value).status_code, 400)
                self.task.refresh_from_db()
                self.assertEqual(self.task.work_type_id, original)
        other = WorkType.objects.get(title="Bug")
        self.assertEqual(self.metadata(work_type=other.pk, stage_version=99).status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_type_id, original)
