import asyncio
import json
import socketserver
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Thread, get_ident

from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from ..acp import AcpUnavailable, create_run, endpoint_status, task_acp_state
from ..agent import run_agent
from ..forms import AcpEndpointForm
from ..models import AcpEndpoint, AgentRun, Task


class AcpCapacityTests(TestCase):
    def setUp(self):
        self.endpoint = AcpEndpoint.objects.create(name="Primary", port=12345)

    def task(self, endpoint=None):
        values = {}
        if endpoint:
            values = {"acp_selection": Task.AcpSelection.EXPLICIT, "selected_acp_endpoint": endpoint}
        return Task.objects.create(title="Task", request="Request", **values)

    def start(self, task):
        return create_run(task, task_version=task.version, request_snapshot="r", plan_snapshot="p")

    def test_default_capacity_and_automatic_fallback(self):
        self.assertEqual(self.endpoint.session_limit, 5)
        backup = AcpEndpoint.objects.create(name="Backup", port=12346, priority=2, session_limit=1)
        runs = [self.start(self.task()) for _ in range(5)]
        self.assertTrue(all(run.acp_endpoint_id == self.endpoint.pk for run in runs))
        self.assertEqual(self.start(self.task()).acp_endpoint, backup)
        with self.assertRaisesMessage(AcpUnavailable, "No enabled ACP is currently available"):
            self.start(self.task())
        self.assertTrue(task_acp_state(self.task())["busy"])

    def test_waiting_and_interrupted_reserve_capacity_until_resolved(self):
        self.endpoint.session_limit = 2
        self.endpoint.save(update_fields=["session_limit"])
        first = self.start(self.task(self.endpoint))
        first.status = AgentRun.Status.WAITING_ANSWER
        first.save(update_fields=["status"])
        second = self.start(self.task(self.endpoint))
        second.status = AgentRun.Status.INTERRUPTED
        second.save(update_fields=["status"])
        waiting_task = self.task(self.endpoint)
        self.assertTrue(task_acp_state(waiting_task)["busy"])
        self.assertEqual(endpoint_status(self.endpoint, 2), "Busy")
        with self.assertRaisesMessage(AcpUnavailable, "selected ACP is busy"):
            self.start(waiting_task)
        first.status = AgentRun.Status.COMPLETED
        first.save(update_fields=["status"])
        self.assertFalse(task_acp_state(waiting_task)["busy"])
        self.assertEqual(self.start(waiting_task).acp_endpoint, self.endpoint)

    def test_reducing_limit_preserves_runs_and_blocks_new_ones(self):
        runs = [self.start(self.task(self.endpoint)) for _ in range(3)]
        self.endpoint.session_limit = 1
        self.endpoint.save(update_fields=["session_limit"])
        waiting = self.task(self.endpoint)
        with self.assertRaises(AcpUnavailable):
            self.start(waiting)
        AgentRun.objects.filter(pk__in=[run.pk for run in runs]).update(status=AgentRun.Status.COMPLETED)
        self.assertEqual(self.start(waiting).acp_endpoint, self.endpoint)

    def test_cannot_start_a_second_active_run_for_the_same_task(self):
        task = self.task()
        self.start(task)
        with self.assertRaisesMessage(AcpUnavailable, "already has an active run"):
            self.start(task)

    def test_limit_form_and_database_require_a_positive_value(self):
        values = {"name": "ACP", "host": "127.0.0.1", "port": 12345, "priority": 1, "session_limit": 0}
        form = AcpEndpointForm(values)
        self.assertFalse(form.is_valid())
        self.assertIn("session_limit", form.errors)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AcpEndpoint.objects.filter(pk=self.endpoint.pk).update(session_limit=0)
        values["session_limit"] = 3
        form = AcpEndpointForm(values, instance=self.endpoint)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.endpoint.refresh_from_db()
        self.assertEqual(self.endpoint.session_limit, 3)


class AcpCapacityTransactionTests(TransactionTestCase):
    def test_two_sessions_run_concurrently_on_one_endpoint(self):
        barrier = Barrier(2)

        class Handler(socketserver.StreamRequestHandler):
            def send(self, payload):
                self.wfile.write((json.dumps(payload) + "\n").encode())
                self.wfile.flush()

            def handle(self):
                request = json.loads(self.rfile.readline())
                self.send({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1}})
                request = json.loads(self.rfile.readline())
                session_id = str(get_ident())
                self.send({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": session_id}})
                request = json.loads(self.rfile.readline())
                barrier.wait(timeout=10)
                self.send({
                    "jsonrpc": "2.0", "method": "session/update",
                    "params": {"sessionId": session_id, "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE"},
                    }},
                })
                self.send({"jsonrpc": "2.0", "id": request["id"], "result": {"stopReason": "end_turn"}})

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        with Server(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                endpoint = AcpEndpoint.objects.create(name="Primary", port=server.server_address[1], session_limit=2)
                tasks = [Task.objects.create(title=str(index), request="r") for index in range(2)]
                runs = [
                    create_run(task, task_version=0, request_snapshot="r", plan_snapshot="p")
                    for task in tasks
                ]

                def execute(run):
                    try:
                        asyncio.run(run_agent(run.pk, "r", "p", endpoint.pk, endpoint.host, endpoint.port))
                    finally:
                        close_old_connections()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(execute, runs))
                for run in runs:
                    run.refresh_from_db()
                    self.assertEqual(run.status, AgentRun.Status.COMPLETED, run.error)
                self.assertEqual(len({run.session_id for run in runs}), 2)
            finally:
                server.shutdown()
                thread.join(timeout=10)

    def test_concurrent_claims_do_not_exceed_capacity(self):
        endpoint = AcpEndpoint.objects.create(name="Primary", port=12345, session_limit=1)
        tasks = [Task.objects.create(title=str(index), request="r") for index in range(2)]
        barrier = Barrier(2)

        def claim(task):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    return create_run(task, task_version=0, request_snapshot="r", plan_snapshot="p").pk
                except AcpUnavailable:
                    return None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, tasks))
        self.assertLessEqual(sum(result is not None for result in results), 1)
        self.assertLessEqual(AgentRun.objects.filter(acp_endpoint=endpoint).count(), 1)
        if not any(results):
            run = create_run(tasks[0], task_version=0, request_snapshot="r", plan_snapshot="p")
            self.assertEqual(run.acp_endpoint_id, endpoint.pk)

    def test_existing_endpoint_and_run_survive_capacity_migration(self):
        old = [("tasks", "0001_initial")]
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(old)
            apps = executor.loader.project_state(old).apps
            endpoint = apps.get_model("tasks", "AcpEndpoint").objects.create(name="Existing", port=12345)
            task = apps.get_model("tasks", "Task").objects.create(title="Existing task", request="r")
            run = apps.get_model("tasks", "AgentRun").objects.create(
                task=task, acp_endpoint=endpoint, task_version=0, request_snapshot="r", plan_snapshot="p",
            )
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            apps = executor.loader.project_state(latest).apps
            self.assertEqual(apps.get_model("tasks", "AcpEndpoint").objects.get(pk=endpoint.pk).session_limit, 5)
            self.assertEqual(apps.get_model("tasks", "AgentRun").objects.get(pk=run.pk).acp_endpoint_id, endpoint.pk)
        finally:
            MigrationExecutor(connection).migrate(latest)
