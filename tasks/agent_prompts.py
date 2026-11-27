from .architect_tasks import architect_task_context, project_task_context
from .models import AgentRun, Task


def build_initial_prompt(run):
    stage, request_text, plan, context = run.stage, run.request_snapshot, run.plan_snapshot, run.context_snapshot
    if stage == AgentRun.PROJECT:
        return (
            "Answer the human's project-wide question using available project evidence. "
            "Distinguish verified facts from assumptions and report uncertainty. "
            "Do not modify project files, commit, push, publish, or change tasks or links directly. "
            "Agent permissions are not a sandbox; use tools only as needed for this question.\n\n"
            f"QUESTION:\n{request_text}\n\n"
            "If you need a human decision, use an available question form, or end a turn with:\n"
            "FLAMETHROWER_QUESTION\n<question and context>\nEND_FLAMETHROWER_QUESTION"
            + project_task_context()
            + "\n\nWhen finished, end your response with exactly one block, without a Markdown fence:\n"
            "FLAMETHROWER_PROJECT_ANSWER\n<answer, evidence, and uncertainties>\nEND_FLAMETHROWER_PROJECT_ANSWER"
        )
    instructions = {
        Task.Stage.ARCHITECT: (
            "Act as Architect for the following human-approved request. Inspect relevant project context "
            "and propose one implementation-useful design, including affected components, dependencies, "
            "risks, permissions, verification, alternatives, and two Mermaid diagrams. "
            "Distinguish verified facts from assumptions and mark unverified claims NOT_YET_VERIFIED. "
            "Do not implement changes, edit project files, run project commands, or approve your own design. "
            "If the request or scope is unclear, stop and explain the decision needed."
        ),
        Task.Stage.DEVELOPER: "Implement the following human-approved request and plan. If the plan is insufficient, stop and explain.",
        Task.Stage.TESTING: "Test the implementation against the approved request and plan. Do not implement fixes or modify project files except test-generated artifacts. Report commands, actual results, failures, and untested areas.",
        Task.Stage.REVIEW: "Independently review the implementation against the approved request, plan, and provided test evidence. Do not modify project files. Report actionable findings and uncertainties; do not claim an independent review occurred merely because this session ran.",
    }
    if stage not in instructions:
        raise RuntimeError(f"Unsupported agent stage: {stage}")
    prompt = (
        f"{instructions[stage]} Preserve unrelated work. Do not commit, push, publish, or perform "
        f"operations outside the approved {'request' if stage == Task.Stage.ARCHITECT else 'plan'}. "
        "Report only work actually performed; do not claim "
        "unrun checks passed.\n\n"
        f"REQUEST:\n{request_text}\n\n"
        f"{'EXISTING UNAPPROVED PLAN' if stage == Task.Stage.ARCHITECT else 'APPROVED PLAN'}:\n{plan}"
    )
    if context:
        prompt += f"\n\nHUMAN-RECORDED CONTEXT:\n{context}"
    prompt += (
        "\n\nIf you need a human decision, use an available question form. If none is available, "
        "end your response with exactly this block and wait for an answer:\n"
        "FLAMETHROWER_QUESTION\n<the question and any necessary context>\nEND_FLAMETHROWER_QUESTION\n"
        "Do not treat an answer as permission to change the approved request or plan."
    )
    if stage == Task.Stage.ARCHITECT:
        prompt += architect_task_context(run.pk)
        prompt += (
            "\n\nIf a complete design is ready for human review, end your final response with "
            "exactly one block in this format, without a Markdown fence around the block:\n"
            "FLAMETHROWER_ARCHITECT_PLAN\n"
            "<the proposed design>\n"
            "END_FLAMETHROWER_ARCHITECT_PLAN\n"
            "If a decision blocks the design, explain it without a plan block."
        )
    if stage != Task.Stage.ARCHITECT:
        prompt += (
            "\n\nIf you discover useful related work outside the approved scope, do not implement it "
            "as part of this task. Instead, put one deferred-task block in your final response, "
            "with valid JSON and no Markdown fence, using this format:\n"
            "FLAMETHROWER_DEFERRED_TASKS\n"
            '{"tasks":[{"title":"short title","request":"self-contained description of the deferred work and why it matters"}]}\n'
            "END_FLAMETHROWER_DEFERRED_TASKS\n"
            "Include at most 20 tasks; each title must be 1–200 characters and each request 1–20000 characters. "
            "Omit the block if there is no deferred work. These are unapproved draft tasks, not work completed. "
            "If the issue prevents completing the approved scope, stop and explain rather than treating it as resolved."
        )
    if stage in (Task.Stage.TESTING, Task.Stage.REVIEW):
        prompt += (
            "\n\nUnless asking a question, put any deferred-task block immediately before the gate recommendation. "
            "When reporting a gate result, end your final response with exactly one gate recommendation block in the following format, "
            "using valid JSON with string values and no Markdown code fence:\n"
            "FLAMETHROWER_GATE_RECOMMENDATION\n"
            '{"result":"passed|failed|unverified","recommendation":"advance|rework|hold",'
            '"summary":"concise conclusion","evidence":"actual commands, results, or findings",'
            '"untested":"what was not verified","reason":"why this action is recommended"}\n'
            "END_FLAMETHROWER_GATE_RECOMMENDATION\n"
            "Choose one value for result and recommendation, not the pipe-separated examples. "
            "Recommend advance only for passed, and never claim checks or review were performed "
            "when they were not. Use unverified and hold when evidence is insufficient. "
            "Recommend rework or hold for failed or unverified results. The human decides whether to accept."
        )
    elif stage != Task.Stage.ARCHITECT:
        prompt += (
            "\nWhen the approved implementation is complete, put any deferred-task block immediately "
            "before this final completion signal:\n"
            "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE"
        )
    return prompt
