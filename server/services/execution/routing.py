"""Bounded discovery context, separate from the model's routing decision."""

from html import escape
from typing import Any

from .roster import AgentCard, AgentRoster


CANDIDATE_LIMIT = 8
RECENT_LIMIT = 2
ROSTER_CHAR_LIMIT = 6000


def candidate_payload(card: AgentCard) -> dict[str, Any]:
    """Names are labels; use the full stable ID even if a label is shortened."""
    return {
        "agent_id": card.agent_id,
        "name": card.name[:160],
        "purpose": card.purpose[:240],
        "latest_request": card.latest_request[:200],
        "last_result": card.last_result[:300],
        "updated_at": card.updated_at,
    }


def select_candidates(
    roster: AgentRoster,
    query: str,
    limit: int = CANDIDATE_LIMIT,
    recent_limit: int = RECENT_LIMIT,
) -> list[AgentCard]:
    """Reserve a small recent set for follow-ups without forcing its reuse.

    Relevance leads; recency only supplies context. Multiple recent drafts stay
    visible so that an ambiguous 'send it' can be clarified by the interaction
    agent. Neither this function nor lexical ranking authorizes a side effect.
    """
    limit = max(0, min(limit, CANDIDATE_LIMIT))
    if not limit:
        return []
    recent_limit = max(0, min(recent_limit, limit))
    recent = roster.recent(recent_limit)
    relevant = roster.search(query, limit=limit)
    selected = {card.agent_id: card for card in relevant[:limit - len(recent)]}
    for card in recent:
        selected.setdefault(card.agent_id, card)
    for card in relevant:
        if len(selected) == limit:
            break
        selected.setdefault(card.agent_id, card)
    return list(selected.values())[:limit]


def _escaped_clip(value: str, budget: int, *, quote: bool = False) -> str:
    """Clip escaped text without splitting an XML entity or hiding truncation."""
    escaped = escape(value, quote=quote)
    if len(escaped) <= budget:
        return escaped
    if budget <= 0:
        return ""
    parts = []
    remaining = budget - 1
    for character in value:
        fragment = escape(character, quote=quote)
        if len(fragment) > remaining:
            break
        parts.append(fragment)
        remaining -= len(fragment)
    return "".join(parts) + "…"


def render_candidates(
    roster: AgentRoster, query: str, max_chars: int = ROSTER_CHAR_LIMIT
) -> str:
    """Render valid escaped XML within a character budget, including wrappers."""
    if max_chars < 256:
        raise ValueError("Candidate budget must be at least 256 characters")
    max_chars = min(max_chars, ROSTER_CHAR_LIMIT)
    lines = [f'<agent_candidates total="{roster.count()}" complete="false">']
    closing = "</agent_candidates>"
    hint = "Shortlist only. Search for omitted tasks; clarify ambiguous follow-ups."
    lines.append(hint)
    cards = select_candidates(roster, query)
    available = max_chars - len("\n".join([*lines, closing]))
    per_card = available // max(1, len(cards)) - 1
    for card in cards:
        payload = candidate_payload(card)
        name = _escaped_clip(payload["name"], max(0, min(160, per_card // 4)), quote=True)
        opening = f'<agent id="{card.agent_id}" name="{name}" updated_at="{card.updated_at:.3f}">'
        keys = ("purpose", "latest_request", "last_result")
        skeleton = opening + "\n" + "\n".join(f"  <{key}></{key}>" for key in keys) + "\n</agent>"
        field_budget = max(0, (per_card - len(skeleton)) // len(keys))
        body = "\n".join(f"  <{key}>{_escaped_clip(payload[key], field_budget)}</{key}>" for key in keys)
        entry = f"{opening}\n{body}\n</agent>"
        if len("\n".join([*lines, entry, closing])) > max_chars:
            # Drop whole cards; never cut XML, an ID, or a tool argument.
            continue
        lines.append(entry)
    lines.append(closing)
    return "\n".join(lines)
