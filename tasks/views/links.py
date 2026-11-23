from django.db import transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST

from ..forms import LinkedTaskCreateForm, TaskLinkForm
from ..models import DismissedSuggestion, LinkType, Task, TaskLink
from ..similarity import settings_instance, suggestions, valid_embedding
from .detail import task_link_context


@xframe_options_sameorigin
def render_task_links(request, task, *, link_form=None, linked_task_create_form=None, status=200):
    return render(request, "tasks/link_tasks.html", {
        "task": task,
        "link_form": link_form if link_form is not None else TaskLinkForm(task=task),
        "linked_task_create_form": linked_task_create_form if linked_task_create_form is not None else LinkedTaskCreateForm(
            task=task, auto_id="linked_create_%s",
        ),
        **task_link_context(task, manage_links=True),
    }, status=status)


@require_GET
def task_links(request, task_id):
    return render_task_links(request, get_object_or_404(Task, pk=task_id))


@require_POST
def add_task_link(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    form = TaskLinkForm(request.POST, task=task)
    if not form.is_valid():
        return render_task_links(request, task, link_form=form, status=400)
    type_id, direction = form.cleaned_data["relation"].split(":")
    link_type = get_object_or_404(LinkType, pk=type_id)
    other = form.cleaned_data["target"]
    source, target = (task, other) if direction == "out" else (other, task)
    if link_type.outward_description == link_type.inward_description and source.pk > target.pk:
        source, target = target, source
    _, created = TaskLink.objects.get_or_create(link_type=link_type, source=source, target=target)
    if not created:
        form.add_error(None, "This relationship already exists.")
        return render_task_links(request, task, link_form=form, status=400)
    return redirect("task_links", task_id=task_id)

@require_POST
def create_linked_task(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    form = LinkedTaskCreateForm(request.POST, task=task, auto_id="linked_create_%s")
    if not form.is_valid():
        return render_task_links(request, task, linked_task_create_form=form, status=400)
    type_id, direction = form.cleaned_data["relation"].split(":")
    link_type = get_object_or_404(LinkType, pk=type_id)
    with transaction.atomic():
        other = Task.objects.create(title=form.cleaned_data["title"], request=form.cleaned_data["request"])
        source, target = (task, other) if direction == "out" else (other, task)
        if link_type.outward_description == link_type.inward_description and source.pk > target.pk:
            source, target = target, source
        TaskLink.objects.create(link_type=link_type, source=source, target=target)
    return redirect("task_links", task_id=task_id)


@require_POST
def remove_task_link(request, task_id, link_id):
    get_object_or_404(Task, pk=task_id)
    with transaction.atomic():
        link = get_object_or_404(
            TaskLink.objects.select_for_update().filter(Q(source_id=task_id) | Q(target_id=task_id)), pk=link_id,
        )
        link.delete()
    if request.POST.get("return_to") == "detail":
        return redirect("task_detail", task_id=task_id)
    return redirect("task_links", task_id=task_id)

@require_POST
def retry_indexing(request, task_id):
    get_object_or_404(Task, pk=task_id)
    Task.objects.filter(pk=task_id, relates_indexing=Task.IndexingStatus.FAILED).update(
        relates_indexing=Task.IndexingStatus.PENDING, indexing_revision=F("indexing_revision") + 1,
        indexing_error="", embedding=None,
    )
    return redirect("task_detail", task_id=task_id)


@require_POST
def retry_work_type_suggestion(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    pending = Task.objects.filter(
        pk=task_id, relates_indexing=Task.IndexingStatus.DONE, indexing_revision=task.indexing_revision,
    ).exclude(work_type_suggestion_error="")
    values = {"work_type_suggestion_signature": "", "work_type_suggestion_error": "", "suggested_work_type": None}
    if not valid_embedding(task.embedding):
        values.update(
            relates_indexing=Task.IndexingStatus.PENDING, indexing_revision=F("indexing_revision") + 1,
            indexing_error="", embedding=None,
        )
    pending.update(**values)
    return redirect("task_detail", task_id=task_id)

@require_POST
def review_suggestion(request, task_id, candidate_id):
    with transaction.atomic():
        task = get_object_or_404(Task, pk=task_id)
        candidate = get_object_or_404(Task, pk=candidate_id)
        action = request.POST.get("action")
        current = any(item["task"].pk == candidate.pk for item in suggestions(task, settings_instance()))
        if (
            action not in ("accept", "dismiss") or not current
            or request.POST.get("task_revision") != str(task.indexing_revision)
            or request.POST.get("candidate_revision") != str(candidate.indexing_revision)
        ):
            return render(request, "tasks/error.html", {"message": "This suggestion is no longer current. Refresh and try again."}, status=409)
        if action == "accept":
            link_type = get_object_or_404(LinkType, name="Relates")
            source, target = sorted((task, candidate), key=lambda item: item.pk)
            TaskLink.objects.get_or_create(link_type=link_type, source=source, target=target)
        else:
            DismissedSuggestion.objects.update_or_create(
                task=task, candidate=candidate,
                defaults={"task_revision": task.indexing_revision, "candidate_revision": candidate.indexing_revision},
            )
    return redirect("task_detail", task_id=task_id)
