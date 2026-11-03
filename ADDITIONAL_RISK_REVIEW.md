# Additional Risk Review

Review date: October 31, 2026.

This document records findings identified after the initial agent overload implementation. It covers both pre-existing defects and gaps in that implementation. **These findings are documented, not remediated.**

Validation combined static code inspection with selected in-memory reproductions using extracted production functions and mocked external services. No live LLM or Gmail requests were made, and no deployed environment was probed.

## Findings Overview

| ID | Finding | Priority | Origin |
| --- | --- | --- | --- |
| 1 | Email operations lack an enforced approval check | High | Existing tool implementation |
| 2 | Execution status can contradict the actual outcome | High | Existing execution result handling |
| 3 | New agents can inherit another agent's legacy journal | High | Gap in the initial journal isolation changes |
| 4 | External cancellation can prevent a batch from draining | High | Existing batch management |
| 5 | Synchronous tools block the event loop and undermine timeouts | High | Existing tool execution |
| 6 | Unrelated requests share batches, and the same agent can execute concurrently | Medium-high | Existing scheduling and batching |
| 7 | Temporary failures or delayed polling can suppress important email notifications | High reliability impact | Existing email monitoring |
| 8 | In-flight work can recreate cleared agent records and journals | Medium-high | Existing clear operation and new catalog update path |
| 9 | Reachable chat endpoints lack application-level authentication | Conditionally high | Existing API and deployment configuration |

## 1. Email Operations Lack an Enforced Approval Check

Location: [gmail.py](server/agents/execution_agent/tools/gmail.py#L376), particularly `gmail_execute_draft()`. Forwarding and reply tools require the same review.

The function checks whether Gmail is connected and then submits the operation. It does not verify approval for the specific draft, recipients, body, or content version.

Trigger: the user requests a draft, but the model incorrectly generates a send call. With a connected account, the tool does not reject the call for missing approval.

The prompts require draft review and confirmation, but this is a model instruction rather than an enforced tool-level check.

Validation: the actual send function was extracted and its underlying email operation replaced. Without any approval state, execution still reached the mocked send operation. No real email was sent.

## 2. Execution Status Can Contradict the Actual Outcome

Location: [runtime.py](server/agents/execution_agent/runtime.py#L224), including `_execute_tool()` and the exception handling in `execute()`.

### 2.1 Error Dictionaries Are Reported as Success

When Gmail is disconnected, a tool returns:

```python
{"error": "Gmail not connected"}
```

The wrapper checks only whether the function raised an exception, then returns `True, result`. Consequently, a model-visible result can contain:

```json
{
  "status": "success",
  "result": {"error": "Gmail not connected"}
}
```

Impact: logs and model-visible status contradict the business outcome. This does not establish that the model will necessarily claim success, but the success flag itself is incorrect.

Validation: reproduced in memory using the actual `_execute_tool()` and a tool substitute returning an error dictionary.

### 2.2 A Successful Operation Can Be Reported as a Failed Task

Trigger sequence:

```text
The email operation succeeds
-> The runtime appends its result to messages
-> The next LLM request fails
-> execute() returns success=False
```

Impact: the user may receive a failure message after the external action has already occurred. Retrying a forward, reply, or newly created draft can repeat the side effect. Whether the same already-sent Gmail draft can be sent again was not evaluated.

Validation: a mocked send recorded its side effect, and the second model request raised an exception. The operation occurred while the overall result reported failure.

## 3. New Agents Can Inherit Another Agent's Legacy Journal

Location: [log_store.py](server/services/execution/log_store.py#L117), in `_legacy_path()` and `iter_entries()`.

The initial implementation writes new records to separate UUID-based files, but reading still unconditionally attempts the legacy slug path derived from the name.

Suppose the original catalog contains only:

```text
Original agent: Email to Alice
Legacy journal: email-to-alice.log
```

A newly created independent agent named `Email_to_Alice` still reads `email-to-alice.log`, even if that exact name never existed in the legacy roster.

Impact: the new task can inherit another task's draft IDs, thread IDs, and history. Isolating new writes does not establish isolation when reading legacy records.

This is a gap in the initial changes, beyond the unavoidable ambiguity of journals that were already merged historically. Existing tests verify that new writes do not leak across names, but do not verify that a new name cannot inherit another task's legacy journal.

## 4. External Cancellation Can Prevent a Batch from Draining

Location: [batch_manager.py](server/agents/execution_agent/batch_manager.py#L48), in `execute_agent()`.

`CancelledError` is not caught by `except Exception`. External cancellation executes the `finally` block and removes the `_pending` entry, but skips `_complete_execution()`. The batch's `pending` count is not decremented.

In-memory reproduction:

```text
After cancellation: tracked_requests=0, batch_pending=1
After the next task completes: batch_pending=1, no dispatch
```

Impact: subsequent tasks on the same manager join a batch that cannot reach zero. Results remain buffered until the state is reset.

This finding concerns external cancellation of the execution coroutine. Ordinary `asyncio.wait_for()` timeout handling follows a separate, explicitly handled path.

## 5. Synchronous Tools Block the Event Loop and Undermine Timeouts

Locations: the synchronous call to `tool_func(**arguments)` in [runtime.py](server/agents/execution_agent/runtime.py#L231), and the synchronous service call in [gmail.py](server/agents/execution_agent/tools/gmail.py#L331).

The runtime calls the tool before checking whether its return value is awaitable. If the tool synchronously waits for a network response, the event loop cannot schedule other coroutines or timeout callbacks during that wait.

In-memory reproduction using the actual tool execution method and a synchronous substitute:

```text
wait_for timeout: 0.01 seconds
Synchronous tool delay: 0.06 seconds
Outcome: normal return after approximately 0.061 seconds, without a timeout
```

Impact: the default 90-second timeout is not a reliable upper bound for synchronous email operations. Other agents, chat requests, and background monitoring can also stall. The underlying SDK's timeout behavior determines the actual blocking duration.

## 6. Request, Batch, and Agent Workflow Isolation Is Insufficient

Locations: batch registration in [batch_manager.py](server/agents/execution_agent/batch_manager.py#L101), and background task creation in [tools.py](server/agents/interaction_agent/tools.py#L195).

### 6.1 Unrelated Requests Join the Same Batch

The manager holds one active batch without a user-turn or workflow identifier.

```text
Request 1 starts task A: check Alice's reply
-> A remains in progress
-> Request 2 starts task B: prepare a weekly report
-> B joins A's batch
```

Even if A finishes first, its result waits for unrelated task B. A continuous stream of new work can further delay batch completion. This behavior was reproduced in memory.

### 6.2 Same-Agent Work Is Neither Serialized nor Deduplicated

Two requests can enter the same agent's runtime concurrently, such as one editing a draft while another sends it. The registration lock and per-entry journal lock do not cover the complete business workflow.

Reusing a `request_id` also executes the request twice. It is a bookkeeping identifier, not an idempotency guarantee. Concurrent entry and repeated-ID execution were verified with in-memory barriers and calls; duplicate email effects require the corresponding business conditions.

## 7. Temporary Failures or Delayed Polling Can Suppress Notifications

Locations: [importance_classifier.py](server/services/gmail/importance_classifier.py#L102) and [importance_watcher.py](server/services/gmail/importance_watcher.py#L103).

### 7.1 Classification Failure Is Indistinguishable from an Unimportant Email

The classifier returns `None` both when the model API fails and when an email is deemed unimportant. The watcher marks either case as processed, preventing classification retries.

```text
An important email arrives
-> The model API temporarily fails
-> The email is marked as processed
-> No notification retry occurs
```

A related issue exists during dispatch: the caller does not check whether `handle_agent_message()` returns an `InteractionResult` with `success=False`.

### 7.2 Delayed Polling Skips Emails Arriving After the Previous Successful Poll

The cutoff selects the later of the previous poll timestamp and the current time minus one polling interval.

For example, the last successful poll is at 10:00 and the next poll is delayed until 10:03. With the default 60-second interval, the cutoff becomes 10:02. An unseen important email received at 10:01 can be fetched but discarded as too old and marked as processed.

Both paths were confirmed by code inspection. No live mailbox fault injection was performed.

## 8. In-Flight Work Can Recreate Cleared Records and Journals

Locations: `clear_history()` in [chat.py](server/routes/chat.py#L26), `_record()` in [roster.py](server/services/execution/roster.py#L237), and the batch manager's completion path.

The clear operation removes history, agent records, and triggers without stopping already-running execution tasks.

```text
A task is executing
-> The user clears the data
-> The original task completes
-> record_result() finds no agent
-> _ensure_agent() creates a new record with the same name
```

In-memory SQLite verification of `record_request -> clear -> record_result` recreated one agent after the count had reached zero. The new record had a different ID and an empty initial purpose. Existing tasks may also continue writing journals or performing email operations.

The original clear workflow lacks coordination with running tasks. The new catalog update path additionally allows a deleted identity to be recreated.

## 9. Reachable Chat Endpoints Lack Application-Level Authentication

Locations: [chat.py](server/routes/chat.py#L18), [config.py](server/config.py#L50), and [app.py](server/app.py#L49).

The default bind address is `0.0.0.0`. Chat history, submission, and clearing endpoints do not enforce application-level authentication.

Trigger conditions: another party can reach the port, and the deployment does not provide separate authentication.

Under those conditions, another party may read conversations, submit tasks, or clear data. Binding all interfaces does not by itself establish public internet exposure; reachability depends on firewalls, networking, and proxy configuration. No deployed environment was probed.

## Relationship to Existing Tests

The existing 32 tests cover agent cards, retrieval, context budgets, identity reuse, and mocked tool orchestration. They do not cover all findings above, particularly cancellation, synchronous blocking, legacy journal ownership, action approval, failure recovery, and clearing data while work remains in flight.

Passing the existing suite does not establish that these risks have been resolved. See [Implementation Changelog](IMPLEMENTATION_CHANGELOG.md) for the initial implementation's file inventory and validation scope.
