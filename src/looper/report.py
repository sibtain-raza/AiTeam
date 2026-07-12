"""KPI / benchmarking report over the cross-run memory.

Usage:
    PYTHONPATH=src python -m looper.report [--output-dir output] [--window N]

Aggregates what the pipeline already records — `<output>/memory/runs.jsonl`
(the per-run index written by run_memory.record_run()) plus each run's own
`sdk-interactions.jsonl` (per-session cost/turns/errors) — into the numbers
that took manual log-reading to find before:

- per-run summary (cost, duration, QA rework rounds, max-turns hits)
- per-role aggregates across runs (sessions, cost, max-turns hit rate)
- turn-budget calibration for engineers (turns used vs. budget — the
  "architect always underestimates" pattern, surfaced automatically;
  `num_turns` is only present in logs written after it was added, so older
  sessions contribute to hit-rate but not to the used/budget ratio)
- defect stats parsed from every QA report in each run's SDK log
  (artifacts.parse_defects()), by severity and owning role

This is a report command, not a dashboard: it reads, aggregates, prints,
and exits. It never writes anything.
"""

import argparse
from pathlib import Path

from .artifacts import parse_defects
from .run_memory import load_runs, load_sdk_entries

_ENGINEER_AGENTS = ("frontend_engineer", "backend_engineer", "devops_engineer")
_MAX_TURNS_ERROR = "hit max_turns before finishing"


def build_report(output_dir: Path, window: int = 20) -> dict:
    """Aggregate the last `window` recorded runs into one data structure —
    separated from formatting so tests (and any future UI) consume the
    numbers, not the printed table."""
    runs = load_runs(output_dir)[-window:]

    per_run = []
    roles: dict[str, dict] = {}
    budget_samples: list[tuple[str, int, int]] = []  # (agent, used, budget)
    defects_by_severity: dict[str, int] = {}
    defects_by_owner: dict[str, int] = {}

    for run in runs:
        per_run.append(
            {
                "stamp": run.get("stamp"),
                "goal": (run.get("goal") or "")[:48],
                "cost_usd": run.get("total_cost_usd", 0),
                "duration_min": round(run.get("total_duration_ms", 0) / 60000, 1),
                "qa_fail_rounds": run.get("qa_fail_rounds", 0),
                "max_turns_hits": sum(
                    stats.get("max_turns_hits", 0)
                    for stats in run.get("per_agent", {}).values()
                ),
            }
        )
        for agent, stats in run.get("per_agent", {}).items():
            role = roles.setdefault(
                agent, {"sessions": 0, "cost_usd": 0.0, "max_turns_hits": 0}
            )
            role["sessions"] += stats.get("sessions", 0)
            role["cost_usd"] += stats.get("cost_usd", 0.0)
            role["max_turns_hits"] += stats.get("max_turns_hits", 0)

        workspace = run.get("workspace")
        if not workspace:
            continue
        seen_defect_ids: set[str] = set()
        for entry in load_sdk_entries(Path(workspace)):
            agent = entry.get("agent")
            if (
                agent in _ENGINEER_AGENTS
                and entry.get("num_turns") is not None
                and entry.get("max_turns")
            ):
                budget_samples.append((agent, entry["num_turns"], entry["max_turns"]))
            if agent == "qa_engineer" and entry.get("response"):
                for defect in parse_defects(entry["response"]):
                    # Ids are immutable across a run's rework loops (global
                    # rule 4), so dedupe per run — a re-verified D-1 in the
                    # second QA pass is the same defect, not a new one.
                    if defect.id in seen_defect_ids:
                        continue
                    seen_defect_ids.add(defect.id)
                    defects_by_severity[defect.severity] = (
                        defects_by_severity.get(defect.severity, 0) + 1
                    )
                    for owner in defect.owners:
                        defects_by_owner[owner] = defects_by_owner.get(owner, 0) + 1

    calibration = None
    if budget_samples:
        ratios = [used / budget for _, used, budget in budget_samples]
        exhausted = sum(1 for _, used, budget in budget_samples if used >= budget)
        calibration = {
            "sessions_measured": len(budget_samples),
            "avg_used_over_budget": round(sum(ratios) / len(ratios), 2),
            "exhausted": exhausted,
            "exhausted_rate": round(exhausted / len(budget_samples), 2),
        }

    return {
        "runs": per_run,
        "roles": {
            name: {**stats, "cost_usd": round(stats["cost_usd"], 2)}
            for name, stats in sorted(roles.items())
        },
        "budget_calibration": calibration,
        "defects_by_severity": defects_by_severity,
        "defects_by_owner": defects_by_owner,
    }


def format_report(report: dict) -> str:
    lines: list[str] = []
    runs = report["runs"]
    if not runs:
        return "No recorded runs yet — cross-run memory is written when a run completes."

    lines.append(f"# Looper KPI report — last {len(runs)} recorded run(s)\n")
    lines.append(f"{'stamp':<28} {'goal':<50} {'cost $':>7} {'min':>6} {'QA fails':>8} {'turn-outs':>9}")
    for r in runs:
        lines.append(
            f"{r['stamp']:<28} {r['goal']:<50} {r['cost_usd']:>7.2f} "
            f"{r['duration_min']:>6.1f} {r['qa_fail_rounds']:>8} {r['max_turns_hits']:>9}"
        )
    total_cost = sum(r["cost_usd"] for r in runs)
    lines.append(f"{'TOTAL':<79} {total_cost:>7.2f}\n")

    lines.append("## Per role (across all listed runs)")
    lines.append(f"{'role':<22} {'sessions':>8} {'cost $':>8} {'max-turns hits':>15}")
    for name, s in report["roles"].items():
        lines.append(f"{name:<22} {s['sessions']:>8} {s['cost_usd']:>8.2f} {s['max_turns_hits']:>15}")

    cal = report["budget_calibration"]
    lines.append("\n## Turn-budget calibration (engineer sessions)")
    if cal:
        lines.append(
            f"{cal['sessions_measured']} measured session(s): "
            f"avg turns used = {cal['avg_used_over_budget']:.0%} of budget, "
            f"{cal['exhausted']} exhausted their budget ({cal['exhausted_rate']:.0%})."
        )
    else:
        lines.append("No sessions with recorded num_turns yet (added to the SDK log recently).")

    lines.append("\n## Defects found by QA (unique per run)")
    if report["defects_by_severity"]:
        by_sev = ", ".join(f"{k}: {v}" for k, v in sorted(report["defects_by_severity"].items()))
        by_owner = ", ".join(f"{k}: {v}" for k, v in sorted(report["defects_by_owner"].items()))
        lines.append(f"By severity — {by_sev}")
        lines.append(f"By owning role — {by_owner or '(no owner tags parsed)'}")
    else:
        lines.append("None parsed.")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate KPIs across recorded Looper runs")
    parser.add_argument("--output-dir", default="output", help="Same dir the pipeline writes to")
    parser.add_argument("--window", type=int, default=20, help="How many recent runs to include")
    args = parser.parse_args()
    print(format_report(build_report(Path(args.output_dir), window=args.window)))


if __name__ == "__main__":
    main()
