"""Offline catalog tests: persistence, retrieval, migration and concurrency."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from server.services.execution.roster import AgentRoster
from server.services.execution.log_store import ExecutionAgentLogStore


class AgentRosterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "roster.json"
        self.roster = AgentRoster(self.path)

    def test_migrates_legacy_names_once_and_preserves_stable_ids(self):
        original = ["Email Alice", "Email-Alice", "张先生", "Email Alice"]
        self.path.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(self.roster.get_agents(), original[:3])
        first = self.roster.get_agent(name="Email Alice")
        second_instance = AgentRoster(self.path)
        self.assertEqual(second_instance.get_agent(agent_id=first.agent_id), first)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), original)
        self.assertNotEqual(first.agent_id, self.roster.get_agent(name="Email-Alice").agent_id)

    def test_initial_purpose_retained_across_requests_and_snippets_bounded(self):
        first = self.roster.record_request("Worker", "Original purpose " + "a" * 2000)
        second = self.roster.record_request("Worker", "Follow-up " + "b" * 2000)
        last = self.roster.record_result("Worker", "Result " + "c" * 3000)
        self.assertEqual(first.agent_id, last.agent_id)
        self.assertEqual(first.purpose, second.purpose)
        self.assertTrue(last.latest_request.startswith("Follow-up"))
        self.assertEqual(len(last.purpose), 500)
        self.assertEqual(len(last.latest_request), 500)
        self.assertEqual(len(last.last_result), 1000)

    def test_legacy_journal_backfill_finds_entities_and_runs_once(self):
        self.path.write_text('["Worker 017", "Worker 017"]', encoding="utf-8")
        legacy_log = self.path.parent / "worker-017.log"
        legacy_log.write_text(
            '<agent_request>Prepare lunch invitation for alice@acme.test</agent_request>\n'
            '<agent_response>Initial draft prepared.</agent_response>\n'
            '<agent_request>Update the lunch location.</agent_request>\n'
            '<agent_response>Updated draft: reference draft987</agent_response>\n',
            encoding="utf-8",
        )
        original = legacy_log.read_bytes()
        card = self.roster.search("alice@acme.test")[0]
        self.assertEqual(card.name, "Worker 017")
        self.assertIn("alice@acme.test", card.purpose)
        self.assertEqual(card.latest_request, "Update the lunch location.")
        self.assertIn("draft987", card.last_result)
        self.assertEqual(legacy_log.read_bytes(), original)
        with patch.object(ExecutionAgentLogStore, "iter_entries", side_effect=AssertionError("journal rescanned")):
            restarted = AgentRoster(self.path)
            self.assertEqual(restarted.search("draft987")[0].agent_id, card.agent_id)
            self.assertEqual(restarted.get_agent(name=card.name), card)

    def test_unreadable_legacy_import_can_retry_after_repair(self):
        self.path.write_text("invalid json", encoding="utf-8")
        self.assertEqual(self.roster.count(), 0)
        self.path.write_text('["Recovered agent"]', encoding="utf-8")
        self.assertEqual(AgentRoster(self.path).get_agents(), ["Recovered agent"])

    def test_searches_cold_agent_by_instruction_entity_not_name(self):
        target = self.roster.record_request("Worker 001", "Draft lunch invitation for alice@acme.test")
        for number in range(80):
            self.roster.record_request(f"Worker {number + 2}", f"Review quarterly budget region{number}")
        self.assertNotIn(target.agent_id, [card.agent_id for card in self.roster.recent()])
        self.assertEqual(self.roster.search("alice@acme.test")[0].agent_id, target.agent_id)
        self.assertEqual(self.roster.search("Worker 001")[0].agent_id, target.agent_id)
        self.assertEqual(self.roster.search("volcanology"), [])
        self.assertEqual(self.roster.search("please can you do it"), [])

    def test_unicode_search_and_result_index_updates(self):
        target = self.roster.record_request("提醒助手", "给张先生准备会议通知")
        self.assertEqual(self.roster.search("张先生")[0].agent_id, target.agent_id)
        self.roster.record_result(target.name, "Draft reference draft987")
        self.assertEqual(self.roster.search("draft987")[0].agent_id, target.agent_id)
        self.roster.record_result(target.name, "Sent reference message654")
        self.assertEqual(self.roster.search("draft987"), [])
        self.assertEqual(self.roster.search("message654")[0].agent_id, target.agent_id)

    def test_limits_and_literal_sql_fts_input(self):
        for number in range(30):
            self.roster.record_request(f"Worker {number}", "shared project")
        self.assertEqual(len(self.roster.search("shared", limit=2)), 2)
        self.assertEqual(len(self.roster.search("shared", limit=1000)), 20)
        self.assertEqual(self.roster.search("shared", limit=0), [])
        self.assertEqual(self.roster.recent(limit=-1), [])
        self.assertEqual(self.roster.search('" OR * NOT : ( ) --'), [])

    def test_cross_instance_concurrent_registration_and_updates(self):
        def write(number):
            roster = AgentRoster(self.path)
            roster.record_request("Shared", f"request {number}")
            roster.record_result(f"Worker {number}", f"result {number}")
            return roster.get_agent(name="Shared").agent_id

        with ThreadPoolExecutor(max_workers=6) as executor:
            ids = list(executor.map(write, range(24)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.roster.count(), 25)
        self.assertEqual(len(self.roster.search("Worker", limit=20)), 20)

    def test_clear_does_not_reimport_legacy_and_preserves_backup(self):
        self.path.write_text('["Old agent"]', encoding="utf-8")
        self.assertEqual(self.roster.count(), 1)
        self.roster.clear()
        restarted = AgentRoster(self.path)
        self.assertEqual(restarted.count(), 0)
        self.assertEqual(restarted.search("Old"), [])
        self.assertTrue(self.path.exists())

    def test_clear_suppresses_retry_of_previously_unreadable_backup(self):
        self.path.write_text("invalid json", encoding="utf-8")
        self.roster.clear()
        self.path.write_text('["Old agent"]', encoding="utf-8")
        self.assertEqual(AgentRoster(self.path).count(), 0)

    def test_lookup_requires_one_exact_selector_and_rejects_blank_names(self):
        with self.assertRaises(ValueError):
            self.roster.get_agent()
        with self.assertRaises(ValueError):
            self.roster.get_agent(name="a", agent_id="b")
        with self.assertRaises(ValueError):
            self.roster.add_agent("  ")
        self.assertIsNone(self.roster.get_agent(name="Missing"))

    def test_fallback_search_remains_bounded_and_handles_chinese(self):
        self.roster.record_request("Worker", "给张先生发邮件")
        self.roster._fts_enabled = False
        self.assertEqual(self.roster.search("张先生", limit=1)[0].name, "Worker")
        self.assertEqual(self.roster.search("unrelated"), [])


if __name__ == "__main__":
    unittest.main()
