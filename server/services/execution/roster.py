"""Searchable execution-agent cards, stored transactionally without model calls.

Legacy JSON names are imported once and retained for recovery. Existing names
still identify execution logs and trigger owners; UUIDs give stable lookup IDs.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from ...logging_config import logger


_STOP_WORDS = frozenset(
    "a an and are as at be been can could did do does for from had has have "
    "help i in is it its me my of on or please that the their them then there "
    "these they this those to us was we were what when where which who will "
    "with would you your yet".split()
)
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def _terms(text: str, *, query: bool = False) -> list[str]:
    """Normalize words and Chinese character/bigram terms for lexical search."""
    words: list[str] = []
    for word in _WORDS.findall(text.casefold()):
        for segment in _CJK.findall(word):
            words.extend(segment)
            words.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        words.extend(part for part in _CJK.sub(" ", word).split() if part not in _STOP_WORDS)
    return list(dict.fromkeys(words))[:32] if query else words


def _search_text(text: str) -> str:
    return " ".join(_terms(text))


@dataclass(frozen=True)
class AgentCard:
    agent_id: str
    name: str
    purpose: str = ""
    latest_request: str = ""
    last_result: str = ""
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentRoster:
    """Complete catalog with bounded snippets and indexed candidate retrieval.

    ``roster_path`` names the legacy JSON file; its ``.sqlite3`` sibling is the
    authoritative store. Each call opens a separate, short-lived connection.
    """

    MAX_RESULTS = 20

    def __init__(self, roster_path: Path):
        self._roster_path = Path(roster_path)
        self._db_path = self._roster_path.with_suffix(".sqlite3")
        self._initialization_lock = threading.Lock()
        self._initialized = False
        self._fts_enabled = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def load(self) -> None:
        """Ensure storage exists; retained for compatibility with old callers."""
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS agents ("
                    "agent_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
                    "purpose TEXT NOT NULL DEFAULT '', latest_request TEXT NOT NULL DEFAULT '', "
                    "last_result TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL)"
                )
                connection.execute("CREATE INDEX IF NOT EXISTS agents_recent ON agents(updated_at DESC)")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS roster_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                had_index = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'agent_search'"
                ).fetchone()
                try:
                    connection.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS agent_search USING fts5("
                        "agent_id UNINDEXED, name, purpose, latest_request, last_result, "
                        "tokenize='unicode61 remove_diacritics 2')"
                    )
                    self._fts_enabled = True
                except sqlite3.OperationalError as exc:
                    if "no such module" not in str(exc).lower():
                        raise
                    logger.warning("SQLite FTS5 unavailable; using lexical catalog fallback")
                if self._fts_enabled and not had_index:
                    for row in connection.execute("SELECT * FROM agents").fetchall():
                        self._update_index(connection, AgentCard(**dict(row)))
                if not connection.execute(
                    "SELECT 1 FROM roster_metadata WHERE key = 'legacy_imported'"
                ).fetchone():
                    if self._import_legacy(connection):
                        connection.execute(
                            "INSERT INTO roster_metadata(key, value) VALUES ('legacy_imported', '1')"
                        )
            self._initialized = True

    def save(self) -> None:
        """Compatibility method: individual mutations are already committed."""
        self.load()

    def _import_legacy(self, connection: sqlite3.Connection) -> bool:
        """Backfill small cards once; never scan old journals during retrieval.

        Slug-based legacy logs can already have ambiguous ownership. Importing
        their snippets makes them discoverable, not verified authorizations.
        The full original JSON and journals remain untouched.
        """
        try:
            data = json.loads(self._roster_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, ValueError) as exc:
            logger.warning("Could not import legacy agent roster: %s", exc)
            return False
        if not isinstance(data, list):
            logger.warning("Legacy agent roster was not a list; original file retained")
            return False

        # Delayed import avoids coupling the catalog's import path to journals.
        from .log_store import ExecutionAgentLogStore

        journals = ExecutionAgentLogStore(self._roster_path.parent)
        for name in dict.fromkeys(str(value) for value in data):
            if not name.strip():
                continue
            current = self._ensure_agent(connection, name)
            purpose = current.purpose
            latest_request = current.latest_request
            last_result = current.last_result
            for tag, _, payload in journals.iter_entries(name):
                if tag == "agent_request":
                    purpose = purpose or payload[:500]
                    latest_request = payload[:500]
                elif tag == "agent_response":
                    last_result = payload[:1000]
            card = AgentCard(
                agent_id=current.agent_id,
                name=name,
                purpose=purpose,
                latest_request=latest_request,
                last_result=last_result,
                updated_at=current.updated_at,
            )
            connection.execute(
                "UPDATE agents SET purpose=?, latest_request=?, last_result=? WHERE agent_id=?",
                (card.purpose, card.latest_request, card.last_result, card.agent_id),
            )
            self._update_index(connection, card)
        return True

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Agent name must be a non-empty string")

    def _update_index(self, connection: sqlite3.Connection, card: AgentCard) -> None:
        if self._fts_enabled:
            rowid = connection.execute(
                "SELECT rowid FROM agents WHERE agent_id = ?", (card.agent_id,)
            ).fetchone()[0]
            connection.execute("DELETE FROM agent_search WHERE rowid = ?", (rowid,))
            connection.execute(
                "INSERT INTO agent_search(rowid, agent_id, name, purpose, latest_request, last_result) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rowid, card.agent_id, *(_search_text(value) for value in (
                    card.name, card.purpose, card.latest_request, card.last_result
                ))),
            )

    def _ensure_agent(self, connection: sqlite3.Connection, name: str) -> AgentCard:
        row = connection.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
        if row is not None:
            return AgentCard(**dict(row))
        card = AgentCard(agent_id=str(uuid.uuid4()), name=name, updated_at=time.time())
        connection.execute(
            "INSERT INTO agents(agent_id, name, updated_at) VALUES (?, ?, ?)",
            (card.agent_id, card.name, card.updated_at),
        )
        self._update_index(connection, card)
        return card

    def add_agent(self, agent_name: str) -> AgentCard:
        self._validate_name(agent_name)
        self.load()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._ensure_agent(connection, agent_name)

    def get_agent(
        self, name: Optional[str] = None, agent_id: Optional[str] = None
    ) -> Optional[AgentCard]:
        """Look up one exact name or stable ID; no fuzzy identity matching."""
        if (name is None) == (agent_id is None):
            raise ValueError("Provide exactly one of name or agent_id")
        self.load()
        with self._connect() as connection:
            column, value = ("name", name) if name is not None else ("agent_id", agent_id)
            row = connection.execute(f"SELECT * FROM agents WHERE {column} = ?", (value,)).fetchone()
        return AgentCard(**dict(row)) if row is not None else None

    def _record(self, name: str, text: str, *, result: bool) -> AgentCard:
        self._validate_name(name)
        self.load()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._ensure_agent(connection, name)
            card = AgentCard(
                agent_id=current.agent_id,
                name=current.name,
                purpose=current.purpose or ("" if result else str(text)[:500]),
                latest_request=current.latest_request if result else str(text)[:500],
                last_result=str(text)[:1000] if result else current.last_result,
                updated_at=time.time(),
            )
            connection.execute(
                "UPDATE agents SET purpose=?, latest_request=?, last_result=?, updated_at=? WHERE agent_id=?",
                (card.purpose, card.latest_request, card.last_result, card.updated_at, card.agent_id),
            )
            self._update_index(connection, card)
            return card

    def record_request(self, name: str, instructions: str) -> AgentCard:
        return self._record(name, instructions, result=False)

    def record_result(self, name: str, response: str) -> AgentCard:
        return self._record(name, response, result=True)

    def get_agents(self) -> list[str]:
        """Legacy management API; do not use the full list for prompts."""
        self.load()
        with self._connect() as connection:
            return [row[0] for row in connection.execute("SELECT name FROM agents ORDER BY rowid")]

    def count(self) -> int:
        self.load()
        with self._connect() as connection:
            return connection.execute("SELECT count(*) FROM agents").fetchone()[0]

    @classmethod
    def _limit(cls, limit: int) -> int:
        return max(0, min(int(limit), cls.MAX_RESULTS))

    def recent(self, limit: int = 3) -> list[AgentCard]:
        self.load()
        with self._connect() as connection:
            return [AgentCard(**dict(row)) for row in connection.execute(
                "SELECT * FROM agents ORDER BY updated_at DESC, rowid DESC LIMIT ?", (self._limit(limit),)
            )]

    def search(self, query: str, limit: int = 5) -> list[AgentCard]:
        """Return candidates, never a decision to reuse an agent.

        Exact names win. Other results match any query term, weighted toward
        names and original purpose. Stopword-only queries return no results;
        recent context is exposed separately. FTS5 avoids catalog-text scans.
        """
        self.load()
        maximum = self._limit(limit)
        if not maximum:
            return []
        terms = _terms(str(query)[:2048], query=True)
        with self._connect() as connection:
            exact = connection.execute("SELECT * FROM agents WHERE name=?", (query,)).fetchone()
            cards = [AgentCard(**dict(exact))] if exact is not None else []
            if not terms or len(cards) >= maximum:
                return cards
            if self._fts_enabled:
                expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = connection.execute(
                    "SELECT a.* FROM agent_search JOIN agents a ON a.agent_id=agent_search.agent_id "
                    "WHERE agent_search MATCH ? "
                    "ORDER BY bm25(agent_search, 0, 5, 3, 2, 1), a.updated_at DESC LIMIT ?",
                    (expression, maximum + 1),
                ).fetchall()
            else:
                # Portable degraded mode; slower than the normal indexed path.
                predicates = " OR ".join(
                    "lower(name || ' ' || purpose || ' ' || latest_request || ' ' || last_result) LIKE ?"
                    for _ in terms
                )
                rows = connection.execute(
                    f"SELECT * FROM agents WHERE {predicates} ORDER BY updated_at DESC LIMIT ?",
                    (*('%' + term + '%' for term in terms), maximum + 1),
                ).fetchall()
            selected = {card.agent_id for card in cards}
            for row in rows:
                if row["agent_id"] not in selected:
                    cards.append(AgentCard(**dict(row)))
                    selected.add(row["agent_id"])
                if len(cards) >= maximum:
                    break
            return cards

    def clear(self) -> None:
        """Clear catalog/index, retaining migration marker and legacy backup."""
        self.load()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._fts_enabled:
                connection.execute("DELETE FROM agent_search")
            connection.execute("DELETE FROM agents")
            # Explicit deletion also suppresses a previously failed import.
            # Repairing a retained backup later must not resurrect cleared work.
            connection.execute(
                "INSERT OR REPLACE INTO roster_metadata(key, value) VALUES ('legacy_imported', '1')"
            )


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_ROSTER_PATH = _DATA_DIR / "execution_agents" / "roster.json"
_agent_roster: Optional[AgentRoster] = None
_singleton_lock = threading.Lock()


def get_agent_roster() -> AgentRoster:
    """Get the catalog lazily, avoiding filesystem writes during import."""
    global _agent_roster
    with _singleton_lock:
        if _agent_roster is None:
            _agent_roster = AgentRoster(_ROSTER_PATH)
        return _agent_roster
