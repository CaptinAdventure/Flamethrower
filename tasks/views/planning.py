import hashlib
import hmac

from django.contrib import messages
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..acp import AcpUnavailable, create_run

from ..forms import ArchitectRefinementForm, RecommendationForm, StageForm
from ..models import AgentRun, Approval, Task, next_priority_expression
from ..recommendations import architect_plan, final_agent_report
from .api_support import action_error


def current_architect_proposal(task):
    run = task.runs.first()
    if (
        task.state != Task.State.SCOPE_APPROVED or task.stage != Task.Stage.ARCHITECT
        or task.status != Task.Status.READY or task.finished
        or not run or run.stage != Task.Stage.ARCHITECT or run.status != AgentRun.Status.COMPLETED
        or run.task_version != task.version or run.work_cycle != task.work_cycle
    ):
        return None
    report = final_agent_report(list(run.events.all()))["text"]
    plan = architect_plan(report)
    if not plan:
        return None
    return {"run": run, "plan": plan, "hash": hashlib.sha256(report.encode("utf-8")).hexdigest()}


def proposal_matches(task, proposal, form):
    return bool(
        proposal and task.version == form.cleaned_data["version"]
        and task.stage_version == form.cleaned_data["stage_version"]
        and proposal["run"].pk == form.cleaned_data["run_id"]
        and hmac.compare_digest(proposal["hash"], form.cleaned_data["report_hash"])
    )


@require_POST
def refine_plan(request, task_id):
    form = StageForm(request.POST)
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if (
            not form.is_valid() or task.version != form.cleaned_data["version"]
            or task.stage_version != form.cleaned_data["stage_version"]
            or task.state != Task.State.SCOPE_APPROVED or task.stage != Task.Stage.ARCHITECT
            or task.status != Task.Status.READY or task.finished
        ):
            return render(request, "tasks/error.html", {"message": "Approve the current request and reload before generating a plan."}, status=409)
        if current_architect_proposal(task):
            return render(request, "tasks/error.html", {"message": "A proposed plan is awaiting your decision. Use Refine and provide feedback instead."}, status=409)
        try:
            run = create_run(
                task, stage=Task.Stage.ARCHITECT, task_version=task.version, work_cycle=task.work_cycle,
                request_snapshot=f"{task.title}\n\n{task.request}", plan_snapshot=task.plan,
            )
        except AcpUnavailable as error:
            return render(request, "tasks/error.html", {"message": str(error)}, status=409)
        if task.automatic_pause_reason:
            Task.objects.filter(pk=task.pk).update(automatic_pause_reason="", updated_at=timezone.now())
    from ..automation import _launch

    _launch(run)
    return redirect("task_detail", task_id=task_id)


@require_POST
def use_architect_plan(request, task_id, *, actor="human"):
    form = RecommendationForm(request.POST)
    if not form.is_valid():
        return action_error(request, "Invalid proposed plan.", 400)
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        proposal = current_architect_proposal(task)
        if not proposal_matches(task, proposal, form):
            return action_error(request, "The proposed plan is no longer current. Reload first.", 409)
        changed = Task.objects.filter(
            pk=task.pk, state=Task.State.SCOPE_APPROVED, stage=Task.Stage.ARCHITECT,
            version=task.version, stage_version=task.stage_version,
        ).exclude(runs__status__in=(*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)).update(
            plan=proposal["plan"], state=Task.State.PLAN_APPROVED, stage=Task.Stage.DEVELOPER,
            priority=next_priority_expression(Task.Stage.DEVELOPER, task.pk),
            version=F("version") + 1, stage_version=F("stage_version") + 1,
            automatic_pause_reason="", updated_at=timezone.now(),
        )
        if not changed:
            return action_error(request, "The task changed or a run needs attention. Reload first.", 409)
        who = "MCP agent" if actor == "mcp" else "Human"
        Approval.objects.create(
            task=task, kind=Approval.Kind.PLAN,
            content=f"{who} accepted Architect run #{proposal['run'].pk}.\n\nRequest: {task.title}\n\n{task.request}\n\nPlan:\n{proposal['plan']}",
        )
    from ..automation import continue_automatic_task

    continue_automatic_task(task.pk)
    messages.success(request, "Plan accepted and approved. Advanced to Developer.")
    return redirect("task_detail", task_id=task_id)


@require_POST
def refine_architect_plan(request, task_id):
    form = ArchitectRefinementForm(request.POST)
    if not form.is_valid():
        from .detail import task_detail

        response = task_detail(request, task_id, architect_refinement_form=form)
        response.status_code = 400
        return response
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        proposal = current_architect_proposal(task)
        if not proposal_matches(task, proposal, form):
            return render(request, "tasks/error.html", {"message": "The proposed plan changed. Reload before refining."}, status=409)
        note = f"Human requested refinement of Architect run #{proposal['run'].pk}:\n{form.cleaned_data['feedback']}"
        try:
            run = create_run(
                task, stage=Task.Stage.ARCHITECT, task_version=task.version, work_cycle=task.work_cycle,
                request_snapshot=f"{task.title}\n\n{task.request}", plan_snapshot=proposal["plan"], context_snapshot=note,
            )
        except AcpUnavailable as error:
            form.add_error(None, str(error))
            from .detail import task_detail

            response = task_detail(request, task_id, architect_refinement_form=form)
            response.status_code = 409
            return response
        task.stage_version += 1
        task.automatic_pause_reason = ""
        task.updated_at = timezone.now()
        task.save(update_fields=["stage_version", "automatic_pause_reason", "updated_at"])
        Approval.objects.create(task=task, kind=Approval.Kind.STAGE, content=note)
    from ..automation import _launch

    _launch(run)
    messages.success(request, "Refinement requested. Inspect the Architect run for progress or errors.")
    return redirect("task_detail", task_id=task_id)


@require_POST
def abandon_architect_plan(request, task_id):
    form = RecommendationForm(request.POST)
    if not form.is_valid():
        return render(request, "tasks/error.html", {"message": "Invalid abandonment request."}, status=400)
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        proposal = current_architect_proposal(task)
        if not proposal_matches(task, proposal, form):
            return render(request, "tasks/error.html", {"message": "The proposed plan changed. Reload before abandoning."}, status=409)
        changed = Task.objects.filter(
            pk=task.pk, version=task.version, stage_version=task.stage_version,
        ).exclude(runs__status__in=(*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)).update(
            status=Task.Status.ABANDONED, automatic_pause_reason="",
            stage_version=F("stage_version") + 1, updated_at=timezone.now(),
        )
        if not changed:
            return render(request, "tasks/error.html", {"message": "The task changed or a run needs attention. Reload first."}, status=409)
        Approval.objects.create(
            task=task, kind=Approval.Kind.STAGE,
            content=f"Human abandoned the task after reviewing Architect run #{proposal['run'].pk}.",
        )
    messages.success(request, "Task marked Abandoned.")
    return redirect("task_detail", task_id=task_id)
