from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, QueryDict

from .forms import TaskForm, TaskLinkForm
from .models import AgentRun, LinkType, Task, TaskLink, WorkType
from .similarity import settings_instance, suggestions
from .views import editing, planning, workflow
from .views.planning import current_architect_proposal

MAX_ID = 9223372036854775807
TEXT_PREVIEW = 20000
GATE_PREVIEW = 1000


class McpInputError(ValueError):
    pass


def fields(data, required, optional=()):
    if not required <= data.keys() or data.keys() - required - set(optional):
        raise McpInputError(f"Expected {', '.join(sorted(required))} and optional {', '.join(sorted(optional)) or 'no other fields'}.")


def integer(value, name, minimum=1, maximum=MAX_ID):
    if type(value) is not int or not minimum <= value <= maximum:
        raise McpInputError(f"{name} must be an integer between {minimum} and {maximum}.")
    return value


def page(data):
    limit = integer(data.get("limit", 20), "limit", 1, 100)
    offset = integer(data.get("offset", 0), "offset", 0, MAX_ID - 101)
    return limit, offset


def get_task(task_id):
    return Task.objects.select_related("work_type").filter(pk=integer(task_id, "task_id")).first()


def task_data(task):
    result = {
        "id": task.pk, "title": task.title,
        "status": task.status, "stage": task.stage, "state": task.state,
        "version": task.version, "stage_version": task.stage_version, "work_cycle": task.work_cycle,
        "severity": task.severity, "work_type_id": task.work_type_id, "work_type": task.work_type.title,
        "priority": task.priority, "finished": task.finished,
        "indexing_status": task.relates_indexing, "indexing_revision": task.indexing_revision,
        "updated_at": task.updated_at.isoformat(),
    }
    for key, text in (("request", task.request), ("plan", task.plan),
                      ("blocked_reason", task.blocked_reason), ("pause_reason", task.automatic_pause_reason)):
        result[key] = text[:TEXT_PREVIEW]
        result[f"{key}_truncated"] = len(text) > TEXT_PREVIEW
    return result


def link_data(link):
    return {
        "id": link.pk, "type_id": link.link_type_id, "type": link.link_type.name,
        "source_id": link.source_id, "target_id": link.target_id,
        "outward_description": link.link_type.outward_description,
        "inward_description": link.link_type.inward_description,
    }


def run_data(run):
    return {
        "id": run.pk, "stage": run.stage, "status": run.status, "work_cycle": run.work_cycle,
        "task_version": run.task_version, "created_at": run.created_at.isoformat(),
    }


def gate_data(decision):
    return {
        "id": decision.pk, "stage": decision.stage, "result": decision.result,
        "evidence": decision.evidence[:GATE_PREVIEW], "evidence_truncated": len(decision.evidence) > GATE_PREVIEW,
        "waiver": decision.waiver[:GATE_PREVIEW], "waiver_truncated": len(decision.waiver) > GATE_PREVIEW,
        "advanced": decision.advanced, "automatic": decision.automatic,
        "recorded_by": "automation" if decision.automatic else decision.recorded_by,
        "source_run_id": decision.source_run_id, "work_cycle": decision.work_cycle,
    }


def missing():
    return JsonResponse({"error": "Task not found."}, status=404)


def list_tasks(request, data):
    fields(data, set(), ("limit", "offset", "status", "stage"))
    limit, offset = page(data)
    tasks = Task.objects.select_related("work_type")
    for key, choices in (("status", Task.Status.values), ("stage", Task.Stage.values)):
        if key in data:
            if not isinstance(data[key], str) or data[key] not in choices:
                raise McpInputError(f"Invalid {key}.")
            tasks = tasks.filter(**{key: data[key]})
    rows = list(tasks.order_by("-updated_at", "-pk")[offset:offset + limit + 1])
    results = []
    for task in rows[:limit]:
        item = task_data(task)
        for key in ("request", "plan", "blocked_reason", "pause_reason"):
            item.pop(key)
            item.pop(f"{key}_truncated")
        results.append(item)
    return JsonResponse({"results": results, "offset": offset, "has_more": len(rows) > limit})


def read_task(request, data):
    fields(data, {"task_id"})
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    result = task_data(task)
    runs = list(task.runs.order_by("-id")[:21])
    decisions = list(task.gate_decisions.order_by("-id")[:21])
    result["runs"] = [run_data(run) for run in runs[:20]]
    result["runs_has_more"] = len(runs) > 20
    result["gate_decisions"] = [gate_data(decision) for decision in decisions[:20]]
    result["gate_decisions_has_more"] = len(decisions) > 20
    links = list(TaskLink.objects.filter(Q(source=task) | Q(target=task)).select_related("link_type").order_by("pk")[:21])
    result["links"] = [link_data(link) for link in links[:20]]
    result["links_has_more"] = len(links) > 20
    proposal = current_architect_proposal(task)
    if proposal:
        plan = proposal["plan"]
        result["architect_proposal"] = {
            "run_id": proposal["run"].pk, "report_hash": proposal["hash"],
            "plan": plan[:TEXT_PREVIEW], "plan_truncated": len(plan) > TEXT_PREVIEW,
        }
    return JsonResponse(result)


def read_task_text(request, data):
    fields(data, {"task_id", "field"}, ("limit", "offset"))
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    field = data["field"]
    if field not in ("request", "plan", "blocked_reason", "pause_reason", "architect_proposal"):
        raise McpInputError("Invalid task text field.")
    limit = integer(data.get("limit", 10000), "limit", 1, 20000)
    offset = integer(data.get("offset", 0), "offset", 0)
    proposal = current_architect_proposal(task) if field == "architect_proposal" else None
    if field == "architect_proposal" and proposal is None:
        return JsonResponse({"error": "No current Architect proposal."}, status=404)
    text = proposal["plan"] if proposal else getattr(task, "automatic_pause_reason" if field == "pause_reason" else field)
    result = {"field": field, "content": text[offset:offset + limit], "offset": offset,
              "has_more": len(text) > offset + limit, "version": task.version, "stage_version": task.stage_version}
    if proposal:
        result["report_hash"] = proposal["hash"]
    return JsonResponse(result)


def list_task_runs(request, data):
    fields(data, {"task_id"}, ("limit", "offset"))
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    limit, offset = page(data)
    runs = list(task.runs.order_by("-id")[offset:offset + limit + 1])
    return JsonResponse({"results": [run_data(run) for run in runs[:limit]], "offset": offset, "has_more": len(runs) > limit})


def list_gate_decisions(request, data):
    fields(data, {"task_id"}, ("limit", "offset"))
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    limit, offset = page(data)
    decisions = list(task.gate_decisions.order_by("-id")[offset:offset + limit + 1])
    return JsonResponse({"results": [gate_data(item) for item in decisions[:limit]], "offset": offset, "has_more": len(decisions) > limit})


def read_gate_text(request, data):
    fields(data, {"task_id", "decision_id", "field"}, ("limit", "offset"))
    task_id = integer(data["task_id"], "task_id")
    decision_id = integer(data["decision_id"], "decision_id")
    limit = integer(data.get("limit", 10000), "limit", 1, 20000)
    offset = integer(data.get("offset", 0), "offset", 0)
    if data["field"] not in ("evidence", "waiver"):
        raise McpInputError("field must be 'evidence' or 'waiver'.")
    task = Task.objects.filter(pk=task_id).first()
    decision = task.gate_decisions.filter(pk=decision_id).first() if task else None
    if decision is None:
        return JsonResponse({"error": "Gate decision not found."}, status=404)
    text = getattr(decision, data["field"])
    return JsonResponse({"id": decision.pk, "field": data["field"], "content": text[offset:offset + limit],
                         "offset": offset, "has_more": len(text) > offset + limit})


def read_run(request, data):
    fields(data, {"task_id", "run_id"}, ("limit", "offset"))
    task_id = integer(data["task_id"], "task_id")
    run_id = integer(data["run_id"], "run_id")
    limit, offset = page(data)
    run = AgentRun.objects.filter(task_id=task_id, pk=run_id).first()
    if run is None:
        return JsonResponse({"error": "Run not found."}, status=404)
    events = list(run.events.order_by("id")[offset:offset + limit + 1])
    return JsonResponse({
        "id": run.pk, "task_id": run.task_id, "stage": run.stage, "status": run.status,
        "work_cycle": run.work_cycle, "task_version": run.task_version,
        "error": run.error[:5000], "error_truncated": len(run.error) > 5000,
        "created_at": run.created_at.isoformat(),
        "events": [{"id": event.pk, "kind": event.kind, "content": event.content[:2000],
                    "content_truncated": len(event.content) > 2000, "created_at": event.created_at.isoformat()}
                   for event in events[:limit]],
        "offset": offset, "has_more": len(events) > limit,
    })


def read_run_error(request, data):
    fields(data, {"task_id", "run_id"}, ("offset", "limit"))
    task_id = integer(data["task_id"], "task_id")
    run_id = integer(data["run_id"], "run_id")
    limit = integer(data.get("limit", 10000), "limit", 1, 20000)
    offset = integer(data.get("offset", 0), "offset", 0)
    run = AgentRun.objects.filter(task_id=task_id, pk=run_id).first()
    if run is None:
        return JsonResponse({"error": "Run not found."}, status=404)
    return JsonResponse({"id": run.pk, "content": run.error[offset:offset + limit],
                         "offset": offset, "has_more": len(run.error) > offset + limit})


def read_run_event(request, data):
    fields(data, {"task_id", "run_id", "event_id"}, ("offset", "limit"))
    task_id = integer(data["task_id"], "task_id")
    run_id = integer(data["run_id"], "run_id")
    event_id = integer(data["event_id"], "event_id")
    limit = integer(data.get("limit", 10000), "limit", 1, 20000)
    offset = integer(data.get("offset", 0), "offset", 0)
    event = AgentRun.objects.filter(task_id=task_id, pk=run_id).first()
    event = event.events.filter(pk=event_id).first() if event else None
    if event is None:
        return JsonResponse({"error": "Run event not found."}, status=404)
    return JsonResponse({
        "id": event.pk, "kind": event.kind, "content": event.content[offset:offset + limit],
        "offset": offset, "has_more": len(event.content) > offset + limit,
    })


def list_links(request, data):
    fields(data, {"task_id"}, ("limit", "offset"))
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    limit, offset = page(data)
    links = list(TaskLink.objects.filter(Q(source=task) | Q(target=task)).select_related("link_type").order_by("pk")[offset:offset + limit + 1])
    return JsonResponse({"results": [link_data(link) for link in links[:limit]], "offset": offset, "has_more": len(links) > limit})


def similar_tasks(request, data):
    fields(data, {"task_id"}, ("limit", "offset"))
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    limit, offset = page(data)
    rows = suggestions(task, settings_instance())[offset:offset + limit + 1]
    results = []
    for row in rows[:limit]:
        item = task_data(row["task"])
        for key in ("request", "plan", "blocked_reason", "pause_reason"):
            item.pop(key)
            item.pop(f"{key}_truncated")
        results.append({**item, "score": row["score"]})
    return JsonResponse({
        "indexing_status": task.relates_indexing, "indexing_revision": task.indexing_revision,
        "results": results, "offset": offset, "has_more": len(rows) > limit,
    })


def create_task(request, data):
    fields(data, {"title", "request"}, ("plan", "work_type_id"))
    for name in ("title", "request", "plan"):
        if name in data and (not isinstance(data[name], str) or len(data[name]) > 20000):
            raise McpInputError(f"{name} must be a string of at most 20000 characters.")
    values = {"title": data["title"], "request": data["request"], "plan": data.get("plan", "")}
    if "work_type_id" in data:
        values["work_type"] = integer(data["work_type_id"], "work_type_id")
    form = TaskForm(values)
    if not form.is_valid():
        return JsonResponse({"error": "Invalid task.", "fields": form.errors.get_json_data()}, status=400)
    task = form.save()
    return JsonResponse(task_data(task), status=201)


def call_view(request, task_id, view, values, *, actor=False):
    post = QueryDict("", mutable=True)
    for key, value in values.items():
        post[key] = str(value) if value is not None else ""
    request.POST = post
    request._mcp_action = True
    response = view(request, task_id, **({"actor": "mcp"} if actor else {}))
    if response.status_code != 302:
        return response
    task = get_task(task_id)
    return JsonResponse(task_data(task))


def update_task(request, data):
    fields(data, {"task_id", "version", "stage_version"}, ("title", "request", "plan"))
    if not any(key in data for key in ("title", "request", "plan")):
        raise McpInputError("Provide at least one task field to update.")
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    for key in ("title", "request", "plan"):
        if key in data and (not isinstance(data[key], str) or len(data[key]) > 20000):
            raise McpInputError(f"{key} must be a string of at most 20000 characters.")
    values = {key: data.get(key, getattr(task, key)) for key in ("title", "request", "plan")}
    values.update(
        version=integer(data["version"], "version", 0), stage_version=integer(data["stage_version"], "stage_version", 0),
    )
    return call_view(request, task.pk, editing.task_edit, values)


def update_metadata(request, data):
    fields(data, {"task_id", "version", "stage_version"}, ("status", "severity", "work_type_id", "blocked_reason"))
    if not any(key in data for key in ("status", "severity", "work_type_id", "blocked_reason")):
        raise McpInputError("Provide at least one metadata field to update.")
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    for key in ("status", "severity", "blocked_reason"):
        if key in data and (not isinstance(data[key], str) or len(data[key]) > 20000):
            raise McpInputError(f"{key} must be a string of at most 20000 characters.")
    work_type_id = integer(data["work_type_id"], "work_type_id") if "work_type_id" in data else task.work_type_id
    values = {key: data.get(key, getattr(task, key)) for key in ("status", "severity", "blocked_reason")}
    values.update(
        work_type=work_type_id, version=integer(data["version"], "version", 0),
        stage_version=integer(data["stage_version"], "stage_version", 0),
    )
    return call_view(request, task.pk, editing.set_task_metadata, values)


def relationship_types(request, data):
    fields(data, set(), ("limit", "offset"))
    limit, offset = page(data)
    items = list(LinkType.objects.all()[offset:offset + limit + 1])
    return JsonResponse({"results": [{
        "id": item.pk, "name": item.name, "outward_description": item.outward_description,
        "inward_description": item.inward_description,
    } for item in items[:limit]], "offset": offset, "has_more": len(items) > limit})


def list_work_types(request, data):
    fields(data, set(), ("limit", "offset"))
    limit, offset = page(data)
    items = list(WorkType.objects.order_by("title", "id")[offset:offset + limit + 1])
    return JsonResponse({
        "results": [{"id": item.pk, "title": item.title, "description": item.description} for item in items[:limit]],
        "offset": offset, "has_more": len(items) > limit,
    })


def link_task(request, data):
    fields(data, {"task_id", "target_id", "link_type_id", "direction"})
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    target_id = integer(data["target_id"], "target_id")
    link_type_id = integer(data["link_type_id"], "link_type_id")
    direction = data["direction"]
    if direction not in ("in", "out"):
        raise McpInputError("direction must be 'in' or 'out'.")
    form = TaskLinkForm({"target": target_id, "relation": f"{link_type_id}:{direction}"}, task=task)
    if not form.is_valid():
        return JsonResponse({"error": "Invalid relationship.", "fields": form.errors.get_json_data()}, status=400)
    link_type = LinkType.objects.get(pk=link_type_id)
    other = form.cleaned_data["target"]
    source, target = (task, other) if direction == "out" else (other, task)
    if link_type.outward_description == link_type.inward_description and source.pk > target.pk:
        source, target = target, source
    with transaction.atomic():
        link, created = TaskLink.objects.get_or_create(link_type=link_type, source=source, target=target)
    if not created:
        return JsonResponse({"error": "This relationship already exists."}, status=409)
    return JsonResponse({"id": link.pk, "source_id": source.pk, "target_id": target.pk, "type_id": link_type.pk}, status=201)


def unlink_task(request, data):
    fields(data, {"task_id", "link_id"})
    task_id = integer(data["task_id"], "task_id")
    link_id = integer(data["link_id"], "link_id")
    with transaction.atomic():
        link = TaskLink.objects.select_for_update().filter(
            Q(source_id=task_id) | Q(target_id=task_id), pk=link_id,
        ).first()
        if link is None:
            return JsonResponse({"error": "Task relationship not found."}, status=404)
        link.delete()
    return JsonResponse({"removed": True, "id": link_id})


def approval(request, data, view):
    fields(data, {"task_id", "version", "stage_version"})
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    values = {
        "version": integer(data["version"], "version", 0),
        "stage_version": integer(data["stage_version"], "stage_version", 0),
    }
    return call_view(request, task.pk, view, values, actor=True)


def approve_request(request, data):
    return approval(request, data, workflow.approve_request)


def approve_plan(request, data):
    return approval(request, data, workflow.approve_plan)


def accept_plan(request, data):
    fields(data, {"task_id", "version", "stage_version", "run_id", "report_hash"})
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    if not isinstance(data["report_hash"], str):
        raise McpInputError("report_hash must be a string.")
    values = {
        "version": integer(data["version"], "version", 0),
        "stage_version": integer(data["stage_version"], "stage_version", 0),
        "run_id": integer(data["run_id"], "run_id"),
        "report_hash": data["report_hash"],
    }
    return call_view(request, task.pk, planning.use_architect_plan, values, actor=True)


def record_gate(request, data):
    fields(data, {"task_id", "version", "stage_version", "result", "evidence", "action"}, ("waiver",))
    task = get_task(data["task_id"])
    if task is None:
        return missing()
    for key in ("result", "evidence", "action", "waiver"):
        if key in data and (not isinstance(data[key], str) or len(data[key]) > 20000):
            raise McpInputError(f"{key} must be a string of at most 20000 characters.")
    values = {
        "version": integer(data["version"], "version", 0),
        "stage_version": integer(data["stage_version"], "stage_version", 0),
        "result": data["result"], "evidence": data["evidence"], "action": data["action"],
        "waiver": data.get("waiver", ""),
    }
    return call_view(request, task.pk, workflow.record_gate, values, actor=True)


OPERATIONS = {
    "list_tasks": list_tasks,
    "get_task": read_task,
    "get_task_text": read_task_text,
    "list_task_runs": list_task_runs,
    "list_gate_decisions": list_gate_decisions,
    "get_gate_text": read_gate_text,
    "get_run": read_run,
    "get_run_error": read_run_error,
    "get_run_event": read_run_event,
    "similar_tasks": similar_tasks,
    "create_task": create_task,
    "update_task": update_task,
    "update_metadata": update_metadata,
    "relationship_types": relationship_types,
    "list_work_types": list_work_types,
    "list_links": list_links,
    "link_task": link_task,
    "unlink_task": unlink_task,
    "approve_request": approve_request,
    "approve_plan": approve_plan,
    "accept_plan": accept_plan,
    "record_gate": record_gate,
}
