import json
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from ..acp import RESERVED_RUN_STATUSES, check_endpoint_connection, endpoint_status
from ..forms import AcpEndpointForm, SafeReadPathsForm
from ..models import AcpEndpoint, AgentRun, RepositorySettings
from ..mcp_permissions import valid_mcp_kind
from .usage import format_cost_amount

logger = logging.getLogger(__name__)


@require_safe
def agent_mcp_configuration(request):
    executable = Path(sys.executable)
    if not executable.is_absolute():
        return HttpResponse(
            "Cannot determine the Python environment running Flamethrower.\n",
            content_type="text/plain; charset=utf-8", status=503,
        )
    command = executable.parent / ("flamethrower-mcp.exe" if os.name == "nt" else "flamethrower-mcp")
    if not command.is_file() or not os.access(command, os.X_OK):
        return HttpResponse(
            f"MCP executable not found or not executable: {command}\n"
            "Install Flamethrower in the Python environment running this server and restart it.\n",
            content_type="text/plain; charset=utf-8", status=503,
        )
    config = {
        "mcpServers": {
            "flamethrower": {
                "type": "local",
                "command": str(command),
                "args": [str(settings.PROJECT_PATH)],
            },
        },
    }
    return HttpResponse(
        "Restart after saving; keep Flamethrower running.\n\n"
        + json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        content_type="text/plain; charset=utf-8",
    )


def acp_list(request, safe_read_paths_form=None):
    repository_settings = RepositorySettings.objects.get(pk=1)
    endpoints = list(AcpEndpoint.objects.all())
    runs = AgentRun.objects.filter(
        acp_endpoint__in=endpoints, status__in=RESERVED_RUN_STATUSES,
    ).select_related("task").order_by("-id")
    current_runs = {endpoint.pk: [] for endpoint in endpoints}
    for run in runs:
        current_runs[run.acp_endpoint_id].append(run)
    rows = []
    for endpoint in endpoints:
        reserved_runs = current_runs[endpoint.pk]
        rows.append({
            "endpoint": endpoint,
            "runs": reserved_runs,
            "status": endpoint_status(endpoint, len(reserved_runs)),
        })
    usage_now = timezone.localtime(timezone.now())
    usage_period_start = usage_now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    usage_by_endpoint = {
        endpoint.pk: {
            "costs": {},
            "total_tokens": None,
            "input_tokens": None,
            "output_tokens": None,
            "token_runs": 0,
            "runs_without_cost": [],
        }
        for endpoint in endpoints
    }
    usage_runs = AgentRun.objects.filter(
        acp_endpoint__in=endpoints,
        created_at__gte=usage_period_start,
        created_at__lte=usage_now,
    ).select_related("task", "acp_endpoint", "usage_snapshot").order_by("created_at", "id")
    for run in usage_runs:
        summary = usage_by_endpoint[run.acp_endpoint_id]
        usage = getattr(run, "usage_snapshot", None)
        if usage and usage.total_tokens is not None:
            summary["token_runs"] += 1
            for name in ("total_tokens", "input_tokens", "output_tokens"):
                value = getattr(usage, name)
                if value is not None:
                    summary[name] = (summary[name] or 0) + value
        if usage and usage.cost_amount is not None and usage.cost_currency:
            currency_summary = summary["costs"].setdefault(
                usage.cost_currency, {"amount": Decimal("0"), "runs": []},
            )
            currency_summary["amount"] += usage.cost_amount
            currency_summary["formatted_amount"] = format_cost_amount(currency_summary["amount"])
            currency_summary["runs"].append(run)
        else:
            summary["runs_without_cost"].append(run)
    for row in rows:
        row["usage"] = usage_by_endpoint[row["endpoint"].pk]
    return render(request, "tasks/acp/list.html", {
        "rows": rows,
        "usage_period_start": usage_period_start,
        "allow_safe_operations": repository_settings.allow_safe_operations,
        "allowed_command_prefixes": repository_settings.allowed_command_prefixes,
        "allowed_mcp_tools": repository_settings.allowed_mcp_tools,
        "safe_read_paths_form": safe_read_paths_form if safe_read_paths_form is not None else SafeReadPathsForm(initial={
            "safe_read_paths": "\n".join(entry["path"] for entry in repository_settings.safe_read_paths),
        }),
    })


@require_POST
def set_safe_operations(request):
    value = request.POST.get("allow_safe_operations")
    if value not in ("true", "false"):
        return render(request, "tasks/error.html", {
            "message": "Invalid repository permission setting.",
        }, status=400)
    enabled = value == "true"
    with transaction.atomic():
        changed = RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=enabled)
        if not changed:
            return render(request, "tasks/error.html", {
                "message": "Repository settings are unavailable.",
            }, status=409)
    messages.success(
        request,
        "Automatic safe read/search approvals enabled." if enabled
        else "Automatic safe read/search approvals disabled.",
    )
    return redirect("acp_list")


@require_POST
def set_safe_read_paths(request):
    form = SafeReadPathsForm(request.POST)
    if not form.is_valid():
        response = acp_list(request, safe_read_paths_form=form)
        response.status_code = 400
        return response
    with transaction.atomic():
        changed = RepositorySettings.objects.filter(pk=1).update(
            safe_read_paths=form.cleaned_data["safe_read_paths"],
        )
        if not changed:
            return render(request, "tasks/error.html", {
                "message": "Repository settings are unavailable.",
            }, status=409)
    messages.success(request, "Additional automatic read/search paths saved.")
    return redirect("acp_list")


@require_POST
def remove_command_prefix(request):
    prefix = request.POST.get("prefix")
    with transaction.atomic():
        if not RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=F("allow_safe_operations")):
            return render(request, "tasks/error.html", {"message": "Repository settings are unavailable."}, status=409)
        repository_settings = RepositorySettings.objects.get(pk=1)
        if prefix not in repository_settings.allowed_command_prefixes:
            return render(request, "tasks/error.html", {"message": "The command prefix is no longer saved."}, status=409)
        repository_settings.allowed_command_prefixes.remove(prefix)
        repository_settings.save(update_fields=["allowed_command_prefixes"])
    messages.success(request, "Command prefix removed.")
    return redirect("acp_list")


@require_POST
def remove_mcp_rule(request):
    server = request.POST.get("server")
    scope = request.POST.get("scope")
    if scope not in ("tool", "server"):
        return render(request, "tasks/error.html", {"message": "Invalid MCP rule."}, status=400)
    rule = {"server": server, "tool": request.POST.get("tool") if scope == "tool" else None}
    mismatch = request.POST.get("kind_mismatch")
    intermediate = request.POST.get("intermediate_kind")
    if mismatch is not None:
        parts = mismatch.split(":")
        if (
            len(parts) != 2 or not all(valid_mcp_kind(kind) for kind in parts)
            or (intermediate is not None and not valid_mcp_kind(intermediate))
            or (parts[0] == parts[1] and intermediate is None)
        ):
            return render(request, "tasks/error.html", {"message": "Invalid MCP rule kind mismatch."}, status=400)
        rule["kindMismatch"] = {"origin": parts[0], "permission": parts[1]}
        if intermediate is not None:
            rule["kindMismatch"]["intermediate"] = intermediate
    elif intermediate is not None:
        return render(request, "tasks/error.html", {"message": "Invalid MCP rule kind mismatch."}, status=400)
    with transaction.atomic():
        if not RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=F("allow_safe_operations")):
            return render(request, "tasks/error.html", {"message": "Repository settings are unavailable."}, status=409)
        settings = RepositorySettings.objects.get(pk=1)
        if rule not in settings.allowed_mcp_tools:
            return render(request, "tasks/error.html", {"message": "The MCP rule is no longer saved."}, status=409)
        settings.allowed_mcp_tools.remove(rule)
        settings.save(update_fields=["allowed_mcp_tools"])
    messages.success(request, "MCP rule removed.")
    return redirect("acp_list")


@require_http_methods(["GET", "POST"])
def acp_create(request):
    form = AcpEndpointForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            endpoint = form.save()
        except IntegrityError:
            form.add_error("name", "An ACP with this name already exists.")
        else:
            messages.success(request, "ACP endpoint created.")
            return redirect("acp_list")
    return render(request, "tasks/acp/form.html", {"form": form, "creating": True})


@require_http_methods(["GET", "POST"])
def acp_edit(request, endpoint_id):
    endpoint = get_object_or_404(AcpEndpoint, pk=endpoint_id)
    form = AcpEndpointForm(request.POST or None, instance=endpoint)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except IntegrityError:
            form.add_error("name", "An ACP with this name already exists.")
        else:
            messages.success(request, "ACP endpoint updated.")
            return redirect("acp_list")
    return render(request, "tasks/acp/form.html", {"form": form, "endpoint": endpoint})


@require_http_methods(["GET", "POST"])
def acp_delete(request, endpoint_id):
    endpoint = get_object_or_404(AcpEndpoint, pk=endpoint_id)
    if request.method == "POST":
        if endpoint.enabled:
            return render(request, "tasks/error.html", {
                "message": "Disable this ACP before deleting it.",
            }, status=409)
        if endpoint.runs.exists():
            return render(request, "tasks/error.html", {
                "message": "This ACP has run history. Reassign or delete its tasks and preserve the endpoint for run history.",
            }, status=409)
        if endpoint.selected_tasks.exists():
            return render(request, "tasks/error.html", {
                "message": "Reassign tasks that explicitly select this ACP before deleting it.",
            }, status=409)
        endpoint.delete()
        messages.success(request, "ACP endpoint deleted.")
        return redirect("acp_list")
    return render(request, "tasks/acp/delete.html", {"endpoint": endpoint})


@require_POST
def acp_check(request, endpoint_id):
    endpoint = get_object_or_404(AcpEndpoint, pk=endpoint_id)
    if not endpoint.enabled:
        return render(request, "tasks/error.html", {
            "message": "Enable this ACP before checking its connection.",
        }, status=409)
    if AgentRun.objects.filter(
        acp_endpoint=endpoint, status__in=RESERVED_RUN_STATUSES,
    ).exists():
        return render(request, "tasks/error.html", {
            "message": "Cannot check this ACP while it has an active run or unacknowledged interruption.",
        }, status=409)
    try:
        check_endpoint_connection(endpoint)
    except Exception as error:
        logger.exception("ACP endpoint check failed for endpoint %s", endpoint.pk)
        messages.error(request, f"ACP connection check failed: {error}")
    else:
        messages.success(request, "ACP connection initialized successfully.")
    return redirect("acp_list")
