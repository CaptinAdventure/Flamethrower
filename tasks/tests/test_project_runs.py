import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync, sync_to_async
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from ..acp import create_project_run, create_run
from ..agent import run_agent, save_event
from ..agent_scheduler import claim_next_recovery, claim_next_run, recover_interrupted_runs
from ..architect_tasks import handle_task_block
from ..models import ACPUsageSnapshot, AcpEndpoint, AgentLinkProposal, AgentQuestion, AgentRun, LinkType, PermissionRequest, Task, TaskLink
from ..questions import TEXT_SCHEMA, answer_question, create_question


class ProjectRunTests(TestCase):
    def setUp(self):
        self.endpoint = AcpEndpoint.objects.create(name="Project ACP", port=12345, session_limit=1)
        self.first = Task.objects.create(title="First", request="first request")
        self.second = Task.objects.create(title="Second", request="second request")
        self.link_type = LinkType.objects.create(
            name="Project test dependency", outward_description="needs project task",
            inward_description="needed by project task",
        )

    def block(self, marker, items):
        return f"FLAMETHROWER_{marker}\n{json.dumps(items)}\nEND_FLAMETHROWER_{marker}"

    def proposal(self, run):
        text = self.block("TASKLINK", [{
            "sourceTaskId": self.first.pk, "destinationTaskId": self.second.pk,
            "linkType": self.link_type.outward_description, "reason": "Evidence of a dependency",
        }])
        return json.loads(handle_task_block(run.pk, text).split("\n", 1)[1].split("\n\n", 1)[0])

    @patch("tasks.views.project_runs.wake_scheduler")
    def test_submit_persists_independent_prompt_and_filters_completed_and_abandoned(self, wake):
        finished = Task.objects.create(title="Finished", request="r", finished=True)
        abandoned = Task.objects.create(title="Abandoned", request="r", status=Task.Status.ABANDONED)
        done = Task.objects.create(title="Done", request="r", status=Task.Status.DONE)
        before = Task.objects.count()
        response = self.client.post(reverse("project_runs"), {"question": "Check the task links"})
        run = AgentRun.objects.get(stage=AgentRun.PROJECT)
        self.assertRedirects(response, reverse("project_run_detail", args=[run.pk]))
        self.assertEqual(Task.objects.count(), before)
        self.assertIsNone(run.task_id)
        self.assertEqual(run.status, AgentRun.Status.PENDING)
        self.assertIn(f'"id": {self.first.pk}', run.initial_prompt)
        for task in (finished, abandoned, done):
            self.assertNotIn(f'"id": {task.pk}', run.initial_prompt)
        self.assertIn("FLAMETHROWER_TASKQUERY", run.initial_prompt)
        self.assertIn("Proposals do not create links", run.initial_prompt)
        wake.assert_called_once_with()
        self.assertEqual(self.client.get(reverse("acp_list")).status_code, 200)

    def test_scheduler_uses_shared_endpoint_capacity_and_recovery(self):
        run = create_project_run("Question")
        claimed = claim_next_run()
        self.assertEqual(claimed.pk, run.pk)
        task = Task.objects.create(title="Workflow", request="r", stage="developer", state="plan_approved")
        pending = create_run(task, stage=task.stage, task_version=task.version, request_snapshot="r", plan_snapshot="p")
        self.assertIsNone(claim_next_run())
        AgentRun.objects.filter(pk=run.pk).update(session_id="project-session")
        recover_interrupted_runs()
        self.assertEqual(claim_next_recovery().pk, run.pk)
        self.assertIsNone(claim_next_run())
        AgentRun.objects.filter(pk=run.pk).update(status=AgentRun.Status.ACKNOWLEDGED)
        self.assertEqual(claim_next_run().pk, pending.pk)

    def test_project_runs_appear_on_endpoint_usage_page(self):
        create_project_run("Question")
        run = claim_next_run()
        ACPUsageSnapshot.objects.create(run=run, cost_amount="0.5", cost_currency="USD")
        response = self.client.get(reverse("acp_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("project_run_detail", args=[run.pk]))

    def test_query_and_link_proposal_require_human_approval(self):
        run = create_project_run("Review task links")
        run = claim_next_run()
        response = handle_task_block(run.pk, self.block("TASKQUERY", [{"queryId": self.first.pk}]))
        self.assertIn("first request", response)
        self.assertIn('"links"', response)
        result = self.proposal(run)
        proposal = run.link_proposals.get()
        self.assertEqual(result["results"][0]["status"], "proposed")
        self.assertFalse(TaskLink.objects.filter(source=self.first, target=self.second, link_type=self.link_type).exists())
        self.assertEqual(self.client.post(reverse("decide_project_link", args=[proposal.pk]), {"decision": "accept"}).status_code, 302)
        proposal.refresh_from_db()
        self.assertEqual(proposal.outcome, AgentLinkProposal.Outcome.ACCEPTED)
        self.assertTrue(TaskLink.objects.filter(source=self.first, target=self.second, link_type=self.link_type).exists())
        self.assertTrue(run.events.filter(kind="human link decision").exists())
        self.assertEqual(self.client.post(reverse("decide_project_link", args=[proposal.pk]), {"decision": "accept"}).status_code, 409)

    def test_stale_and_rejected_proposals_never_create_links(self):
        create_project_run("Check")
        run = claim_next_run()
        self.proposal(run)
        proposal = run.link_proposals.get()
        self.second.version += 1
        self.second.save(update_fields=["version"])
        url = reverse("decide_project_link", args=[proposal.pk])
        self.assertEqual(self.client.post(url, {"decision": "accept"}).status_code, 409)
        self.assertFalse(TaskLink.objects.filter(link_type=self.link_type).exists())
        self.assertEqual(self.client.post(url, {"decision": "reject"}).status_code, 302)
        self.assertFalse(TaskLink.objects.filter(link_type=self.link_type).exists())

    def test_changed_relationship_is_not_approved(self):
        create_project_run("Check")
        run = claim_next_run()
        self.proposal(run)
        proposal = run.link_proposals.get()
        self.link_type.outward_description = "Different relationship"
        self.link_type.save(update_fields=["outward_description"])
        response = self.client.post(reverse("decide_project_link", args=[proposal.pk]), {"decision": "accept"})
        self.assertEqual(response.status_code, 409)
        proposal.refresh_from_db()
        self.assertEqual(proposal.outcome, AgentLinkProposal.Outcome.PENDING)
        self.assertFalse(TaskLink.objects.filter(link_type=self.link_type).exists())

    def test_invalid_project_task_blocks_have_no_side_effects(self):
        create_project_run("Check")
        run = claim_next_run()
        for item in (
            {"destinationTaskId": self.second.pk, "linkType": self.link_type.outward_description},
            {"sourceTaskId": self.first.pk, "destinationTaskId": self.first.pk,
             "linkType": self.link_type.outward_description, "reason": "self"},
            {"sourceTaskId": self.first.pk, "destinationTaskId": self.second.pk,
             "linkType": "unknown", "reason": "unknown type"},
        ):
            with self.subTest(item=item):
                result = handle_task_block(run.pk, self.block("TASKLINK", [item]))
                self.assertIn('"error"', result)
        self.assertFalse(run.link_proposals.exists())
        self.assertFalse(TaskLink.objects.filter(link_type=self.link_type).exists())

    def test_project_questions_and_permissions_use_existing_actions(self):
        run = create_project_run("Question")
        run = claim_next_run()
        question_id = create_question(run.pk, "Which option?", TEXT_SCHEMA, "turn")
        response = self.client.post(reverse("answer_agent_question", args=[question_id]), {"answer-answer": "Option A"})
        self.assertRedirects(response, reverse("project_run_detail", args=[run.pk]))
        self.assertEqual(AgentQuestion.objects.get(pk=question_id).answer, {"answer": "Option A"})
        permission = PermissionRequest.objects.create(run=run, tool_call={}, options=[
            {"optionId": "allow", "kind": "allow_once", "name": "Allow"},
        ])
        response = self.client.post(reverse("resolve_permission", args=[permission.pk]), {"option": "allow"})
        self.assertRedirects(response, reverse("project_run_detail", args=[run.pk]))
        permission.refresh_from_db()
        self.assertTrue(permission.resolved)


class ProjectRunProtocolTests(TransactionTestCase):
    def test_query_proposal_question_and_completion_in_one_session(self):
        endpoint = AcpEndpoint.objects.create(name="ACP", port=12345)
        first = Task.objects.create(title="First", request="First request")
        second = Task.objects.create(title="Second", request="Second request")
        kind = LinkType.objects.create(name="Dependency test", outward_description="depends on test", inward_description="test dependency of")
        run = create_project_run("Check project links")
        run = claim_next_run()
        connection = MagicMock()
        connection.initialize = AsyncMock(return_value=SimpleNamespace(
            protocol_version=1, agent_capabilities=SimpleNamespace(session_capabilities=None),
        ))
        connection.new_session = AsyncMock(return_value=SimpleNamespace(session_id="project-session"))
        connection.close = AsyncMock()
        connection.cancel = AsyncMock()
        responses = [
            f'FLAMETHROWER_TASKQUERY\n[{{"queryId": {first.pk}}}]\nEND_FLAMETHROWER_TASKQUERY',
            f'FLAMETHROWER_TASKLINK\n[{{"sourceTaskId": {first.pk}, "destinationTaskId": {second.pk}, '
            f'"linkType": "{kind.outward_description}", "reason": "Dependency"}}]\nEND_FLAMETHROWER_TASKLINK',
            "FLAMETHROWER_QUESTION\nShould I include the details?\nEND_FLAMETHROWER_QUESTION",
            "FLAMETHROWER_PROJECT_ANSWER\nA link was proposed; please review it.\nEND_FLAMETHROWER_PROJECT_ANSWER",
        ]

        async def prompt(**kwargs):
            await save_event(run.pk, "message", responses.pop(0))
            return SimpleNamespace(stop_reason="end_turn", usage=None)

        async def wait(run_id, question_id):
            await sync_to_async(answer_question, thread_sensitive=True)(question_id, {"answer": "Yes"})
            return {"answer": "Yes"}

        connection.prompt = AsyncMock(side_effect=prompt)
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        with patch("tasks.agent.asyncio.open_connection", new=AsyncMock(return_value=(object(), writer))), \
             patch("tasks.agent.acp.connect_to_agent", return_value=connection), \
             patch("tasks.agent.wait_for_answer", new=wait):
            async_to_sync(run_agent)(run.pk, run.initial_prompt, endpoint.pk, endpoint.host, endpoint.port, run.stage)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.COMPLETED, run.error)
        self.assertEqual(connection.prompt.await_count, 4)
        self.assertTrue(run.events.filter(kind="human answer").exists())
        self.assertEqual(run.link_proposals.count(), 1)
        self.assertFalse(TaskLink.objects.filter(link_type=kind).exists())

    def test_saved_project_run_recovers_and_waits_for_human_direction(self):
        endpoint = AcpEndpoint.objects.create(name="ACP", port=12345)
        run = create_project_run("Inspect the project")
        run = claim_next_run()
        AgentRun.objects.filter(pk=run.pk).update(session_id="project-session")
        recover_interrupted_runs()
        run = claim_next_recovery()
        connection = MagicMock()
        connection.initialize = AsyncMock(return_value=SimpleNamespace(
            protocol_version=1, agent_capabilities=SimpleNamespace(
                session_capabilities=SimpleNamespace(status=object(), resume=object(), title=None),
            ),
        ))
        connection.session_status = AsyncMock(return_value=SimpleNamespace(status="saved"))
        connection.resume_session = AsyncMock()
        connection.close = AsyncMock()
        connection.cancel = AsyncMock()

        async def prompt(**kwargs):
            await save_event(run.pk, "message", "FLAMETHROWER_PROJECT_ANSWER\nReviewed.\nEND_FLAMETHROWER_PROJECT_ANSWER")
            return SimpleNamespace(stop_reason="end_turn", usage=None)

        async def wait(run_id, question_id, *, reader):
            connection.prompt.assert_not_awaited()
            await sync_to_async(answer_question, thread_sensitive=True)(question_id, {"answer": "Continue"})
            return {"answer": "Continue"}

        connection.prompt = AsyncMock(side_effect=prompt)
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        with patch("tasks.agent.asyncio.open_connection", new=AsyncMock(return_value=(object(), writer))), \
             patch("tasks.agent.acp.connect_to_agent", return_value=connection), \
             patch("tasks.agent.wait_for_answer", new=wait):
            async_to_sync(run_agent)(
                run.pk, run.initial_prompt, endpoint.pk, endpoint.host, endpoint.port, run.stage,
                recovery_session_id="project-session",
            )
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.COMPLETED, run.error)
        self.assertEqual(run.questions.get().source, "recovery")
        connection.resume_session.assert_awaited_once()
        connection.prompt.assert_awaited_once()
