from django.apps import AppConfig
from django.db import connections
from django.db.models.signals import post_migrate


def seed_work_types(sender, using, **kwargs):
    from .models.work_type import DEFAULT_WORK_TYPES, WorkType

    if WorkType._meta.db_table not in connections[using].introspection.table_names():
        return
    work_types = WorkType.objects.using(using)
    if not work_types.exists():
        work_types.bulk_create([
            WorkType(title=title, icon=icon, description=description, is_default=title == "Task")
            for title, icon, description in DEFAULT_WORK_TYPES
        ])


class TasksConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tasks'

    def ready(self):
        post_migrate.connect(seed_work_types, sender=self, dispatch_uid="tasks.seed_work_types")
