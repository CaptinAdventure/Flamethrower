import json
import re

from django.db import transaction
from django.utils import timezone

from .models import AgentQuestion, AgentRun, DeferredTask, LinkType, Task, TaskLink
from .recommendations import final_agent_report

START = "FLAMETHROWER_DEFERRED_TASKS"
END = "END_FLAMETHROWER_DEFERRED_TASKS"
BLOCK = re.compile(
    r"(?:\A|\n)FLAMETHROWER_DEFERRED_TASKS\r?\n(.*?)\r?\nEND_FLAMETHROWER_DEFERRED_TASKS"
    r"\s*(?:FLAMETHROWER_GATE_RECOMMENDATION\r?\n.*?\r?\nEND_FLAMETHROWER_GATE_RECOMMENDATION\s*"
    r"|FLAMETHROWER_DEVELOPER_COMPLETE\r?\nEND_FLAMETHROWER_DEVELOPER_COMPLETE\s*)?\Z",
    re.DOTALL,
)


def validate_deferred_task(title, request):
    if not isinstance(title, str) or not isinstance(request, str):
        raise ValueError("Deferred-task titles and requests must be strings.")
    title, request = title.strip(), request.strip()
    if not title or len(title) > 200 or not request or len(request) > 20000:
        raise ValueError("Deferred tasks need a title of 1–200 characters and a request of 1–20000 characters.")
    return title, request


def create_deferred_task(title, request, *, source_task=None, source_run=None, position=None):
    title, request = validate_deferred_task(title, request)
    if source_run:
        source_task = source_run.task
    with transaction.atomic():
        task = Task.objects.create(title=title, request=request, status=Task.Status.NON_ACCEPTED)
        DeferredTask.objects.create(source_run=source_run, source_task=source_task, task=task, position=position)
        if source_task:
            link_type = LinkType.objects.get(name="Relates")
            source, target = sorted((source_task, task), key=lambda item: item.pk)
            TaskLink.objects.create(link_type=link_type, source=source, target=target)
    return task


def parse_deferred_tasks(text):
    lines = text.splitlines()
    starts = lines.count(START)
    ends = lines.count(END)
    if not starts and not ends:
        return [], ""
    match = BLOCK.search(text)
    if starts != 1 or ends != 1 or not match:
        return [], "Deferred-task report is malformed or not at the end of the final response."
    try:
        data = json.loads(match.group(1))
    except (ValueError, RecursionError):
        return [], "Deferred-task report contains invalid JSON."
    if not isinstance(data, dict) or set(data) != {"tasks"} or not isinstance(data["tasks"], list):
        return [], "Deferred-task report must contain a tasks array."
    if len(data["tasks"]) > 20:
        return [], "Deferred-task report exceeds the limit of 20 tasks."
    tasks = []
    for item in data["tasks"]:
        if not isinstance(item, dict) or set(item) != {"title", "request"}:
            return [], "Each deferred task must contain only a title and request."
        try:
            tasks.append(validate_deferred_task(item["title"], item["request"]))
        except ValueError as error:
            return [], str(error)
    return tasks, ""


def complete_run(run_id, stop_reason):
    with transaction.atomic():
        changed = AgentRun.objects.filter(
            pk=run_id, status=AgentRun.Status.RUNNING, cancel_requested=False,
        ).exclude(questions__outcome=AgentQuestion.Outcome.PENDING).exclude(
            permissions__resolved=False,
        ).update(status=AgentRun.Status.COMPLETED, stop_reason=stop_reason, finished_at=timezone.now())
        if not changed:
            raise RuntimeError("The run was cancelled or changed before completion; inspect it before continuing.")
        run = AgentRun.objects.select_related("task").get(pk=run_id)
        if run.stage in (Task.Stage.ARCHITECT, AgentRun.PROJECT):
            return
        report = final_agent_report(list(run.events.all()))
        tasks, warning = parse_deferred_tasks(report["text"])
        if warning:
            AgentRun.objects.filter(pk=run_id).update(deferred_tasks_warning=warning)
            Task.objects.filter(pk=run.task_id, automatic_pause_reason="").update(
                automatic_pause_reason="Deferred-task report is invalid. Inspect the run before continuing the workflow.",
                updated_at=timezone.now(),
            )
        if tasks:
            for position, (title, request) in enumerate(tasks):
                create_deferred_task(title, request, source_run=run, position=position)
