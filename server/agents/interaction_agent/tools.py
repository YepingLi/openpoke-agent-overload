"""Tool definitions for interaction agent."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional

from ...logging_config import logger
from ...services.conversation import get_conversation_log
from ...services.execution import get_agent_roster
from ...services.execution.routing import candidate_payload
from ..execution_agent.batch_manager import ExecutionBatchManager


@dataclass
class ToolResult:
    """Standardized payload returned by interaction-agent tools."""

    success: bool
    payload: Any = None
    user_message: Optional[str] = None
    recorded_reply: bool = False

# Tool schemas for OpenRouter
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_agents",
            "description": "Search the COMPLETE agent catalog for an existing task, including old agents omitted from the shortlist. Use task terms, a person, email, thread or draft ID. Results are candidates, not a decision or authorization.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 1000},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_agent",
            "description": "Deliver instructions to an existing execution agent by stable agent_id (preferred) or exact name. A new name requires explicit create_new=true after checking existing candidates. Never infer approval to send from a search match.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Human-readable agent name describing its purpose (e.g., 'Vercel Job Offer', 'Email to Sharanjeet'). This name will be used to identify and potentially reuse the agent."
                    },
                    "instructions": {"type": "string", "description": "Instructions for the agent to execute."},
                    "agent_id": {"type": "string", "description": "Stable ID from candidate context or search_agents; omit only for exact-name reuse or deliberate creation."},
                    "create_new": {"type": "boolean", "description": "Set true only for a distinct new task after searching for an existing owner. Default false."},
                },
                "required": ["agent_name", "instructions"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "Deliver a natural-language response directly to the user. Use this for updates, confirmations, or any assistant response the user should see immediately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Plain-text message that will be shown to the user and recorded in the conversation log.",
                    },
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_draft",
            "description": "Record an email draft so the user can review the exact text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email for the draft.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject for the draft.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body content (plain text).",
                    },
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait silently when a message is already in conversation history to avoid duplicating responses. Adds a <wait> log entry that is not visible to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why waiting (e.g., 'Message already sent', 'Draft already created').",
                    },
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]

_EXECUTION_BATCH_MANAGER = ExecutionBatchManager()


# Create or reuse execution agent and dispatch instructions asynchronously
def search_agents(query: str, limit: int = 5) -> ToolResult:
    """Search all persisted cards without an additional LLM or remote call."""
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        return ToolResult(success=False, payload={"error": "Provide a non-empty query up to 1000 characters"})
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
        return ToolResult(success=False, payload={"error": "limit must be an integer from 1 to 8"})
    cards = get_agent_roster().search(query, limit=limit)
    return ToolResult(success=True, payload={
        "candidates": [candidate_payload(card) for card in cards],
        "complete": False,
        "hint": "No match is not proof a task is new. Try specific task/entity terms; clarify if ownership is ambiguous.",
    })


def send_message_to_agent(
    agent_name: str,
    instructions: str,
    agent_id: Optional[str] = None,
    create_new: bool = False,
) -> ToolResult:
    """Send instructions to an execution agent."""
    roster = get_agent_roster()
    if not isinstance(instructions, str) or not instructions.strip():
        return ToolResult(success=False, payload={"error": "instructions must be non-empty"})
    if not isinstance(agent_name, str) or not agent_name.strip() or not isinstance(create_new, bool):
        return ToolResult(success=False, payload={"error": "Provide an agent name and a boolean create_new"})
    if agent_id is not None and (not isinstance(agent_id, str) or not agent_id):
        return ToolResult(success=False, payload={"error": "agent_id must be a non-empty string"})
    if agent_id and create_new:
        return ToolResult(success=False, payload={"error": "Select an existing ID or create a new task, not both"})
    card = roster.get_agent(agent_id=agent_id) if agent_id else roster.get_agent(name=agent_name)
    if agent_id and card is None:
        return ToolResult(success=False, payload={"error": "Unknown agent_id; search again. No task was started."})
    if create_new and card is not None:
        return ToolResult(success=False, payload={"error": "Name already belongs to an existing task. Reuse its agent_id or choose a distinct name. No task was started."})
    is_new = card is None
    if is_new and not create_new:
        return ToolResult(success=False, payload={
            "status": "selection_required",
            "candidates": [candidate_payload(entry) for entry in roster.search(f"{agent_name} {instructions}", limit=5)],
            "hint": "Select an existing agent_id, search again, or deliberately set create_new=true. No task was started.",
        })
    if is_new and len(agent_name) > 160:
        return ToolResult(success=False, payload={"error": "New agent names must be at most 160 characters"})

    async def _execute_async() -> None:
        try:
            result = await _EXECUTION_BATCH_MANAGER.execute_agent(agent_name, instructions)
            status = "SUCCESS" if result.success else "FAILED"
            logger.info(f"Agent '{agent_name}' completed: {status}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"Agent '{agent_name}' failed: {str(exc)}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("No running event loop available for async execution")
        return ToolResult(success=False, payload={"error": "No event loop available"})

    if card is None:
        card = roster.add_agent(agent_name)
    agent_name = card.name
    action = "Created" if is_new else "Reused"
    logger.info(f"{action} agent: {agent_name}")
    loop.create_task(_execute_async())

    return ToolResult(
        success=True,
        payload={
            "status": "submitted",
            "agent_name": agent_name,
            "agent_id": card.agent_id,
            "new_agent_created": is_new,
        },
    )


# Send immediate message to user and record in conversation history
def send_message_to_user(message: str) -> ToolResult:
    """Record a user-visible reply in the conversation log."""
    log = get_conversation_log()
    log.record_reply(message)

    return ToolResult(
        success=True,
        payload={"status": "delivered"},
        user_message=message,
        recorded_reply=True,
    )


# Format and record email draft for user review
def send_draft(
    to: str,
    subject: str,
    body: str,
) -> ToolResult:
    """Record a draft update in the conversation log for the interaction agent."""
    log = get_conversation_log()

    message = f"To: {to}\nSubject: {subject}\n\n{body}"

    log.record_reply(message)
    logger.info(f"Draft recorded for: {to}")

    return ToolResult(
        success=True,
        payload={
            "status": "draft_recorded",
            "to": to,
            "subject": subject,
        },
        recorded_reply=True,
    )


# Record silent wait state to avoid duplicate responses
def wait(reason: str) -> ToolResult:
    """Wait silently and add a wait log entry that is not visible to the user."""
    log = get_conversation_log()
    
    # Record a dedicated wait entry so the UI knows to ignore it
    log.record_wait(reason)
    

    return ToolResult(
        success=True,
        payload={
            "status": "waiting",
            "reason": reason,
        },
        recorded_reply=True,
    )


# Return predefined tool schemas for LLM function calling
def get_tool_schemas():
    """Return OpenAI-compatible tool schemas."""
    return TOOL_SCHEMAS


# Route tool calls to appropriate handlers with argument validation and error handling
def handle_tool_call(name: str, arguments: Any) -> ToolResult:
    """Handle tool calls from interaction agent."""
    try:
        if isinstance(arguments, str):
            args = json.loads(arguments) if arguments.strip() else {}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            return ToolResult(success=False, payload={"error": "Invalid arguments format"})

        if name == "send_message_to_agent":
            return send_message_to_agent(**args)
        if name == "search_agents":
            return search_agents(**args)
        if name == "send_message_to_user":
            return send_message_to_user(**args)
        if name == "send_draft":
            return send_draft(**args)
        if name == "wait":
            return wait(**args)

        logger.warning("unexpected tool", extra={"tool": name})
        return ToolResult(success=False, payload={"error": f"Unknown tool: {name}"})
    except json.JSONDecodeError:
        return ToolResult(success=False, payload={"error": "Invalid JSON"})
    except TypeError as exc:
        return ToolResult(success=False, payload={"error": f"Missing required arguments: {exc}"})
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("tool call failed", extra={"tool": name, "error": str(exc)})
        return ToolResult(success=False, payload={"error": "Failed to execute"})
