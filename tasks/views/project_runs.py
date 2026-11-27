import json

from django import forms
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from ..acp import AcpUnavailable, create_project_run
from ..agent_scheduler import wake_scheduler
from ..models import AcpEndpoint, AgentLinkProposal, AgentRun, LinkType, RunEvent, Task, TaskLink
from .activity import format_activity
from .detail import run_attention_context
from .usage import format_cost_amount


class ProjectQuestionForm(forms.Form):
    question = forms.CharField(max_length=20000, widget=forms.Textarea(attrs={"rows": 4}))
    endpoint = forms.ModelChoiceField(
        queryset=AcpEndpoint.objects.none(), required=False, empty_label="Automatic",
        label="ACP endpoint",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["endpoint"].queryset = AcpEndpoint.objects.filter(enabled=True)


@require_http_methods(["GET", "POST"])
def project_runs(request):
    form = ProjectQuestionForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            run = create_project_run(form.cleaned_data["question"], form.cleaned_data["endpoint"])
        except AcpUnavailable as error:
            form.add_error(None, str(error))
        else:
            wake_scheduler()
            return redirect("project_run_detail", run_id=run.pk)
    return render(request, "tasks/project_runs.html", {
        "form": form,
        "runs": AgentRun.objects.filter(stage=AgentRun.PROJECT, task__isnull=True).select_related("acp_endpoint"),
    }, status=400 if request.method == "POST" else 200)


def project_run_detail(request, run_id, *, question_form=None, question_form_question_id=None, status=200):
    run = get_object_or_404(
        AgentRun.objects.select_related("acp_endpoint", "usage_snapshot"),
        pk=run_id, stage=AgentRun.PROJECT, task__isnull=True,
    )
    usage = getattr(run, "usage_snapshot", None)
    return render(request, "tasks/project_run_detail.html", {
        "run": run, "current_run": run,
        "usage": usage, "usage_cost": format_cost_amount(usage.cost_amount) if usage else None,
        "events": format_activity(list(run.events.all())),
        "proposals": run.link_proposals.select_related("source", "target", "link_type"),
        "should_poll": run.status in AgentRun.ACTIVE,
        **run_attention_context(run, question_form, question_form_question_id),
    }, status=status)


@require_POST
def decide_project_link(request, proposal_id):
    decision = request.POST.get("decision")
    if decision not in ("accept", "reject"):
        return render(request, "tasks/error.html", {"message": "Invalid proposal decision."}, status=400)
    with transaction.atomic():
        AgentLinkProposal.objects.filter(pk=proposal_id).update(outcome=F("outcome"))
        proposal = get_object_or_404(
            AgentLinkProposal.objects.select_for_update().select_related("run", "source", "target", "link_type"),
            pk=proposal_id, run__task__isnull=True, run__stage=AgentRun.PROJECT,
        )
        if proposal.outcome != AgentLinkProposal.Outcome.PENDING:
            return render(request, "tasks/error.html", {"message": "This proposal has already been decided."}, status=409)
        if decision == "accept":
            tasks = {item.pk: item for item in Task.objects.select_for_update().filter(
                pk__in=[proposal.source_id, proposal.target_id],
            )}
            link_type = LinkType.objects.select_for_update().get(pk=proposal.link_type_id)
            if (
                tasks[proposal.source_id].version != proposal.source_version
                or tasks[proposal.target_id].version != proposal.target_version
                or link_type.outward_description != proposal.outward_description
                or link_type.inward_description != proposal.inward_description
            ):
                return render(request, "tasks/error.html", {
                    "message": "The tasks or relationship changed since this proposal. Review and request a new proposal.",
                }, status=409)
            link, created = TaskLink.objects.get_or_create(
                source=proposal.source, target=proposal.target, link_type=proposal.link_type,
            )
            proposal.outcome = AgentLinkProposal.Outcome.ACCEPTED
            result = "created" if created else "already exists"
        else:
            proposal.outcome = AgentLinkProposal.Outcome.REJECTED
            result = "rejected"
        proposal.decided_at = timezone.now()
        proposal.save(update_fields=["outcome", "decided_at"])
        RunEvent.objects.create(run=proposal.run, kind="human link decision", content=json.dumps({
            "proposalId": proposal.pk, "sourceTaskId": proposal.source_id,
            "destinationTaskId": proposal.target_id, "linkType": proposal.outward_description,
            "result": result,
        }, ensure_ascii=False))
    return redirect("project_run_detail", run_id=proposal.run_id)
