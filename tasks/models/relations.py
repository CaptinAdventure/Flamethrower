from django.db import models, transaction
from django.db.models import F, Q

from .task import Task


class LinkType(models.Model):
    name = models.CharField(max_length=100, unique=True)
    outward_description = models.CharField(max_length=100)
    inward_description = models.CharField(max_length=100)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

class TaskLink(models.Model):
    link_type = models.ForeignKey(LinkType, on_delete=models.PROTECT, related_name="task_links")
    source = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="outgoing_links")
    target = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="incoming_links")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(source=F("target")), name="task_link_no_self"),
            models.UniqueConstraint(fields=["link_type", "source", "target"], name="task_link_unique"),
        ]

    def record_event(self, action):
        TaskLinkEvent.objects.create(
            source=self.source, target=self.target, action=action,
            source_title=self.source.title, target_title=self.target.title,
            outward_description=self.link_type.outward_description,
            inward_description=self.link_type.inward_description,
        )

    def save(self, *args, **kwargs):
        creating = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if creating:
                self.record_event(TaskLinkEvent.Action.LINKED)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            self.record_event(TaskLinkEvent.Action.UNLINKED)
            return super().delete(*args, **kwargs)


class TaskLinkEvent(models.Model):
    class Action(models.TextChoices):
        LINKED = "linked", "Linked"
        UNLINKED = "unlinked", "Unlinked"

    source = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="outgoing_link_events")
    target = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="incoming_link_events")
    source_title = models.CharField(max_length=200)
    target_title = models.CharField(max_length=200)
    outward_description = models.CharField(max_length=100)
    inward_description = models.CharField(max_length=100)
    action = models.CharField(max_length=16, choices=Action.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class SimilaritySettings(models.Model):
    threshold = models.FloatField(default=0.75)
    max_results = models.PositiveSmallIntegerField(default=5)
    max_tokens = models.PositiveSmallIntegerField(default=512)

    class Meta:
        verbose_name = "similarity settings"
        verbose_name_plural = "similarity settings"

    def __str__(self):
        return "Similarity settings"


class RepositorySettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    allow_safe_operations = models.BooleanField(default=False)
    safe_read_paths = models.JSONField(default=list, blank=True)
    allowed_command_prefixes = models.JSONField(default=list, blank=True)
    allowed_mcp_tools = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(pk=1), name="repository_settings_singleton"),
        ]

    def __str__(self):
        return "Repository settings"


class DismissedSuggestion(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="dismissed_suggestions")
    candidate = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="+")
    task_revision = models.PositiveIntegerField()
    candidate_revision = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "candidate"], name="unique_dismissed_suggestion"),
        ]
