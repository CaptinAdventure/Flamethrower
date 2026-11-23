from django.contrib import admin
from django.db import transaction
from django.db.models import F
from django.template.loader import render_to_string

from .forms import SimilaritySettingsForm
from .models import AcpEndpoint, AgentRun, Approval, DeferredTask, GateDecision, LinkType, PermissionRequest, RunEvent, SimilaritySettings, Task, TaskLink, WorkType
from .similarity import settings_instance


class ReadOnlyModelAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return True


admin.site.register(
    (Approval, AgentRun, DeferredTask, GateDecision, RunEvent, PermissionRequest, LinkType, TaskLink),
    ReadOnlyModelAdmin,
)
admin.site.register(AcpEndpoint)


@admin.register(Task)
class TaskAdmin(ReadOnlyModelAdmin):
    readonly_fields = ("severity_display",)

    def get_fields(self, request, obj=None):
        return [
            "severity_display" if field == "severity" else field
            for field in super().get_fields(request, obj)
            if field != "severity_display"
        ]

    @admin.display(description="Severity")
    def severity_display(self, obj):
        return render_to_string("tasks/components/severity.html", {"severity": obj.severity})

    class Media:
        css = {"all": ("tasks/severity.css",)}


@admin.register(WorkType)
class WorkTypeAdmin(admin.ModelAdmin):
    list_display = ("icon_preview", "title", "description", "is_default")
    list_display_links = ("icon_preview", "title")
    search_fields = ("title", "description")
    ordering = ("title",)
    readonly_fields = ("icon_preview", "is_default")

    @admin.display(description="Icon")
    def icon_preview(self, obj):
        if not obj.pk:
            return "-"
        return render_to_string("tasks/components/work_type.html", {"work_type": obj})

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.is_default)

    class Media:
        css = {"all": ("tasks/work-type.css",)}


@admin.register(SimilaritySettings)
class SimilaritySettingsAdmin(admin.ModelAdmin):
    form = SimilaritySettingsForm
    list_display = ("__str__", "threshold", "max_results", "max_tokens")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).filter(pk=1)

    def changelist_view(self, request, extra_context=None):
        if self.has_view_or_change_permission(request):
            settings_instance()
        return super().changelist_view(request, extra_context)

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            old_max_tokens = SimilaritySettings.objects.get(pk=obj.pk).max_tokens
            super().save_model(request, obj, form, change)
            if old_max_tokens != obj.max_tokens:
                Task.objects.update(
                    relates_indexing=Task.IndexingStatus.PENDING,
                    indexing_revision=F("indexing_revision") + 1,
                    indexing_error="", embedding=None,
                )
