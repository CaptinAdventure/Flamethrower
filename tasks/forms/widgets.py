from django import forms


class SeveritySelect(forms.Select):
    template_name = "tasks/components/severity_select.html"
