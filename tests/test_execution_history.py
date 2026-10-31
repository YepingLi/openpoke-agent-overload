"""Offline regression tests for bounded memory and exact-name log identity."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from server.agents.execution_agent import agent as agent_module
from server.services.execution.log_store import ExecutionAgentLogStore


class ExecutionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ExecutionAgentLogStore(self.root)

    def test_similar_names_have_distinct_persistent_journals(self):
        names = ("Email to Alice", "email-to-alice", "Email_to_Alice")
        for index, name in enumerate(names):
            self.store.record_request(name, f"private task {index}")
        reloaded = ExecutionAgentLogStore(self.root)
        self.assertEqual(len({reloaded._log_path(name) for name in names}), 3)
        for index, name in enumerate(names):
            self.assertEqual(list(reloaded.iter_entries(name))[0][2], f"private task {index}")

    def test_legacy_journal_is_preserved_and_ambiguity_is_disclosed(self):
        old_path = self.root / "email-to-alice.log"
        old_content = '<agent_request timestamp="2025-01-01">Old task</agent_request>\n'
        old_path.write_text(old_content, encoding="utf-8")
        self.store.record_request("Email to Alice", "new task")
        rendered = self.store.load_bounded_transcript("Email to Alice")
        self.assertIn("Old task", rendered)
        self.assertIn("new task", rendered)
        self.assertIn("ownership may be ambiguous", rendered)
        self.assertEqual(old_path.read_text(encoding="utf-8"), old_content)
        # The new write does not contaminate the other formerly colliding name.
        self.assertNotIn("new task", self.store.load_transcript("email-to-alice"))

    def test_full_tool_arguments_and_result_are_durable(self):
        with patch.object(agent_module, "get_execution_agent_logs", return_value=self.store):
            agent = agent_module.ExecutionAgent("Alice")
        arguments = json.dumps({"body": "a" * 300, "draft_id": "draft_beyond_old_limit"})
        result = json.dumps({"body": "b" * 600, "thread_id": "thread_beyond_old_limit"})
        agent.record_tool_execution("gmail_create_draft", arguments, result)
        entries = list(self.store.iter_entries("Alice"))
        self.assertEqual(entries[0][2], f"Calling gmail_create_draft with: {arguments}")
        self.assertEqual(entries[1][2], f"gmail_create_draft: {result}")

    def test_new_journal_distinguishes_literal_escapes_from_newlines(self):
        original = 'body with newline\nnext line, literal \\n, quotes " and <tags>\r\n'
        arguments = json.dumps({"body": original})
        self.store.record_action("Alice", arguments)
        reloaded = ExecutionAgentLogStore(self.root)
        payload = list(reloaded.iter_entries("Alice"))[0][2]
        self.assertEqual(payload, arguments)
        self.assertEqual(json.loads(payload)["body"], original)

    def test_bounded_context_preserves_old_identifiers_and_newest_entries(self):
        self.store.record_tool_response(
            "Alice", "gmail_create_draft", json.dumps({"id": "draft_original", "message": {"threadId": "thread_original"}})
        )
        for index in range(200):
            self.store.record_request("Alice", f"request {index}: " + "x" * 80)
            self.store.record_agent_response("Alice", f"response {index}")
        rendered = self.store.load_bounded_transcript("Alice", max_chars=1500)
        self.assertLessEqual(len(rendered), 1500)
        self.assertIn("request 199", rendered)
        self.assertIn("response 199", rendered)
        self.assertNotIn("request 0:", rendered)
        self.assertIn("draft_id=draft_original", rendered)
        self.assertIn("thread_id=thread_original", rendered)
        self.assertIn("do not prove current state or approval", rendered)
        self.assertEqual(len(list(self.store.iter_entries("Alice"))), 401)

    def test_budget_counts_xml_expansion_and_omits_whole_oversized_entry(self):
        self.store.record_request("Alice", "small older entry")
        self.store.record_tool_response("Alice", "tool", "<&>" * 1000)
        self.store.record_agent_response("Alice", "latest result")
        rendered = self.store.load_bounded_transcript("Alice", max_chars=512)
        self.assertLessEqual(len(rendered), 512)
        self.assertIn("latest result", rendered)
        self.assertNotIn("<tool_response", rendered)
        self.assertIn("oversized entries may be omitted", rendered)

    def test_nested_composio_draft_identifier_survives_history_omission(self):
        result = {"data": {"id": "draft_wrapped", "message": {"id": "not_a_draft", "threadId": "thread_wrapped"}}}
        self.store.record_tool_response("Alice", "gmail_create_draft", json.dumps(result))
        self.store.record_agent_response("Alice", "x" * 10000)
        rendered = self.store.load_bounded_transcript("Alice", max_chars=1500)
        self.assertIn("draft_id=draft_wrapped", rendered)
        self.assertIn("thread_id=thread_wrapped", rendered)
        self.assertNotIn("draft_id=not_a_draft", rendered)

    def test_conversation_limit_retains_only_latest_request_group(self):
        self.store.record_request("Alice", "first request")
        self.store.record_agent_response("Alice", "first response")
        self.store.record_request("Alice", "second request")
        self.store.record_agent_response("Alice", "second response")
        rendered = self.store.load_bounded_transcript("Alice", conversation_limit=1)
        self.assertNotIn("first request", rendered)
        self.assertNotIn("first response", rendered)
        self.assertIn("second request", rendered)
        self.assertIn("second response", rendered)

    def test_agent_default_prompt_history_has_fixed_budget(self):
        for index in range(200):
            self.store.record_request("Alice", f"{index}:" + "x" * 400)
        with patch.object(agent_module, "get_execution_agent_logs", return_value=self.store):
            agent = agent_module.ExecutionAgent("Alice")
        prompt = agent.build_system_prompt_with_history()
        history = prompt.split("# Execution History\n\n", 1)[1]
        self.assertLessEqual(len(history), 12000)
        self.assertIn("199:", history)

    def test_no_history_and_invalid_budget(self):
        self.assertEqual(self.store.load_bounded_transcript("unknown"), "")
        with self.assertRaises(ValueError):
            self.store.load_bounded_transcript("Alice", max_chars=100)

    def test_clear_removes_legacy_and_identity_journals(self):
        (self.root / "legacy.log").write_text("old", encoding="utf-8")
        self.store.record_request("Alice", "new")
        self.store.clear_all()
        self.assertEqual(list(self.root.glob("**/*.log")), [])


if __name__ == "__main__":
    unittest.main()
