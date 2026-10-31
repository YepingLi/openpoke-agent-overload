"""Measure catalog retrieval and prompt size without LLM or Gmail calls.

Run from the repository root:
    python -m evals.agent_overload --sizes 100 1000 10000 --output evals/results.json

The baseline reproduces the previous full-name roster rendering. It is a
context-size baseline, not a simulated baseline model/routing accuracy score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import statistics
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from server.services.execution.roster import AgentRoster
from server.services.execution.routing import render_candidates, select_candidates


FIXTURES = Path(__file__).with_name("agent_overload_cases.json")
PROMPT_CHARACTER_BUDGET = 6000


def _reproducibility_metadata(fixture_path: Path) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "sqlite_version": sqlite3.sqlite_version,
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "source_sha256": {
            relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in (
                "server/services/execution/roster.py",
                "server/services/execution/routing.py",
                "evals/agent_overload.py",
            )
        },
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _seed(roster: AgentRoster, fixture: dict[str, Any], distractors: int) -> dict[str, str]:
    """Create cold targets first, then newer distractors with shared vocabulary."""
    names = {}
    for agent in fixture["agents"]:
        names[agent["key"]] = agent["name"]
        roster.record_request(agent["name"], agent["instructions"])
        if agent.get("result"):
            roster.record_result(agent["name"], agent["result"])
    tasks = (
        "Review the weekly regional budget and finance report",
        "Prepare a restaurant supplier meeting invitation",
        "Find an email about the regional project delivery schedule",
        "Maintain the Monday weekly digest reminder automation",
        "Review the customer contract renewal and procurement spreadsheet",
    )
    for index in range(distractors):
        roster.record_request(
            f"Regional workspace {index:05d}",
            f"{tasks[index % len(tasks)]} for district{index:05d}.",
        )
    return names


def evaluate_size(distractors: int, fixture: dict[str, Any], repeats: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="openpoke-overload-eval-") as directory:
        roster = AgentRoster(Path(directory) / "roster.json")
        seed_started = time.perf_counter()
        names = _seed(roster, fixture, distractors)
        seed_seconds = time.perf_counter() - seed_started
        # This is the previous renderer's full roster, including XML escaping.
        baseline = "<active_agents>\n" + "\n".join(
            f'<agent name="{escape(name, quote=True)}" />' for name in roster.get_agents()
        ) + "\n</active_agents>"

        case_results = []
        retrieval_hits = shortlist_hits = rendered_hits = expected_total = 0
        maximum_section_chars = 0
        errors = []
        search_timings = []
        for case in fixture["cases"]:
            query = case["query"]
            expected_names = {names[key] for key in case["expected"]}
            found = roster.search(query, limit=5)
            shortlist = select_candidates(roster, query)
            section = render_candidates(roster, query, max_chars=PROMPT_CHARACTER_BUDGET)
            found_names = {card.name for card in found}
            shortlist_names = {card.name for card in shortlist}
            rendered_ids = {entry.attrib["id"] for entry in ET.fromstring(section).findall("agent")}
            expected_ids = {roster.get_agent(name=name).agent_id for name in expected_names}
            retrieval_hits += len(expected_names & found_names)
            shortlist_hits += len(expected_names & shortlist_names)
            rendered_hits += len(expected_ids & rendered_ids)
            expected_total += len(expected_names)
            maximum_section_chars = max(maximum_section_chars, len(section))
            if len(section) > PROMPT_CHARACTER_BUDGET:
                errors.append(f"{case['name']}: rendered section exceeded character budget")
            if len(shortlist) > 8:
                errors.append(f"{case['name']}: shortlist exceeded eight candidates")
            if not expected_names and found:
                errors.append(f"{case['name']}: unrelated work returned lexical matches")
            case_results.append({
                "name": case["name"],
                "query": query,
                "expected_agents": sorted(expected_names),
                "retrieved_at_5": [card.name for card in found],
                "shortlist": [card.name for card in shortlist],
                "retrieval_recall_at_5": len(expected_names & found_names) / len(expected_names)
                if expected_names else None,
                "shortlist_recall": len(expected_names & shortlist_names) / len(expected_names)
                if expected_names else None,
                "rendered_shortlist_recall": len(expected_ids & rendered_ids) / len(expected_ids)
                if expected_ids else None,
                "rendered_characters": len(section),
            })
            # Query is warmed above; setup, model calls, and rendering are excluded.
            for _ in range(repeats):
                started = time.perf_counter()
                roster.search(query, limit=5)
                search_timings.append((time.perf_counter() - started) * 1000)

        followup = fixture["ambiguous_followup"]
        for key in followup["recent"]:
            roster.record_request(names[key], "User is reviewing this existing draft.")
        ambiguous_candidates = select_candidates(roster, followup["query"])
        ambiguous_names = {card.name for card in ambiguous_candidates}
        preserved = {names[key] for key in followup["recent"]} <= ambiguous_names
        ambiguous_section = render_candidates(roster, followup["query"], max_chars=PROMPT_CHARACTER_BUDGET)
        ambiguous_rendered_ids = {
            entry.attrib["id"] for entry in ET.fromstring(ambiguous_section).findall("agent")
        }
        ambiguous_expected_ids = {
            roster.get_agent(name=names[key]).agent_id for key in followup["recent"]
        }
        rendered_preserved = ambiguous_expected_ids <= ambiguous_rendered_ids
        if not preserved:
            errors.append("Ambiguous follow-up did not preserve both recent draft owners")
        if not rendered_preserved:
            errors.append("Ambiguous follow-up renderer omitted a recent draft owner")

        return {
            "distractor_count": distractors,
            "catalog_count": roster.count(),
            "catalog_setup_seconds_excluded_from_latency": round(seed_seconds, 3),
            "retrieval_recall_at_5": retrieval_hits / expected_total,
            "shortlist_recall": shortlist_hits / expected_total,
            "rendered_shortlist_recall": rendered_hits / expected_total,
            "expected_agent_labels": expected_total,
            "baseline_full_roster_characters": len(baseline),
            "maximum_candidate_section_characters": maximum_section_chars,
            "character_reduction_fraction": 1 - maximum_section_chars / len(baseline),
            "search_latency_ms": {
                "samples": len(search_timings),
                "p50": round(statistics.median(search_timings), 3),
                "p95": round(_percentile(search_timings, 0.95), 3),
            },
            "ambiguous_followup": {
                "both_recent_draft_owners_preserved": preserved,
                "both_recent_draft_owners_rendered": rendered_preserved,
                "candidates": [card.name for card in ambiguous_candidates],
                "routing_accuracy": None,
                "reason": followup["explanation"],
            },
            "cases": case_results,
            "errors": errors,
        }


def evaluate_diagnostics(fixture: dict[str, Any]) -> dict[str, Any]:
    """Show known semantic gaps without presenting them as lexical-score wins."""
    results = []
    with tempfile.TemporaryDirectory(prefix="openpoke-overload-diagnostics-") as directory:
        roster = AgentRoster(Path(directory) / "roster.json")
        names = _seed(roster, fixture, distractors=0)
        for case in fixture.get("diagnostics", []):
            expected = {names[key] for key in case["expected"]}
            found = roster.search(case["query"], limit=5)
            found_names = {card.name for card in found}
            hits = len(expected & found_names)
            results.append({
                "name": case["name"],
                "query": case["query"],
                "expected_agents": sorted(expected),
                "retrieved_at_5": [card.name for card in found],
                "retrieval_recall_at_5": hits / len(expected) if expected else None,
                "expected_agent_retrieved": bool(hits),
                "explanation": case["explanation"],
            })
    return {
        "catalog": "fixture target agents only, no distractors",
        "excluded_from_positive_recall_gate": True,
        "reason": "Paraphrase cases measure a known limitation separately; gated recall covers the explicitly lexical fixtures only.",
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000, 10000])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--minimum-recall", type=float, default=0.9)
    args = parser.parse_args()
    if min(args.sizes) < 0 or args.repeats < 1 or not 0 <= args.minimum_recall <= 1:
        parser.error("sizes must be nonnegative, repeats positive, minimum recall in [0, 1]")
    fixture = json.loads(args.fixtures.read_text(encoding="utf-8"))
    reproducibility = _reproducibility_metadata(args.fixtures)
    results = []
    for size in args.sizes:
        result = evaluate_size(size, fixture, args.repeats)
        for metric in ("retrieval_recall_at_5", "shortlist_recall", "rendered_shortlist_recall"):
            if result[metric] < args.minimum_recall:
                result["errors"].append(f"{metric} below {args.minimum_recall:.2f}")
        results.append(result)
        print(
            f"{size:>5} distractors: recall@5={result['retrieval_recall_at_5']:.3f}, "
            f"shortlist={result['shortlist_recall']:.3f}, "
            f"context={result['maximum_candidate_section_characters']}/"
            f"{result['baseline_full_roster_characters']} chars, "
            f"search p95={result['search_latency_ms']['p95']:.3f} ms",
            flush=True,
        )
    try:
        fixture_label = str(args.fixtures.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        fixture_label = str(args.fixtures)
    report = {
        "schema_version": 2,
        "evaluation": "agent_catalog_retrieval_and_context_scale",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "reproducibility": reproducibility,
        "fixture_path": fixture_label,
        "minimum_recall": args.minimum_recall,
        "candidate_section_character_budget": PROMPT_CHARACTER_BUDGET,
        "limitations": [
            "Small synthetic relevance dataset; not a representative production benchmark.",
            "Baseline compares full-name roster context size only; no baseline model routing is simulated.",
            "Characters are measured directly; tokens, monetary cost, and end-to-end LLM latency are not measured.",
            "No live LLM, Gmail call, tool side effect, user confirmation, or workflow completion is evaluated.",
            "An ambiguous recent follow-up has no single routing label; candidate preservation is not proof of clarification behavior.",
            "Search is lexical, so paraphrases without matching terms can miss relevant agents.",
        ],
        "results": results,
        "diagnostics": evaluate_diagnostics(fixture),
        "passed": all(not result["errors"] for result in results),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
