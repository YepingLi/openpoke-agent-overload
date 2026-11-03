# Agent Overload Implementation Changelog

This document inventories the files changed in the initial implementation, their previous behavior, the resulting behavior, and the validation scope. Unresolved findings from the subsequent review are documented in [Additional Risk Review](ADDITIONAL_RISK_REVIEW.md).

## 1. Objective and Scope

The objective is to help the interaction agent find existing task owners as execution agents and their histories accumulate, while bounding candidate and historical context sent to models.

The initial implementation includes:

1. A searchable persistent catalog replacing the names-only roster.
2. Bounded relevant and recent candidates, with a complete-catalog search tool.
3. Stable-ID reuse and explicit creation of distinct tasks.
4. Bounded execution history with complete operational journals on disk.
5. Offline tests, scale evaluation, and design documentation.

## 2. Application Files: Eight Files

| File | Change | Previous Behavior | Initial Implementation |
| --- | --- | --- | --- |
| [server/services/execution/roster.py](server/services/execution/roster.py) | Modified | Names-only JSON list with Unix file locking | SQLite cards, stable IDs, full-text indexing, recent lookup, one-time legacy roster and journal backfill, transactional updates |
| [server/services/execution/routing.py](server/services/execution/routing.py) | Added | No dedicated candidate selection or rendering budget | Relevant and recent candidates, ID deduplication, remaining-slot filling, bounded rendering |
| [server/agents/interaction_agent/agent.py](server/agents/interaction_agent/agent.py) | Modified | Every prompt included the complete roster of agent names | Builds a bounded candidate-card section from the current message |
| [server/agents/interaction_agent/tools.py](server/agents/interaction_agent/tools.py) | Modified | Unknown names implicitly created agents | Adds search_agents, stable-ID reuse, selection_required responses, and explicit create_new=true |
| [server/agents/interaction_agent/system_prompt.md](server/agents/interaction_agent/system_prompt.md) | Modified | Primarily instructed the model to choose and reuse agents from the roster | Explains incomplete shortlists, further search, ambiguous ownership, and deliberate task creation |
| [server/agents/execution_agent/batch_manager.py](server/agents/execution_agent/batch_manager.py) | Modified | Executed and aggregated tasks without updating searchable cards | Records requests and results in the shared path, including scheduled work; moves request journaling into that path |
| [server/agents/execution_agent/agent.py](server/agents/execution_agent/agent.py) | Modified | Loaded all history by default and truncated stored tool arguments and results | Loads history within a default 12000-character budget and journals complete tool arguments and results |
| [server/services/execution/log_store.py](server/services/execution/log_store.py) | Modified | Slug-based journals; legacy encoding could conflate newlines and literal escapes | JSON-encoded new entries, name-derived UUID files for new writes, streamed history selection, and bounded historical references |

### 2.1 Agent Cards and Indexed Discovery

```python
AgentCard(
    agent_id=...,
    name=...,
    purpose=...,
    latest_request=...,
    last_result=...,
    updated_at=...,
)
```

- `agent_id`: stable routing identity for selecting an existing agent.
- `name`: original name retained for execution history and trigger compatibility.
- `purpose`: a snippet of the first nonempty task instruction, not a model-generated summary.
- `latest_request`: a snippet of the most recent task instruction.
- `last_result`: a snippet of the latest execution result.
- `updated_at`: latest activity timestamp.

The three text snippets are capped at 500, 500, and 1000 characters. SQLite FTS5 provides lexical retrieval with BM25 ranking, without embeddings or additional model calls. A slower LIKE fallback supports SQLite installations without FTS5.

### 2.2 Candidate Selection and Rendering

The default shortlist contains at most eight agents, reserving up to two positions for recent tasks. Overlapping relevant and recent agents are deduplicated by ID, and remaining positions are filled from relevant candidates.

The rendered candidate section is capped at 6000 characters, including XML tags and escaping. This budget applies to the candidate section, not the entire model request.

Retrieval supplies candidates. The interaction model still determines ownership using the request and conversation. The highest-ranked match does not automatically start a task or authorize an email operation.

### 2.3 Explicit Reuse and Creation

```text
Valid agent_id or exact existing name -> Reuse the existing task owner
Unknown name without explicit creation -> selection_required; no work starts
Unknown name with create_new=true -> Create a distinct task
```

Unknown IDs are rejected. Explicit creation using an already-owned name is also rejected. Searching before creation remains partly a prompt policy; the implementation does not prevent every deliberate semantic duplicate.

### 2.4 Bounded Execution History

Complete tool arguments, results, and journal entries remain on disk. Execution prompts use a default 12000-character history budget, selecting recent complete entries and retaining up to twelve historical draft, thread, or message IDs within a smaller reference budget.

Historical identifiers are references, not proof of current state or approval. History reads still scan the journal. Large results in the current tool loop and disk growth are not fully controlled by the history budget.

Journal filename UUIDs and catalog routing UUIDs serve different purposes. New journal filenames are derived from the exact original name; agent renaming is not implemented.

## 3. Test Files: Three Files, 32 Cases

| File | Cases | Coverage |
| --- | ---: | --- |
| [tests/test_agent_roster.py](tests/test_agent_roster.py) | 12 | Persistence, original purpose retention, lexical retrieval, legacy backfill, concurrent updates, clear and recovery behavior |
| [tests/test_agent_routing.py](tests/test_agent_routing.py) | 9 | Candidate and character bounds, recent context, ID reuse, explicit creation, shared scheduled-work indexing, mocked email orchestration |
| [tests/test_execution_history.py](tests/test_execution_history.py) | 11 | New journal write isolation, complete arguments and results, encoding round-trips, history budgets, and retained historical IDs |

SQLite and journal operations use real implementations in temporary directories. Model requests and email operations use mocks.

The email test scripts model tool calls in advance. It checks runtime parsing, dispatch, and result feedback, but does not establish that a real model understands the user, chooses the correct agent, or selects the correct draft.

## 4. Evaluation Files: Four Files

| File | Purpose |
| --- | --- |
| [evals/__init__.py](evals/__init__.py) | Enables evaluation through python -m |
| [evals/agent_overload.py](evals/agent_overload.py) | Builds temporary synthetic catalogs and measures retrieval recall, rendered recall, context characters, and local search latency |
| [evals/agent_overload_cases.json](evals/agent_overload_cases.json) | Defines target tasks, distractor scenarios, ambiguous follow-ups, and a paraphrase diagnostic |
| [evals/results.json](evals/results.json) | Records a complete scale evaluation, environment details, and source hashes |

Recorded results from the initial full evaluation:

| Distractor Agents | Original Roster Characters | Maximum Candidate Characters | Positive Lexical Recall@5 | Rendered Recall |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 4449 | 3193 | 1.0 | 1.0 |
| 1000 | 42249 | 3194 | 1.0 | 1.0 |
| 10000 | 420249 | 3195 | 1.0 | 1.0 |

Recall covers nine positive relevance labels per scale and must not be interpreted as task success rate. The separately reported paraphrase diagnostic did not retrieve its target, with recall of 0.0.

The evaluation does not measure real-model routing accuracy, actual token costs, or end-to-end latency. No real email was sent.

## 5. Documentation Files: Two Files

| File | Purpose |
| --- | --- |
| [README.md](README.md) | Adds agent discovery information and offline validation commands |
| [docs/agent-overload.md](docs/agent-overload.md) | Describes design, migration, implementation limits, tests, and evaluation |

The initial implementation involved the **17 files** listed above. These two subsequent review and inventory documents are not included in that count.

## 6. Running Validation

From the repository root in PowerShell, use the virtual environment with backend requirements installed:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Run one test file:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_agent_roster.py" -v
```

Run the full offline scale evaluation and update its report:

```powershell
.\.venv\Scripts\python.exe -m evals.agent_overload --sizes 100 1000 10000 --output evals/results.json
```

These checks require neither a live API key nor a connected Gmail account. Passing them does not resolve the findings in [Additional Risk Review](ADDITIONAL_RISK_REVIEW.md).
