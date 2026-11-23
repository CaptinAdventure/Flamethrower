from django import forms

from ..models import LinkType, Task


class LinkedTaskChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"#{obj.pk} - {obj.title}"

class TaskLinkForm(forms.Form):
    relation = forms.ChoiceField(label="Relationship")
    target = LinkedTaskChoiceField(label="Task", queryset=Task.objects.none())

    def __init__(self, *args, task, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "Select a relationship")]
        for link_type in LinkType.objects.all():
            choices.append((f"{link_type.pk}:out", link_type.outward_description))
            if link_type.name == "Relates":
                self.fields["relation"].initial = f"{link_type.pk}:out"
            if link_type.inward_description != link_type.outward_description:
                choices.append((f"{link_type.pk}:in", link_type.inward_description))
        self.fields["relation"].choices = choices
        if "target" in self.fields:
            self.fields["target"].queryset = Task.objects.exclude(pk=task.pk).order_by("title", "pk")


class LinkedTaskCreateForm(TaskLinkForm):
    target = None
    title = forms.CharField(max_length=200, label="Title")
    request = forms.CharField(label="Requested change", widget=forms.Textarea(attrs={"rows": 3}))
