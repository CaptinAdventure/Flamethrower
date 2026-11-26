from django.db import models, transaction
from django.db.models import Max, Subquery, Value
from django.db.models.functions import Coalesce

from .work_type import default_work_type_id


class Task(models.Model):
    class AcpSelection(models.TextChoices):
        AUTO = "auto", "Automatic — highest-priority available ACP"
        EXPLICIT = "explicit", "Use selected ACP"

    class Status(models.TextChoices):
        NON_ACCEPTED = "non_accepted", "Non-Accepted"
        READY = "ready", "Ready"
        BLOCKED = "blocked", "Blocked"
        ABANDONED = "abandoned", "Abandoned"
        DONE = "done", "Done"

    class Severity(models.TextChoices):
        BLOCKER = "blocker", "Blocker"
        CRITICAL = "critical", "Critical"
        MAJOR = "major", "Major"
        MINOR = "minor", "Minor"
        TRIVIAL = "trivial", "Trivial"

    class IndexingStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"

    class State(models.TextChoices):
        DRAFT = "draft", "Planning"
        SCOPE_APPROVED = "scope_approved", "Planning"
        PLAN_APPROVED = "plan_approved", "Plan approved"

    class Stage(models.TextChoices):
        DRAFT = "draft", "Draft"
        ARCHITECT = "architect", "Architect"
        DEVELOPER = "developer", "Developer"
        TESTING = "testing", "Testing"
        REVIEW = "review", "Review"
        FINISHED = "finished", "Finished"

    title = models.CharField(max_length=200)
    work_type = models.ForeignKey("tasks.WorkType", on_delete=models.PROTECT, default=default_work_type_id)
    priority = models.PositiveIntegerField(default=0)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.MAJOR)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.READY)
    blocked_reason = models.TextField(blank=True)
    request = models.TextField()
    plan = models.TextField(blank=True)
    state = models.CharField(max_length=32, choices=State.choices, default=State.DRAFT)
    stage = models.CharField(max_length=16, choices=Stage.choices, default=Stage.DRAFT)
    stage_version = models.PositiveIntegerField(default=0)
    work_cycle = models.PositiveIntegerField(default=0)
    rework_note = models.TextField(blank=True)
    finished = models.BooleanField(default=False)
    automatic_pause_reason = models.TextField(blank=True)
    automatic_gate_returns = models.JSONField(default=dict, blank=True)
    acp_selection = models.CharField(max_length=16, choices=AcpSelection.choices, default=AcpSelection.AUTO)
    selected_acp_endpoint = models.ForeignKey(
        "tasks.AcpEndpoint", null=True, blank=True, on_delete=models.PROTECT, related_name="selected_tasks",
    )
    version = models.PositiveIntegerField(default=0)
    relates_indexing = models.CharField(max_length=16, choices=IndexingStatus.choices, default=IndexingStatus.PENDING)
    indexing_revision = models.PositiveIntegerField(default=0)
    indexing_error = models.TextField(blank=True)
    embedding = models.JSONField(null=True, blank=True)
    suggested_work_type = models.ForeignKey(
        "tasks.WorkType", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    work_type_suggestion_signature = models.CharField(max_length=64, blank=True)
    work_type_suggestion_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(acp_selection="auto", selected_acp_endpoint__isnull=True)
                    | models.Q(acp_selection="explicit", selected_acp_endpoint__isnull=False)
                ),
                name="task_acp_selection_consistent",
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding and not self.priority:
            with transaction.atomic():
                self.priority = next_priority(self.stage)
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)

def next_priority(stage, exclude_id=None):
    tasks = Task.objects.filter(stage=stage)
    if exclude_id is not None:
        tasks = tasks.exclude(pk=exclude_id)
    return (tasks.aggregate(value=Max("priority"))["value"] or 0) + 1

def next_priority_expression(stage, exclude_id):
    priorities = Task.objects.filter(stage=stage).exclude(pk=exclude_id).order_by("-priority").values("priority")
    return Coalesce(Subquery(priorities[:1]), Value(0)) + Value(1)
