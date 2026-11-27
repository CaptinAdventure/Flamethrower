import json
import socket
import threading
import time
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase

from ..architect_tasks import LINK, QUERY, architect_task_context, handle_task_block, parse_task_block
from ..models import AcpEndpoint, AgentRun, LinkType, Task, TaskLink, TaskLinkEvent
from ..recommendations import architect_plan, final_agent_report


def block(marker, items):
    return f"{marker}\n{json.dumps(items)}\nEND_{marker}"


class TaskProtocolParsingTests(SimpleTestCase):
    def test_marker_lines_and_invalid_blocks(self):
        self.assertEqual(parse_task_block(f"Mention `{QUERY}` in prose."), (None, None, ""))
        text = f"Mention `{QUERY}` in prose.\r\n" + block(QUERY, [{"queryId": 2}]).replace("\n", "\r\n")
        self.assertEqual(parse_task_block(text), (QUERY, [{"queryId": 2}], ""))
        invalid = [
            block(QUERY, [{"queryId": True}]),
            block(QUERY, [{"queryId": 2**64}]),
            block(QUERY, []),
            block(QUERY, [{"queryId": 1}] * 21),
            block(QUERY, [{"queryId": 1, "extra": 1}]),
            f'{QUERY}\n[{{"queryId": 1, "queryId": 2}}]\nEND_{QUERY}',
            block(QUERY, [{"queryId": 1}]) + "\n" + block(QUERY, [{"queryId": 2}]),
            block(QUERY, [{"queryId": 1}]) + "\n" + block(LINK, [{"destinationTaskId": 2, "linkType": "blocks"}]),
            block(QUERY, [{"queryId": 1}]) + "\nmore text",
            "FLAMETHROWER_QUESTION\nQuestion?\nEND_FLAMETHROWER_QUESTION\n"
            + block(LINK, [{"destinationTaskId": 2, "linkType": "blocks"}]),
        ]
        for text in invalid:
            with self.subTest(text=text):
                self.assertTrue(parse_task_block(text)[2])


class ArchitectTaskProtocolTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(title="Current", request="Current request", stage=Task.Stage.ARCHITECT)
        self.other = Task.objects.create(title="Other", request="Other request", plan="Other plan")
        self.done = Task.objects.create(title="Done", request="Done request", finished=True)
        self.abandoned = Task.objects.create(title="Abandoned", request="Abandoned request", status=Task.Status.ABANDONED)
        self.run = AgentRun.objects.create(
            task=self.task, stage=Task.Stage.ARCHITECT, status=AgentRun.Status.RUNNING,
            task_version=self.task.version, request_snapshot=self.task.request, plan_snapshot="",
        )

    def test_context_and_query_include_correct_tasks_and_relationships(self):
        context = architect_task_context(self.run.pk)
        data = json.loads(context.split("\n")[3])
        self.assertEqual({item["id"] for item in data["unfinishedTasks"]}, {self.other.pk, self.abandoned.pk})
        self.assertTrue(any(item["inward"] == "is blocked by" for item in data["linkTypes"]))
        TaskLink.objects.create(link_type=LinkType.objects.get(name="Blocks"), source=self.done, target=self.other)
        response = handle_task_block(self.run.pk, block(QUERY, [
            {"queryId": self.other.pk}, {"queryId": self.done.pk}, {"queryId": self.task.pk}, {"queryId": 999999},
        ]))
        result = json.loads(response.split("\n")[1])["results"]
        self.assertEqual(result[0]["request"], "Other request")
        self.assertEqual(result[0]["plan"], "Other plan")
        self.assertEqual(result[0]["links"], [{"taskId": self.done.pk, "linkType": "is blocked by"}])
        self.assertTrue(result[1]["finished"])
        self.assertIn("error", result[2])
        self.assertIn("error", result[3])

    def test_links_use_perspective_are_audited_and_idempotent(self):
        items = [
            {"destinationTaskId": self.other.pk, "linkType": "is blocked by"},
            {"destinationTaskId": self.done.pk, "linkType": "relates to"},
            {"destinationTaskId": self.task.pk, "linkType": "blocks"},
            {"destinationTaskId": 999999, "linkType": "blocks"},
            {"destinationTaskId": self.other.pk, "linkType": "Blocks"},
        ]
        response = handle_task_block(self.run.pk, block(LINK, items))
        results = json.loads(response.split("\n")[1])["results"]
        self.assertEqual([item.get("status") for item in results[:2]], ["created", "created"])
        self.assertTrue(all("error" in item for item in results[2:]))
        link = TaskLink.objects.get(link_type__name="Blocks")
        self.assertEqual((link.source_id, link.target_id), (self.other.pk, self.task.pk))
        self.assertEqual(TaskLinkEvent.objects.count(), 2)
        repeat = handle_task_block(self.run.pk, block(LINK, items[:2]))
        self.assertEqual([item["status"] for item in json.loads(repeat.split("\n")[1])["results"]],
                         ["already exists", "already exists"])
        self.assertEqual(TaskLinkEvent.objects.count(), 2)
        self.assertEqual(self.run.events.filter(kind="task protocol").count(), 2)

    def test_invalid_or_ambiguous_link_never_creates_anything(self):
        LinkType.objects.create(name="Ambiguous", outward_description="blocks", inward_description="other")
        response = handle_task_block(self.run.pk, block(LINK, [
            {"destinationTaskId": self.other.pk, "linkType": "blocks"},
        ]))
        self.assertIn("ambiguous", response)
        response = handle_task_block(self.run.pk, block(LINK, [
            {"destinationTaskId": self.other.pk, "linkType": "is blocked by"}, {"destinationTaskId": False},
        ]))
        self.assertIn("error", response)
        self.assertFalse(TaskLink.objects.exists())

    def test_stale_cancelled_and_failed_audit_do_not_create_links(self):
        text = block(LINK, [{"destinationTaskId": self.other.pk, "linkType": "is blocked by"}])
        self.task.version += 1
        self.task.save(update_fields=["version"])
        with self.assertRaises(RuntimeError):
            handle_task_block(self.run.pk, text)
        self.task.version -= 1
        self.task.save(update_fields=["version"])
        self.run.cancel_requested = True
        self.run.save(update_fields=["cancel_requested"])
        with self.assertRaises(RuntimeError):
            handle_task_block(self.run.pk, text)
        self.run.cancel_requested = False
        self.run.save(update_fields=["cancel_requested"])
        with patch("tasks.architect_tasks.RunEvent.objects.create", side_effect=RuntimeError("audit failure")):
            with self.assertRaisesMessage(RuntimeError, "audit failure"):
                handle_task_block(self.run.pk, text)
        self.assertFalse(TaskLink.objects.exists())
        self.assertFalse(TaskLinkEvent.objects.exists())


class TaskProtocolACPServer:
    def __init__(self, other_id):
        self.other_id = other_id
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.socket.settimeout(10)
        self.port = self.socket.getsockname()[1]
        self.prompts = []
        self.error = None
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        try:
            conn, _ = self.socket.accept()
            with conn, conn.makefile("rwb") as stream:
                conn.settimeout(10)

                def read():
                    return json.loads(stream.readline())

                def send(data):
                    stream.write((json.dumps(data) + "\n").encode())
                    stream.flush()

                request = read()
                send({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1}})
                request = read()
                send({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": "task-session"}})
                messages = [
                    block(QUERY, [{"queryId": self.other_id}, {"queryId": 999999}]),
                    block(LINK, [{"destinationTaskId": self.other_id, "linkType": "unknown"}]),
                    block(LINK, [{"destinationTaskId": self.other_id, "linkType": "is blocked by"}]),
                    "FLAMETHROWER_ARCHITECT_PLAN\nProposed design\nEND_FLAMETHROWER_ARCHITECT_PLAN",
                ]
                for message in messages:
                    request = read()
                    self.prompts.append(request["params"])
                    send({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": "task-session", "update": {
                            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": message},
                        },
                    }})
                    send({"jsonrpc": "2.0", "id": request["id"], "result": {"stopReason": "end_turn"}})
        except Exception as error:
            self.error = error
        finally:
            self.socket.close()


class ArchitectTaskProtocolIntegrationTests(TransactionTestCase):
    @patch("tasks.automation.continue_automatic_run")
    def test_query_link_and_plan_continue_same_session(self, continuation):
        from ..acp import create_run
        from ..agent_scheduler import start_scheduler

        other = Task.objects.create(title="Dependency", request="Dependency details")
        task = Task.objects.create(title="Architect", request="Request", stage=Task.Stage.ARCHITECT)
        LinkType.objects.get_or_create(
            name="Blocks", defaults={"outward_description": "blocks", "inward_description": "is blocked by"},
        )
        server = TaskProtocolACPServer(other.pk)
        server.thread.start()
        endpoint = AcpEndpoint.objects.create(name="Endpoint", port=server.port)
        run = create_run(
            task, stage=Task.Stage.ARCHITECT, task_version=task.version,
            request_snapshot=task.request, plan_snapshot="",
        )
        prepared = run.initial_prompt
        other.title = "Renamed after preparation"
        other.save(update_fields=["title"])
        stop, thread = start_scheduler()
        self.addCleanup(thread.join)
        self.addCleanup(stop.set)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run.refresh_from_db()
            if run.status in (AgentRun.Status.COMPLETED, AgentRun.Status.INTERRUPTED):
                break
            time.sleep(0.05)
        server.thread.join(timeout=5)
        self.assertFalse(server.thread.is_alive())
        self.assertIsNone(server.error)
        self.assertEqual(run.status, AgentRun.Status.COMPLETED, run.error)
        self.assertEqual(len(server.prompts), 4)
        self.assertTrue(all(item["sessionId"] == "task-session" for item in server.prompts))
        prompts = [item["prompt"][0]["text"] for item in server.prompts]
        self.assertEqual(prompts[0], prepared)
        self.assertIn("Dependency", prompts[0])
        self.assertNotIn("Renamed after preparation", prompts[0])
        self.assertIn("Dependency details", prompts[1])
        self.assertIn("Task not found", prompts[1])
        self.assertIn("unknown or ambiguous", prompts[2])
        self.assertIn('"status": "created"', prompts[3])
        self.assertFalse(run.questions.exists())
        self.assertEqual(architect_plan(final_agent_report(list(run.events.all()))["text"]), "Proposed design")
        self.assertTrue(TaskLink.objects.filter(source=other, target=task, link_type__name="Blocks").exists())
        while time.monotonic() < deadline and not continuation.called:
            time.sleep(0.05)
        continuation.assert_called_once_with(run.pk)
