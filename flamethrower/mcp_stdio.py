import argparse
import hashlib
import hmac
import http.client
import json
import secrets
from pathlib import Path

import psutil
from mcp.server.fastmcp import FastMCP

if __package__:
    from .launcher import read_runtime_record
else:
    from launcher import read_runtime_record

server = FastMCP("Flamethrower")
project_path = Path.cwd()


def request_json(connection, endpoint, data, headers=None):
    connection.request("POST", endpoint, body=json.dumps(data).encode("utf-8"), headers={
        "Content-Type": "application/json", **(headers or {}),
    })
    response = connection.getresponse()
    body = response.read(4_000_001)
    if len(body) > 4_000_000:
        raise RuntimeError("The Flamethrower response was too large; search with a smaller limit.")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError(f"Flamethrower returned invalid JSON (HTTP {response.status}); the request outcome may be unknown.") from error
    if response.status >= 400:
        message = result.get("error", "Request failed.") if isinstance(result, dict) else "Request failed."
        if isinstance(result, dict) and result.get("fields"):
            message += f" Details: {json.dumps(result['fields'], ensure_ascii=False)}"
        raise RuntimeError(f"Flamethrower HTTP {response.status}: {message}")
    return result


def call_api(endpoint, data):
    storage = project_path / ".flamethrower"
    if storage.is_symlink():
        raise RuntimeError(f"Refusing symlinked project storage: {storage}")
    try:
        record = read_runtime_record(storage / "server.json")
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Cannot read Flamethrower runtime record: {error}") from error
    if record is None:
        raise RuntimeError("Flamethrower is not running for this project: server.json is missing.")
    if "token" not in record:
        raise RuntimeError("The runtime record has no API token. Restart Flamethrower.")
    try:
        running = psutil.pid_exists(record["pid"])
    except (OSError, OverflowError, psutil.Error) as error:
        raise RuntimeError("Cannot verify the Flamethrower PID.") from error
    if not running:
        raise RuntimeError("Flamethrower PID is not running; the runtime record is stale.")
    connection = http.client.HTTPConnection("127.0.0.1", record["port"], timeout=10)
    try:
        challenge = secrets.token_urlsafe(24)
        health = request_json(connection, "/api/mcp/health/", {"challenge": challenge})
        proof = hmac.new(
            record["token"].encode("ascii"), f"{record['pid']}:{challenge}".encode("ascii"), hashlib.sha256,
        ).hexdigest()
        if (
            not isinstance(health, dict) or health.get("pid") != record["pid"]
            or not isinstance(health.get("proof"), str)
            or not hmac.compare_digest(health["proof"].encode("utf-8"), proof.encode("ascii"))
        ):
            raise RuntimeError("The process at the recorded port is not the expected Flamethrower instance.")
        return request_json(connection, endpoint, data, {"Authorization": f"Bearer {record['token']}"})
    except (OSError, http.client.HTTPException) as error:
        raise RuntimeError(
            "Cannot reach Flamethrower; the request outcome may be unknown. "
            "Before retrying task creation, search for the task."
        ) from error
    finally:
        connection.close()


@server.tool()
def create_deferred_task(title: str, request: str, source_task_id: int | None = None) -> dict:
    """Create an unapproved deferred draft. Optionally link it to an existing source task."""
    return call_api("/api/mcp/create-deferred-task/", {
        "title": title, "request": request, "source_task_id": source_task_id,
    })


@server.tool()
def search_task(query: str, limit: int = 20, offset: int = 0) -> dict:
    """Search task titles, requests and plans; use offset and has_more for pagination."""
    return call_api("/api/mcp/search-task/", {"query": query, "limit": limit, "offset": offset})


def task_operation(operation, **values):
    return call_api("/api/mcp/task/", {"operation": operation, **{key: value for key, value in values.items() if value is not None}})


@server.tool()
def list_tasks(limit: int = 20, offset: int = 0, status: str | None = None, stage: str | None = None) -> dict:
    """List tasks, optionally filtered by status or stage. Paginate with offset and has_more."""
    return task_operation("list_tasks", limit=limit, offset=offset, status=status, stage=stage)


@server.tool()
def get_task(task_id: int) -> dict:
    """Get a task, revisions, recent runs and gate decisions, links, and any current Architect proposal."""
    return task_operation("get_task", task_id=task_id)


@server.tool()
def get_task_text(task_id: int, field: str, limit: int = 10000, offset: int = 0) -> dict:
    """Read task text in character-offset pages: request, plan, blocked_reason, pause_reason, or architect_proposal."""
    return task_operation("get_task_text", task_id=task_id, field=field, limit=limit, offset=offset)


@server.tool()
def list_task_runs(task_id: int, limit: int = 20, offset: int = 0) -> dict:
    """List a task's runs, paginated with offset and has_more."""
    return task_operation("list_task_runs", task_id=task_id, limit=limit, offset=offset)


@server.tool()
def list_task_gate_decisions(task_id: int, limit: int = 20, offset: int = 0) -> dict:
    """List a task's gate decisions, including attribution and evidence previews."""
    return task_operation("list_gate_decisions", task_id=task_id, limit=limit, offset=offset)


@server.tool()
def get_gate_text(task_id: int, decision_id: int, field: str, limit: int = 10000, offset: int = 0) -> dict:
    """Read gate evidence or waiver in character-offset pages; field is evidence or waiver."""
    return task_operation("get_gate_text", task_id=task_id, decision_id=decision_id, field=field, limit=limit, offset=offset)


@server.tool()
def get_run(task_id: int, run_id: int, limit: int = 20, offset: int = 0) -> dict:
    """Read a run and paginated event previews. Events may contain sensitive project content."""
    return task_operation("get_run", task_id=task_id, run_id=run_id, limit=limit, offset=offset)


@server.tool()
def get_run_error(task_id: int, run_id: int, limit: int = 10000, offset: int = 0) -> dict:
    """Read a run's error text in character-offset pages."""
    return task_operation("get_run_error", task_id=task_id, run_id=run_id, limit=limit, offset=offset)


@server.tool()
def get_run_event(task_id: int, run_id: int, event_id: int, limit: int = 10000, offset: int = 0) -> dict:
    """Read an event's content in character-offset pages; it may contain sensitive project data."""
    return task_operation("get_run_event", task_id=task_id, run_id=run_id, event_id=event_id, limit=limit, offset=offset)


@server.tool()
def find_similar_tasks(task_id: int, limit: int = 20, offset: int = 0) -> dict:
    """Get indexed similarity suggestions for an existing task, including indexing status."""
    return task_operation("similar_tasks", task_id=task_id, limit=limit, offset=offset)


@server.tool()
def create_task(title: str, request: str, plan: str = "", work_type_id: int | None = None) -> dict:
    """Create a Ready draft task. Creation does not approve its request or start an agent."""
    return task_operation("create_task", title=title, request=request, plan=plan, work_type_id=work_type_id)


@server.tool()
def update_task(
    task_id: int, version: int, stage_version: int,
    title: str | None = None, request: str | None = None, plan: str | None = None,
) -> dict:
    """Edit a task using current revisions. Changes may invalidate prior approvals; active runs block edits."""
    return task_operation(
        "update_task", task_id=task_id, version=version, stage_version=stage_version,
        title=title, request=request, plan=plan,
    )


@server.tool()
def update_task_metadata(
    task_id: int, version: int, stage_version: int, status: str | None = None,
    severity: str | None = None, work_type_id: int | None = None, blocked_reason: str | None = None,
) -> dict:
    """Update status, severity, work type, or blocking reason using current revisions."""
    return task_operation(
        "update_metadata", task_id=task_id, version=version, stage_version=stage_version,
        status=status, severity=severity, work_type_id=work_type_id, blocked_reason=blocked_reason,
    )


@server.tool()
def list_relationship_types(limit: int = 20, offset: int = 0) -> dict:
    """List relationship IDs and their outward and inward descriptions, with pagination."""
    return task_operation("relationship_types", limit=limit, offset=offset)


@server.tool()
def list_work_types(limit: int = 20, offset: int = 0) -> dict:
    """List available work types and their IDs."""
    return task_operation("list_work_types", limit=limit, offset=offset)


@server.tool()
def list_task_links(task_id: int, limit: int = 20, offset: int = 0) -> dict:
    """List a task's relationships, paginated with offset and has_more."""
    return task_operation("list_links", task_id=task_id, limit=limit, offset=offset)


@server.tool()
def link_tasks(task_id: int, target_id: int, link_type_id: int, direction: str = "out") -> dict:
    """Link existing tasks using an advertised relationship ID and direction: out or in."""
    return task_operation("link_task", task_id=task_id, target_id=target_id, link_type_id=link_type_id, direction=direction)


@server.tool()
def unlink_tasks(task_id: int, link_id: int) -> dict:
    """Remove a relationship involving the given task."""
    return task_operation("unlink_task", task_id=task_id, link_id=link_id)


@server.tool()
def approve_task_request(task_id: int, version: int, stage_version: int) -> dict:
    """Approve a Ready task's request as an MCP agent. This can start an Architect run."""
    return task_operation("approve_request", task_id=task_id, version=version, stage_version=stage_version)


@server.tool()
def approve_task_plan(task_id: int, version: int, stage_version: int) -> dict:
    """Approve a saved plan as an MCP agent. This can start a Developer run."""
    return task_operation("approve_plan", task_id=task_id, version=version, stage_version=stage_version)


@server.tool()
def accept_architect_plan(task_id: int, version: int, stage_version: int, run_id: int, report_hash: str) -> dict:
    """Accept the exact current Architect proposal as an MCP agent. This can start a Developer run."""
    return task_operation(
        "accept_plan", task_id=task_id, version=version, stage_version=stage_version,
        run_id=run_id, report_hash=report_hash,
    )


@server.tool()
def record_task_gate(
    task_id: int, version: int, stage_version: int, result: str, evidence: str,
    action: str, waiver: str = "",
) -> dict:
    """Record a Testing or Review decision as an MCP agent. Advancing may start another run.

    result is passed, failed, or unverified; action is save or advance. Advancing failed or
    unverified results requires a waiver. Supply actual evidence, not an unverified claim.
    """
    return task_operation(
        "record_gate", task_id=task_id, version=version, stage_version=stage_version,
        result=result, evidence=evidence, action=action, waiver=waiver,
    )


def main():
    parser = argparse.ArgumentParser(description="Flamethrower MCP stdio server.")
    parser.add_argument("project", nargs="?", default=".", help="Project directory (default: current directory)")
    args = parser.parse_args()
    global project_path
    project_path = Path(args.project).expanduser().resolve()
    if not project_path.is_dir():
        parser.error(f"Project directory does not exist: {project_path}")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
