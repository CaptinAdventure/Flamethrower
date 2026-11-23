import ipaddress

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from .task import Task


def validate_acp_host(value):
    if value == "localhost":
        return
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValidationError("ACP hosts must be loopback IP addresses or localhost.") from error
    if not address.is_loopback:
        raise ValidationError("ACP hosts must be loopback IP addresses or localhost.")


class AcpEndpoint(models.Model):
    name = models.CharField(max_length=100, unique=True)
    host = models.CharField(max_length=255, default="127.0.0.1", validators=[validate_acp_host])
    port = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(65535)])
    priority = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "id"]
        indexes = [
            models.Index(fields=["enabled", "priority", "id"], name="acp_enabled_priority_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(port__gte=1, port__lte=65535),
                name="acp_port_valid_range",
            ),
        ]

    @property
    def address(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    def __str__(self):
        return f"{self.name} ({self.address})"


class Approval(models.Model):
    class Kind(models.TextChoices):
        SCOPE = "scope", "Scope"
        PLAN = "plan", "Plan"
        COMBINED = "combined", "Request and plan"
        FINISH = "finish", "Task finished"
        STAGE = "stage", "Stage transition"

    task = models.ForeignKey(Task, related_name="approvals", on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

class AgentRun(models.Model):
    class Status(models.TextChoices):
        STARTING = "starting", "Connecting"
        RUNNING = "running", "Running"
        WAITING = "waiting", "Waiting for permission"
        WAITING_ANSWER = "waiting_answer", "Waiting for human answer"
        COMPLETED = "completed", "Agent turn complete"
        INTERRUPTED = "interrupted", "Interrupted — outcome unverified"
        ACKNOWLEDGED = "acknowledged", "Interruption acknowledged"

    ACTIVE = (Status.STARTING, Status.RUNNING, Status.WAITING, Status.WAITING_ANSWER)
    task = models.ForeignKey(Task, related_name="runs", on_delete=models.CASCADE)
    acp_endpoint = models.ForeignKey(
        AcpEndpoint, null=True, blank=True, on_delete=models.PROTECT, related_name="runs",
    )
    stage = models.CharField(max_length=16, choices=Task.Stage.choices, default=Task.Stage.DEVELOPER)
    work_cycle = models.PositiveIntegerField(default=0)
    task_version = models.PositiveIntegerField()
    request_snapshot = models.TextField()
    plan_snapshot = models.TextField()
    context_snapshot = models.TextField(blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.STARTING)
    session_id = models.CharField(max_length=255, blank=True)
    stop_reason = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    deferred_tasks_warning = models.TextField(blank=True)
    cancel_requested = models.BooleanField(default=False)
    automation_processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["acp_endpoint"],
                condition=Q(status__in=("starting", "running", "waiting", "waiting_answer", "interrupted")),
                name="one_active_run_per_acp",
            ),
        ]
        indexes = [
            models.Index(fields=["acp_endpoint", "status"], name="run_acp_status_idx"),
        ]


class ACPUsageSnapshot(models.Model):
    run = models.OneToOneField(AgentRun, related_name="usage_snapshot", on_delete=models.CASCADE)
    cost_amount = models.DecimalField(max_digits=30, decimal_places=12, null=True, blank=True)
    cost_currency = models.CharField(max_length=3, null=True, blank=True)
    context_used = models.PositiveBigIntegerField(null=True, blank=True)
    context_size = models.PositiveBigIntegerField(null=True, blank=True)
    total_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    output_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    thought_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    cached_read_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    cached_write_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    context_reported_at = models.DateTimeField(null=True, blank=True)
    cost_reported_at = models.DateTimeField(null=True, blank=True)
    tokens_reported_at = models.DateTimeField(null=True, blank=True)


class DeferredTask(models.Model):
    source_run = models.ForeignKey(AgentRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="deferred_tasks")
    source_task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="deferred_followups")
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name="deferred_origin")
    position = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_run", "position"], name="unique_deferred_task_position"),
        ]
        ordering = ["position"]

class GateDecision(models.Model):
    class Result(models.TextChoices):
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        UNVERIFIED = "unverified", "NOT_YET_VERIFIED"

    task = models.ForeignKey(Task, related_name="gate_decisions", on_delete=models.CASCADE)
    source_run = models.ForeignKey(AgentRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="accepted_decisions")
    stage = models.CharField(max_length=16, choices=Task.Stage.choices)
    task_version = models.PositiveIntegerField()
    work_cycle = models.PositiveIntegerField(default=0)
    result = models.CharField(max_length=16, choices=Result.choices)
    evidence = models.TextField()
    waiver = models.TextField(blank=True)
    advanced = models.BooleanField(default=False)
    automatic = models.BooleanField(default=False)
    recorded_by = models.CharField(max_length=16, default="human")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

class RunEvent(models.Model):
    run = models.ForeignKey(AgentRun, related_name="events", on_delete=models.CASCADE)
    kind = models.CharField(max_length=64)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

class PermissionRequest(models.Model):
    run = models.ForeignKey(AgentRun, related_name="permissions", on_delete=models.CASCADE)
    tool_call = models.JSONField()
    options = models.JSONField()
    selected_option = models.CharField(max_length=255, blank=True)
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class AgentQuestion(models.Model):
    class Outcome(models.TextChoices):
        PENDING = "pending", "Pending"
        ANSWERED = "answered", "Answered"
        CANCELLED = "cancelled", "Cancelled"

    run = models.ForeignKey(AgentRun, related_name="questions", on_delete=models.CASCADE)
    message = models.TextField()
    schema = models.JSONField()
    source = models.CharField(max_length=16)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.PENDING)
    answer = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)
