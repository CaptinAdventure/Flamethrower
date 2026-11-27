import asyncio
import logging
import threading

from django.db import close_old_connections, connection, transaction
from django.db.models import F
from django.utils import timezone

from .acp import RESERVED_RUN_STATUSES
from .agent import RECOVERY_PAUSE_REASONS, run_agent
from .models import AcpEndpoint, AgentQuestion, AgentRun, RunEvent, Task
from .questions import QUESTION_PAUSE

logger = logging.getLogger(__name__)
_wake = threading.Event()


def wake_scheduler():
    _wake.set()


def recover_interrupted_runs():
    with transaction.atomic():
        AgentRun.objects.filter(status__in=AgentRun.EXECUTING).update(
            status=AgentRun.Status.INTERRUPTED,
            error="Application stopped during this run. The agent may still be working; inspect before retrying.",
            finished_at=timezone.now(),
        )
        AgentRun.objects.filter(status=AgentRun.Status.INTERRUPTED, cancel_requested=False).exclude(
            session_id="",
        ).update(recovery_pending=True)
        questions = AgentQuestion.objects.filter(
            outcome=AgentQuestion.Outcome.PENDING, run__status=AgentRun.Status.INTERRUPTED,
        )
        affected_tasks = list(questions.values_list("run__task_id", flat=True))
        questions.update(outcome=AgentQuestion.Outcome.CANCELLED)
        Task.objects.filter(pk__in=affected_tasks, automatic_pause_reason=QUESTION_PAUSE).exclude(
            runs__questions__outcome=AgentQuestion.Outcome.PENDING,
        ).update(automatic_pause_reason="", updated_at=timezone.now())


def claim_next_recovery():
    if not AgentRun.objects.filter(
        status=AgentRun.Status.INTERRUPTED, recovery_pending=True,
    ).exists():
        return None
    with transaction.atomic():
        if connection.vendor == "sqlite":
            AgentRun.objects.filter(status=AgentRun.Status.INTERRUPTED, recovery_pending=True).update(
                recovery_pending=F("recovery_pending"),
            )
        runs = AgentRun.objects.select_for_update().filter(
            status=AgentRun.Status.INTERRUPTED, recovery_pending=True,
        ).select_related("task", "acp_endpoint").order_by("id")
        for run in runs:
            task = run.task
            reason = ""
            if run.cancel_requested or not run.session_id:
                reason = "The run was cancelled or has no recorded ACP session."
            elif not run.acp_endpoint or not run.acp_endpoint.enabled:
                reason = "The run's ACP endpoint is not available."
            elif (run.acp_host, run.acp_port) != (run.acp_endpoint.host, run.acp_endpoint.port):
                reason = "The original ACP address was not recorded or has changed."
            elif run.permissions.filter(resolved=False).exists():
                reason = "An ACP permission was pending when the run stopped; inspect it before continuing."
            elif task is not None and (
                task.finished or task.status != Task.Status.READY or task.stage != run.stage
                or task.version != run.task_version or task.work_cycle != run.work_cycle
                or task.runs.order_by("-id").values_list("pk", flat=True).first() != run.pk
                or task.state != (Task.State.SCOPE_APPROVED if run.stage == Task.Stage.ARCHITECT else Task.State.PLAN_APPROVED)
            ):
                reason = "The task has changed since this run started."
            elif task is not None and task.automatic_pause_reason not in RECOVERY_PAUSE_REASONS:
                reason = "The task is paused for another reason."
            elif task is None and run.stage != AgentRun.PROJECT:
                reason = "The run does not have a valid project scope."
            if reason:
                run.recovery_pending = False
                run.save(update_fields=["recovery_pending"])
                RunEvent.objects.create(run=run, kind="status", content=f"Automatic recovery skipped: {reason}")
                continue
            run.status = AgentRun.Status.STARTING
            run.recovery_pending = False
            run.save(update_fields=["status", "recovery_pending"])
            return run
    return None


def claim_next_run():
    if not AgentRun.objects.filter(status=AgentRun.Status.PENDING, cancel_requested=False).exists():
        return None
    with transaction.atomic():
        if connection.vendor == "sqlite":
            AcpEndpoint.objects.update(priority=F("priority"))
        endpoints = list(AcpEndpoint.objects.select_for_update().filter(
            enabled=True, last_error="",
        ).order_by("priority", "id"))
        for run in AgentRun.objects.select_for_update().filter(
            status=AgentRun.Status.PENDING, cancel_requested=False,
        ).select_related("task").order_by("id"):
            task = run.task
            if task is not None and task.automatic_pause_reason:
                continue
            if (
                run.initial_prompt is None or (task is None and run.stage != AgentRun.PROJECT)
                or (task is not None and (
                    task.finished or task.status != Task.Status.READY or task.stage != run.stage
                    or task.version != run.task_version or task.work_cycle != run.work_cycle
                ))
            ):
                run.status = AgentRun.Status.CANCELLED
                run.error = "Pending run was not dispatched because its prompt or task state is no longer valid."
                run.finished_at = timezone.now()
                run.save(update_fields=["status", "error", "finished_at"])
                if task:
                    Task.objects.filter(pk=task.pk, automatic_pause_reason="").update(
                        automatic_pause_reason=run.error, updated_at=timezone.now(),
                    )
                continue
            for endpoint in endpoints:
                if run.acp_endpoint_id is not None and run.acp_endpoint_id != endpoint.pk:
                    continue
                count = AgentRun.objects.filter(
                    acp_endpoint=endpoint, status__in=RESERVED_RUN_STATUSES,
                ).count()
                if count >= endpoint.session_limit:
                    continue
                run.acp_endpoint = endpoint
                run.acp_host = endpoint.host
                run.acp_port = endpoint.port
                run.status = AgentRun.Status.RUNNING
                run.save(update_fields=["acp_endpoint", "acp_host", "acp_port", "status"])
                return run
    return None


def execute_run(run, *, recover=False):
    endpoint = run.acp_endpoint
    if endpoint is None:
        raise RuntimeError("The run has no assigned ACP endpoint.")

    def work():
        try:
            arguments = (run.pk, run.initial_prompt, endpoint.pk, endpoint.host, endpoint.port, run.stage)
            asyncio.run(run_agent(*arguments, recovery_session_id=run.session_id) if recover else run_agent(*arguments))
            from .automation import continue_automatic_run

            if run.task_id is not None:
                continue_automatic_run(run.pk)
        except Exception as error:
            logger.exception("Agent worker for run %s failed", run.pk)
            with transaction.atomic():
                AgentRun.objects.filter(pk=run.pk, status__in=AgentRun.EXECUTING).update(
                    status=AgentRun.Status.INTERRUPTED,
                    error=f"Agent worker failed: {error}",
                    finished_at=timezone.now(),
                )
                Task.objects.filter(pk=run.task_id, automatic_pause_reason="").update(
                    automatic_pause_reason="The agent worker failed. Inspect the run before continuing.",
                    updated_at=timezone.now(),
                )
        finally:
            close_old_connections()
    thread = threading.Thread(target=work, name=f"agent-run-{run.pk}", daemon=True)
    thread.start()
    return thread


def start_scheduler():
    stop = threading.Event()

    def schedule():
        while not stop.is_set():
            try:
                close_old_connections()
                run = claim_next_recovery()
                recover = run is not None
                if run is None:
                    run = claim_next_run()
                if run is None:
                    _wake.wait(1)
                    _wake.clear()
                    continue
                try:
                    execute_run(run, recover=True) if recover else execute_run(run)
                except Exception as error:
                    logger.exception("Could not start agent run %s", run.pk)
                    AgentRun.objects.filter(pk=run.pk, status__in=AgentRun.EXECUTING).update(
                        status=AgentRun.Status.INTERRUPTED,
                        error=f"Could not start worker: {error}",
                        finished_at=timezone.now(),
                    )
                    Task.objects.filter(pk=run.task_id, automatic_pause_reason="").update(
                        automatic_pause_reason="The agent worker could not start. Inspect the error before continuing.",
                        updated_at=timezone.now(),
                    )
            except Exception:
                logger.exception("Agent scheduler failed; retrying.")
                stop.wait(1)
            finally:
                close_old_connections()

    thread = threading.Thread(target=schedule, name="agent-scheduler", daemon=True)
    thread.start()
    return stop, thread
