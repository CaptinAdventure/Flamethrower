from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .acp import AcpUnavailable, create_run
from .models import AgentRun, Approval, GateDecision, Task, next_priority_expression
from .recommendations import developer_complete, final_agent_report, recommendation_evidence


def workflow_action_blocked(task):
    return not task.automatic_pause_reason and task.state == Task.State.PLAN_APPROVED


def workflow_continue_block_reason(task, current_run):
    if task.finished or task.status != Task.Status.READY:
        return "Set the task to Ready before continuing the workflow."
    if current_run:
        if current_run.status == AgentRun.Status.INTERRUPTED:
            return "Inspect and acknowledge the interrupted run before continuing the workflow."
        if current_run.status in AgentRun.ACTIVE:
            if (
                current_run.status in (AgentRun.Status.WAITING, AgentRun.Status.WAITING_ANSWER)
                or current_run.permissions.filter(resolved=False).exists()
                or task.automatic_pause_reason
            ):
                return "Resolve the current run before continuing the workflow."
            return "A run is already active."
    if task.state == Task.State.DRAFT or task.stage == Task.Stage.DRAFT:
        return "Approve the request before continuing the workflow."
    matching_run = bool(
        current_run and current_run.stage == task.stage
        and current_run.task_version == task.version and current_run.work_cycle == task.work_cycle
        and current_run.status == AgentRun.Status.COMPLETED
    )
    if task.stage == Task.Stage.ARCHITECT and task.state == Task.State.SCOPE_APPROVED:
        if task.plan.strip() or matching_run:
            return "Review and approve a plan before continuing the workflow."
        return ""
    if task.state != Task.State.PLAN_APPROVED:
        return "Approve the request and plan before continuing the workflow."
    if task.stage == Task.Stage.DEVELOPER and matching_run and task.automatic_pause_reason:
        return "Approve or refuse the completed Developer run before continuing the workflow."
    if task.stage in (Task.Stage.TESTING, Task.Stage.REVIEW):
        counts = task.automatic_gate_returns
        count = counts.get(task.stage, 0) if isinstance(counts, dict) else 0
        if task.automatic_pause_reason and isinstance(count, int) and count >= 3:
            return "Choose a gate action to resolve the repeated-return checkpoint before continuing the workflow."
        decision = task.gate_decisions.filter(
            stage=task.stage, task_version=task.version, work_cycle=task.work_cycle,
        ).first()
        if decision and not decision.advanced:
            return "Resolve the recorded gate result with a gate action before continuing the workflow."
        if matching_run:
            recommendation = final_agent_report(list(current_run.events.all()))["recommendation"]
            if task.automatic_pause_reason or not recommendation or recommendation["recommendation"] == "hold":
                return "Choose a gate action and save your decision before continuing the workflow."
    return ""


def _pause(task_id, reason):
    Task.objects.filter(
        pk=task_id, automatic_pause_reason="",
    ).update(automatic_pause_reason=reason, updated_at=timezone.now())


def _run_context(task):
    if task.stage == Task.Stage.DEVELOPER:
        return task.rework_note
    if task.stage == Task.Stage.REVIEW:
        decision = task.gate_decisions.filter(
            stage=Task.Stage.TESTING, task_version=task.version, work_cycle=task.work_cycle,
        ).first()
        if decision:
            source = (
                "agent-reported, not independently verified" if decision.automatic else
                "MCP agent-recorded, not independently verified" if decision.recorded_by == "mcp" else
                "human-recorded"
            )
            return (
                f"Testing result ({source}): "
                f"{decision.get_result_display()}\n"
                f"Evidence: {decision.evidence}\nWaiver: {decision.waiver or 'None'}"
            )
    return ""


def _launch(run):
    from .agent import start_worker

    try:
        start_worker(run)
    except RuntimeError as error:
        AgentRun.objects.filter(pk=run.pk).update(
            status=AgentRun.Status.INTERRUPTED,
            error=f"Could not start worker: {error}",
            finished_at=timezone.now(),
        )
        _pause(run.task_id, "The agent worker could not start. Inspect the error before continuing.")


def continue_automatic_task(task_id):
    run_to_process = None
    run_to_launch = None
    with transaction.atomic():
        task = Task.objects.select_for_update().get(pk=task_id)
        if (
            task.automatic_pause_reason or task.finished
            or task.status != Task.Status.READY
            or not (
                (task.state == Task.State.SCOPE_APPROVED and task.stage == Task.Stage.ARCHITECT)
                or (task.state == Task.State.PLAN_APPROVED and task.stage in (
                    Task.Stage.DEVELOPER, Task.Stage.TESTING, Task.Stage.REVIEW,
                ))
            )
        ):
            return
        current_run = task.runs.first()
        if current_run and current_run.status in AgentRun.ACTIVE:
            if current_run.status == AgentRun.Status.WAITING:
                _pause(task.pk, "Paused for an ACP permission request. Review and resolve it before continuing.")
            elif current_run.status == AgentRun.Status.WAITING_ANSWER:
                _pause(task.pk, "Paused for an agent question. Answer it before continuing.")
            return
        if current_run and current_run.status == AgentRun.Status.INTERRUPTED:
            _pause(task.pk, "The latest agent run was interrupted. Inspect and acknowledge it before continuing.")
            return

        if task.stage == Task.Stage.ARCHITECT and task.plan.strip():
            return
        if task.stage == Task.Stage.ARCHITECT:
            if (
                current_run and current_run.status == AgentRun.Status.COMPLETED
                and current_run.stage == task.stage and current_run.task_version == task.version
                and current_run.work_cycle == task.work_cycle
            ):
                return
            run_values = {
                "stage": task.stage, "task_version": task.version, "work_cycle": task.work_cycle,
                "request_snapshot": f"{task.title}\n\n{task.request}", "plan_snapshot": task.plan,
            }
            try:
                run_to_launch = create_run(task, **run_values)
            except AcpUnavailable as error:
                _pause(task.pk, str(error))
                return
        else:
            gate_already_recorded = (
                task.stage in (Task.Stage.TESTING, Task.Stage.REVIEW)
                and task.gate_decisions.filter(
                    stage=task.stage, task_version=task.version, work_cycle=task.work_cycle,
                ).exists()
            )
            if (
                current_run and current_run.status == AgentRun.Status.COMPLETED
                and current_run.stage == task.stage and current_run.task_version == task.version
                and current_run.work_cycle == task.work_cycle
                and not gate_already_recorded
            ):
                run_to_process = current_run.pk
            else:
                try:
                    run_to_launch = create_run(
                        task,
                        stage=task.stage,
                        task_version=task.version,
                        work_cycle=task.work_cycle,
                        request_snapshot=f"{task.title}\n\n{task.request}",
                        plan_snapshot=task.plan,
                        context_snapshot=_run_context(task),
                    )
                except AcpUnavailable as error:
                    _pause(task.pk, str(error))
                    return

    if run_to_process:
        continue_automatic_run(run_to_process)
    elif run_to_launch:
        _launch(run_to_launch)


def _advance_to_gate(task, next_stage, *, is_return):
    counts = dict(task.automatic_gate_returns) if isinstance(task.automatic_gate_returns, dict) else {}
    pause_at_gate = False
    if is_return and task.work_cycle > 0:
        count = counts.get(next_stage, 0)
        count = count if isinstance(count, int) and count >= 0 else 0
        count += 1
        counts[next_stage] = count
        pause_at_gate = count >= 3

    changed = Task.objects.filter(
        pk=task.pk, state=Task.State.PLAN_APPROVED, finished=False, status=Task.Status.READY,
        stage=task.stage, version=task.version, stage_version=task.stage_version,
    ).update(
        stage=next_stage,
        priority=next_priority_expression(next_stage, task.pk),
        stage_version=F("stage_version") + 1,
        automatic_gate_returns=counts,
        updated_at=timezone.now(),
    )
    if not changed:
        _pause(task.pk, "The task changed before automation could advance it.")
        return False

    Approval.objects.create(
        task=task,
        kind=Approval.Kind.STAGE,
        content=f"Workflow advanced from {task.get_stage_display()} to {Task.Stage(next_stage).label}.",
    )
    if pause_at_gate:
        _pause(task.pk, f"Paused on the third return to {Task.Stage(next_stage).label}.")
    return True


def _record_gate_return(task_id, gate, work_cycle):
    if work_cycle <= 0:
        return False
    task = Task.objects.get(pk=task_id)
    counts = dict(task.automatic_gate_returns) if isinstance(task.automatic_gate_returns, dict) else {}
    count = counts.get(gate, 0)
    count = count if isinstance(count, int) and count >= 0 else 0
    count += 1
    counts[gate] = count
    Task.objects.filter(pk=task_id).update(
        automatic_gate_returns=counts,
        updated_at=timezone.now(),
    )
    return count >= 3


def continue_automatic_run(run_id):
    should_continue = False
    with transaction.atomic():
        run = AgentRun.objects.select_related("task").get(pk=run_id)
        task = Task.objects.select_for_update().get(pk=run.task_id)
        if run.automation_processed_at:
            return
        if task.automatic_pause_reason or task.finished or task.status != Task.Status.READY:
            return
        if run.status == AgentRun.Status.COMPLETED:
            processed = AgentRun.objects.filter(
                pk=run.pk, automation_processed_at__isnull=True,
            ).update(automation_processed_at=timezone.now())
            if not processed:
                return
        if run.stage == Task.Stage.ARCHITECT:
            if run.status != AgentRun.Status.COMPLETED:
                _pause(task.pk, "The Architect run did not complete normally. Inspect it before continuing.")
            return
        if task.state != Task.State.PLAN_APPROVED:
            _pause(task.pk, "The request and plan are not approved. Review and approve them before continuing.")
            return
        if task.runs.first().pk != run.pk:
            _pause(task.pk, "A newer agent run exists. Inspect the task before continuing.")
            return
        if run.status != AgentRun.Status.COMPLETED:
            reason = (
                "Paused for an ACP permission request. Review and resolve it before continuing."
                if run.status == AgentRun.Status.WAITING
                else "The agent run did not complete normally. Inspect it before continuing."
            )
            _pause(task.pk, reason)
            return
        if (
            run.stage != task.stage or run.task_version != task.version
            or run.work_cycle != task.work_cycle
        ):
            _pause(task.pk, "The completed run no longer matches the task's current stage or approved plan.")
            return

        if task.stage == Task.Stage.DEVELOPER:
            if not developer_complete(final_agent_report(list(run.events.all()))["text"]):
                _pause(task.pk, "The Developer run has no explicit completion signal. Inspect it before continuing.")
                return
            should_continue = _advance_to_gate(task, Task.Stage.TESTING, is_return=True)
        elif task.stage in (Task.Stage.TESTING, Task.Stage.REVIEW):
            report = final_agent_report(list(run.events.all()))
            recommendation = report["recommendation"]
            if not recommendation:
                _pause(task.pk, "The gate report is missing or invalid. Review it and record a manual decision.")
                return

            evidence = recommendation_evidence(run, recommendation)
            result = recommendation["result"]
            action = recommendation["recommendation"]
            existing = GateDecision.objects.filter(
                task=task, source_run=run, stage=task.stage,
                task_version=task.version, work_cycle=task.work_cycle,
            ).first()

            if result == GateDecision.Result.PASSED and action == "advance":
                next_stage = Task.Stage.REVIEW if task.stage == Task.Stage.TESTING else Task.Stage.FINISHED
                changed = Task.objects.filter(
                    pk=task.pk, state=Task.State.PLAN_APPROVED, stage=task.stage,
                    finished=False, status=Task.Status.READY, version=task.version, stage_version=task.stage_version,
                ).update(
                    stage=next_stage,
                    finished=next_stage == Task.Stage.FINISHED,
                    status=Task.Status.DONE if next_stage == Task.Stage.FINISHED else Task.Status.READY,
                    priority=next_priority_expression(next_stage, task.pk),
                    stage_version=F("stage_version") + 1,
                    updated_at=timezone.now(),
                )
                if not changed:
                    _pause(task.pk, "The task changed before automation could record the gate.")
                    return
                if not existing:
                    GateDecision.objects.create(
                        task=task, source_run=run, stage=task.stage,
                        task_version=task.version, work_cycle=task.work_cycle,
                        result=result, evidence=evidence, advanced=True, automatic=True,
                    )
                if next_stage == Task.Stage.REVIEW:
                    Approval.objects.create(
                        task=task, kind=Approval.Kind.STAGE,
                        content="Workflow advanced from Testing to Review after a passing agent recommendation.",
                    )
                    should_continue = True
                    if _record_gate_return(task.pk, Task.Stage.REVIEW, task.work_cycle):
                        _pause(task.pk, "Paused on the third return to Review.")
                        should_continue = False
                else:
                    Approval.objects.create(
                        task=task, kind=Approval.Kind.FINISH,
                        content="Workflow finished the task after a passing Review recommendation.",
                    )
            else:
                if not existing:
                    GateDecision.objects.create(
                        task=task, source_run=run, stage=task.stage,
                        task_version=task.version, work_cycle=task.work_cycle,
                        result=result, evidence=evidence, advanced=False, automatic=True,
                    )
                _pause(task.pk, "Review the gate recommendation and record a manual decision before continuing.")
        else:
            _pause(task.pk, "The workflow cannot continue from this task stage.")

    if should_continue:
        continue_automatic_task(run.task_id)
