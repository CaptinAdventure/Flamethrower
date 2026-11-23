import asyncio

import acp
from acp.interfaces import Client
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import AcpEndpoint, AgentRun, Task


class AcpUnavailable(Exception):
    pass


RESERVED_RUN_STATUSES = (*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)


def create_run(task, **values):
    """Choose and claim an endpoint in the same transaction that creates its run."""
    with transaction.atomic():
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
            if task.acp_selection == Task.AcpSelection.EXPLICIT:
                raise AcpUnavailable("No enabled ACP is configured.")
            if AcpEndpoint.objects.filter(enabled=True).exists():
                raise AcpUnavailable("No enabled ACP is currently available.")
            raise AcpUnavailable("No enabled ACP is configured.")
        if (
            task.acp_selection == Task.AcpSelection.EXPLICIT
            and endpoints[0].last_error
        ):
            raise AcpUnavailable("The selected ACP is unavailable. Check its connection before starting a run.")

        reserved_ids = set(AgentRun.objects.filter(
            acp_endpoint__in=endpoints, status__in=RESERVED_RUN_STATUSES,
        ).values_list("acp_endpoint_id", flat=True))
        available = [endpoint for endpoint in endpoints if endpoint.pk not in reserved_ids]
        if not available:
            if task.acp_selection == Task.AcpSelection.EXPLICIT:
                raise AcpUnavailable("The selected ACP is busy or has an interruption awaiting acknowledgement.")
            raise AcpUnavailable("No enabled ACP is currently available.")

        for endpoint in available:
            try:
                with transaction.atomic():
                    return AgentRun.objects.create(acp_endpoint=endpoint, task=task, **values)
            except IntegrityError:
                if task.acp_selection == Task.AcpSelection.EXPLICIT:
                    raise AcpUnavailable("The selected ACP became busy before the run could start.")
        raise AcpUnavailable("No enabled ACP is currently available.")


def endpoint_reserved_runs():
    return AgentRun.objects.filter(status__in=RESERVED_RUN_STATUSES)


def endpoint_status(endpoint, reserved_run=None):
    if not endpoint.enabled:
        return "Disabled"
    if reserved_run:
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
        run = endpoint_reserved_runs().filter(acp_endpoint=endpoint).select_related("task").first()
        if not endpoint.enabled:
            issue = "The selected ACP is disabled."
        elif endpoint.last_error:
            issue = "The selected ACP is unavailable. Check its connection before starting a run."
        elif run:
            issue = "The selected ACP is busy or has an interruption awaiting acknowledgement."
        else:
            issue = ""
        return {
            "busy": bool(run), "blocking_run": run, "acp_issue": issue,
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
            "acp_selection_label": "Automatic — highest-priority free ACP",
        }
    runs = list(
        endpoint_reserved_runs().filter(acp_endpoint__in=endpoints).select_related("task").order_by("id")
    )
    run_by_endpoint = {run.acp_endpoint_id: run for run in runs}
    free = [endpoint for endpoint in endpoints if endpoint.pk not in run_by_endpoint]
    return {
        "busy": not free,
        "blocking_run": runs[0] if not free and runs else None,
        "acp_issue": "" if free else "No enabled ACP is currently available.",
        "acp_selection_label": "Automatic — highest-priority free ACP",
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
