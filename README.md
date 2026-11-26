# Flamethrower

Local, single-owner change planning with an initial Copilot ACP execution
path. Requests, plans, approvals and run activity are stored in the project's
`.flamethrower/` SQLite database.

## Run

Install Python 3.11 or newer, then from this application directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/flamethrower /path/to/project
```

For Copilot, start a trusted server separately with `copilot --acp --port 64674`
and launch with `.venv/bin/flamethrower --acp-port 64674 /path/to/project`.
The launcher creates an initial `127.0.0.1:64674` ACP endpoint from this
option when initializing or upgrading the project database. Manage endpoints
at **ACPs** in the application: add, edit, enable/disable, prioritize, or
delete them at runtime. Hosts must be loopback IP addresses or `localhost`;
ports must be 1–65535. Endpoint CRUD is available to any local web user because
the application has no end-user login.
Tasks default to **Automatic — highest-priority available ACP**; the lowest numeric
priority is preferred, with endpoint ID as a deterministic tie-breaker. Edit a
task to select a specific enabled ACP instead. Each endpoint allows five
concurrent sessions by default; change its session limit to match the server's
capabilities. Active and unacknowledged interrupted runs count toward the limit.
Reducing the limit does not stop existing runs. Automatic endpoint selection
chooses again for every new run. Concurrent agents can modify the same project files.
ACP status is derived from run history and the last
successful/failed ACP initialization; use **Check connection** on the endpoint
list to refresh it. The endpoint list links busy endpoints to their run and task.
Approve the request, then generate or enter and approve a plan. The workflow
starts eligible agent runs automatically and displays their activity and
one-time permission requests. The agent can send project content to its
configured provider.

When an agent needs clarification, the task waits for a human answer. Supported
ACP form questions and explicitly marked questions appear on the task page; the
answer is sent to the same live session. An ambiguous response without a
recognized completion signal also waits for review instead of advancing. An
answer does not approve changes to the request, plan, or tool permissions.
If the application stops while waiting, the run is interrupted and must be
reviewed rather than silently resumed. ACP form elicitation is an unstable
protocol feature and depends on the agent supporting it.

On **ACPs**, automatic read/search approvals are off by default. Enable them
to allow reported locations inside the project, and optionally add trusted
absolute file or directory paths. Directories include descendants; files match
exactly. Symlinks are resolved when saving and checking paths, so a virtualenv
Python executable may require allowing its external target. Every reported
location must be allowed, and the ACP must offer an “allow once” option.
Relative locations are resolved against the ACP project directory; relative
paths containing `..` still require manual approval.
When an ACP combines locations into one comma-separated path, Flamethrower
recognizes them only if each path exists and the split is unambiguous. A
read/search prompt originating from a shell command, or without a known
read/search origin, is not automatically approved as a safe read. Allowing
such a prompt once permits the entire underlying command.
Writes require manual approval. From an eligible
permission request, you can allow it once and save a reported directory for
future read/search approvals; this does not enable the policy. Saving the
allowlist on the ACP page does not resolve already pending requests. This is an
approval convenience, not a sandbox; agent-supplied metadata may be incomplete,
and reads can expose secrets.

An eligible execute permission can also be allowed once while saving a
user-selected command prefix. The rule takes effect immediately across tasks
and ACP endpoints, independently of read/search settings; remove it on
**ACPs**. Matching requires complete arguments. For commands joined by `&&`
or `||`, every command must match a saved prefix; other shell syntax, quotes,
and expansions require manual approval. Any remaining arguments are allowed: even test
commands can execute project code or modify files. The reported command is
unverified agent metadata, not a sandbox.

For an MCP permission whose reported server and tool can be identified, you
can allow it once and save either that tool or all tools on that server.
Rules apply immediately across tasks and ACP endpoints, for any arguments,
and can be removed on **ACPs**. A server-wide rule can allow commands, writes,
and other powerful actions. Server and tool names are agent-reported, not
verified identities; these rules are not a sandbox.
If an MCP call's reported kind changes, it requires a separate rule for the
recorded kinds; existing MCP, read/search and command-prefix rules do not
automatically approve that recognized mismatched MCP request.

The board shows **Draft → Architect → Developer → Testing → Review → Finished**.
Attention labels identify tasks awaiting human input without moving them out
of their stage. Approve the request before architecture. The Architect can
propose a plan through a separate ACP session. Beneath a proposal, **Accept**
approves the plan and advances to Developer, **Refine** collects feedback for a
new Architect run, and **Abandon** marks the task Abandoned. Accept and Abandon
take effect immediately; Refine opens inline with Save/Cancel and records nothing
until Save. These actions update the task page in place. A plan may
also be entered and approved manually.
Editing the request requires renewed request approval; editing an approved
plan requires renewed plan approval.
After a workflow pause, a completed developer turn remains in Developer until
you inspect and approve it, or refuse it with feedback to start another
Developer run.

Session activity has its own page with the shared task header, run history,
input snapshots, and live updates. Open it from a task's session link or run
history. Approvals, permissions, and run controls stay on the task page.

Tasks have incrementing numeric IDs, severity (Blocker, Critical, Major, Minor or
Trivial; default Major), a status, and a per-stage priority. Drag cards up or
down within a lane, or use their arrow buttons; lower priority numbers come
first. Reordering is disabled during search. Tasks entering a different stage
are placed at the end of that lane; gaps in priority numbers can occur until
the lane is reordered.

Tasks also have a work type, defaulting to **Task**, including existing tasks.
The built-in types are Bug, Enabler, Story, Improvement, Task, Sub-task, Test,
Test Plan, Test Execution, and Event. Manage these and add custom types in
**Work types** in Django admin: each has a title, short description, and built-in
icon or optional uploaded PNG, JPEG, GIF, or WebP. Uploaded icons
take precedence and are stored under `.flamethrower/media/work_types/`.
Choose a type when creating a task or change it under **Task details** on its
Edit page. Types are descriptive; Sub-task does not create a parent link or
change workflow rules. Types in use cannot be deleted, and the default type
can be renamed but cannot be deleted.

After a task is indexed, its page suggests a work type by comparing the task
text with the configured type names and descriptions using the local similarity
model. The suggestion does not change the selected type; review it in Task
details. Changes to type descriptions trigger recalculation. Suggestions are
experimental and depend on the configured descriptions.

Human-created tasks start Ready; agent-created tasks start Non-Accepted and
must be explicitly set to Ready before advancing. Change status and severity
in the task's Edit page. Blocked requires a reason
describing the external constraint, and Blocked or Abandoned tasks cannot
advance or start runs. Finish or cancel an active run before blocking or
abandoning its task. Set the status back to Ready, then continue the workflow
if it is paused. Done is assigned on workflow
completion, and reopening returns the task to Ready. Status does not replace
Testing or Review gate results, and task links do not automatically block
progress. During upgrade, existing finished tasks become Done; agent-created
drafts become Non-Accepted and other existing tasks become Ready.

Use **Link task** in the task header or **Linked tasks** panel to open the
dedicated link-management page. Choose a relationship and search existing
tasks by ID or title. Selecting a search result submits the link; **Unlink**
on the management page removes it. Linked
tasks are grouped by relationship, with stage, severity, priority, and status.
**Create linked task** collects a title and requested change, then creates a
Ready task in Draft and links it when submitted. The new request still needs
approval before work starts. New link/unlink events appear in both tasks'
link history, preserving the relationship labels and titles at that time.
Changes use standard POST submissions and redirect back to the management
page; validation errors stay on that page with the submitted inputs.
Without JavaScript, the existing-task picker uses a standard dropdown and
form submission. Links remain descriptive and do not enforce execution order.

A normal **Link task** click opens the management page in a popover. Middle
clicks, modifier clicks, and the browser's new-tab/window actions still open
the original URL normally. Close the popover with **Close**, **Back to task**,
or Escape; the task's linked-task summary refreshes afterward. Links opt into
this behavior with `data-popover`, optionally using
`data-popover-refresh="task-links"` to refresh that section on close.
Popover target pages must allow same-origin framing; this is enabled only
for link-management responses, not globally.

Quick task entry suggests a title locally using `google/flan-t5-small`. Press
Enter to create the task, or use the arrow to review the suggestion before
saving. The model is downloaded on first use; if generation fails, enter a
title manually. If it produces a repetitive or empty title, the request text is
used instead and a notice is shown. The suggestion uses up to 512 input tokens;
the full request is preserved.

Flamethrower also indexes task titles and requests in the background using
the local `BAAI/bge-small-en-v1.5` sentence model. Existing tasks are queued
on upgrade, and new tasks start pending. Its pinned model files are downloaded
from Hugging Face on first use and cached by the Hugging Face libraries; task
text is processed locally and is not sent to Hugging Face. The model is English
and long inputs are truncated, so suggestions can miss relevant tasks. Model
weights and inference dependencies require additional disk space and memory.
Embeddings are stored in the project's database and may contain information
about task text.

Open **Similarity settings** in Django admin to change the minimum cosine
similarity, maximum suggestions, and model token limit. Changing the token
limit reindexes all tasks; changing the threshold or result limit takes effect
immediately. Similar tasks are suggestions, not links: accept one to create a
Relates link or dismiss it for that task. A dismissed suggestion can reappear
after either task is edited or reindexed. Edits to titles or requests requeue
indexing; plan-only edits do not. Failed jobs show an error and a Retry button
on the task page. Interrupted jobs are requeued on the next launch. The worker
runs only in the `flamethrower` launcher, not when using `manage.py runserver`.
On shutdown the launcher waits for the current model operation to finish.

Workflow progression is automatic for every task. After request approval, it
starts an Architect run if no plan exists, then waits for you to accept and
approve the plan. It never approves the request or plan for you. After plan approval it runs
Developer, Testing and Review, advances
on a structured passing gate recommendation, and sends a structured rework
recommendation back to Developer. It pauses for ACP permission requests,
agent errors or interruptions, unclear or held gate reports, request/plan
changes, when no enabled ACP endpoint is free, or the third return to the same
gate. An interruption reserves only its assigned endpoint until acknowledged.
Agent recommendations are not independent verification; the ACP server remains
trusted and can modify files or execute commands.

When the workflow is paused and a Developer run completes, review it and choose
**Approve** or **Refuse** rather than continuing past the decision. Approving
advances to Testing; refusing requires feedback and starts a new Developer run.
**Continue workflow** is unavailable while an approval, permission,
interruption acknowledgement, or gate decision is required. Resolve the
required action first; a simple pause with no outstanding decision can be
continued explicitly. Existing approved tasks are not launched as a migration
side effect; use **Continue workflow** on an eligible idle task after upgrading.

Testing and Review can use manual evidence, separate Copilot ACP sessions, or
both. When automation pauses at a gate, record a human result and evidence; a
failed or NOT_YET_VERIFIED result requires an explicit recorded waiver to
advance. Saving results without advancing is supported. A completed agent
turn does not prove the gate passed. Review sessions use the same configured server: independent
review and read-only enforcement are NOT_YET_VERIFIED. Testing and review
agents can modify files or execute commands; the server is trusted, not
sandboxed. The Review prompt receives the recorded testing result,
evidence and waiver, which may be sent to the agent's provider.
Testing and Review agents are asked for an explicit structured recommendation.
When a human gate decision is required, Next action shows action buttons rather
than an expanded form.
Choose **Pass** to record evidence and advance, **Fail** or **Hold** to record a
failed or NOT_YET_VERIFIED result without advancing, or **Waiver / Force** to
advance with an explicit waiver. **Return to Developer** asks for rework feedback;
**Run / Rerun** confirms starting another Copilot session. Each action opens an
inline form with **Save** and **Cancel**; selecting an action records nothing.
Saving Pass or Waiver / Force clears the previous stage's pause and continues
to the next stage automatically. Fail and Hold leave the workflow paused. A
confirmation states the saved result and transition.
Next action shows the request, plan, or current run report being assessed,
including recorded gate evidence when available. Agent reports may prefill
evidence, but are not independently verified. Rework suggestions require human
confirmation, and failed or unverified
results still require a human waiver to advance. Older or malformed reports are
shown without inferring a recommendation and can be assessed manually.
Testing or Review can return a task to Developer under the same approved plan;
the next developer prompt receives the rework reason and latest result from
that stage. Each return starts a new work cycle requiring another developer
run and new Testing and Review decisions; waivers remain available. Prior
evidence remains in the history.

The Architect is instructed to inspect context and propose a design without
editing files or running project commands. These instructions are **not
enforced**: the trusted ACP server may run commands, modify files, or use saved
permissions without prompting. Read-only isolation is **NOT_YET_VERIFIED**.
Agent proposals are not verified and require explicit human acceptance and plan
approval. Interrupted runs require inspection and acknowledgement.

The ACP server determines its own tools and saved permissions. The app does not
sandbox the agent or technically enforce the plan; some operations may not
prompt. Trust the server and review changes before proceeding. Only one run
per ACP endpoint is allowed at a time. If a run is interrupted, inspect the server
and project before acknowledging it; work may have continued after disconnect.
An agent turn ending is not independent verification. Run activity and
permissions can contain sensitive data in the local database.

Developer, Testing and Review agents can report related work outside the
approved scope in a structured final-response block. After a successful turn,
Flamethrower creates up to 20 linked Draft tasks, records the source run, and
queues them for similarity indexing. They are not approved or automatically
executed. If a report is malformed, no tasks from it are created, the run shows
a warning, and the workflow pauses for inspection. No block means no
deferred work was reported; an interrupted run is not processed, so inspect its
activity for discoveries. Agents may miss or omit related work, and their
drafts are unverified. Work needed to complete the approved scope must not be
treated as resolved merely because it was deferred.

Omit the project argument to use the current directory. The command creates
`.flamethrower/` in that project, runs database migrations, binds a free
loopback port, and opens the browser. Use `--no-browser` to print the URL
without opening it. While running, `.flamethrower/server.json` contains the
launcher PID, the Flamethrower web port (not the ACP port), and a local API
credential. Treat this file as sensitive; the launcher creates it with
owner-only permissions. The launcher
removes its record on normal shutdown, Ctrl+C, and SIGTERM on a best-effort
basis. A forced termination or crash can leave a stale file; its presence does
not prove the process is running or the port is listening. A subsequent launch
under the project lock replaces a valid stale record. An invalid or symlinked
runtime file is left untouched and prevents startup. Stop with Ctrl+C.

## MCP server

After installing or updating dependencies and starting Flamethrower, configure
your MCP client to launch the standalone stdio server. For example:

```json
{
  "mcpServers": {
    "flamethrower": {
      "command": "/absolute/path/to/Flamethrower/.venv/bin/flamethrower-mcp",
      "args": ["/absolute/path/to/project"]
    }
  }
}
```

The project argument defaults to the current directory. The server reads that
project's runtime file, checks the PID, verifies the server's identity, and
uses the authenticated loopback HTTP API. Restart Flamethrower after upgrading
to apply the database migrations and load the expanded API, and restart the
MCP client connection to discover its tools. Stop or finish active runs before
restarting. The MCP server is not automatically attached to Flamethrower's
ACP sessions.

Alternatively, run the script directly with the project's Python environment:
set `command` to `/absolute/path/to/Flamethrower/.venv/bin/python` and `args` to
`["/absolute/path/to/Flamethrower/flamethrower/mcp_stdio.py", "/absolute/path/to/project"]`.
The second argument must point to the project opened in Flamethrower.

`search_task` searches titles, requests and plans. `list_tasks`, `get_task`,
`get_task_text`, `list_task_runs`, `get_run`, `get_run_error`, `get_run_event`,
`list_task_gate_decisions`, `get_gate_text`, and `find_similar_tasks` expose
task and run information. Paginated calls return `offset` and `has_more`, and truncated event or gate
previews can be read in chunks. Task text, run errors, and Architect proposals
also have paginated reads; revision and proposal hashes let clients detect
changes between pages. An MCP client may send these details to its agent provider.

`create_task` creates a Ready draft without approving it. `update_task` and
`update_task_metadata` require the current `version` and `stage_version`
from `get_task` and use the same validation, run restrictions, and approval
invalidation as the UI. Work types and relationships can be listed with
`list_work_types` and `list_relationship_types`; `list_task_links`,
`link_tasks`, and `unlink_tasks` manage relationships.

`approve_task_request`, `approve_task_plan`, `accept_architect_plan`, and
`record_task_gate` record MCP-agent decisions, not human decisions. They use
the same revision, run, evidence, and waiver checks as the UI. Approvals or
gate advancement can start agent runs, whose existing repository automatic
tool-approval policies still apply. MCP exposes no operation to approve or
resolve tool permissions, change permission allowlists, or start or cancel
runs directly. This API boundary is not a sandbox: trusted agents with other
local access may still interact with the app outside MCP.

`create_deferred_task` remains available for an unapproved Non-Accepted draft.
It takes a title and request, plus an optional
`source_task_id`; it creates an unapproved Non-Accepted draft and links it to
that task when supplied. No originating agent run is required. Search before
creating or retrying, especially if a connection error leaves the result
unknown; submitting the same discovery through MCP and a structured report can
create duplicates.

The main application has no login; the Django admin at `/admin/` requires an
authenticated staff account. Task records are read-only there; similarity
settings can be changed by users with the appropriate permission. After
launching once so project storage and migrations are initialized, create an
admin account with
`FLAMETHROWER_PROJECT=/path/to/project .venv/bin/python manage.py createsuperuser`.
This milestone only listens on `127.0.0.1`; do not expose it through a proxy or
tunnel. Treat local users and programs on the machine as trusted. The storage
directory contains sensitive task data; the application creates a `.gitignore`
inside it, but existing Git tracking and other tools' behavior should be
checked independently.

Enforced independent review, remote validation, and network hosting are not
implemented and are **NOT_YET_VERIFIED**. Existing
`.ai-work` records are not imported or modified.

## Tests

```sh
.venv/bin/python manage.py test
```

Mermaid code fences in Markdown reports render as diagrams using a bundled
local library. Diagrams are isolated from the application and cannot load
external resources. Their source stays available; invalid diagrams show an
error instead of hiding the report. Literal semicolons in sequence-message
prose are escaped for rendering without changing the recorded source. Diagram
backgrounds and colors follow the surrounding panel and selected theme.

Node.js is only needed to rebuild the bundled renderer:

```sh
npm ci
npm run build
```
