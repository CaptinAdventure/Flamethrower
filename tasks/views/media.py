from pathlib import Path

from django.conf import settings
from django.views.decorators.http import require_safe
from django.views.static import serve


@require_safe
def work_type_image(request, path):
    return serve(request, path, document_root=Path(settings.MEDIA_ROOT) / "work_types")
