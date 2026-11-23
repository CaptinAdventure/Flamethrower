from django import forms

from ..models import AcpEndpoint


class AcpEndpointForm(forms.ModelForm):
    class Meta:
        model = AcpEndpoint
        fields = ["name", "host", "port", "priority", "enabled"]

    def clean_name(self):
        return self.cleaned_data["name"].strip()
