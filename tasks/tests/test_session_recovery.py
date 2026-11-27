import json
import socket
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from acp.exceptions import RequestError
from asgiref.sync import async_to_sync, sync_to_async
from django.test import TransactionTestCase

from ..acp import create_run
from ..agent import run_agent, save_event
from ..agent_scheduler import claim_next_recovery, claim_next_run, recover_interrupted_runs
from ..models import AcpEndpoint, AgentQuestion, AgentRun, PermissionRequest, Task


class SessionRecoveryTests(TransactionTestCase):
    def setUp(self):
        self.endpoint = AcpEndpoint.objects.create(name="ACP", port=12345)
        self.task = Task.objects.create(
            title="A task", request="r", stage=Task.Stage.DEVELOPER, state=Task.State.PLAN_APPROVED,
        )
        self.run = create_run(
            self.task, stage=self.task.stage, task_version=self.task.version, work_cycle=self.task.work_cycle,
            request_snapshot="r", plan_snapshot="p",
        )
        self.run = claim_next_run()
        self.connection = MagicMock()
        self.connection.initialize = AsyncMock(return_value=SimpleNamespace(
            protocol_version=1, agent_capabilities=SimpleNamespace(
                session_capabilities=SimpleNamespace(title=None, status=None, resume=None),
            ),
        ))
        self.connection.new_session = AsyncMock(return_value=SimpleNamespace(session_id="session-1"))
        self.connection.set_session_title = AsyncMock(return_value=SimpleNamespace(title="normalized title"))
        self.connection.session_status = AsyncMock(return_value=SimpleNamespace(status="saved"))
        self.connection.resume_session = AsyncMock(return_value=SimpleNamespace())
        self.connection.close = AsyncMock()
        self.connection.cancel = AsyncMock()
        self.writer = MagicMock()
        self.writer.wait_closed = AsyncMock()

        async def prompt(**kwargs):
            await save_event(
                self.run.pk, "message",
                "Done.\nFLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE",
            )
            return SimpleNamespace(stop_reason="end_turn", usage=None)

        self.connection.prompt = AsyncMock(side_effect=prompt)

    def execute(self, *, recovery=False, wait=None):
        with patch("tasks.agent.asyncio.open_connection", new=AsyncMock(return_value=(object(), self.writer))), \
             patch("tasks.agent.acp.connect_to_agent", return_value=self.connection), \
             patch("tasks.agent.wait_for_answer", new=wait or AsyncMock(return_value=None)):
            async_to_sync(run_agent)(
                self.run.pk, self.run.initial_prompt, self.endpoint.pk, self.endpoint.host,
                self.endpoint.port, self.run.stage, **({"recovery_session_id": "session-1"} if recovery else {}),
            )
        self.run.refresh_from_db()

    def interrupted(self):
        AgentRun.objects.filter(pk=self.run.pk).update(session_id="session-1")
        recover_interrupted_runs()
        self.run = claim_next_recovery()
        self.assertIsNotNone(self.run)

    def test_title_uses_task_id_phase_and_title_and_records_normalized_response(self):
        self.connection.initialize.return_value.agent_capabilities.session_capabilities.title = SimpleNamespace()
        self.execute()
        self.connection.set_session_title.assert_awaited_once_with(
            "session-1", f"[{self.task.pk}:developer] A task",
        )
        self.assertTrue(self.run.events.filter(content="ACP session title: normalized title").exists())
        self.assertEqual(self.run.status, AgentRun.Status.COMPLETED)

    def test_unsupported_or_rejected_title_does_not_prevent_prompt(self):
        for supported in (False, True):
            with self.subTest(supported=supported):
                AgentRun.objects.filter(pk=self.run.pk).update(status=AgentRun.Status.RUNNING)
                self.connection.initialize.return_value.agent_capabilities.session_capabilities.title = (
                    SimpleNamespace() if supported else None
                )
                self.connection.set_session_title.reset_mock()
                self.connection.set_session_title.side_effect = RequestError.invalid_params()
                self.execute()
                if supported:
                    self.connection.set_session_title.assert_awaited_once()
                    self.assertTrue(self.run.events.filter(content__contains="title could not be set").exists())
                else:
                    self.connection.set_session_title.assert_not_awaited()
                    self.assertTrue(self.run.events.filter(content__contains="title not set").exists())
                self.assertEqual(self.run.status, AgentRun.Status.COMPLETED)

    def test_null_agent_capabilities_do_not_prevent_new_session(self):
        self.connection.initialize.return_value.agent_capabilities = None
        self.execute()
        self.connection.set_session_title.assert_not_awaited()
        self.assertEqual(self.run.status, AgentRun.Status.COMPLETED)

    def test_saved_session_waits_for_direction_before_prompt(self):
        self.interrupted()
        capabilities = self.connection.initialize.return_value.agent_capabilities.session_capabilities
        capabilities.status = capabilities.resume = SimpleNamespace()

        async def wait(run_id, question_id, *, reader):
            self.connection.prompt.assert_not_awaited()
            from ..questions import answer_question
            await sync_to_async(answer_question, thread_sensitive=True)(question_id, {"answer": "Inspect the changes"})
            return {"answer": "Inspect the changes"}

        self.execute(recovery=True, wait=wait)
        self.connection.new_session.assert_not_awaited()
        self.connection.resume_session.assert_awaited_once()
        self.connection.prompt.assert_awaited_once()
        self.assertIn("Inspect the changes", self.connection.prompt.await_args.kwargs["prompt"][0].text)
        self.assertEqual(self.run.status, AgentRun.Status.COMPLETED)
        self.assertTrue(self.run.events.filter(kind="human answer").exists())

    def test_unknown_open_and_unsupported_sessions_remain_interrupted(self):
        for mode in ("open", "unknown", "unsupported"):
            with self.subTest(mode=mode):
                AgentRun.objects.filter(pk=self.run.pk).update(
                    status=AgentRun.Status.RUNNING, session_id="session-1",
                )
                self.interrupted()
                capabilities = self.connection.initialize.return_value.agent_capabilities.session_capabilities
                capabilities.status = capabilities.resume = None if mode == "unsupported" else SimpleNamespace()
                self.connection.session_status.return_value.status = mode
                self.connection.resume_session.reset_mock()
                self.connection.prompt.reset_mock()
                with self.assertLogs("tasks.agent", level="ERROR"):
                    self.execute(recovery=True)
                self.assertEqual(self.run.status, AgentRun.Status.INTERRUPTED)
                self.connection.resume_session.assert_not_awaited()
                self.connection.prompt.assert_not_awaited()

    def test_cancelled_direction_never_sends_prompt(self):
        self.interrupted()
        capabilities = self.connection.initialize.return_value.agent_capabilities.session_capabilities
        capabilities.status = capabilities.resume = SimpleNamespace()
        with self.assertLogs("tasks.agent", level="ERROR"):
            self.execute(recovery=True)
        self.assertEqual(self.run.status, AgentRun.Status.INTERRUPTED)
        self.assertEqual(self.run.questions.get(source="recovery").outcome, AgentQuestion.Outcome.CANCELLED)
        self.connection.prompt.assert_not_awaited()

    def test_disconnect_while_waiting_for_direction_interrupts_run(self):
        from ..agent import wait_for_answer
        from ..questions import TEXT_SCHEMA, create_question

        question_id = create_question(self.run.pk, "Direction?", TEXT_SCHEMA, "recovery")
        reader = MagicMock()
        reader.at_eof.return_value = True
        with self.assertRaisesMessage(RuntimeError, "connection closed"):
            async_to_sync(wait_for_answer)(self.run.pk, question_id, reader=reader)

    def test_unknown_session_error_and_cancellation_during_status_do_not_resume(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                AgentRun.objects.filter(pk=self.run.pk).update(
                    status=AgentRun.Status.RUNNING, session_id="session-1", cancel_requested=False,
                )
                self.interrupted()
                capabilities = self.connection.initialize.return_value.agent_capabilities.session_capabilities
                capabilities.status = capabilities.resume = SimpleNamespace()

                async def status(session_id):
                    if cancel:
                        await sync_to_async(AgentRun.objects.filter(pk=self.run.pk).update, thread_sensitive=True)(
                            cancel_requested=True,
                        )
                        return SimpleNamespace(status="saved")
                    raise RequestError.invalid_params()

                self.connection.session_status.side_effect = status
                with self.assertLogs("tasks.agent", level="ERROR"):
                    self.execute(recovery=True)
                self.assertEqual(self.run.status, AgentRun.Status.INTERRUPTED)
                self.connection.resume_session.assert_not_awaited()
                self.connection.prompt.assert_not_awaited()

    def test_stale_and_acknowledged_runs_are_not_recovered(self):
        AgentRun.objects.filter(pk=self.run.pk).update(session_id="session-1")
        recover_interrupted_runs()
        self.task.version += 1
        self.task.save(update_fields=["version"])
        self.assertIsNone(claim_next_recovery())
        self.assertTrue(self.run.events.filter(content__contains="task has changed").exists())
        self.task.version -= 1
        self.task.save(update_fields=["version"])
        recover_interrupted_runs()
        AgentRun.objects.filter(pk=self.run.pk).update(status=AgentRun.Status.ACKNOWLEDGED)
        self.assertIsNone(claim_next_recovery())

    def test_restart_cancels_stale_question_and_clears_its_pause(self):
        from ..questions import QUESTION_PAUSE, TEXT_SCHEMA, create_question

        AgentRun.objects.filter(pk=self.run.pk).update(session_id="session-1")
        question_id = create_question(self.run.pk, "Old question?", TEXT_SCHEMA, "turn")
        self.task.refresh_from_db()
        self.assertEqual(self.task.automatic_pause_reason, QUESTION_PAUSE)
        recover_interrupted_runs()
        self.task.refresh_from_db()
        self.assertEqual(self.task.automatic_pause_reason, "")
        self.assertEqual(AgentQuestion.objects.get(pk=question_id).outcome, AgentQuestion.Outcome.CANCELLED)
        self.assertEqual(claim_next_recovery().pk, self.run.pk)

    def test_interruption_pause_can_be_recovered_but_other_pauses_cannot(self):
        AgentRun.objects.filter(pk=self.run.pk).update(session_id="session-1")
        self.task.automatic_pause_reason = "Review the plan first."
        self.task.save(update_fields=["automatic_pause_reason"])
        recover_interrupted_runs()
        self.assertIsNone(claim_next_recovery())
        self.assertTrue(self.run.events.filter(content__contains="paused for another reason").exists())
        self.task.automatic_pause_reason = "The agent run did not complete normally. Inspect it before continuing."
        self.task.save(update_fields=["automatic_pause_reason"])
        recover_interrupted_runs()
        self.run = claim_next_recovery()
        self.assertIsNotNone(self.run)
        capabilities = self.connection.initialize.return_value.agent_capabilities.session_capabilities
        capabilities.status = capabilities.resume = SimpleNamespace()
        with self.assertLogs("tasks.agent", level="ERROR"):
            self.execute(recovery=True)
        self.assertTrue(self.run.events.filter(content__contains="Previous interruption:").exists())
        self.task.refresh_from_db()
        self.assertIn("did not complete normally", self.task.automatic_pause_reason)

    def test_unresolved_permissions_and_changed_endpoint_are_not_recovered(self):
        AgentRun.objects.filter(pk=self.run.pk).update(session_id="session-1")
        PermissionRequest.objects.create(run=self.run, tool_call={}, options=[])
        recover_interrupted_runs()
        self.assertIsNone(claim_next_recovery())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AgentRun.Status.INTERRUPTED)
        self.assertTrue(self.run.events.filter(content__contains="permission was pending").exists())
        self.run.permissions.update(resolved=True)
        self.endpoint.port += 1
        self.endpoint.save(update_fields=["port"])
        recover_interrupted_runs()
        self.assertIsNone(claim_next_recovery())
        self.assertTrue(self.run.events.filter(content__contains="address").exists())

    def test_wire_protocol_for_title_and_saved_resume(self):
        from ..questions import answer_question

        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                sock.listen()
                sock.settimeout(10)
                self.endpoint.port = sock.getsockname()[1]
                self.endpoint.save(update_fields=["port"])
                AgentRun.objects.filter(pk=self.run.pk).update(
                    status=AgentRun.Status.RUNNING, session_id="session-1",
                    acp_host=self.endpoint.host, acp_port=self.endpoint.port,
                )
                if recovery:
                    self.interrupted()
                requests = []
                errors = []

                def serve():
                    try:
                        conn, _ = sock.accept()
                        with conn, conn.makefile("rwb") as stream:
                            def receive():
                                request = json.loads(stream.readline())
                                requests.append(request)
                                return request

                            def reply(request, result):
                                stream.write((json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n").encode())
                                stream.flush()

                            request = receive()
                            reply(request, {"protocolVersion": 1, "agentCapabilities": {
                                "sessionCapabilities": {"title": {}, "status": {}, "resume": {}},
                            }})
                            request = receive()
                            if recovery:
                                reply(request, {"status": "saved"})
                                reply(receive(), {})
                            else:
                                reply(request, {"sessionId": "session-1"})
                                reply(receive(), {"title": "stored title"})
                            request = receive()
                            stream.write((json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                                "sessionId": "session-1", "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE"},
                                },
                            }}) + "\n").encode())
                            stream.flush()
                            reply(request, {"stopReason": "end_turn"})
                    except Exception as error:
                        errors.append(error)
                    finally:
                        sock.close()

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()

                async def wait(run_id, question_id, *, reader):
                    self.assertEqual([item["method"] for item in requests], ["initialize", "session/status", "session/resume"])
                    await sync_to_async(answer_question, thread_sensitive=True)(question_id, {"answer": "Continue carefully"})
                    return {"answer": "Continue carefully"}

                try:
                    with patch("tasks.agent.wait_for_answer", new=wait):
                        async_to_sync(run_agent)(
                            self.run.pk, self.run.initial_prompt, self.endpoint.pk, self.endpoint.host,
                            self.endpoint.port, self.run.stage, **({"recovery_session_id": "session-1"} if recovery else {}),
                        )
                finally:
                    thread.join(timeout=10)
                    sock.close()
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.run.refresh_from_db()
                self.assertEqual(self.run.status, AgentRun.Status.COMPLETED, self.run.error)
                methods = [item["method"] for item in requests]
                expected = ["initialize", "session/status", "session/resume", "session/prompt"] if recovery else [
                    "initialize", "session/new", "session/set_title", "session/prompt",
                ]
                self.assertEqual(methods, expected)
                if not recovery:
                    self.assertEqual(requests[2]["params"]["title"], f"[{self.task.pk}:developer] A task")
