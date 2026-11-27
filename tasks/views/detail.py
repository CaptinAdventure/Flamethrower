import hashlib
import json
from django.conf import settings
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .activity import tool_call_summary
from .planning import current_architect_proposal
from ..acp import task_acp_state
from ..agent import protected_read_directories
from ..automation import workflow_action_blocked, workflow_continue_block_reason
from ..command_permissions import reported_commands
from ..mcp_permissions import reported_mcp_kind_mismatch, reported_mcp_tool
from ..forms import ArchitectRefinementForm, GateDecisionForm, RecommendationForm, ReworkForm, StageForm
from ..models import AgentRun, RepositorySettings, Task, TaskLinkEvent
from ..questions import QuestionForm
from ..recommendations import final_agent_report, recommendation_evidence
from ..similarity import settings_instance, suggestions, work_type_suggestion


def task_link_context(task, *, manage_links=False):
    linked_tasks = [
        {"link": link, "description": link.link_type.outward_description, "other": link.target}
        for link in task.outgoing_links.select_related("link_type", "target__work_type").order_by("link_type__name", "target__title", "target_id")
    ] + [
        {"link": link, "description": link.link_type.inward_description, "other": link.source}
        for link in task.incoming_links.select_related("link_type", "source__work_type").order_by("link_type__name", "source__title", "source_id")
    ]
    linked_task_groups = {}
    for item in linked_tasks:
        item["actions"] = [{
            "url": reverse("remove_task_link", args=[task.pk, item["link"].pk]),
            "label": f"Unlink task #{item['other'].pk}",
            "icon": "remove",
            "style": "danger",
            "hide_popover": True,
            "fields": {} if manage_links else {"return_to": "detail"},
        }]
        linked_task_groups.setdefault(item["description"], []).append(item)
    link_history = []
    for event in TaskLinkEvent.objects.filter(Q(source=task) | Q(target=task)).select_related("source", "target")[:50]:
        outgoing = event.source_id == task.pk
        link_history.append({
            "event": event,
            "description": event.outward_description if outgoing else event.inward_description,
            "other": event.target if outgoing else event.source,
            "title": event.target_title if outgoing else event.source_title,
        })
    return {"linked_tasks": linked_tasks, "linked_task_groups": linked_task_groups, "link_history": link_history}


def suggestion_rows(task):
    rows = suggestions(task, settings_instance())
    for row in rows:
        candidate = row["task"]
        url = reverse("review_suggestion", args=[task.pk, candidate.pk])
        fields = {"task_revision": task.indexing_revision, "candidate_revision": candidate.indexing_revision}
        row["actions"] = [
            {"url": url, "label": f"Link task #{candidate.pk} as relates", "icon": "check",
             "style": "accept", "fields": fields, "name": "action", "value": "accept"},
            {"url": url, "label": f"Dismiss suggestion #{candidate.pk}", "icon": "remove",
             "style": "danger", "fields": fields, "name": "action", "value": "dismiss"},
        ]
    return rows


def run_attention_context(current_run, question_form=None, question_form_question_id=None):
    permission = None
    question = None
    directory_approval_option = None
    directory_approval_paths = []
    directory_approval_enabled = False
    command_approval_commands = []
    mcp_approval_tool = None
    if current_run and current_run.status in AgentRun.ACTIVE:
        question = current_run.questions.filter(outcome="pending").order_by("id").first()
        if question_form is not None and (not question or question.pk != question_form_question_id):
            question_form = None
        if question and not current_run.cancel_requested and question_form is None:
            question_form = QuestionForm(question.schema, prefix="answer")
        permission = current_run.permissions.filter(resolved=False).order_by("-id").first()
        if permission and not current_run.cancel_requested:
            directory_approval_option = next((
                option for option in permission.options
                if option.get("kind") == "allow_once"
                and isinstance(option.get("optionId"), str) and option["optionId"]
            ), None)
            if directory_approval_option:
                repository_settings = RepositorySettings.objects.get(pk=1)
                directory_approval_paths = protected_read_directories(
                    permission.tool_call, repository_settings.safe_read_paths,
                )
                directory_approval_enabled = repository_settings.allow_safe_operations
                commands = reported_commands(permission.tool_call)
                if commands:
                    command_approval_commands = [" ".join(command) for command in commands]
                mcp_approval_tool = reported_mcp_tool(permission.tool_call)
    return {
        "permission": permission,
        "question": question if current_run and not current_run.cancel_requested else None,
        "question_form": question_form,
        "mcp_approval_tool": mcp_approval_tool,
        "mcp_kind_mismatch": reported_mcp_kind_mismatch(permission.tool_call) if mcp_approval_tool else None,
        "directory_approval_option": directory_approval_option,
        "directory_approval_paths": directory_approval_paths,
        "directory_approval_enabled": directory_approval_enabled,
        "command_approval_commands": command_approval_commands,
        "permission_summary": tool_call_summary(permission.tool_call) if permission else None,
        "permission_details": json.dumps(permission.tool_call, indent=2, ensure_ascii=False) if permission else "",
    }


def task_detail(request, task_id, gate_form=None, rework_form=None, architect_refinement_form=None, question_form=None, question_form_question_id=None):
    task = get_object_or_404(Task, pk=task_id)
    runs = task.runs.select_related("acp_endpoint")
    current_run = runs.first()
    selected_run = request.GET.get("run") if request.method == "GET" else None
    if selected_run is not None:
        try:
            run = get_object_or_404(runs, pk=int(selected_run))
        except ValueError:
            raise Http404
        return redirect("run_activity", task_id=task.pk, run_id=run.pk)
    acp_state = task_acp_state(task)
    blocking_run = acp_state["blocking_run"]
    stage_initial = {"version": task.version, "stage_version": task.stage_version}
    current_report = None
    developer_report = ""
    developer_ready = bool(
        task.stage == Task.Stage.DEVELOPER and task.state == Task.State.PLAN_APPROVED
        and current_run and current_run.stage == Task.Stage.DEVELOPER
        and current_run.status == AgentRun.Status.COMPLETED
        and current_run.task_version == task.version and current_run.work_cycle == task.work_cycle
    )
    if developer_ready:
        developer_report = final_agent_report(list(current_run.events.all()))["text"]
    architect_proposal = None
    architect_form = None
    proposal = current_architect_proposal(task) if task.stage == Task.Stage.ARCHITECT else None
    if proposal:
        architect_proposal = proposal["plan"]
        architect_form = RecommendationForm(initial={
            **stage_initial, "run_id": proposal["run"].pk, "report_hash": proposal["hash"],
        })
    if (
            current_run and current_run.status == AgentRun.Status.COMPLETED
            and current_run.stage == task.stage and task.stage in (Task.Stage.TESTING, Task.Stage.REVIEW)
            and current_run.task_version == task.version and current_run.work_cycle == task.work_cycle
    ):
        current_report = final_agent_report(list(current_run.events.all()))
        current_report["run"] = current_run
        current_report["hash"] = hashlib.sha256(current_report["text"].encode("utf-8")).hexdigest()
    latest_decision = task.gate_decisions.filter(
        stage=task.stage, task_version=task.version, work_cycle=task.work_cycle,
    ).first()
    gate_initial = dict(stage_initial)
    rework_initial = dict(stage_initial)
    workflow_action = request.GET.get("next_action", "")
    if gate_form is not None and gate_form.is_bound:
        workflow_action = gate_form.data.get("gate_action") or (
            "waiver" if gate_form.data.get("action") == "advance" and gate_form.data.get("result") != "passed"
            else "pass" if gate_form.data.get("result") == "passed" else "fail"
        )
    elif rework_form is not None and rework_form.is_bound:
        workflow_action = "refuse" if task.stage == Task.Stage.DEVELOPER else "rework"
    elif architect_refinement_form is not None and architect_refinement_form.is_bound:
        workflow_action = "refine_plan"
    if workflow_action not in ("pass", "fail", "hold", "waiver", "rework", "run", "approve", "refuse", "accept_plan", "refine_plan", "abandon_plan"):
        workflow_action = ""
    if latest_decision and not latest_decision.advanced:
        gate_initial.update(result=latest_decision.result, evidence=latest_decision.evidence, waiver=latest_decision.waiver)
    elif current_report and current_report["text"]:
        recommendation = current_report["recommendation"]
        if recommendation:
            gate_initial["evidence"] = recommendation_evidence(current_run, recommendation)
        else:
            gate_initial["evidence"] = f"Agent report from run #{current_run.pk} (not independently verified):\n{current_report['text']}"
    if current_report and current_report["recommendation"] and current_report["recommendation"]["recommendation"] == "rework":
        rework_initial["reason"] = recommendation_evidence(current_run, current_report["recommendation"])
    if workflow_action in ("pass", "fail", "hold", "waiver"):
        gate_initial.update(gate_action=workflow_action, action="save" if workflow_action in ("fail", "hold") else "advance")
        if workflow_action != "waiver":
            gate_initial.update(result={"pass": "passed", "fail": "failed", "hold": "unverified"}[workflow_action], waiver="")
        elif gate_initial.get("result") not in ("failed", "unverified"):
            gate_initial["result"] = "unverified"
    stages = [
        (Task.Stage.DRAFT, "Draft"),
        (Task.Stage.ARCHITECT, "Architect"),
        (Task.Stage.DEVELOPER, "Dev"),
        (Task.Stage.TESTING, "Testing"),
        (Task.Stage.REVIEW, "Review"),
        (Task.Stage.FINISHED, "Done"),
    ]
    current_stage_index = next(i for i, (stage, _) in enumerate(stages) if stage == task.stage)
    workflow_steps = [
        {
            "key": stage,
            "label": label,
            "current": i == current_stage_index,
            "complete": i < current_stage_index,
            "next": i == current_stage_index + 1 and not task.finished,
        }
        for i, (stage, label) in enumerate(stages)
    ]
    indexing_active = Task.objects.filter(
        relates_indexing__in=[Task.IndexingStatus.PENDING, Task.IndexingStatus.IN_PROGRESS],
    ).exists()
    current_run_active = bool(current_run and current_run.status in AgentRun.ACTIVE)
    show_next_action_request = (
        not current_run_active
        and not (current_run and current_run.status == AgentRun.Status.INTERRUPTED)
        and not task.finished
        and task.status == Task.Status.READY
        and (
            task.stage == Task.Stage.DRAFT
            or (task.stage == Task.Stage.ARCHITECT and task.state == Task.State.DRAFT)
        )
    )
    can_approve_request = (
        task.stage in (Task.Stage.DRAFT, Task.Stage.ARCHITECT)
        and task.state == Task.State.DRAFT
        and task.status == Task.Status.READY
        and not task.finished
        and not task.runs.filter(status__in=(*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)).exists()
    )
    return render(request, "tasks/detail.html", {
        "task": task,
        "inline_title_edit": True,
        "can_approve_request": can_approve_request,
        "show_next_action_request": show_next_action_request,
        "workflow_action": workflow_action,
        "workflow_continue_block_reason": workflow_continue_block_reason(task, current_run),
        "manual_workflow_actions_available": not workflow_action_blocked(task),
        **task_link_context(task),
        "workflow_steps": workflow_steps,
        "stage_form": StageForm(initial=stage_initial),
        "gate_form": gate_form if gate_form is not None else GateDecisionForm(initial=gate_initial),
        "rework_form": rework_form if rework_form is not None else ReworkForm(initial=rework_initial),
        "recommendation_form": RecommendationForm(initial={
            **stage_initial, "run_id": current_run.pk, "report_hash": current_report["hash"],
        }) if current_report else None,
        "current_report": current_report,
        "latest_decision": latest_decision,
        "developer_report": developer_report,
        "architect_proposal": architect_proposal,
        "architect_form": architect_form,
        "architect_refinement_form": architect_refinement_form if architect_refinement_form is not None else (
            ArchitectRefinementForm(initial=architect_form.initial) if architect_form else None
        ),
        "approvals": task.approvals.all(),
        "gate_decisions": task.gate_decisions.all(),
        "run": current_run,
        "deferred_origin": getattr(task, "deferred_origin", None),
        "current_run": current_run,
        "current_run_active": current_run_active,
        "blocking_run": blocking_run,
        "should_poll": current_run_active or bool(blocking_run and blocking_run.status in AgentRun.ACTIVE) or indexing_active,
        "indexing_active": indexing_active,
        "suggestions": suggestion_rows(task),
        "work_type_suggestion": work_type_suggestion(task),
        "developer_ready": developer_ready,
        "runs": runs,
        **run_attention_context(current_run, question_form, question_form_question_id),
        **acp_state,
    })
