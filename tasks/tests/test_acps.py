from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone
from django.urls import reverse

from ..acp import AcpUnavailable, create_run, mark_endpoint_checked
from ..forms import AcpEndpointForm
from ..models import ACPUsageSnapshot, AcpEndpoint, AgentRun, RepositorySettings, Task


class AcpEndpointTests(TestCase):
    def setUp(self):
        self.first = AcpEndpoint.objects.create(name="First", host="127.0.0.1", port=64001, priority=1)
        self.second = AcpEndpoint.objects.create(name="Second", host="localhost", port=64002, priority=2)

    def test_acp_list_route_remains_available(self):
        response = self.client.get(reverse("acp_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "First")
        self.assertContains(response, "Second")

    def test_acp_usage_summary_groups_currencies_and_limits_period_by_run_creation(self):
        task = self.task()
        runs = []
        for endpoint, label in (
            (self.first, "USD run"), (self.first, "EUR run"),
            (self.first, "Unreported run"), (self.first, "Old run"),
        ):
            runs.append(AgentRun.objects.create(
                task=task, acp_endpoint=endpoint, task_version=task.version,
                request_snapshot=label, plan_snapshot="Plan", status=AgentRun.Status.COMPLETED,
            ))
        now = timezone.now()
        month_start = timezone.localtime(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        AgentRun.objects.filter(pk__in=[run.pk for run in runs[:3]]).update(created_at=now)
        AgentRun.objects.filter(pk=runs[3].pk).update(created_at=month_start - timedelta(seconds=1))
        ACPUsageSnapshot.objects.create(
            run=runs[0], cost_amount=Decimal("0.25"), cost_currency="USD",
            total_tokens=100, input_tokens=80, output_tokens=20,
        )
        ACPUsageSnapshot.objects.create(
            run=runs[1], cost_amount=Decimal("0.40"), cost_currency="EUR",
            total_tokens=50, input_tokens=35, output_tokens=15,
        )
        ACPUsageSnapshot.objects.create(
            run=runs[3], cost_amount=Decimal("99.00"), cost_currency="USD",
        )

        response = self.client.get(reverse("acp_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0.25 USD")
        self.assertContains(response, "0.4 EUR")
        self.assertNotContains(response, "99.00 USD")
        self.assertContains(response, "150 total")
        self.assertContains(response, f'href="{reverse("run_activity", args=[task.pk, runs[0].pk])}"')
        self.assertContains(response, f'href="{reverse("run_activity", args=[task.pk, runs[2].pk])}"')
        self.assertEqual(len(response.context["rows"][0]["usage"]["runs_without_cost"]), 1)

    def task(self, **values):
        return Task.objects.create(title="Task", request="Request", **values)

    def create_run_for_task(self, task, **values):
        return create_run(
            task, stage=Task.Stage.DEVELOPER, task_version=task.version, work_cycle=task.work_cycle,
            request_snapshot="Request", plan_snapshot="Plan", **values,
        )

    def test_auto_selection_uses_priority_then_falls_back_to_next_free(self):
        self.first.priority = 2
        self.first.session_limit = 1
        self.first.save(update_fields=["priority", "session_limit"])
        self.second.priority = 1
        self.second.session_limit = 1
        self.second.save(update_fields=["priority", "session_limit"])

        first_run = self.create_run_for_task(self.task())
        second_run = self.create_run_for_task(self.task())
        self.assertEqual(first_run.acp_endpoint, self.second)
        self.assertEqual(second_run.acp_endpoint, self.first)
        with self.assertRaisesMessage(AcpUnavailable, "No enabled ACP is currently available"):
            self.create_run_for_task(self.task())


    def test_explicit_selection_rejects_disabled_or_busy_without_fallback(self):
        self.first.session_limit = 1
        self.first.save(update_fields=["session_limit"])
        task = self.task(
            acp_selection=Task.AcpSelection.EXPLICIT, selected_acp_endpoint=self.first,
        )
        self.create_run_for_task(task)
        with self.assertRaisesMessage(AcpUnavailable, "selected ACP is busy"):
            self.create_run_for_task(self.task(
                acp_selection=Task.AcpSelection.EXPLICIT, selected_acp_endpoint=self.first,
            ))

        AgentRun.objects.filter(acp_endpoint=self.first).update(status=AgentRun.Status.COMPLETED)
        self.first.enabled = False
        self.first.save(update_fields=["enabled"])
        with self.assertRaisesMessage(AcpUnavailable, "selected ACP is disabled"):
            self.create_run_for_task(task)

    def test_explicit_selection_rejects_known_failure_until_connection_check_succeeds(self):
        self.first.last_error = "Connection refused"
        self.first.save(update_fields=["last_error"])
        task = self.task(
            acp_selection=Task.AcpSelection.EXPLICIT, selected_acp_endpoint=self.first,
        )
        with self.assertRaisesMessage(AcpUnavailable, "unavailable"):
            self.create_run_for_task(task)

        with patch(
            "tasks.views.acps.check_endpoint_connection",
            side_effect=lambda endpoint: mark_endpoint_checked(endpoint.pk),
        ) as check:
            response = self.client.post(reverse("acp_check", args=[self.first.pk]))
        self.assertEqual(response.status_code, 302)
        check.assert_called_once()
        self.first.refresh_from_db()
        self.assertEqual(self.first.last_error, "")
        self.assertIsNotNone(self.first.last_success_at)
        self.assertEqual(self.create_run_for_task(task).acp_endpoint, self.first)

    def test_distinct_endpoints_can_run_concurrently(self):
        first_task = self.task()
        first_run = self.create_run_for_task(first_task)
        second_task = self.task(acp_selection=Task.AcpSelection.EXPLICIT, selected_acp_endpoint=self.second)
        second_run = self.create_run_for_task(second_task)
        self.assertNotEqual(first_run.acp_endpoint_id, second_run.acp_endpoint_id)

    def test_endpoint_form_rejects_non_loopback_and_invalid_ports(self):
        for values in (
            {"name": "Bad host", "host": "192.0.2.1", "port": 64003, "priority": 1, "session_limit": 5, "enabled": True},
            {"name": "Bad port", "host": "127.0.0.1", "port": 65536, "priority": 1, "session_limit": 5, "enabled": True},
        ):
            with self.subTest(values=values):
                self.assertFalse(AcpEndpointForm(values).is_valid())

    def test_task_selection_is_persisted_without_invalidating_approval(self):
        task = self.task(stage=Task.Stage.DEVELOPER, state=Task.State.PLAN_APPROVED)
        response = self.client.post(reverse("task_edit", args=[task.pk]), {
            "version": task.version, "stage_version": task.stage_version,
            "title": task.title, "request": task.request, "plan": task.plan,
            "acp_selection": Task.AcpSelection.EXPLICIT,
            "selected_acp_endpoint": self.second.pk,
        })
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.acp_selection, Task.AcpSelection.EXPLICIT)
        self.assertEqual(task.selected_acp_endpoint, self.second)
        self.assertEqual(task.state, Task.State.PLAN_APPROVED)
        self.assertEqual(task.version, 0)

    def test_reserved_endpoint_cannot_be_deleted(self):
        task = self.task()
        self.create_run_for_task(task)
        self.first.enabled = False
        self.first.save(update_fields=["enabled"])
        self.assertEqual(
            self.client.post(reverse("acp_delete", args=[self.first.pk])).status_code,
            409,
        )

    def test_repository_safe_read_setting_defaults_off_and_can_be_toggled_with_csrf(self):
        repository_settings = RepositorySettings.objects.get(pk=1)
        self.assertFalse(repository_settings.allow_safe_operations)
        url = reverse("set_safe_operations")
        csrf_client = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(url, {"allow_safe_operations": "true"}).status_code, 403)
        self.assertEqual(self.client.post(url, {"allow_safe_operations": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"allow_safe_operations": "true"}).status_code, 302)
        repository_settings.refresh_from_db()
        self.assertTrue(repository_settings.allow_safe_operations)
        self.assertEqual(self.client.post(url, {"allow_safe_operations": "false"}).status_code, 302)
        repository_settings.refresh_from_db()
        self.assertFalse(repository_settings.allow_safe_operations)

    def test_read_allowlist_saves_resolved_files_and_directories_without_enabling_policy(self):
        url = reverse("set_safe_read_paths")
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory).resolve()
            executable = directory / "python"
            executable.write_text("test")
            alias = directory / "python-link"
            alias.symlink_to(executable)
            self.assertEqual(Client(enforce_csrf_checks=True).post(url, {"safe_read_paths": str(directory)}).status_code, 403)
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertEqual(self.client.post(url, {
                "safe_read_paths": f"{directory}\n{alias}\n{executable}",
            }).status_code, 302)
            settings = RepositorySettings.objects.get(pk=1)
            self.assertEqual(settings.safe_read_paths, [
                {"path": str(directory), "kind": "directory"},
                {"path": str(executable), "kind": "file"},
            ])
            self.assertFalse(settings.allow_safe_operations)
            self.assertEqual(self.client.post(url, {"safe_read_paths": ""}).status_code, 302)
            settings.refresh_from_db()
            self.assertEqual(settings.safe_read_paths, [])

    def test_read_allowlist_rejects_invalid_paths_without_replacing_saved_policy(self):
        url = reverse("set_safe_read_paths")
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory).resolve()
            saved = [{"path": str(directory), "kind": "directory"}]
            RepositorySettings.objects.filter(pk=1).update(safe_read_paths=saved)
            broken_link = directory / "broken"
            broken_link.symlink_to(directory / "missing")
            for value in ("relative/path", str(directory / "missing"), str(broken_link)):
                with self.subTest(value=value):
                    page = self.client.post(url, {"safe_read_paths": f"{directory}\n{value}"})
                    self.assertEqual(page.status_code, 400)
                    self.assertTrue(page.context["safe_read_paths_form"].errors)
                    self.assertEqual(RepositorySettings.objects.get(pk=1).safe_read_paths, saved)

    def test_remove_command_prefix_requires_csrf_and_preserves_other_rules(self):
        RepositorySettings.objects.filter(pk=1).update(allowed_command_prefixes=["python manage.py test", "git status"])
        url = reverse("remove_command_prefix")
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(Client(enforce_csrf_checks=True).post(url, {"prefix": "git status"}).status_code, 403)
        self.assertEqual(self.client.post(url, {"prefix": "missing"}).status_code, 409)
        self.assertEqual(self.client.post(url, {"prefix": "git status"}).status_code, 302)
        self.assertEqual(RepositorySettings.objects.get(pk=1).allowed_command_prefixes, ["python manage.py test"])

    def test_csrf_is_required_for_endpoint_mutations(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("acp_create"), {
            "name": "CSRF check", "host": "127.0.0.1", "port": 64004,
            "priority": 3, "enabled": "on",
        })
        self.assertEqual(response.status_code, 403)
