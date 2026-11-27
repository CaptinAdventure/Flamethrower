import json
import socket
import threading
import time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from ..models import AcpEndpoint, AgentRun, DeferredTask, LinkType, PermissionRequest, RepositorySettings, RunEvent, Task


@override_settings(ACP_PORT="12345")
class AgentViewTests(TransactionTestCase):
    def setUp(self):
        RepositorySettings.objects.get_or_create(pk=1)
        self.endpoint = AcpEndpoint.objects.create(name="Test ACP", port=12345)
        self.task = Task.objects.create(
            title="Change", request="A request", plan="A plan", state=Task.State.PLAN_APPROVED, stage=Task.Stage.DEVELOPER, version=2,
        )

    @patch("tasks.agent_scheduler.wake_scheduler")
    def test_only_approved_current_plan_can_start(self, worker):
        Task.objects.filter(pk=self.task.pk).update(automatic_pause_reason="Resume after reviewing the previous run.")
        url = reverse("start_run", args=[self.task.pk])
        self.assertEqual(self.client.post(url, {"version": 1, "stage_version": 0}).status_code, 409)
        self.assertEqual(self.client.post(url, {"version": 2, "stage_version": 0}).status_code, 302)
        run = AgentRun.objects.get()
        self.assertEqual(run.plan_snapshot, "A plan")
        worker.assert_called_once()
        self.assertEqual(self.client.post(url, {"version": 2, "stage_version": 0}).status_code, 409)
        self.assertEqual(self.client.post(reverse("task_edit", args=[self.task.pk]), {}).status_code, 409)

    def test_permission_requires_exact_advertised_one_time_option(self):
        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
        permission = PermissionRequest.objects.create(
            run=run, tool_call={"title": "Edit"}, options=[
                {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
            ],
        )
        url = reverse("resolve_permission", args=[permission.pk])
        self.assertEqual(self.client.post(url, {"option": "other"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"option": "allow"}).status_code, 302)
        self.assertEqual(self.client.post(url, {"option": "allow"}).status_code, 409)
        permission.refresh_from_db()
        self.assertTrue(permission.resolved)
        self.assertEqual(permission.selected_option, "allow")

    def test_mcp_tool_identity_requires_consistent_unambiguous_metadata(self):
        from ..mcp_permissions import reported_mcp_tool
        from ..views.activity import tool_call_summary

        raw = {"projectPath": "/project", "includeUntracked": True}
        call = {
            "kind": "other", "title": "git_status", "rawInput": raw,
            "originToolCall": {"kind": "other", "title": "CLion-git_status", "rawInput": raw.copy()},
        }
        self.assertEqual(reported_mcp_tool(call), {"server": "CLion", "tool": "git_status"})
        self.assertEqual(tool_call_summary(call)["heading"], "Copilot requests a reported MCP tool")
        for invalid in (
            {**call, "title": "other"},
            {**call, "rawInput": {"projectPath": "/different"}},
            {**call, "kind": None},
            {**call, "originToolCall": {}},
            {**call, "originToolCall": {**call["originToolCall"], "title": "git_status"}},
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(reported_mcp_tool(invalid))

    def test_mcp_tool_and_server_rules_are_scoped_audited_and_removable(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation

        raw = {"projectPath": "/project", "includeUntracked": True}
        call = {
            "kind": "other", "title": "git_status", "rawInput": raw,
            "originToolCall": {"kind": "other", "title": "CLion-git_status", "rawInput": raw.copy()},
        }
        option = {"optionId": "allow", "kind": "allow_once"}
        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
        permission = PermissionRequest.objects.create(run=run, tool_call=call, options=[option, {"optionId": "deny", "kind": "reject_once"}])
        self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {
            "option": "deny", "mcp_scope": "server",
        }).status_code, 400)
        url = reverse("resolve_permission", args=[permission.pk])
        self.assertEqual(Client(enforce_csrf_checks=True).post(url, {"option": "allow", "mcp_scope": "tool"}).status_code, 403)
        self.assertEqual(self.client.post(url, {"option": "allow", "mcp_scope": "wrong"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"option": "allow", "mcp_scope": "tool", "directory": "/"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"option": "allow", "mcp_scope": "tool"}).status_code, 302)
        self.assertEqual(self.client.post(url, {"option": "allow", "mcp_scope": "tool"}).status_code, 409)
        settings = RepositorySettings.objects.get(pk=1)
        tool_rule = {"server": "CLion", "tool": "git_status"}
        self.assertEqual(settings.allowed_mcp_tools, [tool_rule])
        self.assertEqual(json.loads(run.events.get(kind="permission decision").content)["allowedMcpRule"], tool_rule)
        approve = async_to_sync(auto_approve_safe_operation)
        changed_raw = {"projectPath": "/another", "includeIgnored": True}
        changed = {**call, "rawInput": changed_raw, "originToolCall": {**call["originToolCall"], "rawInput": changed_raw}}
        self.assertEqual(approve(run.pk, changed, [option], option), "allow")
        different = {**changed, "title": "execute_tool", "originToolCall": {
            **changed["originToolCall"], "title": "CLion-execute_tool",
        }}
        self.assertIsNone(approve(run.pk, different, [option], option))
        server_rule = {"server": "CLion", "tool": None}
        settings.allowed_mcp_tools.append(server_rule)
        settings.save(update_fields=["allowed_mcp_tools"])
        self.assertEqual(approve(run.pk, different, [option], option), "allow")
        other_server = {**different, "originToolCall": {**different["originToolCall"], "title": "Other-execute_tool"}}
        self.assertIsNone(approve(run.pk, other_server, [option], option))
        remove = reverse("remove_mcp_rule")
        self.assertEqual(Client(enforce_csrf_checks=True).post(remove, {"scope": "server", "server": "CLion"}).status_code, 403)
        self.assertEqual(self.client.post(remove, {"scope": "server", "server": "CLion"}).status_code, 302)
        self.assertIsNone(approve(run.pk, different, [option], option))
        self.assertEqual(self.client.post(remove, {"scope": "tool", "server": "CLion", "tool": "git_status"}).status_code, 302)
        self.assertIsNone(approve(run.pk, changed, [option], option))
        self.assertEqual(self.client.post(remove, {"scope": "server", "server": "CLion"}).status_code, 409)

    def test_mcp_server_rule_can_only_be_saved_for_the_actual_pending_tool(self):
        option = {"optionId": "allow", "kind": "allow_once"}
        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
        call = {"kind": "other", "title": "git_status", "rawInput": {},
                "originToolCall": {"kind": "other", "title": "CLion-git_status", "rawInput": {}}}
        permission = PermissionRequest.objects.create(run=run, tool_call=call, options=[option])
        with patch.object(RunEvent.objects, "create", side_effect=RuntimeError("audit failed")):
            with self.assertRaisesMessage(RuntimeError, "audit failed"):
                self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "allow", "mcp_scope": "server"})
        permission.refresh_from_db()
        self.assertFalse(permission.resolved)
        self.assertEqual(RepositorySettings.objects.get(pk=1).allowed_mcp_tools, [])
        self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {
            "option": "allow", "mcp_scope": "server", "server": "Other",
        }).status_code, 302)
        self.assertEqual(RepositorySettings.objects.get(pk=1).allowed_mcp_tools, [{"server": "CLion", "tool": None}])

    def test_mcp_kind_mismatch_requires_separate_scoped_rule(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation
        from ..mcp_permissions import matching_mcp_rule, reported_mcp_tool

        raw = {"directoryPath": ".", "maxDepth": 3, "projectPath": "/project"}
        call = {
            "kind": "other", "title": "list_directory_tree", "rawInput": raw,
            "originToolCall": {"kind": "read", "title": "CLion-list_directory_tree", "rawInput": raw.copy()},
        }
        self.assertEqual(reported_mcp_tool(call), {"server": "CLion", "tool": "list_directory_tree"})
        old_tool = {"server": "CLion", "tool": "list_directory_tree"}
        old_server = {"server": "CLion", "tool": None}
        self.assertIsNone(matching_mcp_rule(call, [old_tool, old_server]))
        for invalid in (
            {**call, "rawInput": {**raw, "maxDepth": 4}},
            {**call, "originToolCall": {**call["originToolCall"], "kind": ""}},
            {**call, "originToolCall": {**call["originToolCall"], "nonReadKind": ""}},
            {**call, "kind": None},
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(reported_mcp_tool(invalid))
        RepositorySettings.objects.filter(pk=1).update(allowed_mcp_tools=[old_tool, old_server])
        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
        option = {"optionId": "allow", "kind": "allow_once"}
        permission = PermissionRequest.objects.create(run=run, tool_call=call, options=[option])
        approve = async_to_sync(auto_approve_safe_operation)
        self.assertIsNone(approve(run.pk, call, [option], option))
        self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {
            "option": "allow", "mcp_scope": "tool",
        }).status_code, 302)
        mismatch_rule = {
            **old_tool, "kindMismatch": {"origin": "read", "permission": "other"},
        }
        settings = RepositorySettings.objects.get(pk=1)
        self.assertEqual(settings.allowed_mcp_tools, [old_tool, old_server, mismatch_rule])
        self.assertEqual(json.loads(run.events.get(kind="permission decision").content)["allowedMcpRule"], mismatch_rule)
        self.assertEqual(approve(run.pk, call, [option], option), "allow")
        changed_raw = {**raw, "maxDepth": 10}
        changed = {**call, "rawInput": changed_raw, "originToolCall": {
            **call["originToolCall"], "rawInput": changed_raw,
        }}
        self.assertEqual(matching_mcp_rule(changed, [mismatch_rule]), mismatch_rule)
        normal = {**call, "kind": "read"}
        self.assertIsNone(matching_mcp_rule(normal, [mismatch_rule]))
        self.assertEqual(matching_mcp_rule(normal, [old_tool]), old_tool)

        other_tool = {**call, "title": "read_file", "originToolCall": {
            **call["originToolCall"], "title": "CLion-read_file",
        }}
        self.assertIsNone(matching_mcp_rule(other_tool, [mismatch_rule]))
        server_rule = {**old_server, "kindMismatch": mismatch_rule["kindMismatch"]}
        self.assertEqual(matching_mcp_rule(other_tool, [server_rule]), server_rule)
        settings.allowed_mcp_tools.append(server_rule)
        settings.save(update_fields=["allowed_mcp_tools"])
        remove = reverse("remove_mcp_rule")
        self.assertEqual(self.client.post(remove, {
            "scope": "tool", "server": "CLion", "tool": "list_directory_tree", "kind_mismatch": "read:other",
        }).status_code, 302)
        self.assertEqual(self.client.post(remove, {
            "scope": "server", "server": "CLion", "kind_mismatch": "read:other",
        }).status_code, 302)
        settings.refresh_from_db()
        self.assertEqual(settings.allowed_mcp_tools, [old_tool, old_server])
        self.assertEqual(self.client.post(remove, {
            "scope": "server", "server": "CLion", "kind_mismatch": "execute:other:extra",
        }).status_code, 400)

    def test_all_reported_kind_changes_require_a_matching_new_rule(self):
        from ..mcp_permissions import matching_mcp_rule, reported_mcp_tool

        raw = {"q": "onKeyDown"}
        old_rule = {"server": "CLion", "tool": "search_text"}
        for origin, current in (("search", "other"), ("execute", "read"), ("other", "delete"), ("read", "edit")):
            with self.subTest(origin=origin, current=current):
                call = {
                    "kind": current, "title": "search_text", "rawInput": raw,
                    "originToolCall": {"kind": origin, "title": "CLion-search_text", "rawInput": raw},
                }
                rule = {**old_rule, "kindMismatch": {"origin": origin, "permission": current}}
                self.assertEqual(reported_mcp_tool(call), {"server": "CLion", "tool": "search_text"})
                self.assertIsNone(matching_mcp_rule(call, [old_rule]))
                self.assertEqual(matching_mcp_rule(call, [rule]), rule)
                self.assertIsNone(matching_mcp_rule({**call, "kind": origin}, [rule]))

        call = {
            "kind": "read", "title": "search_text", "rawInput": raw,
            "originToolCall": {"kind": "read", "nonReadKind": "execute", "title": "CLion-search_text", "rawInput": raw},
        }
        rule = {**old_rule, "kindMismatch": {"origin": "read", "permission": "read", "intermediate": "execute"}}
        self.assertIsNone(matching_mcp_rule(call, [old_rule]))
        self.assertEqual(matching_mcp_rule(call, [rule]), rule)
        RepositorySettings.objects.filter(pk=1).update(allowed_mcp_tools=[old_rule, rule])
        self.assertEqual(self.client.post(reverse("remove_mcp_rule"), {
            "scope": "tool", "server": "CLion", "tool": "search_text",
            "kind_mismatch": "read:read", "intermediate_kind": "execute",
        }).status_code, 302)
        self.assertEqual(RepositorySettings.objects.get(pk=1).allowed_mcp_tools, [old_rule])

    def test_existing_read_and_command_policies_do_not_bypass_mismatch_rule(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation

        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="running")
        option = {"optionId": "allow", "kind": "allow_once"}
        RepositorySettings.objects.filter(pk=1).update(
            allow_safe_operations=True, allowed_command_prefixes=["git status"],
        )
        target = str(Path(__file__).resolve())
        raw = {"path": target, "command": "git status"}
        call = {
            "title": "inspect", "kind": "read", "rawInput": raw, "locations": [{"path": target}],
            "originToolCall": {"title": "CLion-inspect", "kind": "search", "rawInput": raw},
        }
        approve = async_to_sync(auto_approve_safe_operation)
        with override_settings(PROJECT_PATH=Path(__file__).resolve().parents[2]):
            self.assertIsNone(approve(run.pk, call, [option], option))
            execute = {**call, "kind": "execute"}
            self.assertIsNone(approve(run.pk, execute, [option], option))
            rule = {"server": "CLion", "tool": "inspect", "kindMismatch": {"origin": "search", "permission": "read"}}
            RepositorySettings.objects.filter(pk=1).update(allowed_mcp_tools=[rule])
            self.assertEqual(approve(run.pk, call, [option], option), "allow")
            self.assertEqual(json.loads(run.events.get(kind="permission decision").content)["origin"], "repository_mcp_policy")
            self.assertIsNone(approve(run.pk, execute, [option], option))

    def test_directory_approval_remembers_path_without_enabling_auto_approval(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation

        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory).resolve()
            project = base / "project"
            project.mkdir()
            directory = base / "shared"
            directory.mkdir()
            target = directory / "file"
            target.write_text("test")
            alias = project / "python"
            alias.symlink_to(target)
            run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
            option = {"optionId": "allow", "name": "Allow once", "kind": "allow_once"}
            tool_call = {"kind": "read", "locations": [{"path": "python"}]}
            permission = PermissionRequest.objects.create(run=run, tool_call=tool_call, options=[option])
            url = reverse("resolve_permission", args=[permission.pk])
            values = {"option": "allow", "directory": str(directory)}
            with override_settings(PROJECT_PATH=project):
                self.assertEqual(Client(enforce_csrf_checks=True).post(url, values).status_code, 403)
                self.assertEqual(self.client.post(url, values).status_code, 302)
                permission.refresh_from_db()
                self.assertTrue(permission.resolved)
                self.assertEqual(permission.selected_option, "allow")
                config = RepositorySettings.objects.get(pk=1)
                self.assertEqual(config.safe_read_paths, [{"path": str(directory), "kind": "directory"}])
                self.assertFalse(config.allow_safe_operations)
                event = json.loads(run.events.get(kind="permission decision").content)
                self.assertEqual((event["origin"], event["allowedDirectory"]), ("manual_directory_allowlist", str(directory)))
                approve = async_to_sync(auto_approve_safe_operation)
                self.assertIsNone(approve(run.pk, tool_call, [option], option))
                RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=True)
                self.assertEqual(approve(run.pk, tool_call, [option], option), "allow")
                self.assertEqual(self.client.post(url, values).status_code, 409)

    def test_directory_approval_rejects_tampering_and_ineligible_requests(self):
        from ..agent import protected_read_directories

        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory).resolve()
            project = base / "project"
            project.mkdir()
            directory = base / "shared"
            directory.mkdir()
            target = directory / "file"
            target.write_text("test")
            alias = project / "alias"
            alias.symlink_to(target)
            run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
            options = [
                {"optionId": "allow", "kind": "allow_once"},
                {"optionId": "deny", "kind": "reject_once"},
            ]
            tool_call = {"kind": "search", "locations": [{"path": "alias"}]}
            permission = PermissionRequest.objects.create(run=run, tool_call=tool_call, options=options)
            url = reverse("resolve_permission", args=[permission.pk])
            with override_settings(PROJECT_PATH=project):
                self.assertEqual(protected_read_directories(tool_call), [str(directory)])
                for values in (
                    {"option": "allow", "directory": str(base)},
                    {"option": "allow", "directory": str(project)},
                    {"option": "deny", "directory": str(directory)},
                ):
                    with self.subTest(values=values):
                        self.assertIn(self.client.post(url, values).status_code, (400, 409))
                permission.tool_call = {**tool_call, "kind": "execute"}
                permission.save(update_fields=["tool_call"])
                self.assertEqual(self.client.post(url, {"option": "allow", "directory": str(directory)}).status_code, 409)
                permission.tool_call = {**tool_call, "locations": [{"path": str(alias)}, {"path": "relative"}]}
                permission.save(update_fields=["tool_call"])
                self.assertEqual(self.client.post(url, {"option": "allow", "directory": str(directory)}).status_code, 409)
                permission.tool_call = tool_call
                permission.save(update_fields=["tool_call"])
                other = base / "other"
                other.mkdir()
                other_target = other / "file"
                other_target.write_text("test")
                alias.unlink()
                alias.symlink_to(other_target)
                self.assertEqual(self.client.post(url, {"option": "allow", "directory": str(directory)}).status_code, 409)
                self.assertEqual(protected_read_directories(tool_call), [str(other)])
                alias.unlink()
                alias.symlink_to(target)
                run.cancel_requested = True
                run.save(update_fields=["cancel_requested"])
                self.assertEqual(self.client.post(url, {"option": "allow", "directory": str(directory)}).status_code, 409)
                permission.refresh_from_db()
                self.assertFalse(permission.resolved)
                self.assertEqual(RepositorySettings.objects.get(pk=1).safe_read_paths, [])
                self.assertFalse(run.events.exists())

    def test_directory_approval_does_not_cover_other_reported_locations(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation, protected_read_directories

        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory).resolve()
            project = base / "project"
            first = base / "first"
            second = base / "second"
            for directory in (project, first, second):
                directory.mkdir()
            tool_call = {"kind": "search", "locations": [{"path": str(first)}, {"path": str(second)}]}
            option = {"optionId": "allow", "kind": "allow_once"}
            run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
            permission = PermissionRequest.objects.create(run=run, tool_call=tool_call, options=[option])
            RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=True)
            with override_settings(PROJECT_PATH=project):
                self.assertEqual(protected_read_directories(tool_call), [str(first), str(second)])
                self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {
                    "option": "allow", "directory": str(first),
                }).status_code, 302)
                config = RepositorySettings.objects.get(pk=1)
                self.assertTrue(config.allow_safe_operations)
                self.assertEqual(protected_read_directories(tool_call, config.safe_read_paths), [str(second)])
                approve = async_to_sync(auto_approve_safe_operation)
                self.assertIsNone(approve(run.pk, tool_call, [option], option))
                config.safe_read_paths.append({"path": str(second), "kind": "directory"})
                config.save(update_fields=["safe_read_paths"])
                self.assertEqual(approve(run.pk, tool_call, [option], option), "allow")

    def test_composite_paths_require_all_locations_and_support_directory_approval(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation, protected_read_directories

        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory).resolve()
            project, first, second = base / "project", base / "first", base / "second"
            for directory in (project, first, second):
                directory.mkdir()
            (project / "python").write_text("test")
            tool_call = {"kind": "read", "locations": [{"path": f"{first}, {second}, python"}]}
            option = {"optionId": "allow", "kind": "allow_once"}
            run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
            permission = PermissionRequest.objects.create(run=run, tool_call=tool_call, options=[option])
            RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=True)
            with override_settings(PROJECT_PATH=project):
                self.assertEqual(protected_read_directories(tool_call), [str(first), str(second)])
                self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {
                    "option": "allow", "directory": str(first),
                }).status_code, 302)
                config = RepositorySettings.objects.get(pk=1)
                self.assertEqual(protected_read_directories(tool_call, config.safe_read_paths), [str(second)])
                approve = async_to_sync(auto_approve_safe_operation)
                self.assertIsNone(approve(run.pk, tool_call, [option], option))
                config.safe_read_paths.append({"path": str(second), "kind": "directory"})
                config.save(update_fields=["safe_read_paths"])
                self.assertEqual(approve(run.pk, tool_call, [option], option), "allow")

    def test_composite_paths_reject_ambiguous_missing_and_escaping_locations(self):
        from ..agent import _safe_read_locations, protected_read_directories

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            for name in ("one", "two", "three", "one, two"):
                (root / name).mkdir()
            with override_settings(PROJECT_PATH=root):
                for path in ("one, two, three", "one, missing", "one, ../outside", "one,  two"):
                    with self.subTest(path=path):
                        call = {"kind": "read", "locations": [{"path": path}]}
                        self.assertFalse(_safe_read_locations(call))
                        self.assertEqual(protected_read_directories(call), [])
                self.assertTrue(_safe_read_locations({"locations": [{"path": "one, two"}]}))

    def test_directory_approval_rolls_back_if_audit_fails(self):
        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory).resolve()
            project = base / "project"
            project.mkdir()
            directory = base / "shared"
            directory.mkdir()
            run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
            permission = PermissionRequest.objects.create(
                run=run, tool_call={"kind": "read", "locations": [{"path": str(directory)}]},
                options=[{"optionId": "allow", "kind": "allow_once"}],
            )
            with override_settings(PROJECT_PATH=project):
                with patch.object(RunEvent.objects, "create", side_effect=RuntimeError("audit failed")):
                    with self.assertRaisesMessage(RuntimeError, "audit failed"):
                        self.client.post(reverse("resolve_permission", args=[permission.pk]), {
                            "option": "allow", "directory": str(directory),
                        })
            permission.refresh_from_db()
            self.assertFalse(permission.resolved)
            self.assertEqual(RepositorySettings.objects.get(pk=1).safe_read_paths, [])

    def test_command_prefix_approval_saves_and_automatically_matches_other_requests(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation

        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
        option = {"optionId": "allow", "kind": "allow_once"}
        tool_call = {"kind": "execute", "rawInput": {
            "command": "python manage.py test tasks.tests.test_tasks.TaskSearchTests --verbosity 1", "commands": ["python"],
        }}
        permission = PermissionRequest.objects.create(run=run, tool_call=tool_call, options=[option])
        url = reverse("resolve_permission", args=[permission.pk])
        values = {"option": "allow", "command_prefix": "python manage.py test"}
        self.assertEqual(Client(enforce_csrf_checks=True).post(url, values).status_code, 403)
        self.assertEqual(self.client.post(url, values).status_code, 302)
        permission.refresh_from_db()
        self.assertTrue(permission.resolved)
        settings = RepositorySettings.objects.get(pk=1)
        self.assertEqual(settings.allowed_command_prefixes, ["python manage.py test"])
        self.assertFalse(settings.allow_safe_operations)
        event = json.loads(run.events.get(kind="permission decision").content)
        self.assertEqual((event["origin"], event["allowedCommandPrefix"]), ("manual_command_allowlist", "python manage.py test"))
        other = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="running")
        approve = async_to_sync(auto_approve_safe_operation)
        matching = {"kind": "execute", "rawInput": {"command": "python manage.py test other.tests"}}
        self.assertEqual(approve(other.pk, matching, [option], option), "allow")
        event = json.loads(other.events.get(kind="permission decision").content)
        self.assertEqual((event["origin"], event["allowedCommandPrefix"]), ("repository_command_policy", "python manage.py test"))
        self.assertEqual(self.client.post(url, values).status_code, 409)

    def test_command_prefix_requires_matching_simple_pending_execute_request(self):
        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
        options = [{"optionId": "allow", "kind": "allow_once"}, {"optionId": "deny", "kind": "reject_once"}]
        tool_call = {"kind": "execute", "rawInput": {"command": "python manage.py test app.tests", "commands": ["python"]}}
        permission = PermissionRequest.objects.create(run=run, tool_call=tool_call, options=options)
        url = reverse("resolve_permission", args=[permission.pk])
        for values in (
            {"option": "deny", "command_prefix": "python"},
            {"option": "allow", "command_prefix": "python manage.py tes"},
            {"option": "allow", "command_prefix": "python; rm"},
            {"option": "allow", "command_prefix": ""},
            {"option": "allow", "command_prefix": "python", "directory": "/tmp"},
        ):
            with self.subTest(values=values):
                self.assertEqual(self.client.post(url, values).status_code, 400)
        for command in (
            "python manage.py test app.tests; touch /tmp/x",
            "python manage.py test $(whoami)",
            "python manage.py test app.tests\nwhoami",
            'python manage.py test "app.tests"',
        ):
            with self.subTest(command=command):
                permission.tool_call = {"kind": "execute", "rawInput": {"command": command}}
                permission.save(update_fields=["tool_call"])
                self.assertEqual(self.client.post(url, {"option": "allow", "command_prefix": "python"}).status_code, 400)
        permission.tool_call = tool_call
        permission.save(update_fields=["tool_call"])
        run.cancel_requested = True
        run.save(update_fields=["cancel_requested"])
        self.assertEqual(self.client.post(url, {"option": "allow", "command_prefix": "python"}).status_code, 409)
        permission.refresh_from_db()
        self.assertFalse(permission.resolved)
        self.assertEqual(RepositorySettings.objects.get(pk=1).allowed_command_prefixes, [])
        self.assertFalse(run.events.exists())

    def test_command_prefix_approval_rolls_back_on_audit_failure(self):
        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
        permission = PermissionRequest.objects.create(
            run=run, tool_call={"kind": "execute", "rawInput": {"command": "python manage.py test"}},
            options=[{"optionId": "allow", "kind": "allow_once"}],
        )
        with patch.object(RunEvent.objects, "create", side_effect=RuntimeError("audit failed")):
            with self.assertRaisesMessage(RuntimeError, "audit failed"):
                self.client.post(reverse("resolve_permission", args=[permission.pk]), {
                    "option": "allow", "command_prefix": "python manage.py test",
                })
        permission.refresh_from_db()
        self.assertFalse(permission.resolved)
        self.assertEqual(RepositorySettings.objects.get(pk=1).allowed_command_prefixes, [])

    def test_command_prefix_matching_rejects_nonmatching_or_ambiguous_commands(self):
        from ..command_permissions import matching_command_prefixes

        prefix = ["python manage.py test"]
        for tool_call in (
            {"kind": "read", "rawInput": {"command": "python manage.py test"}},
            {"kind": "execute", "rawInput": {"command": "python manage.py testing"}},
            {"kind": "execute", "rawInput": {"command": "python manage.py test; echo bad"}},
            {"kind": "execute", "rawInput": {"command": "python manage.py test > output"}},
            {"kind": "execute", "rawInput": {"command": "python manage.py test %VARIABLE%"}},
            {"kind": "execute", "rawInput": {"command": "python manage.py test", "commands": ["python", "other"]}},
            {"kind": "execute", "rawInput": {"command": "python manage.py test", "commands": "python"}},
            {"kind": "execute", "rawInput": None},
        ):
            with self.subTest(tool_call=tool_call):
                self.assertIsNone(matching_command_prefixes(tool_call, prefix))

    def test_compound_command_requires_every_part_to_match_a_prefix(self):
        from ..command_permissions import matching_command_prefixes

        prefixes = ["python -m py_compile", "git diff --check"]
        for command in (
            "python -m py_compile tasks/agent.py && git diff --check",
            "python -m py_compile tasks/agent.py || git diff --check",
            "python -m py_compile tasks/agent.py&&git diff --check || python -m py_compile tasks/tests/test_tasks.py",
        ):
            tool_call = {"kind": "execute", "rawInput": {"command": command, "commands": ["python"]}}
            with self.subTest(command=command):
                matches = matching_command_prefixes(tool_call, prefixes)
                self.assertIsNotNone(matches)
                self.assertEqual(len(matches), 3 if command.count("python") == 2 else 2)
                self.assertIsNone(matching_command_prefixes(tool_call, prefixes[:1]))
        for command in (
            "python -m py_compile a.py && rm file",
            "python -m py_compile a.py | git diff --check",
            "python -m py_compile a.py && git diff --check; rm file",
            "python -m py_compile a.py && git diff --check > file",
            "python -m py_compile a.py && git diff --check $(touch file)",
            "python -m py_compile =python && git diff --check",
            "python -m py_compile a.py && && git diff --check",
            "python -m py_compile a.py ||",
            "python -m py_compile a.py\n&& git diff --check",
        ):
            with self.subTest(command=command):
                self.assertIsNone(matching_command_prefixes({"kind": "execute", "rawInput": {"command": command}}, prefixes))
        inconsistent = {"kind": "execute", "rawInput": {
            "command": "python -m py_compile a.py && git diff --check", "commands": ["python", "rm"],
        }}
        self.assertIsNone(matching_command_prefixes(inconsistent, prefixes))

    def test_compound_permission_saves_selected_matching_prefixes(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation

        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="waiting")
        option = {"optionId": "allow", "kind": "allow_once"}
        tool_call = {"kind": "execute", "rawInput": {
            "command": "python -m py_compile tasks/agent.py && git diff --check", "commands": ["python"],
        }}
        permission = PermissionRequest.objects.create(run=run, tool_call=tool_call, options=[option])
        url = reverse("resolve_permission", args=[permission.pk])
        for prefixes in (["python -m py_compile"], ["git diff", "python"], ["", ""]):
            with self.subTest(prefixes=prefixes):
                self.assertEqual(self.client.post(url, {"option": "allow", "command_prefix": prefixes}).status_code, 400)
        self.assertEqual(self.client.post(url, {
            "option": "allow", "command_prefix": ["python -m py_compile", ""],
        }).status_code, 302)
        self.assertEqual(RepositorySettings.objects.get(pk=1).allowed_command_prefixes, ["python -m py_compile"])
        approve = async_to_sync(auto_approve_safe_operation)
        self.assertIsNone(approve(run.pk, tool_call, [option], option))
        second = PermissionRequest.objects.create(run=run, tool_call=tool_call, options=[option])
        self.assertEqual(self.client.post(reverse("resolve_permission", args=[second.pk]), {
            "option": "allow", "command_prefix": ["", "git diff --check"],
        }).status_code, 302)
        self.assertEqual(approve(run.pk, tool_call, [option], option), "allow")
        event = json.loads(run.events.filter(kind="permission decision").last().content)
        self.assertEqual(event["allowedCommandPrefixes"], ["python -m py_compile", "git diff --check"])

    def test_pending_permission_is_resolvable_even_if_run_says_running(self):
        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="running")
        permission = PermissionRequest.objects.create(
            run=run, tool_call={"title": "Pending command", "kind": "execute"},
            options=[{"optionId": "allow", "name": "Allow once", "kind": "allow_once"}],
        )
        self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "allow"}).status_code, 302)
        permission.refresh_from_db()
        self.assertTrue(permission.resolved)

    def test_concurrent_permissions_keep_run_waiting_until_all_are_resolved(self):
        from asgiref.sync import async_to_sync
        from ..agent import create_permission, resume_after_permission

        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="running")
        options = [{"optionId": "allow", "name": "Allow once", "kind": "allow_once"}]
        first = async_to_sync(create_permission)(run.pk, {"title": "First"}, options)
        second = async_to_sync(create_permission)(run.pk, {"title": "Second"}, options)
        self.assertEqual(self.client.post(reverse("resolve_permission", args=[first]), {"option": "allow"}).status_code, 302)
        async_to_sync(resume_after_permission)(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.WAITING)
        self.assertEqual(self.client.post(reverse("resolve_permission", args=[second]), {"option": "allow"}).status_code, 302)
        async_to_sync(resume_after_permission)(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.RUNNING)

    def test_safe_read_locations_resolve_existing_paths_inside_project(self):
        from ..agent import _safe_read_locations

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project"
            root.mkdir()
            inside = root / "file.txt"
            inside.write_text("test")
            outside = Path(temporary_directory) / "outside.txt"
            outside.write_text("test")
            escaping_link = root / "escape"
            escaping_link.symlink_to(outside)
            with override_settings(PROJECT_PATH=root):
                self.assertTrue(_safe_read_locations({"locations": [{"path": str(inside)}]}))
                self.assertTrue(_safe_read_locations({"locations": [{"path": "file.txt"}]}))
                for locations in (
                    None,
                    [],
                    [{"path": "../outside.txt"}],
                    [{"path": str(root / "missing")}],
                    [{"path": str(outside)}],
                    [{"path": str(escaping_link)}],
                    [{"path": None}],
                    ["malformed"],
                    [{"path": str(inside)}, {"path": str(outside)}],
                ):
                    with self.subTest(locations=locations):
                        self.assertFalse(_safe_read_locations({"locations": locations}))

    def test_automatic_permission_is_atomic_and_does_not_change_run_or_pause_reason(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation

        run = AgentRun.objects.create(
            task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="running",
        )
        RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=True)
        option = {"optionId": "allow-exactly-this", "name": "Allow once", "kind": "allow_once"}
        options = [option, {"optionId": "reject", "name": "Reject once", "kind": "reject_once"}]
        tool_call = {"kind": "read", "locations": [{"path": str(Path(__file__).resolve())}]}

        with patch("tasks.agent.RunEvent.objects.create", side_effect=RuntimeError("audit failed")):
            with self.assertRaisesMessage(RuntimeError, "audit failed"):
                async_to_sync(auto_approve_safe_operation)(run.pk, tool_call, options, option)
        self.assertFalse(PermissionRequest.objects.exists())
        run.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.RUNNING)
        self.assertEqual(self.task.automatic_pause_reason, "")

    def test_read_allowlist_covers_directory_descendants_and_exact_files_not_symlink_escapes(self):
        from ..agent import _safe_read_locations

        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory).resolve()
            root = base / "project"
            root.mkdir()
            directory = base / "allowed"
            directory.mkdir()
            nested = directory / "nested"
            nested.mkdir()
            descendant = nested / "file"
            descendant.write_text("test")
            executable = base / "python"
            executable.write_text("test")
            sibling = base / "python-other"
            sibling.write_text("test")
            alias = root / "python"
            alias.symlink_to(executable)
            escape = directory / "escape"
            escape.symlink_to(sibling)
            allowed = [
                {"path": str(directory), "kind": "directory"},
                {"path": str(executable), "kind": "file"},
            ]
            with override_settings(PROJECT_PATH=root):
                for path in (directory, descendant, executable, alias, "python"):
                    self.assertTrue(_safe_read_locations({"locations": [{"path": str(path)}]}, allowed))
                for locations in (
                    [{"path": str(sibling)}],
                    [{"path": str(escape)}],
                    [{"path": str(descendant)}, {"path": str(sibling)}],
                    [{"path": str(directory / "missing")}],
                    [{"path": "../python"}],
                ):
                    with self.subTest(locations=locations):
                        self.assertFalse(_safe_read_locations({"locations": locations}, allowed))
                self.assertFalse(_safe_read_locations({"locations": [{"path": str(alias)}]}))
                alias.unlink()
                alias.symlink_to(sibling)
                self.assertFalse(_safe_read_locations({"locations": [{"path": str(alias)}]}, allowed))
                executable.unlink()
                executable.mkdir()
                self.assertFalse(_safe_read_locations({"locations": [{"path": str(executable)}]}, allowed))

    def test_auto_approval_checks_the_current_allowlist_and_toggle_before_auditing(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation

        run = AgentRun.objects.create(
            task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="running",
        )
        option = {"optionId": "allow", "name": "Allow once", "kind": "allow_once"}
        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory).resolve()
            root = base / "project"
            root.mkdir()
            external = base / "python"
            external.write_text("test")
            tool_call = {"kind": "read", "locations": [{"path": str(external)}]}
            allowed = [{"path": str(external), "kind": "file"}]
            approve = async_to_sync(auto_approve_safe_operation)
            with override_settings(PROJECT_PATH=root):
                RepositorySettings.objects.filter(pk=1).update(safe_read_paths=allowed)
                self.assertIsNone(approve(run.pk, tool_call, [option], option))
                RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=True)
                for kind in ("execute", "edit", "delete", "move", "fetch"):
                    with self.subTest(kind=kind):
                        self.assertIsNone(approve(run.pk, {**tool_call, "kind": kind}, [option], option))
                self.assertFalse(PermissionRequest.objects.exists())
                self.assertEqual(approve(run.pk, tool_call, [option], option), "allow")
                self.assertTrue(PermissionRequest.objects.get().resolved)
                self.assertTrue(json.loads(run.events.get(kind="permission decision").content)["automatic"])
                RepositorySettings.objects.filter(pk=1).update(safe_read_paths=[])
                self.assertIsNone(approve(run.pk, tool_call, [option], option))
                self.assertEqual(PermissionRequest.objects.count(), 1)

    def test_auto_read_rejects_unknown_or_changed_tool_origin(self):
        from asgiref.sync import async_to_sync
        from ..agent import auto_approve_safe_operation

        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="running")
        option = {"optionId": "allow", "kind": "allow_once"}
        RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=True)
        call = {"kind": "read", "locations": [{"path": str(Path(__file__).resolve())}]}
        approve = async_to_sync(auto_approve_safe_operation)
        with override_settings(PROJECT_PATH=Path(__file__).resolve().parents[2]):
            for origin in ({}, {"kind": "execute"}, {"kind": "read", "nonReadKind": "execute"}):
                with self.subTest(origin=origin):
                    self.assertIsNone(approve(run.pk, {**call, "originToolCall": origin}, [option], option))
            self.assertEqual(approve(run.pk, {**call, "originToolCall": {"kind": "read"}}, [option], option), "allow")

    def test_tool_updates_cannot_erase_a_non_read_origin(self):
        from asgiref.sync import async_to_sync
        from ..agent import RunClient

        client = RunClient(1)
        with patch("tasks.agent.save_event", new_callable=AsyncMock):
            for kind, update_kind in (("read", "tool_call"), ("execute", "tool_call_update"), ("read", "tool_call_update")):
                update = Mock()
                update.model_dump.return_value = {"sessionUpdate": update_kind, "toolCallId": "id", "kind": kind}
                async_to_sync(client.session_update)("session", update)
        self.assertEqual(client.tool_origins["id"], {"kind": "read", "nonReadKind": "execute"})

    def test_pending_permission_blocks_continuation_even_if_run_says_running(self):
        run = AgentRun.objects.create(task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="running")
        PermissionRequest.objects.create(run=run, tool_call={"title": "Pending"}, options=[])
        response = self.client.post(reverse("continue_task", args=[self.task.pk]), {
            "version": 2, "stage_version": 0,
        })
        self.assertEqual(response.status_code, 409)

    def test_interrupted_run_blocks_new_work_until_acknowledged(self):
        run = AgentRun.objects.create(
            task=self.task, acp_endpoint=self.endpoint, task_version=2,
            request_snapshot="r", plan_snapshot="p", status="interrupted",
        )
        url = reverse("start_run", args=[self.task.pk])
        self.assertEqual(self.client.post(url, {"version": 2, "stage_version": 0}).status_code, 409)
        self.assertEqual(self.client.post(reverse("acknowledge_run", args=[run.pk])).status_code, 302)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.ACKNOWLEDGED)

class FakeACPServer:
    def __init__(
        self, session_id="test-session", message=None, permission_kind="edit",
        locations=None, permission_options=None, raw_input=None, usage_updates=None, prompt_usage=None, permission_call=None,
        tool_title="Write test", permission_title="Write test",
    ):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.socket.settimeout(10)
        self.port = self.socket.getsockname()[1]
        self.error = None
        self.permission_response = None
        self.session_id = session_id
        self.message = message
        self.permission_kind = permission_kind
        self.locations = locations
        self.permission_options = permission_options
        self.raw_input = raw_input
        self.usage_updates = usage_updates or []
        self.prompt_usage = prompt_usage
        self.permission_call = permission_call or {}
        self.tool_title = tool_title
        self.permission_title = permission_title
        self.prompt = None
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def start(self):
        self.thread.start()

    def serve(self):
        try:
            conn, _ = self.socket.accept()
            with conn, conn.makefile("rwb") as stream:
                def read():
                    return json.loads(stream.readline())

                def send(data):
                    stream.write((json.dumps(data) + "\n").encode())
                    stream.flush()

                request = read()
                send({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1}})
                request = read()
                send({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": self.session_id}})
                request = read()
                self.prompt = request["params"]["prompt"][0]["text"]
                prompt_id = request["id"]
                for usage_update in self.usage_updates:
                    send({
                        "jsonrpc": "2.0", "method": "session/update",
                        "params": {"sessionId": self.session_id, "update": {
                            "sessionUpdate": "usage_update", **usage_update,
                        }},
                    })
                tool_update = {
                    "sessionUpdate": "tool_call", "toolCallId": "tool-1", "title": self.tool_title,
                    "kind": self.permission_kind, "rawInput": self.raw_input if self.raw_input is not None else {"path": "example.txt", "content": "test"},
                }
                if self.locations is not None:
                    tool_update["locations"] = self.locations
                send({
                    "jsonrpc": "2.0", "method": "session/update",
                    "params": {"sessionId": self.session_id, "update": tool_update},
                })
                send({
                    "jsonrpc": "2.0", "id": 50, "method": "session/request_permission",
                    "params": {
                        "sessionId": self.session_id, "toolCall": {"toolCallId": "tool-1", "title": self.permission_title, **self.permission_call},
                        "options": self.permission_options if self.permission_options is not None else [
                            {"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                            {"optionId": "no", "name": "Reject once", "kind": "reject_once"},
                        ],
                    },
                })
                self.permission_response = read()
                send({
                    "jsonrpc": "2.0", "method": "session/update",
                    "params": {"sessionId": self.session_id, "update": {
                        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": self.message if self.message is not None else "Finished."},
                    }},
                })
                if self.message is None:
                    send({
                        "jsonrpc": "2.0", "method": "session/update",
                        "params": {"sessionId": self.session_id, "update": {
                            "sessionUpdate": "agent_message_chunk", "content": {
                                "type": "text",
                                "text": "\nFLAMETHROWER_GATE_RECOMMENDATION\n" + json.dumps({
                                    "result": "unverified", "recommendation": "hold", "summary": "Finished",
                                    "evidence": "No checks", "untested": "All", "reason": "Human review required",
                                }) + "\nEND_FLAMETHROWER_GATE_RECOMMENDATION",
                            },
                        }},
                    })
                response = {"stopReason": "end_turn"}
                if self.prompt_usage is not None:
                    response["usage"] = self.prompt_usage
                send({"jsonrpc": "2.0", "id": prompt_id, "result": response})
        except Exception as error:
            self.error = error
        finally:
            self.socket.close()


class AgentIntegrationTests(TransactionTestCase):
    def setUp(self):
        RepositorySettings.objects.get_or_create(pk=1)
        self.endpoint = AcpEndpoint.objects.create(name="Integration ACP", port=12345)

    def set_endpoint_port(self, port):
        AcpEndpoint.objects.filter(pk=self.endpoint.pk).update(port=port)

    def start_automatic_run(self, task):
        from ..automation import continue_automatic_task
        from ..agent_scheduler import start_scheduler, wake_scheduler

        if not hasattr(self, "scheduler_stop"):
            self.scheduler_stop, thread = start_scheduler()
            def stop_scheduler():
                self.scheduler_stop.set()
                wake_scheduler()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            self.addCleanup(stop_scheduler)
        continue_automatic_task(task.pk)

    def wait_for_automatic_pause(self, task):
        self.wait_for(lambda: Task.objects.get(pk=task.pk).automatic_pause_reason != "")

    def wait_for_workers(self, task):
        run_ids = set(task.runs.values_list("id", flat=True))
        for worker in threading.enumerate():
            if worker.name.startswith("agent-run-") and worker.name.removeprefix("agent-run-").isdigit():
                if int(worker.name.removeprefix("agent-run-")) in run_ids:
                    worker.join(timeout=10)
                    self.assertFalse(worker.is_alive())

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for agent state")

    def test_mcp_search_to_other_permission_uses_explicit_mismatch_rule(self):
        RepositorySettings.objects.filter(pk=1).update(
            allowed_mcp_tools=[{"server": "CLion", "tool": None}],
        )
        raw = {"q": "onKeyDown", "paths": ["**/*.js"], "projectPath": "/project"}
        server = FakeACPServer(
            permission_kind="search", tool_title="CLion-search_text",
            permission_title="search_text", raw_input=raw,
            permission_call={"kind": "other", "rawInput": raw},
        )
        server.start()
        self.set_endpoint_port(server.port)
        task = Task.objects.create(
            title="List", request="A request", plan="A plan",
            state="plan_approved", stage="testing", version=2,
        )
        with override_settings(ACP_PORT=str(server.port)):
            self.start_automatic_run(task)
            self.wait_for(lambda: PermissionRequest.objects.filter(run__task=task).exists())
            permission = PermissionRequest.objects.get(run__task=task)
            self.assertFalse(permission.resolved)
            self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {
                "option": "yes", "mcp_scope": "server",
            }).status_code, 302)
            self.wait_for(lambda: AgentRun.objects.filter(task=task, status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_automatic_pause(task)
            self.wait_for_workers(task)
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertEqual(server.permission_response["result"]["outcome"]["optionId"], "yes")
        self.assertIn({
            "server": "CLion", "tool": None, "kindMismatch": {"origin": "search", "permission": "other"},
        }, RepositorySettings.objects.get(pk=1).allowed_mcp_tools)

    def test_saved_mcp_rule_auto_approves_matching_acp_tool_request(self):
        RepositorySettings.objects.filter(pk=1).update(
            allowed_mcp_tools=[{"server": "CLion", "tool": "git_status"}],
        )
        server = FakeACPServer(
            permission_kind="other", tool_title="CLion-git_status", permission_title="git_status",
            raw_input={"projectPath": "/project", "includeUntracked": True},
        )
        server.start()
        self.set_endpoint_port(server.port)
        task = Task.objects.create(
            title="Status", request="A request", plan="A plan",
            state="plan_approved", stage="testing", version=2,
        )
        with override_settings(ACP_PORT=str(server.port)):
            self.start_automatic_run(task)
            self.wait_for(lambda: AgentRun.objects.filter(task=task, status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_automatic_pause(task)
            self.wait_for_workers(task)
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertEqual(server.permission_response["result"]["outcome"]["optionId"], "yes")
        permission = PermissionRequest.objects.get(run__task=task)
        self.assertTrue(permission.resolved)
        decision = json.loads(permission.run.events.get(kind="permission decision").content)
        self.assertEqual((decision["origin"], decision["allowedMcpRule"]), (
            "repository_mcp_policy", {"server": "CLion", "tool": "git_status"},
        ))

    def test_saved_command_prefix_auto_approves_acp_execute_request(self):
        RepositorySettings.objects.filter(pk=1).update(allowed_command_prefixes=["python manage.py test", "git diff --check"])
        server = FakeACPServer(
            permission_kind="execute",
            raw_input={"command": "python manage.py test tasks.tests.test_acps --verbosity 1 && git diff --check", "commands": ["python"]},
        )
        server.start()
        self.set_endpoint_port(server.port)
        task = Task.objects.create(
            title="Run tests", request="A request", plan="A plan",
            state="plan_approved", stage="testing", version=2,
        )
        with override_settings(ACP_PORT=str(server.port)):
            self.start_automatic_run(task)
            self.wait_for(lambda: AgentRun.objects.filter(task=task, status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_automatic_pause(task)
            self.wait_for_workers(task)
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertEqual(server.permission_response["result"]["outcome"]["optionId"], "yes")
        permission = PermissionRequest.objects.get(run__task=task)
        self.assertTrue(permission.resolved)
        self.assertEqual(json.loads(permission.run.events.get(kind="permission decision").content)["origin"], "repository_command_policy")

    def test_completed_agent_report_creates_deferred_draft(self):
        LinkType.objects.get_or_create(
            name="Relates", defaults={"outward_description": "relates to", "inward_description": "relates to"},
        )
        data = {"tasks": [{"title": "Follow-up", "request": "Address the separate issue later."}]}
        message = "FLAMETHROWER_DEFERRED_TASKS\n" + json.dumps(data) + "\nEND_FLAMETHROWER_DEFERRED_TASKS"
        message += "\nFLAMETHROWER_GATE_RECOMMENDATION\n" + json.dumps({
            "result": "unverified", "recommendation": "hold", "summary": "Finished",
            "evidence": "No checks", "untested": "All", "reason": "Human review required",
        }) + "\nEND_FLAMETHROWER_GATE_RECOMMENDATION"
        server = FakeACPServer(message=message)
        server.start()
        self.set_endpoint_port(server.port)
        task = Task.objects.create(title="Change", request="A request", plan="A plan", state="plan_approved", stage="testing", version=2)
        with override_settings(ACP_PORT=str(server.port)):
            self.start_automatic_run(task)
            self.wait_for(lambda: PermissionRequest.objects.exists())
            permission = PermissionRequest.objects.get()
            self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "yes"})
            self.wait_for(lambda: AgentRun.objects.filter(status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_automatic_pause(task)
            self.wait_for_workers(task)
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertIn("FLAMETHROWER_DEFERRED_TASKS", server.prompt)
        self.assertIn("do not implement", server.prompt)
        generated = DeferredTask.objects.select_related("task").get()
        self.assertEqual(generated.task.title, "Follow-up")
        self.assertEqual(generated.source_run.task_id, task.pk)

    def test_tcp_run_permission_and_completion(self):
        server = FakeACPServer()
        server.start()
        self.set_endpoint_port(server.port)
        task = Task.objects.create(title="Change", request="A request", plan="A plan", state="plan_approved", stage="testing", version=2)
        with override_settings(ACP_PORT=str(server.port)):
            self.start_automatic_run(task)
            self.wait_for(lambda: PermissionRequest.objects.exists())
            permission = PermissionRequest.objects.get()
            self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "yes"}).status_code, 302)
            self.wait_for(lambda: AgentRun.objects.filter(status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_automatic_pause(task)
            self.wait_for_workers(task)
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertEqual(server.permission_response["result"]["outcome"]["optionId"], "yes")
        run = AgentRun.objects.get()
        self.assertEqual(run.session_id, "test-session")
        self.assertTrue(run.events.filter(kind="message", content="Finished.").exists())
        self.assertTrue(run.events.filter(kind="permission decision").exists())

    def test_acp_usage_updates_replace_cumulative_values_and_save_prompt_tokens(self):
        server = FakeACPServer(
            usage_updates=[
                {"used": 120, "size": 500, "cost": {"amount": 0.1, "currency": "USD"}},
                {"used": 180, "size": 500, "cost": {"amount": 0.25, "currency": "USD"}},
            ],
            prompt_usage={
                "totalTokens": 900, "inputTokens": 700, "outputTokens": 200,
                "thoughtTokens": 25, "cachedReadTokens": 40,
            },
        )
        server.start()
        self.set_endpoint_port(server.port)
        task = Task.objects.create(
            title="Track usage", request="A request", plan="A plan",
            state="plan_approved", stage="testing", version=2,
        )
        with override_settings(ACP_PORT=str(server.port)):
            self.start_automatic_run(task)
            self.wait_for(lambda: PermissionRequest.objects.filter(run__task=task).exists())
            permission = PermissionRequest.objects.get(run__task=task)
            self.assertEqual(self.client.post(
                reverse("resolve_permission", args=[permission.pk]), {"option": "yes"},
            ).status_code, 302)
            self.wait_for(lambda: AgentRun.objects.filter(task=task, status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_automatic_pause(task)
            self.wait_for_workers(task)
        server.thread.join(timeout=5)

        self.assertIsNone(server.error)
        usage = AgentRun.objects.get(task=task).usage_snapshot
        self.assertEqual((usage.cost_amount, usage.cost_currency), (Decimal("0.25"), "USD"))
        self.assertEqual((usage.context_used, usage.context_size), (180, 500))
        self.assertEqual(
            (usage.total_tokens, usage.input_tokens, usage.output_tokens, usage.thought_tokens, usage.cached_read_tokens),
            (900, 700, 200, 25, 40),
        )

    def test_enabled_safe_read_is_auto_approved_and_audited(self):
        from pathlib import Path

        RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=True)
        with override_settings(PROJECT_PATH=Path(__file__).resolve().parents[2]):
            for kind in ("read", "search"):
                with self.subTest(kind=kind):
                    server = FakeACPServer(
                        permission_kind=kind,
                        locations=[{"path": str(Path(__file__).resolve())}],
                    )
                    server.start()
                    self.set_endpoint_port(server.port)
                    task = Task.objects.create(
                        title=kind, request="A request", plan="A plan",
                        state="plan_approved", stage="testing", version=2,
                    )
                    with override_settings(ACP_PORT=str(server.port)):
                        self.start_automatic_run(task)
                        self.wait_for(lambda: AgentRun.objects.filter(
                            task=task, status=AgentRun.Status.COMPLETED,
                        ).exists())
                        self.wait_for_automatic_pause(task)
                        self.wait_for_workers(task)
                    server.thread.join(timeout=5)
                    self.assertIsNone(server.error)
                    self.assertEqual(server.permission_response["result"]["outcome"]["optionId"], "yes")
                    permission = PermissionRequest.objects.get(run__task=task)
                    self.assertTrue(permission.resolved)
                    self.assertEqual(permission.selected_option, "yes")
                    event = permission.run.events.get(kind="permission decision")
                    details = json.loads(event.content)
                    self.assertTrue(details["automatic"])
                    self.assertEqual(details["origin"], "repository_safe_read_policy")
                    self.assertEqual(details["selectedOption"]["optionId"], "yes")

    def test_composite_read_prompt_from_shell_requires_confirmation(self):
        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory).resolve()
            root, external = base / "project", base / "browsers"
            root.mkdir()
            external.mkdir()
            (root / "python").write_text("test")
            RepositorySettings.objects.filter(pk=1).update(
                allow_safe_operations=True, safe_read_paths=[{"path": str(external), "kind": "directory"}],
            )
            server = FakeACPServer(
                permission_kind="execute", raw_input={"command": "ls browsers; python -c something"},
                permission_call={
                    "kind": "read", "rawInput": {"path": f"{external}, python"},
                    "locations": [{"path": f"{external}, python"}],
                },
            )
            server.start()
            self.set_endpoint_port(server.port)
            task = Task.objects.create(
                title="Browser check", request="A request", plan="A plan",
                state="plan_approved", stage="testing", version=2,
            )
            with override_settings(PROJECT_PATH=root, ACP_PORT=str(server.port)):
                self.start_automatic_run(task)
                self.wait_for(lambda: PermissionRequest.objects.filter(run__task=task).exists())
                permission = PermissionRequest.objects.get(run__task=task)
                self.assertFalse(permission.resolved)
                self.assertEqual(permission.tool_call["originToolCall"]["kind"], "execute")
                self.assertEqual(permission.tool_call["originToolCall"]["rawInput"]["command"], "ls browsers; python -c something")
                self.assertEqual(self.client.post(
                    reverse("resolve_permission", args=[permission.pk]), {"option": "no"},
                ).status_code, 302)
                self.wait_for(lambda: AgentRun.objects.filter(task=task, status=AgentRun.Status.COMPLETED).exists())
                self.wait_for_automatic_pause(task)
                self.wait_for_workers(task)
            server.thread.join(timeout=5)
            self.assertIsNone(server.error)
            self.assertEqual(server.permission_response["result"]["outcome"]["optionId"], "no")
            self.assertFalse(permission.run.events.filter(kind="permission decision", content__contains='"automatic": true').exists())

    def test_allowlisted_external_python_target_is_auto_approved_through_project_symlink(self):
        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory).resolve()
            root = base / "project"
            root.mkdir()
            external = base / "python"
            external.write_text("test")
            alias = root / "python"
            alias.symlink_to(external)
            RepositorySettings.objects.filter(pk=1).update(
                allow_safe_operations=True, safe_read_paths=[{"path": str(external), "kind": "file"}],
            )
            server = FakeACPServer(permission_kind="read", locations=[{"path": "python"}])
            server.start()
            self.set_endpoint_port(server.port)
            task = Task.objects.create(
                title="Read Python", request="A request", plan="A plan",
                state="plan_approved", stage="testing", version=2,
            )
            with override_settings(PROJECT_PATH=root, ACP_PORT=str(server.port)):
                self.start_automatic_run(task)
                self.wait_for(lambda: AgentRun.objects.filter(task=task, status=AgentRun.Status.COMPLETED).exists())
                self.wait_for_automatic_pause(task)
                self.wait_for_workers(task)
            server.thread.join(timeout=5)
            self.assertIsNone(server.error)
            self.assertEqual(server.permission_response["result"]["outcome"]["optionId"], "yes")
            permission = PermissionRequest.objects.get(run__task=task)
            self.assertTrue(permission.resolved)
            self.assertTrue(json.loads(permission.run.events.get(kind="permission decision").content)["automatic"])

    def test_enabled_safe_read_with_outside_location_still_requires_manual_approval(self):
        server = FakeACPServer(
            permission_kind="read",
            locations=[{"path": "/tmp/outside-project-file"}],
        )
        server.start()
        self.set_endpoint_port(server.port)
        RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=True)
        task = Task.objects.create(title="Read", request="A request", plan="A plan", state="plan_approved", stage="testing", version=2)
        with override_settings(ACP_PORT=str(server.port)):
            self.start_automatic_run(task)
            self.wait_for(lambda: PermissionRequest.objects.exists())
            permission = PermissionRequest.objects.get()
            self.assertFalse(permission.resolved)
            self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "no"}).status_code, 302)
            self.wait_for(lambda: AgentRun.objects.filter(status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_automatic_pause(task)
            self.wait_for_workers(task)
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertEqual(server.permission_response["result"]["outcome"]["optionId"], "no")
        self.assertFalse(permission.run.events.filter(kind="permission decision", content__contains='"automatic": true').exists())

    def test_safe_read_without_allow_once_option_still_requires_manual_approval(self):
        server = FakeACPServer(
            permission_kind="read",
            locations=[{"path": str(Path(__file__).resolve())}],
            permission_options=[{"optionId": "no", "name": "Reject once", "kind": "reject_once"}],
        )
        server.start()
        self.set_endpoint_port(server.port)
        RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=True)
        task = Task.objects.create(title="Read", request="A request", plan="A plan", state="plan_approved", stage="testing", version=2)
        with override_settings(ACP_PORT=str(server.port)):
            self.start_automatic_run(task)
            self.wait_for(lambda: PermissionRequest.objects.exists())
            permission = PermissionRequest.objects.get()
            self.assertFalse(permission.resolved)
            self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "no"}).status_code, 302)
            self.wait_for(lambda: AgentRun.objects.filter(status=AgentRun.Status.COMPLETED).exists())
            self.wait_for_automatic_pause(task)
            self.wait_for_workers(task)
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertEqual(server.permission_response["result"]["outcome"]["optionId"], "no")
        self.assertFalse(permission.run.events.filter(kind="permission decision", content__contains='"automatic": true').exists())

    def test_testing_and_review_use_distinct_sessions_and_recorded_evidence(self):
        task = Task.objects.create(
            title="Change", request="A request", plan="A plan", state="plan_approved", stage="testing", version=2,
        )
        servers = [FakeACPServer("testing-session"), FakeACPServer("review-session")]
        for server in servers:
            server.start()
        for index, server in enumerate(servers):
            self.set_endpoint_port(server.port)
            task.refresh_from_db()
            with override_settings(ACP_PORT=str(server.port)):
                if index == 0:
                    self.start_automatic_run(task)
                run = task.runs.first()
                self.wait_for(lambda: run.permissions.exists())
                permission = run.permissions.get()
                self.assertEqual(self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "no"}).status_code, 302)
                self.wait_for(lambda: AgentRun.objects.filter(pk=run.pk, status=AgentRun.Status.COMPLETED).exists())
                self.wait_for_automatic_pause(task)
                self.wait_for_workers(task)
            server.thread.join(timeout=5)
            self.assertIsNone(server.error)
            if index == 0:
                self.set_endpoint_port(servers[1].port)
                self.assertEqual(self.client.post(reverse("record_gate", args=[task.pk]), {
                    "version": 2, "stage_version": 0, "result": "failed", "evidence": "Regression test failed",
                    "waiver": "Accepted for review", "action": "advance",
                }).status_code, 302)
            else:
                self.wait_for_automatic_pause(task)
                self.wait_for_workers(task)
        self.assertIn("Test the implementation", servers[0].prompt)
        self.assertIn("Independently review", servers[1].prompt)
        self.assertIn("Regression test failed", servers[1].prompt)
        self.assertIn("Accepted for review", servers[1].prompt)
        self.assertEqual(list(task.runs.order_by("id").values_list("stage", flat=True)), ["testing", "review"])
        self.assertEqual(list(task.runs.order_by("id").values_list("session_id", flat=True)), ["testing-session", "review-session"])
        task.refresh_from_db()
        self.assertEqual(task.stage, Task.Stage.REVIEW)

    def test_testing_recommendation_advances_automatically_after_acp_turn(self):
        recommendation = {
            "result": "passed", "recommendation": "advance", "summary": "Tests passed",
            "evidence": "test command: 3 passed", "untested": "Browser", "reason": "Checks completed",
        }
        message = "FLAMETHROWER_GATE_RECOMMENDATION\n" + json.dumps(recommendation) + "\nEND_FLAMETHROWER_GATE_RECOMMENDATION"
        server = FakeACPServer(message=message)
        server.start()
        self.set_endpoint_port(server.port)
        task = Task.objects.create(title="Change", request="A request", plan="A plan", state="plan_approved", stage="testing", version=2)
        with override_settings(ACP_PORT=str(server.port)):
            self.start_automatic_run(task)
            first_run = task.runs.first()
            with patch("tasks.agent_scheduler.wake_scheduler") as review_worker:
                self.wait_for(lambda: PermissionRequest.objects.filter(run=first_run).exists())
                AcpEndpoint.objects.filter(pk=self.endpoint.pk).update(last_error="Hold queued review")
                permission = first_run.permissions.get()
                self.assertEqual(self.client.post(
                    reverse("resolve_permission", args=[permission.pk]), {"option": "no"},
                ).status_code, 302)
                self.wait_for(lambda: AgentRun.objects.filter(
                    pk=first_run.pk, status=AgentRun.Status.COMPLETED,
                ).exists())
                self.wait_for(lambda: AgentRun.objects.filter(
                    task=task, stage=Task.Stage.REVIEW,
                ).exists())
                review_run = AgentRun.objects.get(task=task, stage=Task.Stage.REVIEW)
                self.assertEqual(review_run.stage, Task.Stage.REVIEW)
                self.wait_for(lambda: review_worker.called)
                review_worker.assert_called_once_with()
            self.wait_for_workers(task)
        server.thread.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertIn("FLAMETHROWER_GATE_RECOMMENDATION", server.prompt)
        task.refresh_from_db()
        self.assertEqual(task.stage, Task.Stage.REVIEW)
        decision = task.gate_decisions.get()
        self.assertEqual(decision.source_run_id, first_run.pk)
        self.assertTrue(decision.automatic)

    def test_cancel_before_connection_never_sends_prompt(self):
        from asgiref.sync import async_to_sync
        from ..agent import run_agent

        task = Task.objects.create(title="Change", request="A request", plan="A plan")
        run = AgentRun.objects.create(
            task=task, acp_endpoint=self.endpoint, task_version=0,
            request_snapshot="r", plan_snapshot="p", cancel_requested=True,
        )
        with patch("tasks.agent.asyncio.open_connection") as connect, self.assertLogs("tasks.agent", level="ERROR"):
            async_to_sync(run_agent)(
                run.pk, run.initial_prompt, self.endpoint.pk,
                self.endpoint.host, self.endpoint.port, run.stage,
            )
        connect.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.INTERRUPTED)
        self.assertIn("cancelled before the prompt", run.error)
