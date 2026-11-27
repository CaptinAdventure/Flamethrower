import asyncio
import sqlite3

import acp
from acp.interfaces import Client
from django.db import OperationalError, connection, transaction
from django.db.models import F
from django.utils import timezone

from .agent_prompts import build_initial_prompt
from .models import AcpEndpoint, AgentRun, Task


class AcpUnavailable(Exception):
    pass


RESERVED_RUN_STATUSES = (*AgentRun.EXECUTING, AgentRun.Status.INTERRUPTED)


def create_run(task, **values):
    """Prepare a durable run and its exact initial prompt in one transaction."""
    try:
        with transaction.atomic():
            task = Task.objects.select_for_update().get(pk=task.pk)
            if connection.vendor == "sqlite":
                Task.objects.filter(pk=task.pk).update(priority=F("priority"))
            if task.acp_selection == Task.AcpSelection.EXPLICIT:
                if not task.selected_acp_endpoint_id:
                    raise AcpUnavailable("Select an ACP endpoint before starting this task.")
                endpoint = AcpEndpoint.objects.filter(pk=task.selected_acp_endpoint_id, enabled=True).first()
                if endpoint is None:
                    raise AcpUnavailable("The selected ACP is disabled or no longer exists.")
            else:
                endpoint = None
                if not AcpEndpoint.objects.filter(enabled=True).exists():
                    raise AcpUnavailable("No enabled ACP is configured.")
            if task.runs.filter(status__in=(*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)).exists():
                raise AcpUnavailable("This task already has an active run or an interruption awaiting acknowledgement.")
            run = AgentRun.objects.create(acp_endpoint=endpoint, task=task, **values)
            run.initial_prompt = build_initial_prompt(run)
            run.save(update_fields=["initial_prompt"])
            return run
    except OperationalError as error:
        cause = error.__cause__
        code = getattr(cause, "sqlite_errorcode", None)
        if (
            connection.vendor == "sqlite" and code is not None
            and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        ):
            raise AcpUnavailable("The database is busy while reserving an ACP session. Retry the run.") from error
        raise


def create_project_run(request_text, endpoint=None):
    with transaction.atomic():
        if endpoint is not None:
            endpoint = AcpEndpoint.objects.filter(pk=endpoint.pk, enabled=True).first()
            if endpoint is None:
                raise AcpUnavailable("The selected ACP is disabled or no longer exists.")
        elif not AcpEndpoint.objects.filter(enabled=True).exists():
            raise AcpUnavailable("No enabled ACP is configured.")
        run = AgentRun.objects.create(
            task=None, stage=AgentRun.PROJECT, task_version=0, request_snapshot=request_text,
            plan_snapshot="", acp_endpoint=endpoint,
        )
        run.initial_prompt = build_initial_prompt(run)
        run.save(update_fields=["initial_prompt"])
        return run


def endpoint_reserved_runs():
    return AgentRun.objects.filter(status__in=RESERVED_RUN_STATUSES)


def endpoint_status(endpoint, reserved_count=0):
    if not endpoint.enabled:
        return "Disabled"
    if reserved_count >= endpoint.session_limit:
        return "Busy"
    if endpoint.last_error:
        return "Unavailable"
    if endpoint.last_checked_at is None:
        return "Unknown"
    return "Available"


def mark_endpoint_checked(endpoint_id, error=None):
    now = timezone.now()
    updates = {"last_checked_at": now, "last_error": error or "", "updated_at": now}
    if error is None:
        updates["last_success_at"] = now
    AcpEndpoint.objects.filter(pk=endpoint_id).update(**updates)


def task_acp_state(task):
    if task.acp_selection == Task.AcpSelection.EXPLICIT:
        endpoint = task.selected_acp_endpoint
        if not endpoint:
            return {
                "busy": False, "blocking_run": None, "acp_issue": "No ACP is selected.",
                "acp_selection_label": "No ACP selected",
            }
        if not endpoint.enabled:
            issue = "The selected ACP is disabled."
        else:
            issue = ""
        return {
            "busy": False, "blocking_run": None, "acp_issue": issue,
            "acp_selection_label": f"{endpoint.name} ({endpoint.address})",
        }

    return {
        "busy": False,
        "blocking_run": None,
        "acp_issue": "" if AcpEndpoint.objects.filter(enabled=True).exists() else "No enabled ACP is configured.",
        "acp_selection_label": "Automatic — highest-priority available ACP",
    }


async def _check_endpoint_connection(endpoint):
    class ProbeClient(Client):
        async def session_update(self, session_id, update, **kwargs):
            return None

    reader = writer = connection = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(endpoint.host, endpoint.port), timeout=10,
        )
        connection = acp.connect_to_agent(ProbeClient(), writer, reader)
        initialized = await asyncio.wait_for(
            connection.initialize(protocol_version=acp.PROTOCOL_VERSION), timeout=30,
        )
        if initialized.protocol_version != acp.PROTOCOL_VERSION:
            raise RuntimeError(f"Unsupported ACP protocol version: {initialized.protocol_version}")
    finally:
        if connection:
            await connection.close()
        if writer:
            writer.close()
            await writer.wait_closed()


def check_endpoint_connection(endpoint):
    try:
        asyncio.run(_check_endpoint_connection(endpoint))
    except Exception as error:
        mark_endpoint_checked(endpoint.pk, str(error))
        raise
    mark_endpoint_checked(endpoint.pk)
