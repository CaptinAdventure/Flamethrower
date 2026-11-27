import json
import socket
import threading
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from acp.exceptions import RequestError
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from ..acp import create_run
from ..agent_scheduler import claim_next_run
from ..automation import continue_automatic_run
from ..deferred import complete_run, parse_deferred_tasks
from ..models import AcpEndpoint, AgentQuestion, AgentRun, PermissionRequest, RunEvent, Task
from ..questions import QuestionForm, TEXT_SCHEMA, answer_question, create_question, validate_question_schema
from ..recommendations import architect_plan, developer_complete, final_agent_report, question_text


class MarkerParsingTests(SimpleTestCase):
    def test_inline_marker_mentions_do_not_invalidate_terminal_blocks(self):
        cases = (
            (architect_plan, "FLAMETHROWER_ARCHITECT_PLAN", "Use the marker in the plan.", "Use the marker in the plan."),
            (question_text, "FLAMETHROWER_QUESTION", "Which option?", "Which option?"),
        )
        for parser, marker, body, expected in cases:
            with self.subTest(marker=marker):
                text = f"The previous response omitted `{marker}`.\n{marker}\n{body}\nEND_{marker}"
                self.assertEqual(parser(text), expected)
                self.assertEqual(parser(text.replace("\n", "\r\n")), expected)
                self.assertEqual(parser(text + f"\n{marker}\n{body}\nEND_{marker}"), "")
        self.assertTrue(developer_complete(
            "The previous fixture omitted `FLAMETHROWER_DEVELOPER_COMPLETE`.\n"
            "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE"
        ))
        self.assertFalse(developer_complete(
            "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE\n"
            "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE"
        ))

    def test_gate_requires_unique_marker_lines_but_allows_mentions(self):
        gate = {
            "result": "unverified", "recommendation": "hold", "summary": "FLAMETHROWER_GATE_RECOMMENDATION",
            "evidence": "Not run", "untested": "All", "reason": "Needs review",
        }
        block = "FLAMETHROWER_GATE_RECOMMENDATION\n" + json.dumps(gate) + "\nEND_FLAMETHROWER_GATE_RECOMMENDATION"
        event = lambda text: [SimpleNamespace(kind="message", content=text)]
        self.assertEqual(final_agent_report(event("Mention `FLAMETHROWER_GATE_RECOMMENDATION`.\n" + block))["recommendation"], gate)
        self.assertIsNone(final_agent_report(event(block + "\n" + block))["recommendation"])

    def test_thoughts_do_not_interrupt_or_supply_final_response_markers(self):
        event = lambda kind, content: SimpleNamespace(kind=kind, content=content)
        events = [
            event("message", "Done.\nFLAMETHROWER_DEVELOPER_"),
            event("thought", "FLAMETHROWER_QUESTION\nNot a real question\nEND_FLAMETHROWER_QUESTION"),
            event("message", "COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE"),
            event("thought content", '{"type":"image"}'),
        ]
        report = final_agent_report(events)
        self.assertTrue(developer_complete(report["text"]))
        self.assertNotIn("Not a real question", report["text"])
        self.assertEqual(final_agent_report([event("thought", "FLAMETHROWER_DEVELOPER_COMPLETE")])["text"], "")


class QuestionTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(title="Question", request="A request", plan="A plan")
        self.run = AgentRun.objects.create(
            task=self.task, task_version=0, request_snapshot="r", plan_snapshot="p", status="running",
        )

    def test_form_validates_choices_types_and_required_values(self):
        schema = {
            "type": "object",
            "properties": {
                "engine": {"type": "string", "oneOf": [{"const": "pg", "title": "Postgres"}, {"const": "sqlite", "title": "SQLite"}]},
                "retry": {"type": "integer", "minimum": 1},
                "enabled": {"type": "boolean"},
                "features": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
            },
            "required": ["engine", "retry", "enabled", "features"],
        }
        form = QuestionForm(schema, {
            "engine": "pg", "retry": "2", "enabled": "false", "features": ["a", "b"],
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.content, {"engine": "pg", "retry": 2, "enabled": False, "features": ["a", "b"]})
        form = QuestionForm(schema, {"engine": "pg", "retry": "0", "enabled": "false", "features": ["a"]})
        self.assertFalse(form.is_valid())
        self.assertIn("retry", form.errors)

    def test_answer_is_recorded_once_and_waiting_permission_remains(self):
        permission = PermissionRequest.objects.create(run=self.run, tool_call={}, options=[])
        question_id = create_question(self.run.pk, "Which option?", TEXT_SCHEMA, "turn")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AgentRun.Status.WAITING_ANSWER)
        answer_question(question_id, {"answer": "Option A"})
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AgentRun.Status.WAITING)
        self.task.refresh_from_db()
        self.assertIn("permission request", self.task.automatic_pause_reason)
        self.assertEqual(json.loads(self.run.events.get(kind="human answer").content)["answer"], {"answer": "Option A"})
        with self.assertRaisesMessage(ValueError, "no longer pending"):
            answer_question(question_id, {"answer": "Another"})
        permission.resolved = True
        permission.save(update_fields=["resolved"])
        self.assertEqual(AgentQuestion.objects.get(pk=question_id).answer, {"answer": "Option A"})

    def test_answer_rejects_stale_invalid_and_cancelled_requests(self):
        question_id = create_question(self.run.pk, "Question?", TEXT_SCHEMA, "turn")
        url = reverse("answer_agent_question", args=[question_id])
        self.assertEqual(Client(enforce_csrf_checks=True).post(url, {"answer-answer": "yes"}).status_code, 403)
        self.assertEqual(self.client.post(url, {"answer-answer": ""}).status_code, 400)
        self.run.cancel_requested = True
        self.run.save(update_fields=["cancel_requested"])
        self.assertEqual(self.client.post(url, {"answer-answer": "yes"}).status_code, 409)
        self.assertEqual(AgentQuestion.objects.get(pk=question_id).outcome, AgentQuestion.Outcome.PENDING)

    def test_invalid_schemas_are_rejected(self):
        schemas = [
            {"type": "object", "properties": {"x": {"type": "string"}}, "required": [{}]},
            {"type": "object", "properties": {"x": {"type": "array", "items": {"enum": 1, "anyOf": []}}}},
            {"type": "object", "properties": {"x": {"type": "object"}}},
            {"type": "object", "properties": {"x": {"type": "number", "enum": ["1"]}}},
            {"type": "object", "properties": {"x": {"type": "string", "$ref": "https://example.invalid"}}},
            {"type": "object", "properties": {"x": {"type": "string", "$dynamicRef": "https://example.invalid"}}},
        ]
        for schema in schemas:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                validate_question_schema(schema)
        self.assertFalse(self.run.questions.exists())

    def test_boolean_defaults_and_optional_fields(self):
        schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": False},
                "note": {"type": "string"},
            },
        }
        self.assertEqual(QuestionForm(schema).fields["enabled"].initial, "false")
        form = QuestionForm(schema, {"enabled": "false", "note": ""})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.content, {"enabled": False})

    def test_cancellation_cancels_pending_question(self):
        from ..agent import wait_for_answer

        question_id = create_question(self.run.pk, "Question?", TEXT_SCHEMA, "turn")
        self.run.cancel_requested = True
        self.run.save(update_fields=["cancel_requested"])
        self.assertIsNone(async_to_sync(wait_for_answer)(self.run.pk, question_id))
        self.assertEqual(AgentQuestion.objects.get(pk=question_id).outcome, AgentQuestion.Outcome.CANCELLED)

    def test_unsupported_elicitation_mode_is_rejected(self):
        from ..agent import RunClient

        client = RunClient(self.run.pk)
        with self.assertRaises(RequestError):
            async_to_sync(client.create_elicitation)("Question?", object())
        self.assertIn("unsupported question mode", client.elicitation_error)
        self.assertFalse(self.run.questions.exists())

    def test_answer_and_audit_event_are_atomic(self):
        question_id = create_question(self.run.pk, "Question?", TEXT_SCHEMA, "turn")
        with patch("tasks.questions.RunEvent.objects.create", side_effect=RuntimeError("storage failure")):
            with self.assertRaisesMessage(RuntimeError, "storage failure"):
                answer_question(question_id, {"answer": "yes"})
        self.assertEqual(AgentQuestion.objects.get(pk=question_id).outcome, AgentQuestion.Outcome.PENDING)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AgentRun.Status.WAITING_ANSWER)

    def test_pending_question_blocks_completion_and_reserves_endpoint(self):
        endpoint = AcpEndpoint.objects.create(name="Endpoint", port=12345, session_limit=1)
        self.run.acp_endpoint = endpoint
        self.run.save(update_fields=["acp_endpoint"])
        create_question(self.run.pk, "Question?", TEXT_SCHEMA, "turn")
        with self.assertRaises(RuntimeError):
            complete_run(self.run.pk, "end_turn")
        task = Task.objects.create(
            title="Other", request="r", stage=Task.Stage.DEVELOPER, state=Task.State.PLAN_APPROVED,
        )
        pending = create_run(
            task, stage=task.stage, task_version=task.version, request_snapshot="r", plan_snapshot="p",
        )
        self.assertEqual(pending.status, AgentRun.Status.PENDING)
        self.assertIsNone(pending.acp_endpoint)
        self.assertIsNone(claim_next_run())
        self.run.refresh_from_db()
        self.run.status = AgentRun.Status.INTERRUPTED
        self.run.save(update_fields=["status"])
        self.assertIsNone(claim_next_run())
        self.run.status = AgentRun.Status.ACKNOWLEDGED
        self.run.save(update_fields=["status"])
        claimed = claim_next_run()
        self.assertEqual((claimed.pk, claimed.acp_endpoint_id), (pending.pk, endpoint.pk))

    def test_deferred_report_can_precede_developer_completion(self):
        report = (
            'FLAMETHROWER_DEFERRED_TASKS\n{"tasks":[{"title":"Later","request":"Separate work"}]}'
            '\nEND_FLAMETHROWER_DEFERRED_TASKS\nFLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE'
        )
        self.assertEqual(parse_deferred_tasks(report), ([("Later", "Separate work")], ""))
        self.assertTrue(developer_complete(report))

    def test_ambiguous_completed_developer_run_does_not_advance(self):
        self.task.stage = Task.Stage.DEVELOPER
        self.task.state = Task.State.PLAN_APPROVED
        self.task.save(update_fields=["stage", "state"])
        self.run.stage = Task.Stage.DEVELOPER
        self.run.status = AgentRun.Status.COMPLETED
        self.run.save(update_fields=["stage", "status"])
        RunEvent.objects.create(run=self.run, kind="message", content="Which database should I use?")
        continue_automatic_run(self.run.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.DEVELOPER)
        self.assertIn("no explicit completion", self.task.automatic_pause_reason)
        self.assertTrue(developer_complete("Done.\nFLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE"))


class QuestionACPServer:
    def __init__(self, mode):
        self.mode = mode
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.socket.settimeout(10)
        self.port = self.socket.getsockname()[1]
        self.error = None
        self.answer = None
        self.prompts = []
        self.initialize = None
        self.thread = threading.Thread(target=self.serve, daemon=True)

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
                self.initialize = request
                send({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1}})
                request = read()
                send({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": "question-session"}})
                request = read()
                self.prompts.append(request["params"])
                prompt_id = request["id"]
                if self.mode == "form":
                    send({
                        "jsonrpc": "2.0", "id": 50, "method": "elicitation/create", "params": {
                            "mode": "form", "sessionId": "question-session", "message": "Which database?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"database": {"type": "string", "enum": ["sqlite", "postgres"]}},
                                "required": ["database"],
                            },
                        },
                    })
                    self.answer = read()
                else:
                    message = "Which database?" if self.mode == "ambiguous" else (
                        "FLAMETHROWER_QUESTION\nWhich database?\nEND_FLAMETHROWER_QUESTION"
                    )
                    send({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": "question-session", "update": {
                            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": message},
                        },
                    }})
                    send({"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}})
                    request = read()
                    self.prompts.append(request["params"])
                    prompt_id = request["id"]
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "question-session", "update": {
                        "sessionUpdate": "agent_message_chunk", "content": {
                            "type": "text", "text": "Done.\nFLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE",
                        },
                    },
                }})
                send({"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}})
        except Exception as error:
            self.error = error
        finally:
            self.socket.close()


class QuestionIntegrationTests(TransactionTestCase):
    @patch("tasks.automation.continue_automatic_run")
    def test_form_and_turn_questions_resume_the_same_session(self, continuation):
        from ..acp import create_run
        from ..agent import question_result as original_question_result
        from ..agent_scheduler import start_scheduler, wake_scheduler

        for index, mode in enumerate(("form", "question", "ambiguous"), start=1):
            with self.subTest(mode=mode):
                question_ready = threading.Event()
                resume_agent = threading.Event()
                abort_agent = threading.Event()
                finished = threading.Event()
                continuation.side_effect = lambda run_id: finished.set()

                @sync_to_async(thread_sensitive=True)
                def observe_question(question_id):
                    result = original_question_result.__wrapped__(question_id)
                    if result[0] == AgentQuestion.Outcome.PENDING:
                        # The shared-cache SQLite test DB rejects concurrent reads during the answer write.
                        question_ready.set()
                        if not resume_agent.wait(timeout=10) or abort_agent.is_set():
                            raise RuntimeError("Question integration test did not release the agent.")
                    return result

                server = QuestionACPServer(mode)
                server.thread.start()
                endpoint = AcpEndpoint.objects.create(name=mode, port=server.port)
                task = Task.objects.create(
                    title=mode, request="r", plan="p", state="plan_approved", stage="developer",
                    acp_selection=Task.AcpSelection.EXPLICIT, selected_acp_endpoint=endpoint,
                )
                run = create_run(
                    task, stage="developer", task_version=0,
                    request_snapshot="r", plan_snapshot="p",
                )
                with patch("tasks.agent.question_result", new=observe_question):
                    stop, scheduler = start_scheduler()
                    worker = None
                    try:
                        self.assertTrue(question_ready.wait(timeout=10), "Agent did not wait for its question.")
                        worker = next(
                            (thread for thread in threading.enumerate() if thread.name == f"agent-run-{run.pk}"),
                            None,
                        )
                        self.assertIsNotNone(worker)
                        stop.set()
                        wake_scheduler()
                        scheduler.join(timeout=10)
                        self.assertFalse(scheduler.is_alive())
                        run.refresh_from_db()
                        self.assertEqual(run.status, AgentRun.Status.WAITING_ANSWER)
                        question = AgentQuestion.objects.get(run=run, outcome="pending")
                        if mode == "ambiguous":
                            self.assertIn("Review the run activity", question.message)
                        value = {"answer-database": "postgres"} if mode == "form" else {"answer-answer": "Use PostgreSQL"}
                        self.assertEqual(self.client.post(reverse("answer_agent_question", args=[question.pk]), value).status_code, 302)
                        resume_agent.set()
                        self.assertTrue(finished.wait(timeout=10), "Agent did not finish its run.")
                        worker.join(timeout=10)
                        self.assertFalse(worker.is_alive())
                        run.refresh_from_db()
                        self.assertEqual(run.status, AgentRun.Status.COMPLETED, run.error)
                        server.thread.join(timeout=5)
                        self.assertFalse(server.thread.is_alive())
                        self.assertIsNone(server.error)
                        self.assertIn("elicitation", server.initialize["params"]["clientCapabilities"])
                        if mode == "form":
                            self.assertEqual(server.answer["result"], {"action": "accept", "content": {"database": "postgres"}})
                            self.assertEqual(len(server.prompts), 1)
                        else:
                            self.assertEqual(len(server.prompts), 2)
                            self.assertEqual(server.prompts[1]["sessionId"], "question-session")
                            self.assertIn("Use PostgreSQL", server.prompts[1]["prompt"][0]["text"])
                        self.assertEqual(continuation.call_count, index)
                        continuation.assert_called_with(run.pk)
                    finally:
                        abort_agent.set()
                        resume_agent.set()
                        stop.set()
                        wake_scheduler()
                        scheduler.join(timeout=10)
                        if worker is None:
                            worker = next(
                                (thread for thread in threading.enumerate() if thread.name == f"agent-run-{run.pk}"),
                                None,
                            )
                        if worker:
                            worker.join(timeout=10)
                        server.socket.close()
                        server.thread.join(timeout=10)
        self.assertEqual(continuation.call_count, 3)
