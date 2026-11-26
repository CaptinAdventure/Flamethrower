import logging

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import Truncator
from django.views.decorators.http import require_http_methods, require_POST

from ..forms import QuickTaskForm, StageForm, TaskForm, TaskMetadataForm
from ..models import AgentRun, Task, next_priority_expression
from ..title_generation import UnusableTitleError, generate_title
from .board import task_list

logger = logging.getLogger(__name__)


class TaskTitleForm(forms.Form):
    title = forms.CharField(max_length=200)


class TaskRequestForm(forms.Form):
    request = forms.CharField(widget=forms.Textarea)


def persist_task_content(
    task_id, original, values, version, stage_version, *,
    acp_selection=None, selected_acp_endpoint=None, update_acp=False,
):
    content_changed = any(
        values[key] != getattr(original, key) for key in ("title", "request", "plan")
    )
    acp_changed = update_acp and (
        acp_selection != original.acp_selection
        or selected_acp_endpoint != original.selected_acp_endpoint
    )
    candidates = Task.objects.filter(pk=task_id, version=version, stage_version=stage_version)
    if not content_changed:
        if not acp_changed:
            return True
        return bool(candidates.exclude(runs__status__in=AgentRun.ACTIVE).update(
            acp_selection=acp_selection,
            selected_acp_endpoint=selected_acp_endpoint,
            updated_at=timezone.now(),
        ))

    state = original.state
    if values["title"] != original.title or values["request"] != original.request:
        state = Task.State.DRAFT
        stage = Task.Stage.DRAFT
    elif values["plan"] != original.plan and state == Task.State.PLAN_APPROVED:
        state = Task.State.SCOPE_APPROVED
        stage = Task.Stage.ARCHITECT
    else:
        stage = original.stage
    request_changed = values["title"] != original.title or values["request"] != original.request
    plan_changed = values["plan"] != original.plan
    pause_reason = original.automatic_pause_reason
    gate_returns = original.automatic_gate_returns
    if (
        (request_changed and original.state in (Task.State.SCOPE_APPROVED, Task.State.PLAN_APPROVED))
        or (plan_changed and original.state == Task.State.PLAN_APPROVED)
    ):
        pause_reason = "An approved request or plan changed. Review and reapprove it before continuing the workflow."
        gate_returns = {}
    indexing_updates = {}
    if values["title"] != original.title or values["request"] != original.request:
        indexing_updates = {
            "relates_indexing": Task.IndexingStatus.PENDING,
            "indexing_revision": F("indexing_revision") + 1,
            "indexing_error": "", "embedding": None,
        }
    priority_update = {"priority": next_priority_expression(stage, task_id)} if stage != original.stage else {}
    acp_update = {} if not update_acp else {
        "acp_selection": acp_selection, "selected_acp_endpoint": selected_acp_endpoint,
    }
    return bool(candidates.exclude(runs__status__in=AgentRun.ACTIVE).update(
        title=values["title"], request=values["request"], plan=values["plan"],
        state=state, stage=stage, finished=False, rework_note="", stage_version=F("stage_version") + 1,
        automatic_pause_reason=pause_reason, automatic_gate_returns=gate_returns,
        version=F("version") + 1, updated_at=timezone.now(),
        status=Task.Status.READY if original.status == Task.Status.DONE else original.status,
        **indexing_updates,
        **priority_update,
        **acp_update,
    ))


@require_POST
def quick_task(request):
    quick_form = QuickTaskForm(request.POST)
    if not quick_form.is_valid():
        return task_list(request, quick_form=quick_form, status=400)
    action = request.POST.get("action", "create")
    if action not in ("create", "edit"):
        return render(request, "tasks/error.html", {"message": "Invalid quick task action."}, status=400)
    request_text = quick_form.cleaned_data["request"]
    title_notice = ""
    try:
        title = generate_title(request_text)
    except UnusableTitleError:
        logger.warning("The local model produced an unusable quick task title")
        title = Truncator(" ".join(request_text.split())).chars(200)
        title_notice = "The local model produced an unusable title, so the request text was used instead. Review the title."
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        logger.exception("Could not generate a quick task title")
        form = TaskForm(initial={"request": request_text})
        return render(request, "tasks/form.html", {
            "form": form, "creating": True,
            "title_generation_error": f"Could not generate a title locally ({type(error).__name__}). Enter one manually.",
        })
    values = {"title": title, "request": request_text, "plan": ""}
    if action == "edit":
        form = TaskForm(initial=values)
    else:
        form = TaskForm(values)
        if form.is_valid():
            form.save()
            if title_notice:
                messages.warning(request, title_notice)
            return redirect("task_list")
    return render(request, "tasks/form.html", {
        "form": form, "creating": True, "generated_title": not title_notice, "title_notice": title_notice,
    })


@require_http_methods(["GET", "POST"])
def task_create(request):
    form = TaskForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        task = form.save()
        return redirect("task_detail", task_id=task.pk)
    return render(request, "tasks/form.html", {"form": form, "creating": True})

def render_task_edit(request, task, form, version_form, *, metadata_form=None, run_active=False, status=200):
    if getattr(request, "_mcp_action", False):
        from .api_support import action_error

        errors = metadata_form.errors if metadata_form is not None and metadata_form.is_bound else form.errors
        return action_error(request, "Task changes are invalid or stale.", status if status >= 400 else 400, errors=errors)
    initial = {"version": task.version, "stage_version": task.stage_version}
    if metadata_form is None:
        metadata_form = TaskMetadataForm(task=task, initial={
            "status": task.status, "severity": task.severity, "work_type": task.work_type_id, "blocked_reason": task.blocked_reason,
        })
    return render(request, "tasks/form.html", {
        "task": task, "creating": False, "form": form, "version_form": version_form,
        "metadata_form": metadata_form, "metadata_version_form": StageForm(initial=initial, auto_id="metadata_%s"),
        "run_active": run_active,
    }, status=status)

@require_POST
def set_task_metadata(request, task_id):
    from .api_support import action_error

    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        form = TaskMetadataForm(request.POST, task=task)
        version_form = StageForm(request.POST)
        if not version_form.is_valid():
            return action_error(request, "Invalid task details request. Reload and try again.", 400)
        if task.version != version_form.cleaned_data["version"] or task.stage_version != version_form.cleaned_data["stage_version"]:
            return action_error(request, "The task changed. Reload before changing its details.", 409)
        if not form.is_valid():
            return render_task_edit(
                request, task, TaskForm(instance=task),
                StageForm(initial={"version": task.version, "stage_version": task.stage_version}),
                metadata_form=form, run_active=task.runs.filter(status__in=AgentRun.ACTIVE).exists(), status=400,
            )
        status = form.cleaned_data["status"]
        if status != task.status and status in (Task.Status.BLOCKED, Task.Status.ABANDONED) and task.runs.filter(status__in=AgentRun.ACTIVE).exists():
            return action_error(request, "Finish or cancel the active run before blocking or abandoning this task.", 409)
        reason = form.cleaned_data["blocked_reason"] if status == Task.Status.BLOCKED else ""
        work_type = form.cleaned_data["work_type"]
        if (status, form.cleaned_data["severity"], work_type.pk, reason) == (task.status, task.severity, task.work_type_id, task.blocked_reason):
            return redirect("task_detail", task_id=task_id)
        pause_reason = task.automatic_pause_reason
        if status != Task.Status.READY and not pause_reason:
            pause_reason = "Task status prevents advancement. Set it to Ready before continuing the workflow."
        candidates = Task.objects.filter(pk=task.pk, version=task.version, stage_version=task.stage_version)
        if status != task.status and status in (Task.Status.BLOCKED, Task.Status.ABANDONED):
            candidates = candidates.exclude(runs__status__in=AgentRun.ACTIVE)
        changed = candidates.update(
            status=status, severity=form.cleaned_data["severity"], work_type=work_type, blocked_reason=reason,
            automatic_pause_reason=pause_reason, stage_version=F("stage_version") + 1, updated_at=timezone.now(),
        )
        if not changed:
            return action_error(request, "The task changed. Reload before changing its details.", 409)
    return redirect("task_detail", task_id=task_id)

@require_http_methods(["GET", "POST"])
def task_edit(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    run_active = task.runs.filter(status__in=AgentRun.ACTIVE).exists()
    if run_active and request.method == "POST":
        from .api_support import action_error

        return action_error(request, "Cannot edit a task while its agent is running.", 409)
    form = TaskForm(request.POST or None, instance=task)
    version_form = StageForm(request.POST or None, initial={"version": task.version, "stage_version": task.stage_version})
    if request.method == "POST" and form.is_valid() and version_form.is_valid():
        # ModelForm validation mutates its instance; fetch the persisted values separately.
        original = get_object_or_404(Task, pk=task_id)
        values = form.cleaned_data
        if original.version != version_form.cleaned_data["version"] or original.stage_version != version_form.cleaned_data["stage_version"]:
            form.add_error(None, "This task changed in another view. Reload before editing.")
            return render_task_edit(request, task, form, version_form, status=409)
        acp_selection = values["acp_selection"]
        selected_acp_endpoint = values["selected_acp_endpoint"]
        if persist_task_content(
            task_id, original, values,
            version_form.cleaned_data["version"], version_form.cleaned_data["stage_version"],
            acp_selection=acp_selection, selected_acp_endpoint=selected_acp_endpoint,
            update_acp=not getattr(request, "_mcp_action", False),
        ):
            return redirect("task_detail", task_id=task_id)
        form.add_error(None, "This task changed in another view. Reload before editing.")
    return render_task_edit(
        request, task, form, version_form, run_active=run_active,
        status=409 if request.method == "POST" and form.non_field_errors() else 200,
    )


@require_POST
def update_task_title(request, task_id):
    title_form = TaskTitleForm(request.POST)
    version_form = StageForm(request.POST)
    if not title_form.is_valid() or not version_form.is_valid():
        errors = {}
        if title_form.errors:
            errors.update(title_form.errors.get_json_data())
        if version_form.errors:
            errors.update(version_form.errors.get_json_data())
        return JsonResponse({"errors": errors}, status=400)

    version = version_form.cleaned_data["version"]
    stage_version = version_form.cleaned_data["stage_version"]
    title = title_form.cleaned_data["title"]
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if task.runs.filter(status__in=AgentRun.ACTIVE).exists():
            return JsonResponse({"error": "Cannot edit a task while its agent is running."}, status=409)
        if task.version != version or task.stage_version != stage_version:
            return JsonResponse({"error": "This task changed in another view. Reload before editing."}, status=409)

        if title != task.title:
            changed = Task.objects.filter(
                pk=task_id, version=version, stage_version=stage_version,
            ).exclude(runs__status__in=AgentRun.ACTIVE).update(
                title=title,
                stage_version=F("stage_version") + 1,
                relates_indexing=Task.IndexingStatus.PENDING,
                indexing_revision=F("indexing_revision") + 1,
                indexing_error="",
                embedding=None,
                updated_at=timezone.now(),
            )
            if not changed:
                return JsonResponse({"error": "This task changed in another view. Reload before editing."}, status=409)
            task.refresh_from_db()

    return JsonResponse({
        "title": task.title,
        "version": task.version,
        "stage_version": task.stage_version,
    })


@require_POST
def update_task_request(request, task_id):
    request_form = TaskRequestForm(request.POST)
    version_form = StageForm(request.POST)
    if not request_form.is_valid() or not version_form.is_valid():
        errors = {}
        if request_form.errors:
            errors.update(request_form.errors.get_json_data())
        if version_form.errors:
            errors.update(version_form.errors.get_json_data())
        return JsonResponse({"errors": errors}, status=400)

    version = version_form.cleaned_data["version"]
    stage_version = version_form.cleaned_data["stage_version"]
    with transaction.atomic():
        task = get_object_or_404(Task.objects.select_for_update(), pk=task_id)
        if task.runs.filter(status__in=AgentRun.ACTIVE).exists():
            return JsonResponse({"error": "Cannot edit a task while its agent is running."}, status=409)
        if task.version != version or task.stage_version != stage_version:
            return JsonResponse({"error": "This task changed in another view. Reload before editing."}, status=409)
        values = {"title": task.title, "request": request_form.cleaned_data["request"], "plan": task.plan}
        if not persist_task_content(task_id, task, values, version, stage_version):
            return JsonResponse({"error": "This task changed in another view. Reload before editing."}, status=409)
        task.refresh_from_db()

    return JsonResponse({
        "request": task.request,
        "version": task.version,
        "stage_version": task.stage_version,
    })
