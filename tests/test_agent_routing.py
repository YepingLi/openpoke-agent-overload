"""Discovery and dispatch tests with real persistence and mocked execution."""

import asyncio
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from server.agents.interaction_agent import agent as prompt_builder
from server.agents.interaction_agent import tools
from server.agents.execution_agent import batch_manager
from server.agents.execution_agent import agent as execution_agent
from server.agents.execution_agent import runtime as execution_runtime
from server.agents.execution_agent.runtime import ExecutionResult
from server.services.execution.log_store import ExecutionAgentLogStore
from server.services.execution.roster import AgentRoster
from server.services.execution.routing import render_candidates, select_candidates


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.roster = AgentRoster(Path(self.directory.name) / "roster.json")

    def test_cold_owner_found_without_injecting_complete_roster(self):
        owner = self.roster.record_request("Worker 017", "Alice lunch invitation alice@acme.test")
        for number in range(100):
            self.roster.record_request(f"Budget worker {number}", f"Review region{number} budgets")
        with patch.object(prompt_builder, "get_agent_roster", return_value=self.roster):
            content = prompt_builder.prepare_message_with_history("Alice lunch", "Recent chat")[0]["content"]
        section = content[content.index("<agent_candidates"):content.index("</agent_candidates>") + len("</agent_candidates>")]
        root = ET.fromstring(section)
        self.assertEqual(root.attrib["total"], "101")
        self.assertEqual(root.attrib["complete"], "false")
        self.assertIn(owner.agent_id, [node.attrib["id"] for node in root.findall("agent")])
        self.assertLessEqual(len(root.findall("agent")), 8)
        self.assertNotIn('name="Budget worker 0"', section)

    def test_adversarial_metadata_preserves_reserved_recent_candidates(self):
        for number in range(6):
            name = f"Alice lunch {number} " + '&"<>' * 50
            self.roster.record_request(name, "Alice lunch " + '&"<>' * 200)
            self.roster.record_result(name, '&"<>' * 300)
        recent = [self.roster.record_request(f"Recent draft {number}", "Bob draft " + "<&" * 300) for number in range(2)]
        selected = select_candidates(self.roster, "Alice lunch")
        rendered = render_candidates(self.roster, "Alice lunch")
        ids = {node.attrib["id"] for node in ET.fromstring(rendered).findall("agent")}
        self.assertEqual(ids, {card.agent_id for card in selected})
        self.assertTrue({card.agent_id for card in recent}.issubset(ids))
        self.assertLessEqual(len(rendered), 6000)
        self.assertIn("…", rendered)

    def test_ambiguous_followup_keeps_both_drafts_as_candidates(self):
        first = self.roster.record_request("Alice lunch", "Prepare lunch draft")
        second = self.roster.record_request("Alice budget", "Prepare budget draft")
        candidates = select_candidates(self.roster, "send it")
        self.assertEqual({first.agent_id, second.agent_id}, {card.agent_id for card in candidates})
        # Discovery supplies alternatives; it never starts either operation.
        self.assertEqual(self.roster.count(), 2)

    def test_unknown_name_requires_explicit_creation_and_search_is_bounded(self):
        self.roster.record_request("Original Alice", "Alice lunch")
        with patch.object(tools, "get_agent_roster", return_value=self.roster):
            result = tools.send_message_to_agent("New Alice spelling", "Alice lunch")
            self.assertFalse(result.success)
            self.assertEqual(result.payload["status"], "selection_required")
            self.assertEqual(self.roster.count(), 1)
            self.assertFalse(tools.send_message_to_agent("Original Alice", "Distinct task", create_new=True).success)
            self.assertFalse(tools.search_agents("Alice", limit=99).success)
            self.assertFalse(tools.search_agents("", limit=5).success)
            found = tools.handle_tool_call("search_agents", {"query": "Alice"})
            self.assertTrue(found.success)
            self.assertEqual(found.payload["candidates"][0]["name"], "Original Alice")


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.roster = AgentRoster(Path(self.directory.name) / "roster.json")
        self.logs = ExecutionAgentLogStore(Path(self.directory.name) / "logs")

    async def test_stable_id_reuses_exact_owner_even_if_display_name_is_shortened(self):
        owner = self.roster.record_request("Original owner " + "x" * 200, "Alice draft")
        executed = asyncio.Event()
        calls = []

        async def execute(name, instructions):
            calls.append((name, instructions))
            executed.set()
            return ExecutionResult(name, True, "Completed")

        with patch.object(tools, "get_agent_roster", return_value=self.roster), patch.object(
            tools._EXECUTION_BATCH_MANAGER, "execute_agent", side_effect=execute
        ):
            result = tools.send_message_to_agent("shortened label", "Continue draft", agent_id=owner.agent_id)
            self.assertTrue(result.success)
            self.assertFalse(result.payload["new_agent_created"])
            await asyncio.wait_for(executed.wait(), timeout=1)
        self.assertEqual(calls, [(owner.name, "Continue draft")])
        self.assertEqual(self.roster.count(), 1)

    async def test_unknown_id_does_not_create_or_execute(self):
        with patch.object(tools, "get_agent_roster", return_value=self.roster), patch.object(
            tools._EXECUTION_BATCH_MANAGER, "execute_agent", new_callable=AsyncMock
        ) as execute:
            result = tools.send_message_to_agent("New", "Send", agent_id="missing")
            self.assertFalse(result.success)
            self.assertEqual(self.roster.count(), 0)
            execute.assert_not_called()

    async def test_explicit_distinct_task_can_be_created(self):
        executed = asyncio.Event()

        async def execute(name, instructions):
            executed.set()
            return ExecutionResult(name, True, "Done")

        with patch.object(tools, "get_agent_roster", return_value=self.roster), patch.object(
            tools._EXECUTION_BATCH_MANAGER, "execute_agent", side_effect=execute
        ):
            result = tools.send_message_to_agent("New work", "Distinct task", create_new=True)
            self.assertTrue(result.success)
            self.assertTrue(result.payload["new_agent_created"])
            await asyncio.wait_for(executed.wait(), timeout=1)
        self.assertEqual(self.roster.count(), 1)

    async def test_shared_execution_path_indexes_scheduled_requests_and_results(self):
        manager = batch_manager.ExecutionBatchManager()
        result = ExecutionResult("Scheduled report", True, "Draft ready draft_987")
        with patch.object(batch_manager, "get_agent_roster", return_value=self.roster), patch.object(
            batch_manager, "get_execution_agent_logs", return_value=self.logs
        ), patch.object(batch_manager, "ExecutionAgentRuntime") as runtime, patch.object(
            manager, "_dispatch_to_interaction_agent", new_callable=AsyncMock
        ) as dispatch:
            runtime.return_value.execute = AsyncMock(return_value=result)
            actual = await manager.execute_agent("Scheduled report", "Quarterly budget for Alice")
        self.assertEqual(actual, result)
        self.assertEqual(self.roster.search("draft_987")[0].name, result.agent_name)
        self.assertIn("Quarterly budget", self.logs.load_transcript(result.agent_name))
        dispatch.assert_awaited_once()

    async def test_mocked_email_followup_resumes_original_draft_through_real_runtime(self):
        owner = self.roster.record_request("Worker 017", "Alice lunch invitation")
        other = self.roster.record_request("Worker 018", "Alice budget proposal")
        self.logs.record_tool_response(owner.name, "gmail_create_draft", json.dumps({
            "body": "Old lunch draft " * 100,
            "draft_id": "draft_original_lunch",
        }))
        self.logs.record_tool_response(other.name, "gmail_create_draft", '{"draft_id":"draft_other_budget"}')
        for index in range(100):
            self.logs.record_action(owner.name, f"Older audit event {index}: " + "x" * 300)
        manager = batch_manager.ExecutionBatchManager()
        completed = asyncio.Event()
        sent_ids = []
        llm_calls = []

        def send_mock(draft_id):
            sent_ids.append(draft_id)
            return {"message_id": "sent_lunch_message"}

        async def llm_mock(system_prompt, messages, with_tools):
            self.assertIn("draft_original_lunch", system_prompt)
            self.assertNotIn("draft_other_budget", system_prompt)
            self.assertLessEqual(len(system_prompt), len(execution_agent.SYSTEM_PROMPT_TEMPLATE) + 12500)
            llm_calls.append(1)
            if len(llm_calls) == 1:
                message = {"tool_calls": [{
                    "id": "call_send_lunch",
                    "function": {"name": "gmail_execute_draft", "arguments": '{"draft_id":"draft_original_lunch"}'},
                }]}
            else:
                self.assertEqual(messages[-1]["role"], "tool")
                self.assertIn("sent_lunch_message", messages[-1]["content"])
                message = {"content": "Lunch invitation sent."}
            return {"choices": [{"message": message}]}

        async def dispatch_mock(payload):
            self.assertIn("Worker 017", payload)
            self.assertIn("Lunch invitation sent", payload)
            completed.set()

        settings = SimpleNamespace(openrouter_api_key="offline-test", execution_agent_model="offline-test")
        with patch.object(tools, "get_agent_roster", return_value=self.roster), patch.object(
            tools, "_EXECUTION_BATCH_MANAGER", manager
        ), patch.object(batch_manager, "get_agent_roster", return_value=self.roster), patch.object(
            batch_manager, "get_execution_agent_logs", return_value=self.logs
        ), patch.object(execution_agent, "get_execution_agent_logs", return_value=self.logs), patch.object(
            execution_runtime, "get_settings", return_value=settings
        ), patch.object(execution_runtime, "get_tool_registry", return_value={"gmail_execute_draft": send_mock}), patch.object(
            execution_runtime, "get_tool_schemas", return_value=[]
        ), patch.object(execution_runtime.ExecutionAgentRuntime, "_make_llm_call", side_effect=llm_mock), patch.object(
            manager, "_dispatch_to_interaction_agent", side_effect=dispatch_mock
        ):
            candidate = self.roster.search("Alice lunch")[0]
            self.assertEqual(candidate.agent_id, owner.agent_id)
            result = tools.send_message_to_agent(candidate.name, "User confirmed the lunch draft; send it.", agent_id=candidate.agent_id)
            self.assertTrue(result.success)
            await asyncio.wait_for(completed.wait(), timeout=2)
        self.assertEqual(sent_ids, ["draft_original_lunch"])
        self.assertEqual(self.roster.count(), 2)
        self.assertEqual(len(llm_calls), 2)


if __name__ == "__main__":
    unittest.main()
