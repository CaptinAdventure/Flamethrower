import json
from unittest.mock import patch

from django.test import TestCase

from ..deferred import complete_run, parse_deferred_tasks
from ..models import AgentRun, DeferredTask, RunEvent, Task, TaskLink
from ..recommendations import final_agent_report


def block(tasks):
    return "FLAMETHROWER_DEFERRED_TASKS\n" + json.dumps({"tasks": tasks}) + "\nEND_FLAMETHROWER_DEFERRED_TASKS"


class DeferredTaskTests(TestCase):
    def setUp(self):
        self.source = Task.objects.create(title="Source", request="Approved request")
        self.run = AgentRun.objects.create(
            task=self.source, stage=Task.Stage.REVIEW, task_version=0,
            request_snapshot="Approved request", plan_snapshot="Plan", status=AgentRun.Status.RUNNING,
        )

    def report(self, text):
        RunEvent.objects.create(run=self.run, kind="message", content=text)

    def test_completed_run_creates_linked_draft_with_provenance(self):
        text = block([{"title": " Later task ", "request": " Fix this separately. "}])
        self.report("Review finished.\n\n" + text)
        complete_run(self.run.pk, "end_turn")
        generated = DeferredTask.objects.select_related("task").get()
        child = generated.task
        self.assertEqual((child.title, child.request), ("Later task", "Fix this separately."))
        self.assertEqual((child.state, child.stage), (Task.State.DRAFT, Task.Stage.DRAFT))
        self.assertEqual(child.status, Task.Status.NON_ACCEPTED)
        self.assertEqual(child.relates_indexing, Task.IndexingStatus.PENDING)
        self.assertEqual((generated.source_run_id, generated.position), (self.run.pk, 0))
        link = TaskLink.objects.get()
        self.assertEqual((link.source_id, link.target_id, link.link_type.name), (self.source.pk, child.pk, "Relates"))
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AgentRun.Status.COMPLETED)

    def test_gate_block_remains_compatible_with_deferred_report(self):
        gate = {
            "result": "passed", "recommendation": "advance", "summary": "Looks good",
            "evidence": "Tests passed", "untested": "None", "reason": "Checks completed",
        }
        text = block([{"title": "Later", "request": "Separate work"}]) + (
            "\n\nFLAMETHROWER_GATE_RECOMMENDATION\n" + json.dumps(gate) + "\nEND_FLAMETHROWER_GATE_RECOMMENDATION"
        )
        self.report(text[:40])
        self.report(text[40:])
        self.assertEqual(final_agent_report(list(self.run.events.all()))["recommendation"], gate)
        complete_run(self.run.pk, "end_turn")
        self.assertEqual(DeferredTask.objects.count(), 1)

    def test_malformed_report_creates_nothing_and_pauses_automation(self):
        self.report(block([{"title": "Valid", "request": "Valid"}, {"title": "", "request": "Invalid"}]))
        complete_run(self.run.pk, "end_turn")
        self.run.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(self.run.status, AgentRun.Status.COMPLETED)
        self.assertTrue(self.run.deferred_tasks_warning)
        self.assertIn("Deferred-task report is invalid", self.source.automatic_pause_reason)
        self.assertFalse(DeferredTask.objects.exists())
        self.assertFalse(TaskLink.objects.exists())

    def test_parser_rejects_misplaced_duplicate_and_invalid_blocks(self):
        valid = block([{"title": "t", "request": "r"}])
        for text in (valid + "\nextra", valid + "\n" + valid, "FLAMETHROWER_DEFERRED_TASKS\n{bad}\nEND_FLAMETHROWER_DEFERRED_TASKS", block([{"title": "t", "request": "r"}] * 21)):
            with self.subTest(text=text):
                tasks, warning = parse_deferred_tasks(text)
                self.assertEqual(tasks, [])
                self.assertTrue(warning)
        self.assertEqual(parse_deferred_tasks("No deferred work."), ([], ""))

    def test_prose_mentions_are_not_deferred_task_markers(self):
        self.assertEqual(parse_deferred_tasks("The `FLAMETHROWER_DEFERRED_TASKS` marker is optional."), ([], ""))
        text = "Mention `END_FLAMETHROWER_DEFERRED_TASKS`.\n" + block([{"title": "Later", "request": "Separate work"}])
        self.assertEqual(parse_deferred_tasks(text), ([("Later", "Separate work")], ""))

    def test_cancelled_run_and_reprocessing_do_not_create_tasks(self):
        self.report(block([{"title": "Later", "request": "Separate work"}]))
        AgentRun.objects.filter(pk=self.run.pk).update(cancel_requested=True)
        with self.assertRaises(RuntimeError):
            complete_run(self.run.pk, "end_turn")
        self.assertFalse(DeferredTask.objects.exists())
        AgentRun.objects.filter(pk=self.run.pk).update(cancel_requested=False)
        complete_run(self.run.pk, "end_turn")
        with self.assertRaises(RuntimeError):
            complete_run(self.run.pk, "end_turn")
        self.assertEqual(DeferredTask.objects.count(), 1)

    def test_creation_error_rolls_back_completion_and_all_tasks(self):
        self.report(block([{"title": "Later", "request": "Separate work"}]))
        with patch("tasks.deferred.TaskLink.objects.create", side_effect=RuntimeError("storage failure")):
            with self.assertRaisesMessage(RuntimeError, "storage failure"):
                complete_run(self.run.pk, "end_turn")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AgentRun.Status.RUNNING)
        self.assertEqual(Task.objects.count(), 1)
        self.assertFalse(DeferredTask.objects.exists())
