"""Execution Agent implementation."""

from pathlib import Path
from typing import List, Optional, Dict, Any

from ...services.execution import get_execution_agent_logs
from ...logging_config import logger


# Load system prompt template from file
_prompt_path = Path(__file__).parent / "system_prompt.md"
if _prompt_path.exists():
    SYSTEM_PROMPT_TEMPLATE = _prompt_path.read_text(encoding="utf-8").strip()
else:
    # Placeholder template - you'll replace this with actual instructions
    SYSTEM_PROMPT_TEMPLATE = """You are an execution agent responsible for completing specific tasks using available tools.

Agent Name: {agent_name}
Purpose: {agent_purpose}

Instructions:
[TO BE FILLED IN BY USER]

You have access to Gmail tools to help complete your tasks. When given instructions:
1. Analyze what needs to be done
2. Use the appropriate tools to complete the task
3. Provide clear status updates on your actions

Be thorough, accurate, and efficient in your execution."""


class ExecutionAgent:
    """Manages state and history for an execution agent."""

    # Initialize execution agent with name, conversation limits, and log store access
    def __init__(
        self,
        name: str,
        conversation_limit: Optional[int] = None,
        history_char_limit: int = 12000,
    ):
        """
        Initialize an execution agent.

        Args:
            name: Human-readable agent name (e.g., 'conversation with keith')
            conversation_limit: Optional limit on recent requests within the character budget.
            history_char_limit: Maximum characters of rendered execution history.
        """
        self.name = name
        self.conversation_limit = conversation_limit
        self.history_char_limit = history_char_limit
        self._log_store = get_execution_agent_logs()

    # Generate system prompt template with agent name and purpose derived from name
    def build_system_prompt(self) -> str:
        """Build the system prompt for this agent."""
        agent_purpose = f"Handle tasks related to: {self.name}"

        return SYSTEM_PROMPT_TEMPLATE.format(
            agent_name=self.name,
            agent_purpose=agent_purpose
        )

    # Combine base system prompt with conversation history, applying conversation limits
    def build_system_prompt_with_history(self) -> str:
        """
        Build system prompt including agent history.

        Returns:
            System prompt with embedded history transcript
        """
        base_prompt = self.build_system_prompt()

        transcript = self._log_store.load_bounded_transcript(
            self.name,
            max_chars=self.history_char_limit,
            conversation_limit=self.conversation_limit,
        )

        if transcript:
            return f"{base_prompt}\n\n# Execution History\n\n{transcript}"

        return base_prompt

    # Format current instruction as user message for LLM consumption
    def build_messages_for_llm(self, current_instruction: str) -> List[Dict[str, str]]:
        """
        Build message array for LLM call.

        Args:
            current_instruction: Current instruction from interaction agent

        Returns:
            List of messages in OpenRouter format
        """
        return [
            {"role": "user", "content": current_instruction}
        ]

    # Log the agent's final response to the execution log store
    def record_response(self, response: str) -> None:
        """Record agent's response to the log."""
        self._log_store.record_agent_response(self.name, response)

    # Keep full audit records; prompt construction applies the context budget.
    def record_tool_execution(self, tool_name: str, arguments: str, result: str) -> None:
        """Record tool execution details."""
        self._log_store.record_action(self.name, f"Calling {tool_name} with: {arguments}")
        # Record the tool response
        self._log_store.record_tool_response(self.name, tool_name, result)
