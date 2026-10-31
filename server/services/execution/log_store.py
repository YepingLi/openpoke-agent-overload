"""Execution agent log management with structured XML-style tags."""

from __future__ import annotations

import re
import threading
import json
from collections import OrderedDict, deque
from html import escape, unescape
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from uuid import NAMESPACE_URL, uuid5

from ...logging_config import logger
from ...utils.timezones import now_in_user_timezone


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_EXECUTION_LOG_DIR = _DATA_DIR / "execution_agents"


def _slugify(name: str) -> str:
    """Convert agent name to filesystem-safe slug."""
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "agent"


def _encode_payload(payload: str) -> str:
    """Encode a JSON string so literal backslashes and newlines round-trip."""
    return escape(json.dumps(payload, ensure_ascii=False), quote=False)


def _decode_payload(payload: str) -> str:
    """Decode the original unversioned journal format."""
    return unescape(payload).replace("\\n", "\n")


_ATTR_PATTERN = re.compile(r"(\w+)\s*=\s*\"([^\"]*)\"")
_IDENTIFIER_KEYS = {
    "draft_id": "draft_id", "draftId": "draft_id",
    "thread_id": "thread_id", "threadId": "thread_id",
    "message_id": "message_id", "messageId": "message_id",
}


def _storage_id(agent_name: str) -> str:
    """Stable identity for an exact name; punctuation and case remain significant."""
    return str(uuid5(NAMESPACE_URL, f"openpoke:execution:{agent_name}"))


def _render_entry(tag: str, timestamp: str, payload: str) -> str:
    attributes = f' timestamp="{escape(timestamp, quote=True)}"' if timestamp else ""
    return f"<{tag}{attributes}>{escape(payload, quote=False)}</{tag}>"


def _historical_ids(tag: str, payload: str) -> Iterator[Tuple[str, str]]:
    """Extract explicit identifiers, never arbitrary prose or truncated JSON."""
    if tag == "tool_response":
        tool_name, _, raw = payload.partition(": ")
    elif tag == "agent_action" and payload.startswith("Calling "):
        tool_name, _, raw = payload.removeprefix("Calling ").partition(" with: ")
    else:
        return
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return
    stack = [(value, True)]
    while stack:
        item, top_level = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                field = _IDENTIFIER_KEYS.get(key)
                # Gmail draft responses often use a top-level `id` field.
                if key == "id" and top_level and tool_name == "gmail_create_draft":
                    field = "draft_id"
                if field and isinstance(child, str) and 0 < len(child) <= 256:
                    yield field, child
                elif isinstance(child, (dict, list)):
                    # Known Composio envelopes may wrap the draft object.
                    wrapped_draft = top_level and key in {"data", "result", "response_data", "draft"}
                    stack.append((child, wrapped_draft))
        elif isinstance(item, list):
            stack.extend((child, False) for child in item)


class ExecutionAgentLogStore:
    """Append-only journal for execution agents with XML-style tags."""

    def __init__(self, base_dir: Path):
        self._base_dir = base_dir
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            (self._base_dir / "identities").mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"Failed to create directory: {exc}")

    def _lock_for(self, agent_name: str) -> threading.Lock:
        """Get or create a lock for an agent."""
        slug = _storage_id(agent_name)
        with self._global_lock:
            if slug not in self._locks:
                self._locks[slug] = threading.Lock()
            return self._locks[slug]

    def _log_path(self, agent_name: str) -> Path:
        """Get log file path for an agent."""
        return self._base_dir / "identities" / f"{_storage_id(agent_name)}.log"

    def _legacy_path(self, agent_name: str) -> Path:
        return self._base_dir / f"{_slugify(agent_name)}.log"

    def _append(self, agent_name: str, tag: str, payload: str) -> None:
        """Append an entry with the given tag."""
        encoded = _encode_payload(str(payload))
        timestamp = now_in_user_timezone("%Y-%m-%d %H:%M:%S")
        entry = f"<{tag} timestamp=\"{timestamp}\" encoding=\"json\">{encoded}</{tag}>\n"

        with self._lock_for(agent_name):
            try:
                with self._log_path(agent_name).open("a", encoding="utf-8") as handle:
                    handle.write(entry)
            except Exception as exc:
                logger.error(f"Failed to append to log: {exc}")

    def _parse_line(self, line: str) -> Optional[Tuple[str, str, str]]:
        """Parse a single log line."""
        stripped = line.strip()
        if not (stripped.startswith("<") and "</" in stripped):
            return None

        open_end = stripped.find(">")
        close_start = stripped.rfind("</")
        close_end = stripped.rfind(">")

        if open_end == -1 or close_start == -1 or close_end == -1:
            return None

        open_tag_content = stripped[1:open_end]
        if " " in open_tag_content:
            tag, attr_string = open_tag_content.split(" ", 1)
        else:
            tag, attr_string = open_tag_content, ""

        closing_tag = stripped[close_start + 2 : close_end]
        if closing_tag != tag:
            return None

        attributes: Dict[str, str] = {
            match.group(1): match.group(2) for match in _ATTR_PATTERN.finditer(attr_string)
        }
        timestamp = attributes.get("timestamp", "")
        raw_payload = stripped[open_end + 1 : close_start]
        if attributes.get("encoding") == "json":
            try:
                payload = json.loads(unescape(raw_payload))
            except (TypeError, ValueError):
                return None
            if not isinstance(payload, str):
                return None
        else:
            payload = _decode_payload(raw_payload)
        return tag, timestamp, payload

    def record_request(self, agent_name: str, instructions: str) -> None:
        """Record an incoming request from the interaction agent."""
        self._append(agent_name, "agent_request", instructions)

    def record_action(self, agent_name: str, description: str) -> None:
        """Record an agent action (tool call)."""
        self._append(agent_name, "agent_action", description)

    def record_tool_response(self, agent_name: str, tool_name: str, response: str) -> None:
        """Record the response from a tool."""
        self._append(agent_name, "tool_response", f"{tool_name}: {response}")

    def record_agent_response(self, agent_name: str, response: str) -> None:
        """Record the agent's final response."""
        self._append(agent_name, "agent_response", response)

    def iter_entries(self, agent_name: str) -> Iterator[Tuple[str, str, str]]:
        """Stream legacy history then identity-specific entries without copying a log.

        Legacy files remain read-only. Their slug-based ownership may already be
        ambiguous; a bounded prompt explicitly warns about this limitation.
        """
        with self._lock_for(agent_name):
            paths = list(dict.fromkeys((self._legacy_path(agent_name), self._log_path(agent_name))))
            for path in paths:
                try:
                    with path.open(encoding="utf-8") as handle:
                        for line in handle:
                            parsed = self._parse_line(line)
                            if parsed is not None:
                                yield parsed
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    logger.error(f"Failed to read log: {exc}")

    def load_transcript(self, agent_name: str) -> str:
        """Load the full transcript for inclusion in system prompt."""
        parts: List[str] = []
        for tag, timestamp, payload in self.iter_entries(agent_name):
            parts.append(_render_entry(tag, timestamp, payload))
        return "\n".join(parts)

    def load_bounded_transcript(
        self,
        agent_name: str,
        max_chars: int = 12000,
        conversation_limit: Optional[int] = None,
    ) -> str:
        """Return recent complete entries and a bounded cache of historical IDs.

        The budget includes escaped payloads, labels, and omission notices. Full
        audit records stay on disk. Reading remains O(log size), while retained
        prompt data is O(max_chars); this is not a semantic memory retriever.
        """
        if max_chars < 512:
            raise ValueError("History budget must be at least 512 characters")
        notice = (
            "[History is a bounded selection. Older or oversized entries may be omitted; "
            "the full journal remains on disk. Do not guess missing details.]"
        )
        legacy_notice = (
            "[Legacy slug journal present: ownership may be ambiguous across similar agent names. "
            "Verify legacy facts before continuing.]"
        ) if self._legacy_path(agent_name).is_file() else ""
        prefix = "\n".join(part for part in (notice, legacy_notice) if part)
        reference_budget = min(2048, max_chars // 3)
        tail_budget = max_chars - len(prefix) - reference_budget - 4
        entries: deque[Tuple[str, str]] = deque()
        identifiers: OrderedDict[Tuple[str, str], None] = OrderedDict()
        total_chars = 0
        request_count = 0
        seen = 0
        for tag, timestamp, payload in self.iter_entries(agent_name):
            seen += 1
            for identifier in _historical_ids(tag, payload):
                identifiers.pop(identifier, None)
                identifiers[identifier] = None
                if len(identifiers) > 12:
                    identifiers.popitem(last=False)
            rendered = _render_entry(tag, timestamp, payload)
            if len(rendered) > tail_budget:
                continue
            entries.append((tag, rendered))
            total_chars += len(rendered) + 1
            request_count += tag == "agent_request"
            if conversation_limit is not None and conversation_limit > 0 and request_count > conversation_limit:
                # Discard the entire older request group, not only its opener.
                while entries and (request_count > conversation_limit or entries[0][0] != "agent_request"):
                    old_tag, old_rendered = entries.popleft()
                    total_chars -= len(old_rendered) + 1
                    request_count -= old_tag == "agent_request"
            while entries and total_chars > tail_budget:
                old_tag, old_rendered = entries.popleft()
                total_chars -= len(old_rendered) + 1
                request_count -= old_tag == "agent_request"
        if not seen:
            return ""
        reference_lines = [
            "[Historical identifiers only: these do not prove current state or approval.]"
        ]
        for field, value in reversed(identifiers):
            line = f"{field}={escape(value, quote=True)}"
            if len("\n".join([*reference_lines, line])) <= reference_budget:
                reference_lines.append(line)
        references = "\n".join(reference_lines) if len(reference_lines) > 1 else ""
        return "\n".join(
            part for part in (prefix, references, "\n".join(entry for _, entry in entries)) if part
        )

    def load_recent(self, agent_name: str, limit: int = 10) -> list[tuple[str, str, str]]:
        """Load recent log entries."""
        return list(deque(self.iter_entries(agent_name), maxlen=max(0, limit)))

    def list_agents(self) -> list[str]:
        """List legacy slugs and stable storage identities with logs."""
        try:
            return sorted(path.stem for path in self._base_dir.glob("**/*.log"))
        except Exception as exc:
            logger.error(f"Failed to list agents: {exc}")
            return []

    def clear_all(self) -> None:
        """Clear all execution agent logs."""
        try:
            for log_file in self._base_dir.glob("**/*.log"):
                log_file.unlink()
            logger.info("Cleared all execution agent logs")
        except Exception as exc:
            logger.error(f"Failed to clear execution logs: {exc}")


_execution_agent_logs = ExecutionAgentLogStore(_EXECUTION_LOG_DIR)


def get_execution_agent_logs() -> ExecutionAgentLogStore:
    """Get the singleton log store instance."""
    return _execution_agent_logs
