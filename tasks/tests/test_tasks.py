import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from flamethrower.launcher import prepare_storage
from ..models import AgentRun, GateDecision, LinkType, RunEvent, Task, TaskLink, TaskLinkEvent
from ..views.board import lane_order_token


class TaskSearchTests(TestCase):

    def test_search_filters_title_request_and_plan_across_stages(self):
        title_match = Task.objects.create(title="EXPORT data", request="request")
        request_match = Task.objects.create(title="Request match", request="Add export support", stage=Task.Stage.REVIEW)
        plan_match = Task.objects.create(title="Plan match", request="request", plan="Implement export", stage=Task.Stage.FINISHED, finished=True)
        abandoned_match = Task.objects.create(title="Abandoned export", request="request", status=Task.Status.ABANDONED)
        Task.objects.create(title="Unrelated task", request="request", plan="plan")

        response = self.client.get(reverse("task_list"), {"q": "  export  "})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["search_query"], "export")
        lanes = {lane["name"]: [item["task"].pk for item in lane["tasks"]] for lane in response.context["lanes"]}
        self.assertEqual(lanes["Draft"], [title_match.pk])
        self.assertEqual(lanes["Review"], [request_match.pk])
        self.assertNotIn("Finished", lanes)
        self.assertEqual([item["task"].pk for item in response.context["finished_tasks"]], [plan_match.pk])
        self.assertEqual([item["task"].pk for item in response.context["abandoned_tasks"]], [abandoned_match.pk])
        self.assertContains(response, 'id="finished-tasks"')
        self.assertContains(response, 'id="abandoned-tasks"')
        self.assertContains(response, 'class="summary-task-row"')
        self.assertContains(response, "#%s Plan match" % plan_match.pk)
        self.assertContains(response, "#%s Abandoned export" % abandoned_match.pk)

    def test_task_list_shows_empty_finished_summary(self):
        response = self.client.get(reverse("task_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No finished tasks")

    def test_search_shows_empty_finished_summary_message(self):
        Task.objects.create(title="Unfinished export", request="request")

        response = self.client.get(reverse("task_list"), {"q": "export"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No matching finished tasks")

    def test_task_list_shows_empty_abandoned_summary(self):
        response = self.client.get(reverse("task_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No abandoned tasks")

    def test_search_shows_empty_abandoned_summary_message(self):
        Task.objects.create(title="Unfinished export", request="request")

        response = self.client.get(reverse("task_list"), {"q": "export"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No matching abandoned tasks")

    def test_task_list_keeps_board_and_quick_task_without_acp_card(self):
        response = self.client.get(reverse("task_list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ACP endpoints")
        self.assertContains(response, 'id="task-board"')
        self.assertContains(response, 'id="quick-task-form"')
        section_start = '<section class="card quick-task" aria-labelledby="quick-task-heading">'
        self.assertContains(response, section_start, count=1)
        section = response.content.decode().split(section_start, 1)[1].split("</section>", 1)[0]
        self.assertIn('<h2 id="quick-task-heading">Quick task</h2>', section)
        self.assertIn(f'<form method="post" action="{reverse("quick_task")}" id="quick-task-form">', section)
        self.assertIn('<label class="sr-only" for="id_request">Quick task</label>', section)
        self.assertIn('id="quick-task-status"', section)

    def test_header_settings_contains_navigation_and_theme_choices(self):
        response = self.client.get(reverse("task_list"))

        self.assertContains(response, '<details class="header-settings">', count=1)
        self.assertContains(response, f'href="{reverse("admin:index")}"', count=1)
        self.assertContains(response, f'href="{reverse("acp_list")}"', count=1)
        disclosure = response.content.decode().split('<details class="header-settings">', 1)[1].split("</details>", 1)[0]
        self.assertIn(f'href="{reverse("admin:index")}"', disclosure)
        self.assertIn(f'href="{reverse("acp_list")}"', disclosure)
        for choice in ("light", "dark", "system"):
            self.assertIn(f'name="theme" value="{choice}"', disclosure)


class TaskLinkTests(TestCase):
    def setUp(self):
        self.first = Task.objects.create(title="First task", request="request")
        self.second = Task.objects.create(title="Second task", request="request")
        self.third = Task.objects.create(title="Third task", request="request")

    def add_link(self, task, other, link_type, direction="out"):
        return self.client.post(reverse("add_task_link", args=[task.pk]), {
            "relation": f"{link_type.pk}:{direction}",
            "target": other.pk,
        })

    def test_default_link_types_are_available(self):
        self.assertEqual(
            set(LinkType.objects.values_list("name", "outward_description", "inward_description")),
            {
                ("Relates", "relates to", "relates to"),
                ("Duplicate", "duplicates", "is duplicated by"),
                ("Blocks", "blocks", "is blocked by"),
                ("Cloners", "clones", "is cloned by"),
            },
        )

    def test_directional_link_can_be_added(self):
        blocks = LinkType.objects.get(name="Blocks")
        response = self.add_link(self.first, self.second, blocks, "in")
        self.assertEqual(response.status_code, 302)
        link = TaskLink.objects.get()
        self.assertEqual((link.source_id, link.target_id), (self.second.pk, self.first.pk))

    def test_duplicate_and_self_links_are_rejected(self):
        relates = LinkType.objects.get(name="Relates")
        self.assertEqual(self.add_link(self.first, self.second, relates).status_code, 302)
        response = self.add_link(self.second, self.first, relates)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TaskLink.objects.count(), 1)
        response = self.add_link(self.first, self.first, relates)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TaskLink.objects.count(), 1)

    def test_reverse_direction_cannot_duplicate_a_directional_link(self):
        blocks = LinkType.objects.get(name="Blocks")
        self.assertEqual(self.add_link(self.first, self.second, blocks).status_code, 302)
        response = self.add_link(self.second, self.first, blocks, "in")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TaskLink.objects.count(), 1)

    def test_link_can_only_be_removed_from_either_endpoint(self):
        link = TaskLink.objects.create(
            link_type=LinkType.objects.get(name="Duplicate"), source=self.first, target=self.second,
        )
        self.assertEqual(self.client.post(reverse("remove_task_link", args=[self.third.pk, link.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("remove_task_link", args=[self.first.pk, link.pk])).status_code, 405)
        self.assertTrue(TaskLink.objects.filter(pk=link.pk).exists())
        self.assertEqual(self.client.post(reverse("remove_task_link", args=[self.second.pk, link.pk])).status_code, 302)
        self.assertFalse(TaskLink.objects.filter(pk=link.pk).exists())


    def test_unlink_can_return_to_the_detail_page(self):
        link = TaskLink.objects.create(
            link_type=LinkType.objects.get(name="Relates"), source=self.first, target=self.second,
        )
        url = reverse("remove_task_link", args=[self.first.pk, link.pk])
        response = self.client.post(url, {"return_to": "detail"})
        self.assertRedirects(response, reverse("task_detail", args=[self.first.pk]))
        self.assertFalse(TaskLink.objects.filter(pk=link.pk).exists())
        self.assertEqual(TaskLinkEvent.objects.filter(action=TaskLinkEvent.Action.UNLINKED).count(), 1)

    def test_link_and_unlink_history_is_reciprocal_and_preserves_titles(self):
        blocks = LinkType.objects.get(name="Blocks")
        self.add_link(self.first, self.second, blocks)
        link = TaskLink.objects.get()
        event = TaskLinkEvent.objects.get()
        self.assertEqual(event.action, TaskLinkEvent.Action.LINKED)
        Task.objects.filter(pk=self.second.pk).update(title="Renamed task")
        self.assertEqual(
            self.client.post(reverse("remove_task_link", args=[self.second.pk, link.pk])).status_code, 302,
        )
        self.assertEqual(list(TaskLinkEvent.objects.values_list("action", flat=True)), ["unlinked", "linked"])
        for task, description in ((self.first, "blocks"), (self.second, "is blocked by")):
            page = self.client.get(reverse("task_detail", args=[task.pk]))
            history = page.context["link_history"]
            self.assertEqual([item["description"] for item in history], [description, description])
            self.assertEqual(len(page.context["linked_tasks"]), 0)
        first_history = self.client.get(reverse("task_detail", args=[self.first.pk])).context["link_history"]
        self.assertEqual(first_history[1]["title"], "Second task")
        self.assertEqual(first_history[0]["title"], "Renamed task")

    def test_create_linked_task_is_atomic_and_respects_relationship_direction_and_approvals(self):
        blocks = LinkType.objects.get(name="Blocks")
        response = self.client.post(reverse("create_linked_task", args=[self.first.pk]), {
            "relation": f"{blocks.pk}:in", "title": "New linked task", "request": "Requested change",
        })
        self.assertRedirects(response, reverse("task_links", args=[self.first.pk]))
        other = Task.objects.get(title="New linked task")
        self.assertEqual((other.stage, other.state, other.status), (Task.Stage.DRAFT, Task.State.DRAFT, Task.Status.READY))
        self.assertEqual(other.request, "Requested change")
        link = TaskLink.objects.get()
        self.assertEqual((link.source_id, link.target_id), (other.pk, self.first.pk))
        self.assertEqual(TaskLinkEvent.objects.count(), 1)

    def test_create_linked_task_validation_does_not_create_work(self):
        response = self.client.post(reverse("create_linked_task", args=[self.first.pk]), {
            "relation": "invalid", "title": "Preserved title", "request": "",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Task.objects.count(), 3)
        self.assertFalse(TaskLink.objects.exists())
        self.assertFalse(TaskLinkEvent.objects.exists())

    def test_link_history_failure_rolls_back_link_creation_deletion_and_new_task(self):
        relates = LinkType.objects.get(name="Relates")
        with patch("tasks.models.relations.TaskLinkEvent.objects.create", side_effect=RuntimeError("history unavailable")):
            with self.assertRaisesMessage(RuntimeError, "history unavailable"):
                self.add_link(self.first, self.second, relates)
            self.assertFalse(TaskLink.objects.exists())
            with self.assertRaisesMessage(RuntimeError, "history unavailable"):
                self.client.post(reverse("create_linked_task", args=[self.first.pk]), {
                    "relation": f"{relates.pk}:out", "title": "Must roll back", "request": "Request",
                })
            self.assertEqual(Task.objects.count(), 3)
        self.add_link(self.first, self.second, relates)
        link = TaskLink.objects.get()
        with patch("tasks.models.relations.TaskLinkEvent.objects.create", side_effect=RuntimeError("history unavailable")):
            with self.assertRaisesMessage(RuntimeError, "history unavailable"):
                self.client.post(reverse("remove_task_link", args=[self.first.pk, link.pk]))
        self.assertTrue(TaskLink.objects.filter(pk=link.pk).exists())
        self.assertEqual(TaskLinkEvent.objects.count(), 1)

    def test_link_mutations_require_csrf(self):
        relates = LinkType.objects.get(name="Relates")
        self.add_link(self.first, self.second, relates)
        link = TaskLink.objects.get()
        client = Client(enforce_csrf_checks=True)
        for url in (
            reverse("add_task_link", args=[self.first.pk]),
            reverse("create_linked_task", args=[self.first.pk]),
            reverse("remove_task_link", args=[self.first.pk, link.pk]),
        ):
            self.assertEqual(client.post(url, {}).status_code, 403)
        self.assertEqual(TaskLinkEvent.objects.count(), 1)

    def test_link_management_rejects_missing_tasks_and_posts(self):
        manager_url = reverse("task_links", args=[self.first.pk])
        self.assertEqual(self.client.get(reverse("task_links", args=[self.third.pk + 1])).status_code, 404)
        self.assertEqual(self.client.post(manager_url).status_code, 405)

    def test_link_popovers_allow_same_origin_frames_without_relaxing_other_pages(self):
        detail = self.client.get(reverse("task_detail", args=[self.first.pk]))
        self.assertEqual(detail["X-Frame-Options"], "DENY")
        manager = self.client.get(reverse("task_links", args=[self.first.pk]))
        self.assertEqual(manager["X-Frame-Options"], "SAMEORIGIN")
        for name, data in (
            ("add_task_link", {"relation": "invalid", "target": self.second.pk}),
            ("create_linked_task", {"relation": "invalid", "title": "", "request": ""}),
        ):
            response = self.client.post(reverse(name, args=[self.first.pk]), data)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

class AdminSiteTests(TestCase):
    def test_admin_requires_authentication(self):
        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response.url)

    def test_superuser_can_view_registered_models_read_only(self):
        user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("admin:tasks_task_changelist"))

        self.assertEqual(response.status_code, 200)

class SwimlaneTests(TestCase):
    def test_abandoned_tasks_are_out_of_lanes_and_below_finished_tasks(self):
        abandoned = Task.objects.create(
            title="Abandoned task", request="request", status=Task.Status.ABANDONED,
            stage=Task.Stage.DEVELOPER, priority=1,
        )
        abandoned_finished = Task.objects.create(
            title="Abandoned finished-stage task", request="request", status=Task.Status.ABANDONED,
            stage=Task.Stage.FINISHED, priority=1,
        )
        finished = Task.objects.create(
            title="Finished task", request="request", stage=Task.Stage.FINISHED, finished=True,
        )
        response = self.client.get(reverse("task_list"))

        lanes = {lane["name"]: [item["task"].pk for item in lane["tasks"]] for lane in response.context["lanes"]}
        self.assertTrue(all(abandoned.pk not in task_ids for task_ids in lanes.values()))
        self.assertTrue(all(abandoned_finished.pk not in task_ids for task_ids in lanes.values()))
        self.assertEqual(
            {item["task"].pk for item in response.context["abandoned_tasks"]},
            {abandoned.pk, abandoned_finished.pk},
        )
        self.assertEqual([item["task"].pk for item in response.context["finished_tasks"]], [finished.pk])
        page = response.content.decode()
        self.assertLess(page.index('id="finished-tasks"'), page.index('id="abandoned-tasks"'))

    def test_abandoned_tasks_are_excluded_from_reorder_lane_tokens(self):
        first = Task.objects.create(title="First", request="request", priority=1)
        abandoned = Task.objects.create(
            title="Abandoned", request="request", status=Task.Status.ABANDONED, priority=2,
        )
        second = Task.objects.create(title="Second", request="request", priority=3)
        response = self.client.get(reverse("task_list"))
        lane = next(lane for lane in response.context["lanes"] if lane["key"] == Task.Stage.DRAFT)

        self.assertEqual([item["task"].pk for item in lane["tasks"]], [first.pk, second.pk])
        self.assertEqual(lane["order_token"], lane_order_token([first, second]))
        reorder_response = self.client.post(reverse("reorder_task", args=[second.pk]), {
            "order_token": lane["order_token"], "direction": "up",
        })
        self.assertEqual(reorder_response.status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((second.priority, first.priority), (1, 2))
        self.assertTrue(Task.objects.filter(pk=abandoned.pk).exists())

    def test_abandoned_task_cannot_be_reordered(self):
        abandoned = Task.objects.create(
            title="Abandoned", request="request", status=Task.Status.ABANDONED, priority=1,
        )
        task = Task.objects.create(title="Visible", request="request", priority=2)
        response = self.client.post(reverse("reorder_task", args=[abandoned.pk]), {
            "order_token": "invalid", "direction": "down",
        })

        self.assertEqual(response.status_code, 400)
        abandoned.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((abandoned.priority, task.priority), (1, 2))

    def test_tasks_stay_in_their_stage_with_attention_for_input(self):
        def task(title, stage, finished=False, priority=0):
            return Task.objects.create(title=title, request="request", plan="plan", stage=stage, finished=finished, priority=priority)

        draft = task("draft", "draft")
        architect = task("architect", "architect")
        running = task("running", "developer")
        waiting = task("waiting", "developer")
        completed = task("completed", "developer")
        testing = task("testing", "testing")
        review = task("review", "review")
        finished_lower_priority = task("finished lower priority", "finished", finished=True, priority=2)
        finished_higher_priority = task("finished higher priority", "finished", finished=True, priority=1)
        for item, status in ((running, "running"), (waiting, "waiting"), (completed, "completed")):
            AgentRun.objects.create(task=item, task_version=item.version, request_snapshot="r", plan_snapshot="p", status=status)
        response = self.client.get(reverse("task_list"))
        lanes = {lane["name"]: [item["task"].pk for item in lane["tasks"]] for lane in response.context["lanes"]}
        self.assertEqual(lanes["Draft"], [draft.pk])
        self.assertEqual(lanes["Architect"], [architect.pk])
        self.assertCountEqual(lanes["Developer"], [running.pk, waiting.pk, completed.pk])
        self.assertEqual(lanes["Testing"], [testing.pk])
        self.assertEqual(lanes["Review"], [review.pk])
        self.assertNotIn("Finished", lanes)
        self.assertEqual(
            [item["task"].pk for item in response.context["finished_tasks"]],
            [finished_higher_priority.pk, finished_lower_priority.pk],
        )
        finished_section = response.content.decode().partition('id="finished-tasks"')[2]
        self.assertIn('class="summary-task-row"', finished_section)
        self.assertNotIn('class="priority-actions"', finished_section)

class GateRecommendationTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(
            title="Change", request="request", plan="plan", state=Task.State.PLAN_APPROVED,
            stage=Task.Stage.TESTING, version=2,
        )

    def run_with_report(self, report, stage=Task.Stage.TESTING):
        run = AgentRun.objects.create(
            task=self.task, task_version=2, request_snapshot="r", plan_snapshot="p", status="completed", stage=stage,
        )
        RunEvent.objects.create(run=run, kind="tool", content="{}")
        for chunk in (report[:30], report[30:]):
            RunEvent.objects.create(run=run, kind="message", content=chunk)
        return run

    def recommendation(self, result="passed", action="advance"):
        data = {
            "result": result, "recommendation": action, "summary": "Checks completed",
            "evidence": "test command: 34 passed", "untested": "Browser rendering", "reason": "Checks match the plan",
        }
        return "Final report\nFLAMETHROWER_GATE_RECOMMENDATION\n" + json.dumps(data) + "\nEND_FLAMETHROWER_GATE_RECOMMENDATION"

    def acceptance_data(self):
        page = self.client.get(reverse("task_detail", args=[self.task.pk]))
        return page, page.context["recommendation_form"].initial

    def test_explicit_passing_recommendation_advances_automatically_and_is_recorded(self):
        run = self.run_with_report(self.recommendation())
        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.REVIEW)
        decision = self.task.gate_decisions.get()
        self.assertEqual(decision.source_run, run)
        self.assertEqual(decision.result, GateDecision.Result.PASSED)
        self.assertTrue(decision.advanced)
        self.assertTrue(decision.automatic)
        self.assertIn("Browser rendering", decision.evidence)

    def test_passing_review_recommendation_finishes_automatically(self):
        self.task.stage = Task.Stage.REVIEW
        self.task.save()
        run = self.run_with_report(self.recommendation(), stage=Task.Stage.REVIEW)
        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)
        self.task.refresh_from_db()
        self.assertTrue(self.task.finished)
        decision = self.task.gate_decisions.get()
        self.assertEqual(decision.source_run, run)
        self.assertTrue(decision.automatic)

    def test_old_report_is_visible_but_no_pass_is_inferred(self):
        run = self.run_with_report("34 tests passed. Browser rendering was not tested.")
        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)
        page, values = self.acceptance_data()
        self.assertEqual(page.context["gate_form"].initial.get("result"), None)
        self.assertIn("34 tests passed", page.context["gate_form"].initial["evidence"])
        self.assertEqual(self.client.post(reverse("accept_recommendation", args=[self.task.pk]), values).status_code, 409)


    def test_failed_rework_recommendation_pauses_for_human_decision(self):
        run = self.run_with_report(self.recommendation(result="failed", action="rework"))
        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        self.assertEqual(self.task.work_cycle, 0)
        self.assertIn("manual decision", self.task.automatic_pause_reason)
        decision = self.task.gate_decisions.get()
        self.assertEqual(decision.source_run, run)
        self.assertFalse(decision.advanced)
        self.assertTrue(decision.automatic)

    def test_failed_advance_recommendation_is_recorded_but_requires_human_action(self):
        run = self.run_with_report(self.recommendation(result="failed", action="hold"))
        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        self.assertIn("manual decision", self.task.automatic_pause_reason)
        decision = self.task.gate_decisions.get()
        self.assertEqual(decision.source_run, run)
        self.assertEqual(decision.result, GateDecision.Result.FAILED)
        self.assertTrue(decision.automatic)
        self.assertFalse(decision.advanced)

    def test_malformed_recommendation_does_not_invent_a_result(self):
        run = self.run_with_report(
            'FLAMETHROWER_GATE_RECOMMENDATION\n{"result":"passed"}\nEND_FLAMETHROWER_GATE_RECOMMENDATION',
        )
        from ..automation import continue_automatic_run

        continue_automatic_run(run.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.TESTING)
        self.assertIn("missing or invalid", self.task.automatic_pause_reason)
        self.assertFalse(self.task.gate_decisions.exists())

    def test_outdated_run_or_changed_report_cannot_be_accepted(self):
        old = self.run_with_report(self.recommendation())
        _, values = self.acceptance_data()
        RunEvent.objects.create(run=old, kind="message", content="changed")
        self.assertEqual(self.client.post(reverse("accept_recommendation", args=[self.task.pk]), values).status_code, 409)
        self.run_with_report(self.recommendation())
        self.assertEqual(self.client.post(reverse("accept_recommendation", args=[self.task.pk]), values).status_code, 409)
        self.assertFalse(self.task.gate_decisions.exists())

class StorageTests(TestCase):
    def test_creates_ignore_without_changing_existing_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            work = project / ".ai-work"
            work.mkdir()
            record = work / "existing.md"
            record.write_text("existing", encoding="utf-8")
            prepare_storage(project)
            prepare_storage(project)
            self.assertEqual((project / ".flamethrower" / ".gitignore").read_text(), "*\n")
            self.assertEqual(record.read_text(), "existing")

    def test_refuses_to_overwrite_an_existing_ignore_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            storage = project / ".flamethrower"
            storage.mkdir()
            ignore = storage / ".gitignore"
            ignore.write_text("keep this\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare_storage(project)
            self.assertEqual(ignore.read_text(), "keep this\n")

    def test_refuses_symlinked_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            (project / ".flamethrower").symlink_to(Path(temporary), target_is_directory=True)
            with self.assertRaises(ValueError):
                prepare_storage(project)
