from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings


class InitialMigrationTests(TransactionTestCase):
    def test_fresh_database_without_configured_endpoint(self):
        initial = [("tasks", "0001_initial")]
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate([("tasks", None)])
            with override_settings(ACP_PORT=""):
                executor = MigrationExecutor(connection)
                executor.migrate(initial)
            apps = executor.loader.project_state(initial).apps
            self.assertFalse(apps.get_model("tasks", "AcpEndpoint").objects.exists())
        finally:
            MigrationExecutor(connection).migrate([("tasks", None)])
            MigrationExecutor(connection).migrate(latest)

    def test_fresh_database_has_defaults_and_configured_endpoint(self):
        initial = [("tasks", "0001_initial")]
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate([("tasks", None)])
            with override_settings(ACP_PORT="4312"):
                executor = MigrationExecutor(connection)
                executor.migrate(initial)
            apps = executor.loader.project_state(initial).apps

            work_types = apps.get_model("tasks", "WorkType").objects
            self.assertEqual(
                list(work_types.values_list("title", flat=True)),
                ["Bug", "Enabler", "Story", "Improvement", "Task", "Sub-task",
                 "Test", "Test Plan", "Test Execution", "Event"],
            )
            self.assertFalse(work_types.filter(description="").exists())
            self.assertEqual(work_types.get(is_default=True).title, "Task")
            self.assertEqual(apps.get_model("tasks", "LinkType").objects.count(), 4)
            self.assertTrue(apps.get_model("tasks", "RepositorySettings").objects.filter(pk=1).exists())
            endpoint = apps.get_model("tasks", "AcpEndpoint").objects.get(name="Default ACP")
            self.assertEqual((endpoint.host, endpoint.port, endpoint.enabled), ("127.0.0.1", 4312, True))
        finally:
            MigrationExecutor(connection).migrate([("tasks", None)])
            MigrationExecutor(connection).migrate(latest)
