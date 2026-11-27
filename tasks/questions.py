import json

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .models import AgentQuestion, AgentRun, RunEvent, Task

QUESTION_PAUSE = "Paused for an agent question. Answer it before continuing."
PERMISSION_PAUSE = "Paused for an ACP permission request. Review and resolve it before continuing."
TEXT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "title": "Your answer", "minLength": 1, "maxLength": 20000}},
    "required": ["answer"],
}


def validate_question_schema(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("The question schema must describe an object.")
    serialized = json.dumps(schema)
    if len(serialized) > 65536:
        raise ValueError("The question schema is too large.")
    if any(f'"{key}":' in serialized for key in ("$ref", "$dynamicRef", "$recursiveRef")):
        raise ValueError("Question schemas may not contain references.")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ValueError("Invalid question schema.") from error
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties or len(properties) > 20:
        raise ValueError("The question must contain between 1 and 20 fields.")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(key not in properties for key in required):
        raise ValueError("The question has invalid required fields.")
    for key, spec in properties.items():
        if not isinstance(key, str) or not key or len(key) > 200 or key == "__all__" or not isinstance(spec, dict):
            raise ValueError("The question contains an invalid field.")
        if spec.get("type") not in ("string", "integer", "number", "boolean", "array"):
            raise ValueError(f"Unsupported question field type for {key}.")
        for choice_key in ("enum", "oneOf"):
            choices = spec.get(choice_key)
            if choices is None:
                continue
            if spec["type"] != "string":
                raise ValueError(f"Unsupported choices for {key}.")
            if not isinstance(choices, list) or not choices or len(choices) > 100:
                raise ValueError(f"Invalid choices for {key}.")
            if choice_key == "enum" and any(not isinstance(item, str) for item in choices):
                raise ValueError(f"Invalid choices for {key}.")
            if choice_key == "oneOf" and any(
                not isinstance(item, dict) or not isinstance(item.get("const"), str) for item in choices
            ):
                raise ValueError(f"Invalid choices for {key}.")
        if spec["type"] == "array":
            items = spec.get("items")
            if not isinstance(items, dict) or items.get("type", "string") != "string":
                raise ValueError(f"Unsupported multiple-choice field {key}.")
            choices = items.get("anyOf") if "anyOf" in items else items.get("enum")
            valid_choice = (
                (lambda item: isinstance(item, dict) and isinstance(item.get("const"), str))
                if "anyOf" in items else (lambda item: isinstance(item, str))
            )
            if not isinstance(choices, list) or not choices or len(choices) > 100 or not all(map(valid_choice, choices)):
                raise ValueError(f"Invalid multiple-choice field {key}.")


class QuestionForm(forms.Form):
    def __init__(self, schema, *args, **kwargs):
        super().__init__(*args, **kwargs)
        validate_question_schema(schema)
        self.schema = schema
        required = schema.get("required", [])
        for key, spec in schema["properties"].items():
            kwargs = {
                "label": spec.get("title") or key,
                "help_text": spec.get("description", ""),
                "required": key in required,
                "initial": spec.get("default"),
            }
            kind = spec["type"]
            if kind == "string":
                choices = [(item["const"], item.get("title", item["const"])) for item in spec.get("oneOf", [])]
                choices = choices or [(item, item) for item in spec.get("enum", [])]
                field = forms.ChoiceField(choices=[("", "Choose an option")] + choices, **kwargs) if choices else forms.CharField(
                    max_length=min(spec.get("maxLength", 20000), 20000), widget=forms.Textarea(attrs={"rows": 3}), **kwargs,
                )
            elif kind == "integer":
                field = forms.IntegerField(**kwargs)
            elif kind == "number":
                field = forms.FloatField(**kwargs)
            elif kind == "boolean":
                if isinstance(kwargs["initial"], bool):
                    kwargs["initial"] = str(kwargs["initial"]).lower()
                field = forms.TypedChoiceField(
                    choices=[("", "Choose an option"), ("true", "Yes"), ("false", "No")],
                    coerce=lambda value: value == "true", empty_value=None, **kwargs,
                )
            else:
                items = spec["items"]
                choices = [(item["const"], item.get("title", item["const"])) for item in items.get("anyOf", [])]
                choices = choices or [(item, item) for item in items.get("enum", [])]
                kwargs["required"] = False
                field = forms.MultipleChoiceField(choices=choices, widget=forms.CheckboxSelectMultiple, **kwargs)
            self.fields[key] = field

    def clean(self):
        values = super().clean()
        if self.errors:
            return values
        content = {
            key: value for key, value in values.items()
            if value not in (None, "") and (value != [] or key in self.schema.get("required", []))
        }
        errors = list(Draft202012Validator(self.schema, format_checker=FormatChecker()).iter_errors(content))
        for error in errors:
            key = next(iter(error.absolute_path), None)
            self.add_error(key if key in self.fields else None, error.message)
        self.content = content
        return values


def _lock_run(run_id):
    if not AgentRun.objects.filter(pk=run_id, status__in=AgentRun.ACTIVE, cancel_requested=False).update(cancel_requested=F("cancel_requested")):
        raise RuntimeError("The run is no longer active.")


def refresh_wait_state(run_id):
    with transaction.atomic():
        _lock_run(run_id)
        run = AgentRun.objects.get(pk=run_id)
        if run.questions.filter(outcome=AgentQuestion.Outcome.PENDING).exists():
            status, reason = AgentRun.Status.WAITING_ANSWER, QUESTION_PAUSE
        elif run.permissions.filter(resolved=False).exists():
            status, reason = AgentRun.Status.WAITING, PERMISSION_PAUSE
        else:
            status, reason = AgentRun.Status.RUNNING, ""
        AgentRun.objects.filter(pk=run_id).update(status=status)
        if run.task_id is None:
            return
        task = Task.objects.get(pk=run.task_id)
        if not task.automatic_pause_reason or task.automatic_pause_reason in (QUESTION_PAUSE, PERMISSION_PAUSE):
            Task.objects.filter(pk=task.pk).update(automatic_pause_reason=reason, updated_at=timezone.now())


def create_question(run_id, message, schema, source):
    if not isinstance(message, str) or not message.strip() or len(message) > 20000:
        raise ValueError("The question message must contain 1–20000 characters.")
    validate_question_schema(schema)
    with transaction.atomic():
        _lock_run(run_id)
        question = AgentQuestion.objects.create(run_id=run_id, message=message, schema=schema, source=source)
        refresh_wait_state(run_id)
    return question.pk


def answer_question(question_id, content):
    with transaction.atomic():
        run_id = AgentQuestion.objects.values_list("run_id", flat=True).get(pk=question_id)
        _lock_run(run_id)
        question = AgentQuestion.objects.select_for_update().get(pk=question_id)
        if question.outcome != AgentQuestion.Outcome.PENDING:
            raise ValueError("This question is no longer pending.")
        validator = Draft202012Validator(question.schema, format_checker=FormatChecker())
        errors = list(validator.iter_errors(content))
        if errors:
            raise ValidationError(errors[0].message)
        changed = AgentQuestion.objects.filter(pk=question_id, outcome=AgentQuestion.Outcome.PENDING).update(
            outcome=AgentQuestion.Outcome.ANSWERED, answer=content, answered_at=timezone.now(),
        )
        if not changed:
            raise ValueError("This question is no longer pending.")
        RunEvent.objects.create(run_id=question.run_id, kind="human answer", content=json.dumps({
            "question_id": question.pk, "question": question.message, "answer": content,
        }, ensure_ascii=False))
        refresh_wait_state(question.run_id)
