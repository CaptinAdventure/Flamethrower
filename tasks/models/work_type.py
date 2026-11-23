from django.core.validators import FileExtensionValidator
from django.db import models


DEFAULT_WORK_TYPES = (
    ("Bug", "bug", "A defect or unexpected behavior that needs fixing."),
    ("Enabler", "enabler", "Technical work that supports future delivery."),
    ("Story", "story", "User-focused functionality or a requirement."),
    ("Improvement", "improvement", "An enhancement to existing functionality."),
    ("Task", "task", "A general piece of work."),
    ("Sub-task", "sub_task", "A smaller piece of work within a larger task."),
    ("Test", "test", "A test case or verification activity."),
    ("Test Plan", "test_plan", "A plan describing test scope and approach."),
    ("Test Execution", "test_execution", "A run of tests and their results."),
    ("Event", "event", "An event to plan, track, or record."),
)


class WorkType(models.Model):
    class Icon(models.TextChoices):
        BUG = "bug", "Bug"
        ENABLER = "enabler", "Enabler"
        STORY = "story", "Story"
        IMPROVEMENT = "improvement", "Improvement"
        TASK = "task", "Task"
        SUB_TASK = "sub_task", "Sub-task"
        TEST = "test", "Test"
        TEST_PLAN = "test_plan", "Test Plan"
        TEST_EXECUTION = "test_execution", "Test Execution"
        EVENT = "event", "Event"

    title = models.CharField(max_length=80, unique=True)
    icon = models.CharField(max_length=20, choices=Icon.choices, default=Icon.TASK)
    icon_image = models.ImageField(
        upload_to="work_types/", blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "gif", "webp"])],
        help_text="Optional PNG, JPEG, GIF or WebP. Overrides the built-in icon; clear it to use the built-in icon again.",
    )
    description = models.CharField(max_length=300)
    is_default = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["is_default"], condition=models.Q(is_default=True), name="one_default_work_type"),
        ]

    def __str__(self):
        return self.title


def default_work_type_id():
    return WorkType.objects.filter(is_default=True).values_list("pk", flat=True).get()
