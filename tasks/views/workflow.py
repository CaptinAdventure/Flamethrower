import hashlib
import hmac

from django.contrib import messages
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..acp import AcpUnavailable, create_run
from ..automation import workflow_action_blocked, workflow_continue_block_reason
from ..forms import GateDecisionForm, RecommendationForm, ReworkForm, StageForm
from ..models import AgentRun, Approval, GateDecision, Task, next_priority_expression
from ..recommendations import final_agent_report, recommendation_evidence
from .api_support import action_error
from .detail import task_detail
from .planning import refine_plan, use_architect_plan


def completed_developer_run(task, run):
    return bool(
        task.stage == Task.Stage.DEVELOPER and task.state == Task.State.PLAN_APPROVED
        and run and run.stage == Task.Stage.DEVELOPER and run.status == AgentRun.Status.COMPLETED
        and run.task_version == task.version and run.work_cycle == task.work_cycle
    )


@require_POST
def approve_request(request, task_id, *, actor="human"):
    form = StageForm(request.POST)
    if not form.is_valid():
        return action_error(request, "Invalid request approval.", 400)
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        changed = Task.objects.filter(
            pk=task_id, state=Task.State.DRAFT, stage__in=(Task.Stage.DRAFT, Task.Stage.ARCHITECT),
            status=Task.Status.READY, version=form.cleaned_data["version"],
            stage_version=form.cleaned_data["stage_version"],
        ).exclude(runs__status__in=(*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)).update(
            state=Task.State.SCOPE_APPROVED, stage=Task.Stage.ARCHITECT,
            priority=next_priority_expression(Task.Stage.ARCHITECT, task_id) if task.stage == Task.Stage.DRAFT else task.priority,
            stage_version=F("stage_version") + 1, automatic_pause_reason="", updated_at=timezone.now(),
        )
        if not changed:
            return action_error(request, "The task changed or is not ready for request approval. Reload it first.", 409)
        prefix = "MCP agent approved the request.\n\n" if actor == "mcp" else ""
        Approval.objects.create(task=task, kind=Approval.Kind.SCOPE, content=f"{prefix}{task.title}\n\n{task.request}")
    from ..automation import continue_automatic_task

    continue_automatic_task(task_id)
    return redirect("task_detail", task_id=task_id)


@require_POST
def approve_plan(request, task_id, *, actor="human"):
    version_form = StageForm(request.POST)
    if not version_form.is_valid():
        return action_error(request, "Invalid approval request.", 400)
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if not task.plan.strip():
            return action_error(request, "Write a plan before approving it.", 400)
        changed = Task.objects.filter(
            pk=task_id, state=Task.State.SCOPE_APPROVED,
            stage=Task.Stage.ARCHITECT, status=Task.Status.READY, version=version_form.cleaned_data["version"],
            stage_version=version_form.cleaned_data["stage_version"],
        ).exclude(runs__status__in=(*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)).update(
            state=Task.State.PLAN_APPROVED, stage=Task.Stage.DEVELOPER,
            priority=next_priority_expression(Task.Stage.DEVELOPER, task_id),
            stage_version=F("stage_version") + 1, version=F("version") + 1,
            automatic_pause_reason="", updated_at=timezone.now(),
        )
        if not changed:
            return action_error(request, "The task changed or is not ready for this approval. Reload it first.", 409)
        content = f"Request: {task.title}\n\n{task.request}\n\nPlan:\n{task.plan}"
        if actor == "mcp":
            content = "MCP agent approved the plan.\n\n" + content
        Approval.objects.create(task=task, kind=Approval.Kind.PLAN, content=content)
    from ..automation import continue_automatic_task

    continue_automatic_task(task_id)
    return redirect("task_detail", task_id=task_id)

@require_POST
def continue_task(request, task_id):
    form = StageForm(request.POST)
    if not form.is_valid():
        return render(request, "tasks/error.html", {"message": "Invalid workflow continuation request."}, status=400)
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if (
            task.version != form.cleaned_data["version"]
            or task.stage_version != form.cleaned_data["stage_version"]
        ):
            return render(request, "tasks/error.html", {"message": "The task changed. Reload before continuing the workflow."}, status=409)
        current_run = task.runs.first()
        reason = workflow_continue_block_reason(task, current_run)
        if reason:
            return render(request, "tasks/error.html", {"message": reason}, status=409)
        task.automatic_pause_reason = ""
        task.updated_at = timezone.now()
        task.save(update_fields=["automatic_pause_reason", "updated_at"])
    from ..automation import continue_automatic_task

    continue_automatic_task(task_id)
    return redirect("task_detail", task_id=task_id)

@require_POST
def advance_stage(request, task_id):
    form = StageForm(request.POST)
    if not form.is_valid():
        return render(request, "tasks/error.html", {"message": "Invalid stage transition."}, status=400)
    continue_automation = False
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if workflow_action_blocked(task):
            return render(request, "tasks/error.html", {"message": "The workflow is already advancing. Wait for it to pause before making a manual decision."}, status=409)
        if task.stage == Task.Stage.DRAFT:
            next_stage = Task.Stage.ARCHITECT
        elif task.stage == Task.Stage.DEVELOPER:
            run = task.runs.first()
            if not completed_developer_run(task, run):
                return render(request, "tasks/error.html", {"message": "A completed developer run for the approved plan is required."}, status=409)
            next_stage = Task.Stage.TESTING
            continue_automation = True
        else:
            return render(request, "tasks/error.html", {"message": "This stage cannot advance using that action."}, status=409)
        changed = Task.objects.filter(
            pk=task_id, stage=task.stage, status=Task.Status.READY, version=form.cleaned_data["version"],
            stage_version=form.cleaned_data["stage_version"],
        ).exclude(runs__status__in=(*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)).update(
            stage=next_stage, priority=next_priority_expression(next_stage, task_id),
            stage_version=F("stage_version") + 1, updated_at=timezone.now(),
            **({"automatic_pause_reason": ""} if task.stage == Task.Stage.DEVELOPER else {}),
        )
        if not changed:
            return render(request, "tasks/error.html", {"message": "The task changed or a run needs attention. Reload before advancing."}, status=409)
        content = (
            f"Human approved Developer run #{run.pk} and advanced to Testing."
            if task.stage == Task.Stage.DEVELOPER
            else f"Human advanced from {task.get_stage_display()} to {Task.Stage(next_stage).label}."
        )
        Approval.objects.create(task=task, kind=Approval.Kind.STAGE, content=content)
    if continue_automation:
        from ..automation import continue_automatic_task

        continue_automatic_task(task_id)
    return redirect("task_detail", task_id=task_id)


@require_POST
def refuse_developer(request, task_id):
    form = ReworkForm(request.POST)
    if not form.is_valid():
        response = task_detail(request, task_id, rework_form=form)
        response.status_code = 400
        return response
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if workflow_action_blocked(task):
            return render(request, "tasks/error.html", {"message": "The workflow is already advancing. Wait for it to pause before making a manual decision."}, status=409)
        previous = task.runs.first()
        if not completed_developer_run(task, previous) or task.status != Task.Status.READY or task.finished:
            return render(request, "tasks/error.html", {"message": "No current completed Developer run is available for refusal."}, status=409)
        note = f"Human refused Developer run #{previous.pk} and requested rework:\n{form.cleaned_data['reason']}"
        try:
            run = create_run(
                task, stage=Task.Stage.DEVELOPER, task_version=task.version,
                work_cycle=task.work_cycle + 1,
                request_snapshot=f"{task.title}\n\n{task.request}", plan_snapshot=task.plan,
                context_snapshot=note,
            )
        except AcpUnavailable as error:
            form.add_error(None, str(error))
            response = task_detail(request, task_id, rework_form=form)
            response.status_code = 409
            return response
        changed = Task.objects.filter(
            pk=task_id, stage=Task.Stage.DEVELOPER, state=Task.State.PLAN_APPROVED,
            status=Task.Status.READY, finished=False, version=form.cleaned_data["version"],
            stage_version=form.cleaned_data["stage_version"],
        ).update(
            stage_version=F("stage_version") + 1, work_cycle=F("work_cycle") + 1,
            rework_note=note, automatic_pause_reason="", updated_at=timezone.now(),
        )
        if not changed:
            run.delete()
            return render(request, "tasks/error.html", {"message": "The task changed. Reload before refusing the run."}, status=409)
        Approval.objects.create(task=task, kind=Approval.Kind.STAGE, content=note)
    from ..automation import _launch

    _launch(run)
    return redirect("task_detail", task_id=task_id)


@require_POST
def start_run(request, task_id):
    version_form = StageForm(request.POST)
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if workflow_action_blocked(task):
            return render(request, "tasks/error.html", {"message": "The workflow is already advancing. Wait for it to pause before starting a manual run."}, status=409)
        if (
            not version_form.is_valid() or task.version != version_form.cleaned_data["version"]
            or task.stage_version != version_form.cleaned_data["stage_version"]
            or task.state != Task.State.PLAN_APPROVED or task.finished
            or task.status != Task.Status.READY
            or task.stage not in (Task.Stage.DEVELOPER, Task.Stage.TESTING, Task.Stage.REVIEW)
        ):
            return render(request, "tasks/error.html", {"message": "Approve the current request and plan and reload the current stage before starting."}, status=409)
        if completed_developer_run(task, task.runs.first()):
            return render(request, "tasks/error.html", {"message": "Approve or refuse the completed Developer run first."}, status=409)
        testing_decision = task.gate_decisions.filter(
            stage=Task.Stage.TESTING, task_version=task.version, work_cycle=task.work_cycle,
        ).first()
        context = task.rework_note if task.stage == Task.Stage.DEVELOPER else ""
        if task.stage == Task.Stage.REVIEW and testing_decision:
            source = "Agent-reported" if testing_decision.automatic else "MCP agent-recorded" if testing_decision.recorded_by == "mcp" else "Human-recorded"
            context = f"{source} testing result: {testing_decision.get_result_display()}\nEvidence: {testing_decision.evidence}\nWaiver: {testing_decision.waiver or 'None'}"
        try:
            run = create_run(
                task, stage=task.stage, task_version=task.version, work_cycle=task.work_cycle,
                request_snapshot=f"{task.title}\n\n{task.request}", plan_snapshot=task.plan,
                context_snapshot=context,
            )
        except AcpUnavailable as error:
            return render(request, "tasks/error.html", {"message": str(error)}, status=409)
        if task.automatic_pause_reason:
            Task.objects.filter(pk=task.pk).update(automatic_pause_reason="")
    from ..agent_scheduler import wake_scheduler
    try:
        wake_scheduler()
    except RuntimeError as error:
        run.status = AgentRun.Status.INTERRUPTED
        run.error = f"Could not start worker: {error}"
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
    return redirect("task_detail", task_id=task_id)

@require_POST
def finish_task(request, task_id):
    get_object_or_404(Task, pk=task_id)
    return render(request, "tasks/error.html", {"message": "Record a Review gate result before finishing."}, status=409)

def save_gate_decision(task, form, source_run=None, *, actor="human"):
    advance = form.cleaned_data["action"] == "advance"
    next_stage = (Task.Stage.REVIEW if task.stage == Task.Stage.TESTING else Task.Stage.FINISHED) if advance else task.stage
    if advance and task.status != Task.Status.READY:
        return False
    priority_update = {"priority": next_priority_expression(next_stage, task.pk)} if next_stage != task.stage else {}
    changed = Task.objects.filter(
        pk=task.pk, state=Task.State.PLAN_APPROVED, stage=task.stage, finished=False,
        version=form.cleaned_data["version"], stage_version=form.cleaned_data["stage_version"],
    ).exclude(runs__status__in=(*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)).update(
        stage=next_stage, finished=next_stage == Task.Stage.FINISHED,
        status=Task.Status.DONE if next_stage == Task.Stage.FINISHED else task.status,
        automatic_pause_reason="" if advance else task.automatic_pause_reason,
        stage_version=F("stage_version") + 1, updated_at=timezone.now(),
        **priority_update,
    )
    if not changed:
        return False
    GateDecision.objects.create(
        task=task, source_run=source_run, stage=task.stage, task_version=task.version, work_cycle=task.work_cycle,
        result=form.cleaned_data["result"], evidence=form.cleaned_data["evidence"],
        waiver=form.cleaned_data["waiver"], advanced=advance, recorded_by=actor,
    )
    if next_stage == Task.Stage.FINISHED:
        who = "MCP agent" if actor == "mcp" else "Human"
        Approval.objects.create(task=task, kind=Approval.Kind.FINISH, content=f"{who} recorded the Review gate and marked the task finished.")
    return True


def gate_saved_response(request, task, form):
    advance = form.cleaned_data["action"] == "advance"
    if advance:
        from ..automation import continue_automatic_task

        continue_automatic_task(task.pk)
    outcome = (
        ("Advanced to Review." if task.stage == Task.Stage.TESTING else "Task finished.")
        if advance else "Stage unchanged."
    )
    result = GateDecision.Result(form.cleaned_data["result"]).label
    messages.success(request, f"{task.get_stage_display()} result saved: {result}. {outcome}")
    return redirect("task_detail", task_id=task.pk)


@require_POST
def record_gate(request, task_id, *, actor="human"):
    form = GateDecisionForm(request.POST)
    if not form.is_valid():
        if actor == "mcp":
            return action_error(request, "Invalid gate decision.", 400, errors=form.errors)
        response = task_detail(request, task_id, gate_form=form)
        response.status_code = 400
        return response
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if workflow_action_blocked(task):
            return action_error(request, "The workflow is already advancing. Wait for it to pause before recording a manual gate result.", 409)
        if task.stage not in (Task.Stage.TESTING, Task.Stage.REVIEW):
            return action_error(request, "There is no testing or review gate to record.", 409)
        if not save_gate_decision(task, form, actor=actor):
            return action_error(request, "The task changed or a run needs attention. Reload before recording the gate.", 409)
    return gate_saved_response(request, task, form)

@require_POST
def accept_recommendation(request, task_id):
    form = RecommendationForm(request.POST)
    if not form.is_valid():
        return render(request, "tasks/error.html", {"message": "Invalid recommendation request."}, status=400)
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if workflow_action_blocked(task):
            return render(request, "tasks/error.html", {"message": "The workflow is already advancing. Wait for it to pause before accepting a recommendation manually."}, status=409)
        run = task.runs.first()
        if (
            task.stage not in (Task.Stage.TESTING, Task.Stage.REVIEW)
            or not run or run.pk != form.cleaned_data["run_id"]
            or run.stage != task.stage or run.status != AgentRun.Status.COMPLETED
            or run.task_version != task.version or run.work_cycle != task.work_cycle
        ):
            return render(request, "tasks/error.html", {"message": "This recommendation is no longer current. Reload the task."}, status=409)
        report = final_agent_report(list(run.events.all()))
        digest = hashlib.sha256(report["text"].encode("utf-8")).hexdigest()
        recommendation = report["recommendation"]
        if (
            not hmac.compare_digest(digest, form.cleaned_data["report_hash"])
            or not recommendation
            or recommendation["result"] != GateDecision.Result.PASSED
            or recommendation["recommendation"] != "advance"
        ):
            return render(request, "tasks/error.html", {"message": "The agent did not provide a current passing recommendation to accept."}, status=409)
        decision_form = GateDecisionForm({
            "version": form.cleaned_data["version"], "stage_version": form.cleaned_data["stage_version"],
            "result": GateDecision.Result.PASSED, "evidence": recommendation_evidence(run, recommendation),
            "waiver": "", "action": "advance",
        })
        if not decision_form.is_valid():
            return render(request, "tasks/error.html", {"message": "The recommendation could not be recorded."}, status=400)
        if not save_gate_decision(task, decision_form, source_run=run):
            return render(request, "tasks/error.html", {"message": "The task changed or a run needs attention. Reload before accepting."}, status=409)
    return gate_saved_response(request, task, decision_form)

@require_POST
def return_to_developer(request, task_id):
    form = ReworkForm(request.POST)
    if not form.is_valid():
        response = task_detail(request, task_id, rework_form=form)
        response.status_code = 400
        return response
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if workflow_action_blocked(task):
            return render(request, "tasks/error.html", {"message": "The workflow is already advancing. Wait for it to pause before requesting manual rework."}, status=409)
        if task.stage not in (Task.Stage.TESTING, Task.Stage.REVIEW):
            return render(request, "tasks/error.html", {"message": "Rework is only available from Testing or Review."}, status=409)
        decision = task.gate_decisions.filter(stage=task.stage, task_version=task.version, work_cycle=task.work_cycle).first()
        note = f"Human requested rework from {task.get_stage_display()}:\n{form.cleaned_data['reason']}"
        if decision:
            source = "agent-reported" if decision.automatic else "MCP agent-recorded" if decision.recorded_by == "mcp" else "human-recorded"
            note += f"\n\nLatest {source} {task.get_stage_display()} result: {decision.get_result_display()}\nEvidence: {decision.evidence}"
            if decision.waiver:
                note += f"\nWaiver: {decision.waiver}"
        changed = Task.objects.filter(
            pk=task_id, stage=task.stage, state=Task.State.PLAN_APPROVED, finished=False, status=Task.Status.READY,
            version=form.cleaned_data["version"], stage_version=form.cleaned_data["stage_version"],
        ).exclude(runs__status__in=(*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)).update(
            stage=Task.Stage.DEVELOPER, priority=next_priority_expression(Task.Stage.DEVELOPER, task_id),
            stage_version=F("stage_version") + 1,
            work_cycle=F("work_cycle") + 1, rework_note=note,
            automatic_pause_reason="", updated_at=timezone.now(),
        )
        if not changed:
            return render(request, "tasks/error.html", {"message": "The task changed or a run needs attention. Reload before returning to Developer."}, status=409)
        Approval.objects.create(task=task, kind=Approval.Kind.STAGE, content=note)
    from ..automation import continue_automatic_task

    continue_automatic_task(task_id)
    return redirect("task_detail", task_id=task_id)

@require_POST
def reopen_task(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    version_form = StageForm(request.POST)
    if not version_form.is_valid():
        return render(request, "tasks/error.html", {"message": "Invalid reopen request."}, status=400)
    changed = Task.objects.filter(
        pk=task_id, finished=True, stage=Task.Stage.FINISHED, version=version_form.cleaned_data["version"],
        stage_version=version_form.cleaned_data["stage_version"],
    ).update(
        finished=False, stage=Task.Stage.REVIEW, status=Task.Status.READY,
        priority=next_priority_expression(Task.Stage.REVIEW, task_id), stage_version=F("stage_version") + 1,
        automatic_pause_reason="Task reopened. Review its current state before continuing the workflow.",
        updated_at=timezone.now(),
    )
    if not changed:
        return render(request, "tasks/error.html", {"message": "The task changed. Reload it before reopening."}, status=409)
    return redirect("task_detail", task_id=task_id)
