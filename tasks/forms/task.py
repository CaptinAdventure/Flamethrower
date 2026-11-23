from django import forms
from django.db.models import Q

from ..models import AcpEndpoint, Task, WorkType
from .widgets import SeveritySelect


def selected_work_type(form, current_id):
    work_type = form.cleaned_data.get("work_type")
    if work_type is None:
        if "work_type" in form.data:
            raise forms.ValidationError("Choose a work type.")
        # Existing clients and quick actions may omit this newly added field.
        return WorkType.objects.get(pk=current_id)
    return work_type


class QuickTaskForm(forms.Form):
    request = forms.CharField(
        label="Quick task",
        widget=forms.TextInput(attrs={"placeholder": "Describe a change", "aria-label": "Requested change"}),
    )


class TaskForm(forms.ModelForm):
    work_type = forms.ModelChoiceField(
        queryset=WorkType.objects.all(), required=False, empty_label="Choose a work type",
        widget=forms.Select(attrs={"required": True}),
    )
    acp_selection = forms.ChoiceField(
        choices=Task.AcpSelection.choices, label="ACP selection", required=False,
        initial=Task.AcpSelection.AUTO,
    )
    selected_acp_endpoint = forms.ModelChoiceField(
        queryset=AcpEndpoint.objects.none(), required=False, label="ACP endpoint",
        empty_label="Choose an enabled ACP endpoint",
    )

    class Meta:
        model = Task
        fields = ["work_type", "title", "request", "plan"]
        labels = {"request": "Requested change", "plan": "Plan"}
        widgets = {
            "request": forms.Textarea(attrs={"rows": 7}),
            "plan": forms.Textarea(attrs={"rows": 10}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["work_type"].initial = self.instance.work_type_id
        endpoints = AcpEndpoint.objects.filter(enabled=True)
        if self.instance and self.instance.pk and self.instance.selected_acp_endpoint_id:
            endpoints = AcpEndpoint.objects.filter(
                Q(enabled=True) | Q(pk=self.instance.selected_acp_endpoint_id),
            )
        self.fields["selected_acp_endpoint"].queryset = endpoints.order_by("priority", "id")
        if self.instance and self.instance.pk:
            self.fields.pop("work_type")
            self.initial.setdefault("acp_selection", self.instance.acp_selection)
            self.initial.setdefault("selected_acp_endpoint", self.instance.selected_acp_endpoint_id)
        self.fields["selected_acp_endpoint"].widget.attrs["data-acp-endpoint"] = "true"

    def clean_work_type(self):
        return selected_work_type(self, self.instance.work_type_id)

    def clean(self):
        values = super().clean()
        selection = values.get("acp_selection")
        endpoint = values.get("selected_acp_endpoint")
        if not selection:
            selection = (
                self.instance.acp_selection if self.instance and self.instance.pk
                else Task.AcpSelection.AUTO
            )
            values["acp_selection"] = selection
        if (
            self.instance and self.instance.pk and "selected_acp_endpoint" not in self.data
            and self.instance.selected_acp_endpoint_id
        ):
            endpoint = self.instance.selected_acp_endpoint
            values["selected_acp_endpoint"] = endpoint
        if selection == Task.AcpSelection.AUTO:
            values["selected_acp_endpoint"] = None
        elif selection == Task.AcpSelection.EXPLICIT:
            if not endpoint:
                self.add_error("selected_acp_endpoint", "Choose an enabled ACP endpoint.")
            elif not endpoint.enabled and not (
                self.instance and self.instance.pk
                and self.instance.acp_selection == Task.AcpSelection.EXPLICIT
                and self.instance.selected_acp_endpoint_id == endpoint.pk
            ):
                self.add_error("selected_acp_endpoint", "Enable the selected ACP before saving.")
        return values

    def save(self, commit=True):
        task = super().save(commit=False)
        task.acp_selection = self.cleaned_data["acp_selection"]
        task.selected_acp_endpoint = self.cleaned_data["selected_acp_endpoint"]
        if commit:
            task.save()
            self.save_m2m()
        return task


class TaskMetadataForm(forms.Form):
    work_type = forms.ModelChoiceField(
        queryset=WorkType.objects.all(), required=False, empty_label="Choose a work type",
        widget=forms.Select(attrs={"required": True}),
    )
    severity = forms.ChoiceField(choices=Task.Severity.choices, widget=SeveritySelect)
    status = forms.ChoiceField(choices=Task.Status.choices)
    blocked_reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, task, **kwargs):
        super().__init__(*args, **kwargs)
        self.task = task
        self.initial.setdefault("work_type", task.work_type_id)
        allowed = [Task.Status.READY, Task.Status.BLOCKED, Task.Status.ABANDONED]
        if task.status == Task.Status.NON_ACCEPTED:
            allowed.insert(0, Task.Status.NON_ACCEPTED)
        elif task.status == Task.Status.DONE:
            allowed = [Task.Status.DONE]
        self.fields["status"].choices = [(value, label) for value, label in Task.Status.choices if value in allowed]

    def clean_work_type(self):
        return selected_work_type(self, self.task.work_type_id)

    def clean(self):
        values = super().clean()
        if values.get("status") == Task.Status.BLOCKED and not values.get("blocked_reason", "").strip():
            self.add_error("blocked_reason", "Explain the constraint blocking this task.")
        return values
