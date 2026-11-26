import asyncio
import sqlite3
from collections import Counter

import acp
from acp.interfaces import Client
from django.db import OperationalError, connection, transaction
from django.db.models import Count, F
from django.utils import timezone

from .models import AcpEndpoint, AgentRun, Task


class AcpUnavailable(Exception):
    pass


RESERVED_RUN_STATUSES = (*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)


def create_run(task, **values):
    """Choose and claim an endpoint in the same transaction that creates its run."""
    try:
        with transaction.atomic():
            Task.objects.select_for_update().get(pk=task.pk)
            if task.acp_selection == Task.AcpSelection.EXPLICIT:
                if not task.selected_acp_endpoint_id:
                    raise AcpUnavailable("Select an ACP endpoint before starting this task.")
                endpoints = list(AcpEndpoint.objects.select_for_update().filter(
                    pk=task.selected_acp_endpoint_id,
                ))
                if not endpoints or not endpoints[0].enabled:
                    raise AcpUnavailable("The selected ACP is disabled or no longer exists.")
            else:
                enabled = AcpEndpoint.objects.select_for_update().filter(enabled=True)
                endpoints = list(enabled.filter(last_error="").order_by("priority", "id"))
            if not endpoints:
                if AcpEndpoint.objects.filter(enabled=True).exists():
                    raise AcpUnavailable("No enabled ACP is currently available.")
                raise AcpUnavailable("No enabled ACP is configured.")
            if task.acp_selection == Task.AcpSelection.EXPLICIT and endpoints[0].last_error:
                raise AcpUnavailable("The selected ACP is unavailable. Check its connection before starting a run.")

            if connection.vendor == "sqlite":
                # SQLite ignores select_for_update; acquire its write lock before counting reservations.
                AcpEndpoint.objects.filter(pk__in=[endpoint.pk for endpoint in endpoints]).update(priority=F("priority"))
            if task.runs.filter(status__in=RESERVED_RUN_STATUSES).exists():
                raise AcpUnavailable("This task already has an active run or an interruption awaiting acknowledgement.")
            counts = dict(AgentRun.objects.filter(
                acp_endpoint__in=endpoints, status__in=RESERVED_RUN_STATUSES,
            ).values("acp_endpoint_id").annotate(total=Count("id")).values_list("acp_endpoint_id", "total"))
            for endpoint in endpoints:
                if counts.get(endpoint.pk, 0) < endpoint.session_limit:
                    return AgentRun.objects.create(acp_endpoint=endpoint, task=task, **values)
            if task.acp_selection == Task.AcpSelection.EXPLICIT:
                raise AcpUnavailable("The selected ACP is busy or has an interruption awaiting acknowledgement.")
            raise AcpUnavailable("No enabled ACP is currently available.")
    except OperationalError as error:
        cause = error.__cause__
        code = getattr(cause, "sqlite_errorcode", None)
        if (
            connection.vendor == "sqlite" and code is not None
            and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        ):
            raise AcpUnavailable("The database is busy while reserving an ACP session. Retry the run.") from error
        raise


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
        runs = list(endpoint_reserved_runs().filter(acp_endpoint=endpoint).select_related("task").order_by("id"))
        full = len(runs) >= endpoint.session_limit
        run = None
        if full:
            run = next((item for item in runs if item.status in AgentRun.ACTIVE), runs[0])
        if not endpoint.enabled:
            issue = "The selected ACP is disabled."
        elif endpoint.last_error:
            issue = "The selected ACP is unavailable. Check its connection before starting a run."
        elif full:
            issue = "The selected ACP is busy or has an interruption awaiting acknowledgement."
        else:
            issue = ""
        return {
            "busy": full, "blocking_run": run, "acp_issue": issue,
            "acp_selection_label": f"{endpoint.name} ({endpoint.address})",
        }

    endpoints = list(AcpEndpoint.objects.filter(enabled=True, last_error="").order_by("priority", "id"))
    if not endpoints:
        issue = (
            "No enabled ACP is currently available."
            if AcpEndpoint.objects.filter(enabled=True).exists()
            else "No enabled ACP is configured."
        )
        return {
            "busy": False, "blocking_run": None, "acp_issue": issue,
            "acp_selection_label": "Automatic — highest-priority available ACP",
        }
    runs = list(
        endpoint_reserved_runs().filter(acp_endpoint__in=endpoints).select_related("task").order_by("id")
    )
    counts = Counter(run.acp_endpoint_id for run in runs)
    free = [endpoint for endpoint in endpoints if counts[endpoint.pk] < endpoint.session_limit]
    blocking_run = next((run for run in runs if run.status in AgentRun.ACTIVE), runs[0] if runs else None)
    return {
        "busy": not free,
        "blocking_run": blocking_run if not free else None,
        "acp_issue": "" if free else "No enabled ACP is currently available.",
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
