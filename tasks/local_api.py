import hmac
import hashlib
import json
import os
import re

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .deferred import create_deferred_task, validate_deferred_task
from .models import Task
from .search import search_tasks


def authorized(request):
    token = settings.LOCAL_API_TOKEN
    header = request.headers.get("Authorization", "")
    return (
        request.META.get("REMOTE_ADDR") == "127.0.0.1"
        and bool(token)
        and hmac.compare_digest(header.encode("utf-8"), f"Bearer {token}".encode("utf-8"))
    )


def payload(request):
    if request.content_type != "application/json":
        raise ValueError("Content-Type must be application/json.")
    if len(request.body) > 65536:
        raise ValueError("Request body is too large.")
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("Invalid JSON.") from error
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    return data


@csrf_exempt
@require_POST
def health(request):
    if request.META.get("REMOTE_ADDR") != "127.0.0.1" or not settings.LOCAL_API_TOKEN:
        return JsonResponse({"error": "Unavailable."}, status=401)
    try:
        data = payload(request)
        challenge = data.get("challenge")
        if set(data) != {"challenge"} or not isinstance(challenge, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", challenge):
            raise ValueError("Invalid challenge.")
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    pid = os.getpid()
    proof = hmac.new(
        settings.LOCAL_API_TOKEN.encode("ascii"), f"{pid}:{challenge}".encode("ascii"), hashlib.sha256,
    ).hexdigest()
    return JsonResponse({"pid": pid, "proof": proof})


@csrf_exempt
@require_POST
def create_deferred(request):
    if not authorized(request):
        return JsonResponse({"error": "Unauthorized."}, status=401)
    try:
        data = payload(request)
        if not {"title", "request"} <= data.keys() or data.keys() - {"title", "request", "source_task_id"}:
            raise ValueError("Expected title, request, and optional source_task_id.")
        title, description = validate_deferred_task(data["title"], data["request"])
        source_id = data.get("source_task_id")
        if source_id is not None and (type(source_id) is not int or source_id <= 0):
            raise ValueError("source_task_id must be a positive integer.")
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    source = None
    if source_id is not None:
        source = Task.objects.filter(pk=source_id).first()
        if source is None:
            return JsonResponse({"error": "Source task not found."}, status=404)
    task = create_deferred_task(title, description, source_task=source)
    return JsonResponse({
        "id": task.pk, "title": task.title, "status": task.status, "stage": task.stage,
        "source_task_id": source_id,
    }, status=201)


@csrf_exempt
@require_POST
def search_task(request):
    if not authorized(request):
        return JsonResponse({"error": "Unauthorized."}, status=401)
    try:
        data = payload(request)
        if "query" not in data or data.keys() - {"query", "limit", "offset"}:
            raise ValueError("Expected query and optional limit and offset.")
        query = data["query"]
        limit, offset = data.get("limit", 20), data.get("offset", 0)
        if not isinstance(query, str) or not query.strip() or len(query.strip()) > 200:
            raise ValueError("query must be 1–200 characters.")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 9223372036854775707:
            raise ValueError("limit must be 1–100 and offset must be non-negative.")
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    tasks = list(search_tasks(query.strip()).order_by("-updated_at", "-pk")[offset:offset + limit + 1])
    return JsonResponse({
        "results": [{
            "id": task.pk, "title": task.title,
            "request": task.request[:1000], "request_truncated": len(task.request) > 1000,
            "plan": task.plan[:1000], "plan_truncated": len(task.plan) > 1000,
            "status": task.status, "stage": task.stage, "severity": task.severity, "priority": task.priority,
        } for task in tasks[:limit]],
        "has_more": len(tasks) > limit,
        "offset": offset,
    })


@csrf_exempt
@require_POST
def task_operation(request):
    if not authorized(request):
        return JsonResponse({"error": "Unauthorized."}, status=401)
    try:
        data = payload(request)
        operation_name = data.pop("operation", None)
        if not isinstance(operation_name, str):
            raise ValueError("Expected an operation name.")
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    from .mcp_tasks import McpInputError, OPERATIONS

    operation = OPERATIONS.get(operation_name)
    if operation is None:
        return JsonResponse({"error": "Unsupported operation."}, status=400)
    try:
        return operation(request, data)
    except McpInputError as error:
        return JsonResponse({"error": str(error)}, status=400)
