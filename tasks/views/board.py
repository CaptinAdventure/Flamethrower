import hashlib

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..forms import QuickTaskForm
from ..models import AgentRun, Task
from ..search import search_tasks


def task_list(request, *, quick_form=None, status=200):
    search_query = request.GET.get("q", "").strip()
    tasks = Task.objects.all()
    if search_query:
        tasks = search_tasks(search_query)
    stages = [(stage, label) for stage, label in Task.Stage.choices if stage != Task.Stage.FINISHED]
    lanes = [{"key": stage, "name": label, "tasks": []} for stage, label in stages]
    by_stage = {stage: lane["tasks"] for (stage, _), lane in zip(stages, lanes)}
    finished_tasks = []
    abandoned_tasks = []
    for task in tasks.select_related("work_type").order_by("stage", "priority", "pk").prefetch_related("runs", "gate_decisions"):
        run = next(iter(task.runs.all()), None)
        run_status = ""
        if run and (
            run.status in (*AgentRun.ACTIVE, AgentRun.Status.INTERRUPTED)
            or (run.stage == task.stage and run.task_version == task.version and run.work_cycle == task.work_cycle)
        ):
            run_status = "Cancellation requested" if run.cancel_requested and run.status in AgentRun.ACTIVE else run.get_status_display()
        if run and run.cancel_requested and run.status in AgentRun.ACTIVE:
            attention = ""
        elif run and run.status == AgentRun.Status.WAITING:
            attention = "Permission required"
        elif run and run.status == AgentRun.Status.WAITING_ANSWER:
            attention = "Answer required"
        elif run and run.status in AgentRun.ACTIVE:
            attention = ""
        elif run and run.status == AgentRun.Status.INTERRUPTED:
            attention = "Inspect interruption"
        elif task.status == Task.Status.NON_ACCEPTED:
            attention = "Accept task"
        elif task.status == Task.Status.BLOCKED:
            attention = "Blocked"
        elif task.status == Task.Status.ABANDONED:
            attention = ""
        elif task.stage == Task.Stage.DRAFT:
            attention = "Approve request"
        elif task.stage == Task.Stage.ARCHITECT:
            if task.state == Task.State.DRAFT:
                attention = "Approve request"
            else:
                attention = "Approve plan" if task.plan.strip() else "Prepare plan"
        elif task.stage == Task.Stage.DEVELOPER:
            if run and run.stage == Task.Stage.DEVELOPER and run.status == AgentRun.Status.COMPLETED and run.task_version == task.version and run.work_cycle == task.work_cycle:
                attention = "Inspect development"
            else:
                attention = "Start development"
        elif task.stage in (Task.Stage.TESTING, Task.Stage.REVIEW):
            decision = next((
                item for item in task.gate_decisions.all()
                if item.stage == task.stage and item.task_version == task.version and item.work_cycle == task.work_cycle
            ), None)
            if decision:
                attention = "Review recorded result"
            elif run and run.status == AgentRun.Status.COMPLETED and run.stage == task.stage and run.task_version == task.version and run.work_cycle == task.work_cycle:
                attention = "Review agent report"
            else:
                attention = "Record gate result"
        else:
            attention = ""
        item = {"task": task, "attention": attention, "run_status": run_status}
        if task.status == Task.Status.ABANDONED:
            abandoned_tasks.append(item)
        elif task.stage == Task.Stage.FINISHED:
            finished_tasks.append(item)
        else:
            by_stage[task.stage].append(item)
    for lane in lanes:
        lane["order_token"] = lane_order_token([item["task"] for item in lane["tasks"]])
    return render(request, "tasks/list.html", {
        "lanes": lanes,
        "finished_tasks": finished_tasks,
        "abandoned_tasks": abandoned_tasks,
        "project_path": settings.PROJECT_PATH,
        "search_query": search_query,
        "quick_form": quick_form if quick_form is not None else QuickTaskForm(),
    }, status=status)

def lane_order_token(tasks):
    value = ",".join(f"{task.pk}:{task.priority}" for task in tasks)
    return hashlib.sha256(value.encode()).hexdigest()

@require_POST
def reorder_task(request, task_id):
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if task.status == Task.Status.ABANDONED:
            return render(request, "tasks/error.html", {"message": "Abandoned tasks cannot be reordered."}, status=400)
        lane = list(Task.objects.filter(stage=task.stage).exclude(status=Task.Status.ABANDONED).order_by("priority", "pk"))
        if lane_order_token(lane) != request.POST.get("order_token", ""):
            return render(request, "tasks/error.html", {"message": "This lane changed. Reload before reordering."}, status=409)
        index = next(i for i, item in enumerate(lane) if item.pk == task.pk)
        direction = request.POST.get("direction")
        if direction in ("up", "down"):
            new_index = index + (-1 if direction == "up" else 1)
            if not 0 <= new_index < len(lane):
                return render(request, "tasks/error.html", {"message": "The task is already at the edge of this lane."}, status=400)
            lane.insert(new_index, lane.pop(index))
        elif direction in ("before", "after"):
            try:
                target_id = int(request.POST.get("target", ""))
            except ValueError:
                return render(request, "tasks/error.html", {"message": "Invalid priority target."}, status=400)
            if target_id == task.pk or not any(item.pk == target_id for item in lane):
                return render(request, "tasks/error.html", {"message": "The target must be another task in the same lane."}, status=400)
            moved = lane.pop(index)
            target_index = next(i for i, item in enumerate(lane) if item.pk == target_id)
            lane.insert(target_index + (direction == "after"), moved)
        else:
            return render(request, "tasks/error.html", {"message": "Invalid priority direction."}, status=400)
        for priority, item in enumerate(lane, 1):
            item.priority = priority
        Task.objects.bulk_update(lane, ["priority"])
    return redirect("task_list")
