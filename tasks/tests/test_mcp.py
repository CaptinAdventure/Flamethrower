import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from flamethrower import mcp_stdio
from ..forms import TaskForm
from ..models import AcpEndpoint, AgentRun, Approval, DeferredTask, GateDecision, LinkType, PermissionRequest, RunEvent, Task, TaskLink, TaskLinkEvent

TOKEN = "test-token-with-at-least-thirty-two-characters"
EXPECTED_TOOLS = {
    "create_deferred_task", "search_task", "list_tasks", "get_task", "get_task_text", "get_run", "get_run_error", "get_run_event",
    "find_similar_tasks", "create_task", "update_task", "update_task_metadata", "list_work_types",
    "list_task_runs", "list_task_gate_decisions", "get_gate_text",
    "list_relationship_types", "list_task_links", "link_tasks", "unlink_tasks",
    "approve_task_request", "approve_task_plan", "accept_architect_plan", "record_task_gate",
}


class McpConfigurationTests(SimpleTestCase):
    def test_plain_text_configuration_uses_running_environment_and_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary)
            command = scripts / ("flamethrower-mcp.exe" if os.name == "nt" else "flamethrower-mcp")
            command.touch(mode=0o755)
            project = scripts / 'project "with spaces" \u00e9'
            with override_settings(PROJECT_PATH=project), patch("tasks.views.acps.sys.executable", str(scripts / "python")):
                response = self.client.get(reverse("agent_mcp_configuration"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
            instructions, config = response.content.decode().split("\n\n", 1)
            self.assertIn("~/.copilot/mcp-config.json", instructions)
            self.assertIn("merging", instructions)
            self.assertEqual(json.loads(config), {
                "mcpServers": {"flamethrower": {
                    "type": "local", "command": str(command), "args": [str(project)],
                }},
            })

    def test_missing_executable_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("tasks.views.acps.sys.executable", str(Path(temporary) / "python")):
                response = self.client.get(reverse("agent_mcp_configuration"))
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"MCP executable not found", response.content)


@override_settings(LOCAL_API_TOKEN=TOKEN)
class McpApiTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.source = Task.objects.create(title="Source", request="Existing export request", plan="A plan")

    def post_json(self, url, data, **headers):
        return self.client.post(url, data=json.dumps(data), content_type="application/json", **headers)

    def test_api_requires_token_and_loopback(self):
        url = reverse("mcp_create_deferred_task")
        payload = {"title": "Later", "request": "Separate work"}
        self.assertEqual(self.post_json(url, payload).status_code, 401)
        self.assertEqual(self.post_json(url, payload, HTTP_AUTHORIZATION="Bearer incorrect").status_code, 401)
        self.assertEqual(self.post_json(url, payload, HTTP_AUTHORIZATION=f"Bearer {TOKEN}", REMOTE_ADDR="192.0.2.1").status_code, 401)
        self.assertFalse(DeferredTask.objects.exists())
        with override_settings(LOCAL_API_TOKEN=""):
            self.assertEqual(self.post_json(url, payload, HTTP_AUTHORIZATION=f"Bearer {TOKEN}").status_code, 401)

    def test_health_proves_instance_without_disclosing_token(self):
        challenge = "x" * 32
        response = self.post_json(reverse("mcp_health"), {"challenge": challenge})
        self.assertEqual(response.status_code, 200)
        expected = hmac.new(TOKEN.encode(), f"{os.getpid()}:{challenge}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(response.json(), {"pid": os.getpid(), "proof": expected})
        self.assertNotIn(TOKEN.encode(), response.content)

    def test_create_without_run_and_optionally_link_source(self):
        url = reverse("mcp_create_deferred_task")
        headers = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
        for source in (None, self.source.pk):
            response = self.post_json(url, {"title": " Follow-up ", "request": " Separate work ", "source_task_id": source}, **headers)
            self.assertEqual(response.status_code, 201)
            task = Task.objects.get(pk=response.json()["id"])
            origin = task.deferred_origin
            self.assertEqual((task.status, task.stage, task.relates_indexing), ("non_accepted", "draft", "PENDING"))
            self.assertIsNone(origin.source_run_id)
            self.assertEqual(origin.source_task_id, source)
        link = TaskLink.objects.get()
        self.assertEqual((link.source_id, link.link_type.name), (self.source.pk, "Relates"))

    def test_create_validates_input_without_partial_writes(self):
        url = reverse("mcp_create_deferred_task")
        headers = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
        for payload in (
            {"title": "", "request": "r"}, {"title": "t", "request": " "},
            {"title": "t", "request": "r", "source_task_id": True},
            {"title": "t", "request": "r", "extra": 1},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.post_json(url, payload, **headers).status_code, 400)
        self.assertEqual(self.post_json(url, {"title": "t", "request": "r", "source_task_id": 9999}, **headers).status_code, 404)
        self.assertEqual(self.client.post(url, data="{broken", content_type="application/json", **headers).status_code, 400)
        self.assertEqual(Task.objects.count(), 1)

    def test_search_reuses_board_matching_and_paginates(self):
        Task.objects.create(title="Second", request="r", plan="Export this")
        Task.objects.create(title="Unmatched", request="r")
        url = reverse("mcp_search_task")
        headers = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
        first = self.post_json(url, {"query": "EXPORT", "limit": 1}, **headers).json()
        second = self.post_json(url, {"query": "EXPORT", "limit": 1, "offset": 1}, **headers).json()
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len({first["results"][0]["id"], second["results"][0]["id"]}), 2)
        self.assertEqual(self.post_json(url, {"query": ""}, **headers).status_code, 400)
        self.assertEqual(self.post_json(url, {"query": "export", "offset": 10**50}, **headers).status_code, 400)

    def operation(self, name, **data):
        return self.post_json(reverse("mcp_task_operation"), {"operation": name, **data}, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    def test_task_operations_require_auth_and_cannot_resolve_permissions_or_change_policy(self):
        url = reverse("mcp_task_operation")
        self.assertEqual(self.post_json(url, {"operation": "list_tasks"}).status_code, 401)
        self.assertEqual(self.post_json(url, {"operation": "list_tasks"}, HTTP_AUTHORIZATION=f"Bearer {TOKEN}", REMOTE_ADDR="192.0.2.1").status_code, 401)
        self.assertEqual(self.post_json(url, {"operation": "list_tasks"}, HTTP_AUTHORIZATION=f"Bearer {TOKEN}").status_code, 200)
        run = AgentRun.objects.create(task=self.source, task_version=0, request_snapshot="r", plan_snapshot="p", status="waiting")
        permission = PermissionRequest.objects.create(
            run=run, tool_call={"kind": "execute"}, options=[{"optionId": "allow", "kind": "allow_once"}],
        )
        for action in ("resolve_permission", "approve_permission", "set_safe_operations", "set_command_prefixes", "set_mcp_rules", "remove_mcp_rule", "start_run"):
            with self.subTest(action=action):
                self.assertEqual(self.operation(action, permission_id=permission.pk, option="allow").status_code, 400)
        permission.refresh_from_db()
        self.assertFalse(permission.resolved)

    def test_create_list_and_update_task_preserve_workflow_guards(self):
        response = self.operation("create_task", title="New task", request="Initial request", plan="A plan")
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual((data["status"], data["stage"], data["state"]), ("ready", "draft", "draft"))
        task = Task.objects.get(pk=data["id"])
        self.assertFalse(task.runs.exists())
        self.assertEqual(self.operation("create_task", title="", request="r").status_code, 400)
        self.assertEqual(self.operation("list_tasks", limit=1, status="ready").json()["has_more"], True)
        self.assertEqual(self.operation("list_tasks", limit=True).status_code, 400)
        response = self.operation("update_task", task_id=task.pk, version=0, stage_version=0, request="Updated")
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual((task.request, task.version, task.stage_version), ("Updated", 1, 1))
        self.assertEqual(self.operation("update_task", task_id=task.pk, version=0, stage_version=0, title="Stale").status_code, 409)
        AgentRun.objects.create(task=task, task_version=1, request_snapshot="r", plan_snapshot="p", status="running")
        self.assertEqual(self.operation("update_task", task_id=task.pk, version=1, stage_version=1, title="Active").status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.title, "New task")

    def test_edit_rejects_run_started_after_initial_activity_check(self):
        original_is_valid = TaskForm.is_valid

        def validate_and_start(form):
            valid = original_is_valid(form)
            AgentRun.objects.create(task=self.source, task_version=0, request_snapshot="r", plan_snapshot="", status="running")
            return valid

        with patch.object(TaskForm, "is_valid", validate_and_start):
            response = self.operation("update_task", task_id=self.source.pk, version=0, stage_version=0, title="Changed")
        self.assertEqual(response.status_code, 409)
        self.source.refresh_from_db()
        self.assertEqual(self.source.title, "Source")

    def test_edit_does_not_overwrite_concurrent_acp_selection(self):
        endpoint = AcpEndpoint.objects.create(name="Local", port=12345)
        original_is_valid = TaskForm.is_valid

        def validate_and_select(form):
            valid = original_is_valid(form)
            Task.objects.filter(pk=self.source.pk).update(acp_selection="explicit", selected_acp_endpoint=endpoint)
            return valid

        with patch.object(TaskForm, "is_valid", validate_and_select):
            response = self.operation("update_task", task_id=self.source.pk, version=0, stage_version=0, title="Changed")
        self.assertEqual(response.status_code, 200)
        self.source.refresh_from_db()
        self.assertEqual((self.source.title, self.source.acp_selection, self.source.selected_acp_endpoint_id), ("Changed", "explicit", endpoint.pk))

    def test_editing_approved_request_invalidates_approval_and_reindexes(self):
        Task.objects.filter(pk=self.source.pk).update(stage="developer", state="plan_approved")
        response = self.operation("update_task", task_id=self.source.pk, version=0, stage_version=0, request="Changed")
        self.assertEqual(response.status_code, 200)
        self.source.refresh_from_db()
        self.assertEqual((self.source.stage, self.source.state), ("draft", "draft"))
        self.assertIn("reapprove", self.source.automatic_pause_reason)
        self.assertEqual((self.source.relates_indexing, self.source.indexing_revision), ("PENDING", 1))

    def test_metadata_and_relationships_use_existing_constraints(self):
        response = self.operation("update_metadata", task_id=self.source.pk, version=0, stage_version=0, status="blocked")
        self.assertEqual(response.status_code, 400)
        self.assertIn("blocked_reason", response.json()["fields"])
        response = self.operation("update_metadata", task_id=self.source.pk, version=0, stage_version=0, status="blocked", blocked_reason="Waiting")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["blocked_reason"], "Waiting")
        self.assertEqual(self.operation("update_metadata", task_id=self.source.pk, version=0, stage_version=0, status="ready").status_code, 409)
        other = Task.objects.create(title="Other", request="r")
        relation = LinkType.objects.get(name="Relates")
        values = {"task_id": self.source.pk, "target_id": other.pk, "link_type_id": relation.pk, "direction": "out"}
        response = self.operation("link_task", **values)
        self.assertEqual(response.status_code, 201)
        link_id = response.json()["id"]
        self.assertEqual(self.operation("link_task", **values).status_code, 409)
        self.assertEqual(self.operation("list_links", task_id=other.pk).json()["results"][0]["id"], link_id)
        self.assertEqual(self.operation("unlink_task", task_id=other.pk, link_id=link_id).status_code, 200)
        self.assertEqual(self.operation("unlink_task", task_id=other.pk, link_id=link_id).status_code, 404)
        self.assertEqual(list(TaskLinkEvent.objects.order_by("id").values_list("action", flat=True)), ["linked", "unlinked"])

    def test_read_task_and_run_events_are_bounded_and_paginated(self):
        run = AgentRun.objects.create(task=self.source, task_version=0, request_snapshot="r", plan_snapshot="p", status="completed")
        event = RunEvent.objects.create(run=run, kind="message", content="x" * 6000)
        data = self.operation("get_task", task_id=self.source.pk).json()
        self.assertEqual((data["id"], data["runs"][0]["id"]), (self.source.pk, run.pk))
        self.assertEqual(self.operation("similar_tasks", task_id=self.source.pk).json()["indexing_status"], "PENDING")
        preview = self.operation("get_run", task_id=self.source.pk, run_id=run.pk).json()["events"][0]
        self.assertTrue(preview["content_truncated"])
        self.assertEqual(len(preview["content"]), 2000)
        page = self.operation("get_run_event", task_id=self.source.pk, run_id=run.pk, event_id=event.pk, offset=5000).json()
        self.assertEqual((len(page["content"]), page["has_more"]), (1000, False))
        self.assertEqual(self.operation("get_run_event", task_id=999, run_id=run.pk, event_id=event.pk).status_code, 404)

    def test_large_text_has_bounded_previews_and_retrievable_pages(self):
        long_text = "x" * 25001
        Task.objects.filter(pk=self.source.pk).update(request=long_text)
        run = AgentRun.objects.create(task=self.source, task_version=0, request_snapshot="r", plan_snapshot="", error=long_text)
        detail = self.operation("get_task", task_id=self.source.pk).json()
        self.assertTrue(detail["request_truncated"])
        self.assertEqual(len(detail["request"]), 20000)
        page = self.operation("get_task_text", task_id=self.source.pk, field="request", offset=20000).json()
        self.assertEqual((len(page["content"]), page["has_more"], page["version"]), (5001, False, 0))
        self.assertEqual(self.operation("get_task_text", task_id=self.source.pk, field="invalid").status_code, 400)
        self.assertTrue(self.operation("get_run", task_id=self.source.pk, run_id=run.pk).json()["error_truncated"])
        page = self.operation("get_run_error", task_id=self.source.pk, run_id=run.pk, offset=20000).json()
        self.assertEqual((len(page["content"]), page["has_more"]), (5001, False))
        search = self.post_json(reverse("mcp_search_task"), {"query": "Source"}, HTTP_AUTHORIZATION=f"Bearer {TOKEN}").json()
        self.assertTrue(search["results"][0]["request_truncated"])
        self.assertEqual(len(search["results"][0]["request"]), 1000)

    def test_run_gate_and_type_lists_paginate_and_gate_text_is_retrievable(self):
        for _ in range(2):
            AgentRun.objects.create(task=self.source, task_version=0, request_snapshot="r", plan_snapshot="")
            GateDecision.objects.create(task=self.source, task_version=0, stage="testing", result="passed", evidence="e" * 1500)
        for name, args in (("list_task_runs", {"task_id": self.source.pk}),
                           ("list_gate_decisions", {"task_id": self.source.pk}),
                           ("list_work_types", {}), ("relationship_types", {})):
            first = self.operation(name, limit=1, **args).json()
            second = self.operation(name, limit=1, offset=1, **args).json()
            self.assertTrue(first["has_more"])
            self.assertNotEqual(first["results"][0]["id"], second["results"][0]["id"])
        gate = self.operation("list_gate_decisions", task_id=self.source.pk, limit=1).json()["results"][0]
        self.assertTrue(gate["evidence_truncated"])
        page = self.operation("get_gate_text", task_id=self.source.pk, decision_id=gate["id"], field="evidence", offset=1000).json()
        self.assertEqual((len(page["content"]), page["has_more"]), (500, False))

    def test_maximum_pages_remain_below_transport_limit_with_unicode(self):
        run = AgentRun.objects.create(task=self.source, task_version=0, request_snapshot="r", plan_snapshot="")
        RunEvent.objects.bulk_create([RunEvent(run=run, kind="message", content="\U0001f600" * 3000) for _ in range(100)])
        GateDecision.objects.bulk_create([
            GateDecision(task=self.source, task_version=0, stage="testing", result="passed", evidence="\U0001f600" * 1500, waiver="\U0001f600" * 1500)
            for _ in range(100)
        ])
        for operation, extra in (("get_run", {"run_id": run.pk}), ("list_gate_decisions", {})):
            response = self.operation(operation, task_id=self.source.pk, limit=100, **extra)
            self.assertEqual(response.status_code, 200)
            self.assertLess(len(response.content), 4_000_000)

    @patch("tasks.automation.continue_automatic_task")
    def test_mcp_request_and_plan_approvals_use_versions_and_record_actor(self, continuation):
        response = self.operation("approve_request", task_id=self.source.pk, version=0, stage_version=0)
        self.assertEqual(response.status_code, 200)
        self.source.refresh_from_db()
        self.assertEqual((self.source.stage, self.source.state), ("architect", "scope_approved"))
        self.assertIn("MCP agent", self.source.approvals.get(kind=Approval.Kind.SCOPE).content)
        self.assertEqual(self.operation("approve_plan", task_id=self.source.pk, version=0, stage_version=0).status_code, 409)
        response = self.operation("approve_plan", task_id=self.source.pk, version=self.source.version, stage_version=self.source.stage_version)
        self.assertEqual(response.status_code, 200)
        self.source.refresh_from_db()
        self.assertEqual((self.source.stage, self.source.state), ("developer", "plan_approved"))
        self.assertIn("MCP agent", self.source.approvals.get(kind=Approval.Kind.PLAN).content)
        self.assertEqual(continuation.call_count, 2)

    @patch("tasks.automation.continue_automatic_task")
    def test_mcp_accepts_only_current_architect_proposal_and_records_actor(self, continuation):
        Task.objects.filter(pk=self.source.pk).update(stage="architect", state="scope_approved")
        run = AgentRun.objects.create(
            task=self.source, stage="architect", task_version=0, work_cycle=0,
            request_snapshot="r", plan_snapshot="", status="completed",
        )
        RunEvent.objects.create(run=run, kind="message", content="FLAMETHROWER_ARCHITECT_PLAN\nProposed design\nEND_FLAMETHROWER_ARCHITECT_PLAN")
        proposal = self.operation("get_task", task_id=self.source.pk).json()["architect_proposal"]
        values = {
            "task_id": self.source.pk, "version": 0, "stage_version": 0,
            "run_id": run.pk, "report_hash": proposal["report_hash"],
        }
        self.assertEqual(self.operation("accept_plan", **{**values, "report_hash": "0" * 64}).status_code, 409)
        self.assertEqual(self.operation("accept_plan", **values).status_code, 200)
        self.source.refresh_from_db()
        self.assertEqual((self.source.plan, self.source.state), ("Proposed design", "plan_approved"))
        self.assertIn("MCP agent", self.source.approvals.get(kind=Approval.Kind.PLAN).content)
        continuation.assert_called_once_with(self.source.pk)

    @patch("tasks.automation.continue_automatic_task")
    def test_mcp_gate_decisions_require_evidence_and_waiver_and_are_attributed(self, continuation):
        Task.objects.filter(pk=self.source.pk).update(
            stage="testing", state="plan_approved", automatic_pause_reason="Review the gate.",
        )
        values = {"task_id": self.source.pk, "version": 0, "stage_version": 0, "result": "failed", "evidence": "Actual failure", "action": "advance"}
        self.assertEqual(self.operation("record_gate", **values).status_code, 400)
        self.assertFalse(GateDecision.objects.filter(task=self.source).exists())
        response = self.operation("record_gate", **values, waiver="Accepted risk")
        self.assertEqual(response.status_code, 200)
        decision = GateDecision.objects.get(task=self.source)
        self.assertEqual((decision.recorded_by, decision.automatic, decision.waiver), ("mcp", False, "Accepted risk"))
        self.assertEqual(self.operation("list_gate_decisions", task_id=self.source.pk).json()["results"][0]["recorded_by"], "mcp")
        self.assertEqual(self.operation("get_gate_text", task_id=self.source.pk, decision_id=decision.pk, field="waiver").json()["content"], "Accepted risk")
        self.source.refresh_from_db()
        self.assertEqual(self.source.stage, "review")
        continuation.assert_called_once_with(self.source.pk)


class McpClientTests(TestCase):
    def test_api_validation_details_reach_the_client(self):
        connection = Mock()
        connection.getresponse.return_value.status = 400
        connection.getresponse.return_value.read.return_value = json.dumps({
            "error": "Invalid metadata.", "fields": {"blocked_reason": [{"message": "Explain the constraint."}]},
        }).encode()
        with self.assertRaisesMessage(RuntimeError, "Explain the constraint."):
            mcp_stdio.request_json(connection, "/api/mcp/task/", {})

    @patch("flamethrower.mcp_stdio.call_api")
    def test_gate_tool_sends_operation_and_decision_action_separately(self, call_api):
        mcp_stdio.record_task_gate(1, 2, 3, "passed", "Verified", "advance")
        call_api.assert_called_once_with("/api/mcp/task/", {
            "operation": "record_gate", "task_id": 1, "version": 2, "stage_version": 3,
            "result": "passed", "evidence": "Verified", "action": "advance", "waiver": "",
        })

    def test_missing_stale_and_legacy_runtime_records_fail_before_http(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(mcp_stdio, "project_path", Path(temporary)):
            storage = Path(temporary) / ".flamethrower"
            storage.mkdir()
            path = storage / "server.json"
            with self.assertRaisesMessage(RuntimeError, "missing"):
                mcp_stdio.search_task("export")
            path.write_text(json.dumps({"application": "flamethrower", "pid": os.getpid(), "port": 1234}))
            with self.assertRaisesMessage(RuntimeError, "no API token"):
                mcp_stdio.search_task("export")
            path.write_text(json.dumps({"application": "flamethrower", "pid": os.getpid(), "port": 1234, "token": TOKEN}))
            with patch("flamethrower.mcp_stdio.psutil.pid_exists", return_value=False):
                with self.assertRaisesMessage(RuntimeError, "stale"):
                    mcp_stdio.search_task("export")

    def test_unrelated_server_receives_no_task_data_or_token(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(mcp_stdio, "project_path", Path(temporary)):
            storage = Path(temporary) / ".flamethrower"
            storage.mkdir()
            (storage / "server.json").write_text(json.dumps({
                "application": "flamethrower", "pid": os.getpid(), "port": 1234, "token": TOKEN,
            }))
            connection = Mock()
            connection.getresponse.return_value.status = 200
            connection.getresponse.return_value.read.return_value = b'{"pid": 1, "proof": "wrong"}'
            with patch("flamethrower.mcp_stdio.http.client.HTTPConnection", return_value=connection):
                with self.assertRaisesMessage(RuntimeError, "not the expected Flamethrower instance"):
                    mcp_stdio.create_deferred_task("Private title", "Private request")
            self.assertEqual(connection.request.call_count, 1)
            self.assertNotIn(TOKEN, str(connection.request.call_args))
            self.assertNotIn("Private", str(connection.request.call_args))


class McpStdioIntegrationTests(SimpleTestCase):
    def test_direct_script_initializes_from_another_directory(self):
        async def exercise(project):
            script = Path(mcp_stdio.__file__).resolve()
            params = StdioServerParameters(
                command=sys.executable, args=[str(script), str(project)], cwd=project,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({tool.name for tool in tools.tools}, EXPECTED_TOOLS)

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(exercise(Path(temporary)))

    def test_stdio_tools_call_running_launcher_over_http(self):
        setup = """
import django, threading
original = django.setup
def setup(*args, **kwargs):
    original(*args, **kwargs)
    import tasks.similarity
    class Worker:
        def join(self): pass
    tasks.similarity.start_worker = lambda: (threading.Event(), Worker())
django.setup = setup
from flamethrower.launcher import main
main()
"""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            path = project / ".flamethrower" / "server.json"
            process = subprocess.Popen(
                [sys.executable, "-c", setup, "--no-browser", str(project)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(f"Launcher exited early: {process.communicate()}")
                    if path.exists():
                        record = json.loads(path.read_text())
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{record['port']}/", timeout=1) as response:
                                if response.status == 200:
                                    break
                        except OSError:
                            pass
                    time.sleep(0.1)
                else:
                    self.fail("Launcher did not start.")

                async def exercise():
                    params = StdioServerParameters(
                        command=sys.executable, args=["-m", "flamethrower.mcp_stdio", str(project)],
                    )
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            self.assertEqual({tool.name for tool in tools.tools}, EXPECTED_TOOLS)
                            first = await session.call_tool("create_deferred_task", {"title": "Export follow-up", "request": "Implement CSV export later."})
                            self.assertFalse(first.isError)
                            created = json.loads(first.content[0].text)
                            self.assertEqual(created["status"], "non_accepted")
                            second = await session.call_tool("create_deferred_task", {
                                "title": "Related follow-up", "request": "A related issue", "source_task_id": created["id"],
                            })
                            self.assertFalse(second.isError)
                            found = await session.call_tool("search_task", {"query": "EXPORT"})
                            self.assertFalse(found.isError)
                            self.assertEqual(json.loads(found.content[0].text)["results"][0]["id"], created["id"])
                            invalid = await session.call_tool("create_deferred_task", {"title": "", "request": "x"})
                            self.assertTrue(invalid.isError)
                            ready = await session.call_tool("create_task", {"title": "Ready task", "request": "A request"})
                            self.assertFalse(ready.isError)
                            task = json.loads(ready.content[0].text)
                            self.assertEqual(task["status"], "ready")
                            updated = await session.call_tool("update_task", {
                                "task_id": task["id"], "version": task["version"], "stage_version": task["stage_version"],
                                "title": "Updated task",
                            })
                            self.assertFalse(updated.isError)
                            self.assertEqual(json.loads(updated.content[0].text)["title"], "Updated task")
                            forbidden = await session.call_tool("approve_permission", {"permission_id": 1})
                            self.assertTrue(forbidden.isError)

                asyncio.run(exercise())
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                process.communicate()
