from django import forms

from ..models import GateDecision


class VersionForm(forms.Form):
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

class StageForm(VersionForm):
    stage_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

class ReworkForm(StageForm):
    reason = forms.CharField(label="Reason for rework", widget=forms.Textarea(attrs={"rows": 3}))

class RecommendationForm(StageForm):
    run_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    report_hash = forms.RegexField(r"^[0-9a-f]{64}$", widget=forms.HiddenInput)

class ArchitectRefinementForm(RecommendationForm):
    feedback = forms.CharField(label="What should the Architect change?", widget=forms.Textarea(attrs={"rows": 4}))

class GateDecisionForm(StageForm):
    gate_action = forms.ChoiceField(
        required=False, choices=[("pass", "Pass"), ("fail", "Fail"), ("hold", "Hold"), ("waiver", "Waiver / Force")],
        widget=forms.HiddenInput,
    )
    result = forms.ChoiceField(label="Your assessment", choices=[("", "Select a result"), *GateDecision.Result.choices])
    evidence = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))
    waiver = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    action = forms.ChoiceField(choices=[("save", "Save"), ("advance", "Advance")], widget=forms.HiddenInput)

    def clean(self):
        values = super().clean()
        intent = values.get("gate_action")
        expected = {"pass": ("passed", "advance"), "fail": ("failed", "save"), "hold": ("unverified", "save")}
        if intent in expected and (values.get("result"), values.get("action")) != expected[intent]:
            raise forms.ValidationError("The result does not match the selected action. Select the action again.")
        if intent == "waiver" and (
            values.get("result") not in (GateDecision.Result.FAILED, GateDecision.Result.UNVERIFIED)
            or values.get("action") != "advance"
        ):
            raise forms.ValidationError("A waiver must advance a failed or unverified result.")
        if values.get("action") == "advance" and values.get("result") in (GateDecision.Result.FAILED, GateDecision.Result.UNVERIFIED) and not values.get("waiver"):
            self.add_error("waiver", "Explain the explicit waiver before advancing a failed or unverified gate.")
        return values
