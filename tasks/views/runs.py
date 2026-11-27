import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from ..agent import protected_read_directories
from ..command_permissions import command_tokens, reported_commands
from ..mcp_permissions import reported_mcp_kind_mismatch, reported_mcp_tool
from ..models import AgentQuestion, AgentRun, PermissionRequest, RepositorySettings, RunEvent, Task
from ..questions import QUESTION_PAUSE, QuestionForm, answer_question, refresh_wait_state
from .activity import format_activity
from .usage import format_cost_amount


def run_redirect(run):
    if run.task_id is None:
        return redirect("project_run_detail", run_id=run.pk)
    return redirect("task_detail", task_id=run.task_id)


@require_GET
def run_activity(request, task_id, run_id):
    run = get_object_or_404(
        AgentRun.objects.select_related("task", "acp_endpoint", "usage_snapshot"),
        pk=run_id, task_id=task_id,
    )
    task = run.task
    usage = getattr(run, "usage_snapshot", None)
    return render(request, "tasks/run_activity.html", {
        "task": task,
        "run": run,
        "usage": usage,
        "usage_cost": format_cost_amount(usage.cost_amount) if usage else None,
        "runs": task.runs.select_related("acp_endpoint"),
        "events": format_activity(list(run.events.all())),
        "deferred_origin": getattr(task, "deferred_origin", None),
        "deferred_tasks": run.deferred_tasks.select_related("task__work_type"),
        "should_poll": run.status in AgentRun.ACTIVE,
        "run_needs_attention": run.status in (*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED),
    })


@require_POST
def cancel_run(request, run_id):
    run = get_object_or_404(AgentRun, pk=run_id)
    with transaction.atomic():
        cancelled = AgentRun.objects.filter(pk=run_id, status=AgentRun.Status.PENDING).update(
            status=AgentRun.Status.CANCELLED, cancel_requested=True, finished_at=timezone.now(),
        )
        if cancelled:
            Task.objects.filter(pk=run.task_id, automatic_pause_reason="").update(
                automatic_pause_reason="The pending run was cancelled.", updated_at=timezone.now(),
            )
            RunEvent.objects.create(run_id=run_id, kind="status", content="Pending run cancelled before dispatch.")
        elif not AgentRun.objects.filter(pk=run_id, status__in=AgentRun.EXECUTING).update(cancel_requested=True):
            return render(request, "tasks/error.html", {"message": "This run is no longer active."}, status=409)
    return run_redirect(run)

@require_POST
def acknowledge_run(request, run_id):
    run = get_object_or_404(AgentRun, pk=run_id)
    if not AgentRun.objects.filter(pk=run_id, status=AgentRun.Status.INTERRUPTED).update(status=AgentRun.Status.ACKNOWLEDGED):
        return render(request, "tasks/error.html", {"message": "This run is not awaiting acknowledgement."}, status=409)
    Task.objects.filter(
        pk=run.task_id, automatic_pause_reason__in=("", QUESTION_PAUSE),
    ).update(
        automatic_pause_reason="An interrupted run was acknowledged. Review it before continuing the workflow.",
        updated_at=timezone.now(),
    )
    return run_redirect(run)


@require_POST
def answer_agent_question(request, question_id):
    question = get_object_or_404(AgentQuestion.objects.select_related("run"), pk=question_id)
    if question.outcome != AgentQuestion.Outcome.PENDING or question.run.status not in AgentRun.ACTIVE or question.run.cancel_requested:
        return render(request, "tasks/error.html", {"message": "This question is no longer available."}, status=409)
    form = QuestionForm(question.schema, request.POST, prefix="answer")
    if not form.is_valid():
        if question.run.task_id is None:
            from .project_runs import project_run_detail

            return project_run_detail(
                request, question.run_id, question_form=form, question_form_question_id=question.pk, status=400,
            )
        from .detail import task_detail

        response = task_detail(request, question.run.task_id, question_form=form, question_form_question_id=question.pk)
        response.status_code = 400
        return response
    try:
        answer_question(question.pk, form.content)
    except (ValueError, RuntimeError, ValidationError) as error:
        return render(request, "tasks/error.html", {"message": str(error)}, status=409)
    messages.success(request, "Answer recorded. The agent will receive it if the session remains active.")
    return run_redirect(question.run)


@require_POST
def resolve_permission(request, permission_id):
    with transaction.atomic():
        permission = get_object_or_404(PermissionRequest.objects.select_for_update(), pk=permission_id)
        selected = request.POST.get("option", "")
        option = next((option for option in permission.options if option["optionId"] == selected and option["kind"] in ("allow_once", "reject_once")), None)
        if not option:
            return render(request, "tasks/error.html", {"message": "Invalid permission option."}, status=400)
        directory = request.POST.get("directory")
        submitted_prefixes = request.POST.getlist("command_prefix")
        command_rule = "command_prefix" in request.POST
        mcp_scope = request.POST.get("mcp_scope")
        if sum((directory is not None, command_rule, mcp_scope is not None)) > 1:
            return render(request, "tasks/error.html", {"message": "Choose only one permission rule."}, status=400)
        repository_settings = None
        command_prefixes = []
        mcp_rule = None
        if directory is not None or command_rule or mcp_scope is not None:
            if option["kind"] != "allow_once":
                return render(request, "tasks/error.html", {"message": "A rule can only be saved when allowing the request."}, status=400)
            if not RepositorySettings.objects.filter(pk=1).update(allow_safe_operations=F("allow_safe_operations")):
                return render(request, "tasks/error.html", {"message": "Repository settings are unavailable."}, status=409)
            repository_settings = RepositorySettings.objects.get(pk=1)
            if directory is not None and directory not in protected_read_directories(permission.tool_call, repository_settings.safe_read_paths):
                return render(request, "tasks/error.html", {"message": "The directory is not eligible or the permission changed. Reload it first."}, status=409)
            if command_rule:
                commands = reported_commands(permission.tool_call)
                if not commands or len(commands) != len(submitted_prefixes):
                    return render(request, "tasks/error.html", {"message": "The commands are invalid or the permission changed. Reload it first."}, status=400)
                for tokens, prefix in zip(commands, submitted_prefixes):
                    if not prefix:
                        continue
                    prefix_tokens = command_tokens(prefix)
                    if not prefix_tokens or tokens[:len(prefix_tokens)] != prefix_tokens:
                        return render(request, "tasks/error.html", {"message": "A command prefix is invalid or does not match its command."}, status=400)
                    normalized = " ".join(prefix_tokens)
                    if normalized not in command_prefixes:
                        command_prefixes.append(normalized)
                if not command_prefixes:
                    return render(request, "tasks/error.html", {"message": "Enter at least one command prefix."}, status=400)
            if mcp_scope is not None:
                identity = reported_mcp_tool(permission.tool_call)
                if mcp_scope not in ("tool", "server") or identity is None:
                    return render(request, "tasks/error.html", {"message": "The MCP tool is invalid or the permission changed. Reload it first."}, status=400)
                mcp_rule = {"server": identity["server"], "tool": identity["tool"] if mcp_scope == "tool" else None}
                mismatch = reported_mcp_kind_mismatch(permission.tool_call)
                if mismatch:
                    mcp_rule["kindMismatch"] = mismatch
        changed = PermissionRequest.objects.filter(
            pk=permission_id, resolved=False, run__status__in=AgentRun.ACTIVE, run__cancel_requested=False,
        ).update(resolved=True, selected_option=selected)
        if not changed:
            return render(request, "tasks/error.html", {"message": "This permission request is no longer pending."}, status=409)
        if directory is not None:
            repository_settings.safe_read_paths.append({"path": directory, "kind": "directory"})
            repository_settings.save(update_fields=["safe_read_paths"])
        elif command_rule:
            for prefix in command_prefixes:
                if prefix not in repository_settings.allowed_command_prefixes:
                    repository_settings.allowed_command_prefixes.append(prefix)
            repository_settings.save(update_fields=["allowed_command_prefixes"])
        elif mcp_rule:
            if mcp_rule not in repository_settings.allowed_mcp_tools:
                repository_settings.allowed_mcp_tools.append(mcp_rule)
                repository_settings.save(update_fields=["allowed_mcp_tools"])
        decision = {"toolCall": permission.tool_call, "selectedOption": option}
        if directory is not None:
            decision.update({
                "origin": "manual_directory_allowlist",
                "allowedDirectory": directory,
                "reason": "The request was allowed once and the directory was saved for future read/search approvals.",
            })
        elif command_rule:
            decision.update({
                "origin": "manual_command_allowlist",
                "allowedCommandPrefixes": command_prefixes,
                "reason": "The request was allowed once and command prefixes were saved for future approvals.",
            })
            if len(command_prefixes) == 1:
                decision["allowedCommandPrefix"] = command_prefixes[0]
        elif mcp_rule:
            decision.update({
                "origin": "manual_mcp_allowlist", "allowedMcpRule": mcp_rule,
                "reason": "The request was allowed once and the reported MCP identity was saved for future approvals.",
            })
        RunEvent.objects.create(run_id=permission.run_id, kind="permission decision", content=json.dumps(decision, indent=2, ensure_ascii=False))
        refresh_wait_state(permission.run_id)
    if directory is not None:
        message = f"Allowed this request once and saved {directory} for automatic read/search approvals."
        if not repository_settings.allow_safe_operations:
            message += " Automatic approvals remain disabled."
        messages.success(request, message)
    elif command_rule:
        messages.success(request, f"Allowed this request once and enabled command prefixes: {', '.join(command_prefixes)}")
    elif mcp_rule:
        target = f"{mcp_rule['server']}-{mcp_rule['tool']}" if mcp_rule["tool"] else f"all tools on {mcp_rule['server']}"
        messages.success(request, f"Allowed this request once and enabled automatic approval for {target}.")
    return run_redirect(permission.run)
