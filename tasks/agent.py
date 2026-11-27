import asyncio
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import acp
from acp.interfaces import Client
from acp.exceptions import RequestError
from acp.schema import (
    AcceptElicitationResponse, AllowedOutcome, CancelElicitationResponse, ClientCapabilities,
    DeniedOutcome, ElicitationCapabilities, ElicitationFormCapabilities,
    ElicitationFormRequestMode, ElicitationFormSessionMode, RequestPermissionResponse, TextContentBlock,
)
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .deferred import complete_run
from .acp import mark_endpoint_checked
from .architect_tasks import handle_task_block
from .command_permissions import matching_command_prefixes
from .mcp_permissions import matching_mcp_rule, reported_mcp_kind_mismatch, reported_mcp_tool
from .models import ACPUsageSnapshot, AgentQuestion, AgentRun, PermissionRequest, RepositorySettings, RunEvent, Task
from .questions import QUESTION_PAUSE, TEXT_SCHEMA, create_question, refresh_wait_state
from .recommendations import architect_plan, developer_complete, final_agent_report, project_answer, question_text

logger = logging.getLogger(__name__)

RECOVERY_PAUSE_REASONS = (
    "", QUESTION_PAUSE,
    "The Architect run did not complete normally. Inspect it before continuing.",
    "The agent run did not complete normally. Inspect it before continuing.",
    "The latest agent run was interrupted. Inspect and acknowledge it before continuing.",
)


@sync_to_async(thread_sensitive=True)
def update_run(run_id, **values):
    AgentRun.objects.filter(pk=run_id).update(**values)


@sync_to_async(thread_sensitive=True)
def save_event(run_id, kind, content):
    RunEvent.objects.create(run_id=run_id, kind=kind, content=content)


def _validated_integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _validated_usage_update(data):
    if not isinstance(data, dict):
        return None
    used = _validated_integer(data.get("used"))
    size = _validated_integer(data.get("size"))
    if used is None or size is None or used > size:
        return None
    values = {"context_used": used, "context_size": size, "context_reported_at": timezone.now()}
    if "cost" in data:
        cost = data["cost"]
        if cost is not None:
            if not isinstance(cost, dict):
                return None
            amount = cost.get("amount")
            currency = cost.get("currency")
            if not isinstance(amount, (int, float, Decimal)) or isinstance(amount, bool):
                return None
            try:
                amount = Decimal(str(amount))
            except (InvalidOperation, ValueError):
                return None
            if not amount.is_finite() or amount < 0:
                return None
            integer_digits = max(0, len(amount.as_tuple().digits) + amount.as_tuple().exponent)
            if (
                integer_digits > 18 or len(amount.as_tuple().digits) > 30
                or max(0, -amount.as_tuple().exponent) > 12
                or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)
            ):
                return None
            values.update(cost_amount=amount, cost_currency=currency, cost_reported_at=timezone.now())
    return values


def _validated_token_usage(data):
    if not isinstance(data, dict):
        return None
    aliases = {
        "totalTokens": "total_tokens",
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "thoughtTokens": "thought_tokens",
        "cachedReadTokens": "cached_read_tokens",
        "cachedWriteTokens": "cached_write_tokens",
    }
    values = {}
    for source, target in aliases.items():
        if source in data:
            value = _validated_integer(data[source])
            if value is None:
                return None
            values[target] = value
    if not all(name in values for name in ("total_tokens", "input_tokens", "output_tokens")):
        return None
    values["tokens_reported_at"] = timezone.now()
    return values


@sync_to_async(thread_sensitive=True)
def _save_usage_snapshot(run_id, values):
    with transaction.atomic():
        snapshot, _ = ACPUsageSnapshot.objects.get_or_create(run_id=run_id)
        for field, value in values.items():
            setattr(snapshot, field, value)
        snapshot.save(update_fields=list(values))


async def record_usage_update(run_id, data):
    values = _validated_usage_update(data)
    if values is None:
        logger.warning("Ignoring invalid ACP usage update for run %s", run_id)
        return
    await _save_usage_snapshot(run_id, values)


async def record_token_usage(run_id, data):
    values = _validated_token_usage(data)
    if values is None:
        logger.warning("Ignoring invalid ACP token usage for run %s", run_id)
        return
    await _save_usage_snapshot(run_id, values)


@sync_to_async(thread_sensitive=True)
def is_cancelled(run_id):
    return AgentRun.objects.get(pk=run_id).cancel_requested


@sync_to_async(thread_sensitive=True)
def create_permission(run_id, tool_call, options):
    with transaction.atomic():
        permission = PermissionRequest.objects.create(run_id=run_id, tool_call=tool_call, options=options)
        changed = AgentRun.objects.filter(
            pk=run_id, status__in=AgentRun.ACTIVE, cancel_requested=False,
        ).update(status=AgentRun.Status.WAITING)
        if not changed:
            raise RuntimeError("The run is no longer active; the permission request was not recorded.")
        refresh_wait_state(run_id)
    return permission.pk


def _resolve_read_location(location, root):
    if not isinstance(location, dict):
        return None
    reported_path = location.get("path")
    if not isinstance(reported_path, str) or not reported_path:
        return None
    path = Path(reported_path)
    if not path.is_absolute():
        if ".." in path.parts:
            return None
        path = root / path
    return path.resolve(strict=True)


def _resolved_read_locations(locations, root):
    if not isinstance(locations, list) or not locations:
        return None
    resolved = []
    for location in locations:
        try:
            path = _resolve_read_location(location, root)
        except FileNotFoundError:
            path = None
        if path is not None:
            resolved.append(path)
            continue
        raw = location.get("path") if isinstance(location, dict) else None
        if not isinstance(raw, str) or ", " not in raw:
            return None
        parts = raw.split(", ")
        if len(parts) > 16 or any(not part or part != part.strip() for part in parts):
            return None
        for part in parts:
            try:
                path = _resolve_read_location({"path": part}, root)
            except FileNotFoundError:
                return None
            if path is None or not (path.is_file() or path.is_dir()):
                return None
            resolved.append(path)
        for start in range(len(parts)):
            for end in range(start + 2, len(parts) + 1):
                try:
                    combined = _resolve_read_location({"path": ", ".join(parts[start:end])}, root)
                except FileNotFoundError:
                    continue
                if combined is not None:
                    return None
    return resolved


def _safe_read_locations(tool_call, allowed_paths=()):
    locations = tool_call.get("locations")
    try:
        root = Path(settings.PROJECT_PATH).resolve(strict=True)
        resolved_locations = _resolved_read_locations(locations, root)
        if not resolved_locations:
            return False
        allowed_locations = [(root, True)]
        for entry in allowed_paths:
            allowed = Path(entry["path"])
            directory = entry["kind"] == "directory"
            exists = allowed.is_dir() if directory else allowed.is_file()
            if exists and allowed.resolve(strict=True) == allowed:
                allowed_locations.append((allowed, directory))
        for resolved in resolved_locations:
            if not any(
                resolved.is_relative_to(allowed) if directory else resolved == allowed
                for allowed, directory in allowed_locations
            ):
                return False
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def protected_read_directories(tool_call, allowed_paths=()):
    if tool_call.get("kind") not in ("read", "search"):
        return []
    locations = tool_call.get("locations")
    if not isinstance(locations, list) or not locations:
        return []
    directories = []
    try:
        root = Path(settings.PROJECT_PATH).resolve(strict=True)
        resolved_locations = _resolved_read_locations(locations, root)
        if not resolved_locations:
            return []
        for resolved in resolved_locations:
            if not resolved.is_file() and not resolved.is_dir():
                return []
            if _safe_read_locations({"locations": [{"path": str(resolved)}]}, allowed_paths):
                continue
            directory = str(resolved if resolved.is_dir() else resolved.parent)
            if directory not in directories:
                directories.append(directory)
    except (OSError, RuntimeError, ValueError):
        return []
    return directories


@sync_to_async(thread_sensitive=True)
def auto_approve_safe_operation(run_id, tool_call, options, option):
    with transaction.atomic():
        active_run = AgentRun.objects.filter(
            pk=run_id, status__in=AgentRun.ACTIVE, cancel_requested=False,
        )
        # Acquire SQLite's write lock before reading the policy so changes to the
        # setting and cancellation cannot race this decision.
        if not active_run.update(cancel_requested=F("cancel_requested")):
            return None
        repository_settings = RepositorySettings.objects.get(pk=1)
        mcp_mismatch = reported_mcp_tool(tool_call) and reported_mcp_kind_mismatch(tool_call)
        prefixes = None if mcp_mismatch else matching_command_prefixes(tool_call, repository_settings.allowed_command_prefixes)
        mcp_rule = matching_mcp_rule(tool_call, repository_settings.allowed_mcp_tools)
        origin = tool_call.get("originToolCall")
        safe_read = (
            not mcp_mismatch and repository_settings.allow_safe_operations
            and tool_call.get("kind") in ("read", "search")
            and (
                "originToolCall" not in tool_call
                or isinstance(origin, dict) and origin.get("kind") in ("read", "search") and "nonReadKind" not in origin
            )
            and _safe_read_locations(tool_call, repository_settings.safe_read_paths)
        )
        if not prefixes and not safe_read and not mcp_rule:
            return None
        permission = PermissionRequest.objects.create(
            run_id=run_id, tool_call=tool_call, options=options,
            selected_option=option["optionId"], resolved=True,
        )
        RunEvent.objects.create(
            run_id=run_id,
            kind="permission decision",
            content=json.dumps({
                "toolCall": tool_call,
                "selectedOption": option,
                "automatic": True,
                "origin": "repository_mcp_policy" if mcp_rule else "repository_command_policy" if prefixes else "repository_safe_read_policy",
                "reason": "The reported MCP tool matched a saved rule." if mcp_rule else
                          "Every reported command matched a saved prefix." if prefixes else
                          "Every reported location resolved within the configured project directory "
                          "or matched an explicitly allowed file or directory.",
                **({"allowedMcpRule": mcp_rule} if mcp_rule else {}),
                **({"allowedCommandPrefixes": prefixes} if prefixes else {}),
                **({"allowedCommandPrefix": prefixes[0]} if prefixes and len(prefixes) == 1 else {}),
            }, indent=2, ensure_ascii=False),
        )
        return permission.selected_option


@sync_to_async(thread_sensitive=True)
def permission_result(permission_id):
    permission = PermissionRequest.objects.get(pk=permission_id)
    return permission.resolved, permission.selected_option


@sync_to_async(thread_sensitive=True)
def resume_after_permission(run_id):
    if AgentRun.objects.filter(pk=run_id, status__in=AgentRun.ACTIVE, cancel_requested=False).exists():
        refresh_wait_state(run_id)


@sync_to_async(thread_sensitive=True)
def question_result(question_id):
    question = AgentQuestion.objects.get(pk=question_id)
    return question.outcome, question.answer


@sync_to_async(thread_sensitive=True)
def cancel_questions(run_id):
    with transaction.atomic():
        cancelled = AgentQuestion.objects.filter(run_id=run_id, outcome=AgentQuestion.Outcome.PENDING).update(
            outcome=AgentQuestion.Outcome.CANCELLED,
        )
        if cancelled:
            Task.objects.filter(runs__pk=run_id, automatic_pause_reason=QUESTION_PAUSE).exclude(
                runs__questions__outcome=AgentQuestion.Outcome.PENDING,
            ).update(
                automatic_pause_reason="The agent run did not complete normally. Inspect it before continuing.",
                updated_at=timezone.now(),
            )


async def wait_for_answer(run_id, question_id, *, reader=None):
    while True:
        if await is_cancelled(run_id):
            await cancel_questions(run_id)
            return None
        if reader is not None and reader.at_eof():
            raise RuntimeError("The ACP connection closed while waiting for recovery direction.")
        outcome, answer = await question_result(question_id)
        if outcome == AgentQuestion.Outcome.ANSWERED:
            return answer
        if outcome == AgentQuestion.Outcome.CANCELLED:
            return None
        await asyncio.sleep(0.5)


@sync_to_async(thread_sensitive=True)
def current_run_report(run_id):
    return final_agent_report(list(RunEvent.objects.filter(run_id=run_id)))


@sync_to_async(thread_sensitive=True)
def pause_automatic_for_permission(run_id, reason):
    Task.objects.filter(
        runs__pk=run_id, automatic_pause_reason="",
    ).update(
        automatic_pause_reason=reason,
        updated_at=timezone.now(),
    )


class RunClient(Client):
    def __init__(self, run_id):
        self.run_id = run_id
        self.tool_calls = {}
        self.tool_origins = {}
        self.elicitation_error = ""

    async def session_update(self, session_id, update, **kwargs):
        data = update.model_dump(by_alias=True, exclude_none=True, mode="json")
        kind = data.get("sessionUpdate", "")
        if kind == "usage_update":
            await record_usage_update(self.run_id, data)
        elif kind == "agent_message_chunk" and data.get("content", {}).get("type") == "text":
            await save_event(self.run_id, "message", data["content"]["text"])
        elif kind in ("tool_call", "tool_call_update"):
            tool_call_id = data.get("toolCallId")
            if tool_call_id:
                details = {key: value for key, value in data.items() if key != "sessionUpdate"}
                if kind == "tool_call" and tool_call_id not in self.tool_origins:
                    self.tool_origins[tool_call_id] = {
                        key: details[key] for key in ("kind", "title", "rawInput") if key in details
                    }
                elif tool_call_id in self.tool_origins and details.get("kind") not in (
                    None, "read", "search", self.tool_origins[tool_call_id].get("kind"),
                ):
                    self.tool_origins[tool_call_id]["nonReadKind"] = details["kind"]
                self.tool_calls.setdefault(tool_call_id, {}).update(details)
            await save_event(self.run_id, "tool", json.dumps(data, indent=2, ensure_ascii=False))

    async def create_elicitation(self, message, mode, **kwargs):
        if not isinstance(mode, (ElicitationFormSessionMode, ElicitationFormRequestMode)):
            self.elicitation_error = "The agent requested an unsupported question mode."
            raise RequestError.invalid_params({"details": self.elicitation_error})
        if isinstance(mode, ElicitationFormSessionMode):
            run = await sync_to_async(AgentRun.objects.get, thread_sensitive=True)(pk=self.run_id)
            if mode.session_id != run.session_id:
                self.elicitation_error = "The agent question belongs to a different session."
                raise RequestError.invalid_params({"details": self.elicitation_error})
        schema = mode.requested_schema.model_dump(by_alias=True, exclude_none=True, mode="json")
        try:
            question_id = await sync_to_async(create_question, thread_sensitive=True)(self.run_id, message, schema, "acp")
        except ValueError as error:
            self.elicitation_error = f"Unsupported agent question: {error}"
            raise RequestError.invalid_params({"details": self.elicitation_error}) from error
        answer = await wait_for_answer(self.run_id, question_id)
        if answer is None:
            return CancelElicitationResponse(action="cancel")
        return AcceptElicitationResponse(action="accept", content=answer)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        choices = [
            option.model_dump(by_alias=True, exclude_none=True, mode="json")
            for option in options if option.kind in ("allow_once", "reject_once")
        ]
        if not choices:
            await save_event(self.run_id, "permission", "No one-time permission option was offered; request cancelled.")
            await pause_automatic_for_permission(
                self.run_id,
                "The ACP permission request had no supported one-time choices and was rejected. Inspect the run before continuing.",
            )
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        details = self.tool_calls.get(tool_call.tool_call_id, {}).copy()
        details.update(tool_call.model_dump(by_alias=True, exclude_none=True, mode="json"))
        details["originToolCall"] = self.tool_origins.get(tool_call.tool_call_id, {})
        allow_once = next(
            (
                option for option in choices
                if option.get("kind") == "allow_once"
                and isinstance(option.get("optionId"), str)
                and option["optionId"]
            ),
            None,
        )
        if (
            allow_once
            and (details.get("kind") in ("read", "search", "execute") or reported_mcp_tool(details))
        ):
            selected = await auto_approve_safe_operation(self.run_id, details, choices, allow_once)
            if selected is not None:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", optionId=selected),
                )
            if await is_cancelled(self.run_id):
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        permission_id = await create_permission(self.run_id, details, choices)
        while True:
            if await is_cancelled(self.run_id):
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
            resolved, selected = await permission_result(permission_id)
            if resolved:
                await resume_after_permission(self.run_id)
                return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", optionId=selected))
            await asyncio.sleep(0.5)


async def watch_cancel(connection, session_id, run_id):
    while not await is_cancelled(run_id):
        await asyncio.sleep(0.5)
    await connection.cancel(session_id)
    await save_event(run_id, "status", "Cancellation requested; awaiting the agent.")
    await asyncio.sleep(15)


async def stop_if_cancelled(run_id):
    if await is_cancelled(run_id):
        raise RuntimeError("Run cancelled before the prompt was sent.")


@sync_to_async(thread_sensitive=True)
def session_title(run_id):
    run = AgentRun.objects.select_related("task").get(pk=run_id)
    if run.task_id is None:
        return f"[project:{run.pk}] {run.request_snapshot.splitlines()[0][:150]}"
    return f"[{run.task_id}:{run.stage}] {run.task.title}"


def validate_recovery(run_id, status):
    run = AgentRun.objects.select_related("task", "acp_endpoint").get(pk=run_id)
    task = run.task
    if (
        run.status != status or run.cancel_requested
        or not run.acp_endpoint or not run.acp_endpoint.enabled
        or (run.acp_host, run.acp_port) != (run.acp_endpoint.host, run.acp_endpoint.port)
        or run.permissions.filter(resolved=False).exists()
        or (task is None and run.stage != AgentRun.PROJECT)
        or (task is not None and (
            task.finished or task.status != Task.Status.READY or task.stage != run.stage
            or task.version != run.task_version or task.work_cycle != run.work_cycle
            or task.automatic_pause_reason not in RECOVERY_PAUSE_REASONS
            or task.runs.order_by("-id").values_list("pk", flat=True).first() != run.pk
            or task.state != (Task.State.SCOPE_APPROVED if run.stage == Task.Stage.ARCHITECT else Task.State.PLAN_APPROVED)
        ))
    ):
        raise RuntimeError("The run, task, or ACP endpoint changed during recovery; inspect before continuing.")
    return run


@sync_to_async(thread_sensitive=True)
def prepare_recovered_question(run_id):
    with transaction.atomic():
        AgentRun.objects.select_for_update().get(pk=run_id)
        run = validate_recovery(run_id, AgentRun.Status.STARTING)
        previous_error = run.error
        run.status = AgentRun.Status.RUNNING
        run.error = ""
        run.finished_at = None
        run.save(update_fields=["status", "error", "finished_at"])
        RunEvent.objects.create(
            run=run, kind="status",
            content=(
                "Saved ACP session reattached. The interrupted turn's outcome is unverified; waiting for human direction."
                + (f"\nPrevious interruption: {previous_error}" if previous_error else "")
            ),
        )
        Task.objects.filter(pk=run.task_id, automatic_pause_reason__in=RECOVERY_PAUSE_REASONS).update(
            automatic_pause_reason="", updated_at=timezone.now(),
        )
        return create_question(
            run_id,
            "The saved ACP session was reattached, but the interrupted turn's outcome is unverified. "
            "Inspect the run and project, then provide instructions for the agent.",
            TEXT_SCHEMA, "recovery",
        )


async def run_agent(run_id, prompt, endpoint_id, host, port, stage, *, recovery_session_id=None):
    connection = None
    writer = None
    monitor = None
    endpoint_initialized = False
    connection_attempted = False
    try:
        await stop_if_cancelled(run_id)
        if prompt is None and recovery_session_id is None:
            raise RuntimeError("The initial prompt was not recorded; this run cannot be dispatched.")
        connection_attempted = True
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10,
        )
        client = RunClient(run_id)
        connection = acp.connect_to_agent(client, writer, reader, use_unstable_protocol=True)
        initialized = await asyncio.wait_for(connection.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(elicitation=ElicitationCapabilities(form=ElicitationFormCapabilities())),
        ), timeout=30)
        if initialized.protocol_version != acp.PROTOCOL_VERSION:
            raise RuntimeError(f"Unsupported ACP protocol version: {initialized.protocol_version}")
        await sync_to_async(mark_endpoint_checked, thread_sensitive=True)(endpoint_id)
        endpoint_initialized = True
        await stop_if_cancelled(run_id)
        capabilities = (
            initialized.agent_capabilities.session_capabilities if initialized.agent_capabilities else None
        )
        if recovery_session_id is not None:
            if not capabilities or capabilities.status is None or capabilities.resume is None:
                raise RuntimeError("ACP does not advertise session status and resume; inspect the interrupted run.")
            status = await asyncio.wait_for(connection.session_status(recovery_session_id), timeout=30)
            if status.status != "saved":
                raise RuntimeError(f"ACP session status is {status.status!r}, not 'saved'; inspect the interrupted run.")
            await sync_to_async(validate_recovery, thread_sensitive=True)(run_id, AgentRun.Status.STARTING)
            await asyncio.wait_for(connection.resume_session(
                session_id=recovery_session_id, cwd=str(settings.PROJECT_PATH), mcp_servers=[],
            ), timeout=60)
            session_id = recovery_session_id
            question_id = await prepare_recovered_question(run_id)
            answer = await wait_for_answer(run_id, question_id, reader=reader)
            if answer is None:
                raise RuntimeError("Recovery direction was cancelled; inspect the agent and project before continuing.")
            await sync_to_async(validate_recovery, thread_sensitive=True)(run_id, AgentRun.Status.RUNNING)
            prompt = (
                "The session was reattached after Flamethrower restarted. The interrupted turn's outcome is unverified. "
                "The human inspected the run and provided this direction:\n"
                f"{answer['answer']}\n\nContinue only within the original "
                f"{'project question' if stage == AgentRun.PROJECT else 'approved request and plan'}. "
                "Use the required question or completion format from the original instructions."
            )
        else:
            session = await asyncio.wait_for(connection.new_session(cwd=str(settings.PROJECT_PATH), mcp_servers=[]), timeout=60)
            session_id = session.session_id
            await update_run(run_id, session_id=session_id)
            if capabilities and capabilities.title is not None:
                title = await session_title(run_id)
                try:
                    result = await asyncio.wait_for(connection.set_session_title(session_id, title), timeout=30)
                    await save_event(run_id, "status", f"ACP session title: {result.title}")
                except (RequestError, asyncio.TimeoutError) as error:
                    await save_event(run_id, "status", f"ACP session title could not be set: {error}")
            else:
                await save_event(run_id, "status", "ACP does not advertise session title support; title not set.")
        await stop_if_cancelled(run_id)
        monitor = asyncio.create_task(watch_cancel(connection, session_id, run_id))
        while True:
            prompt_task = asyncio.ensure_future(connection.prompt(session_id=session_id, prompt=[TextContentBlock(text=prompt)]))
            done, _ = await asyncio.wait([prompt_task, monitor], return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
                prompt_task.cancel()
                raise RuntimeError("Agent did not finish after cancellation; its final state is NOT_YET_VERIFIED.")
            response = await prompt_task
            if response.usage is not None:
                await record_token_usage(
                    run_id,
                    response.usage.model_dump(by_alias=True, exclude_none=True, mode="json"),
                )
            if await is_cancelled(run_id):
                raise RuntimeError("Cancellation was requested; inspect the agent and project before continuing.")
            if client.elicitation_error:
                raise RuntimeError(client.elicitation_error)
            if response.stop_reason != "end_turn":
                raise RuntimeError(f"Agent turn ended with stop reason {response.stop_reason}; outcome is NOT_YET_VERIFIED.")
            report = await current_run_report(run_id)
            text = report["text"]
            if stage in (Task.Stage.ARCHITECT, AgentRun.PROJECT):
                task_response = await sync_to_async(handle_task_block, thread_sensitive=True)(run_id, text)
                if task_response is not None:
                    prompt = task_response
                    continue
            question = question_text(text)
            complete = (
                bool(architect_plan(text)) if stage == Task.Stage.ARCHITECT else
                bool(project_answer(text)) if stage == AgentRun.PROJECT else
                developer_complete(text) if stage == Task.Stage.DEVELOPER else
                bool(report["recommendation"])
            )
            if not question and complete:
                break
            message = question or (
                "The agent did not provide a recognized completion or question. "
                "Review the run activity and provide guidance before it continues."
            )
            question_id = await sync_to_async(create_question, thread_sensitive=True)(run_id, message, TEXT_SCHEMA, "turn")
            answer = await wait_for_answer(run_id, question_id)
            if answer is None:
                raise RuntimeError("The question was cancelled; inspect the agent and project before continuing.")
            prompt = (
                "The human answered the pending question or review request:\n"
                f"{answer['answer']}\n\nContinue only within the original "
                f"{'project question' if stage == AgentRun.PROJECT else 'approved request and plan'}. "
                "If clarification is still needed, ask another question. "
                "Use the required question or completion format from the original instructions."
            )
        await sync_to_async(complete_run, thread_sensitive=True)(run_id, response.stop_reason)
    except Exception as error:
        logger.exception("Agent run %s interrupted", run_id)
        if connection_attempted and not endpoint_initialized:
            await sync_to_async(mark_endpoint_checked, thread_sensitive=True)(endpoint_id, str(error))
        await update_run(run_id, status=AgentRun.Status.INTERRUPTED, error=str(error), finished_at=timezone.now())
    finally:
        await cancel_questions(run_id)
        if monitor:
            monitor.cancel()
        if connection:
            await connection.close()
        if writer:
            writer.close()
            await writer.wait_closed()
