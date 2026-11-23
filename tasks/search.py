from django.db.models import Q

from .models import Task


def search_tasks(query):
    return Task.objects.filter(
        Q(title__icontains=query) | Q(request__icontains=query) | Q(plan__icontains=query),
    )
