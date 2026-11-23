from django.http import JsonResponse
from django.shortcuts import render


def action_error(request, message, status, *, errors=None):
    if getattr(request, "_mcp_action", False):
        data = {"error": message}
        if errors:
            data["fields"] = errors.get_json_data()
        return JsonResponse(data, status=status)
    return render(request, "tasks/error.html", {"message": message}, status=status)
