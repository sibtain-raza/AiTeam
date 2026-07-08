import type { RunEvent } from "../hooks/useRunEvents";

/** Fixed by pipeline.py's graph — not derived from events, since a role
 * with zero events yet (nothing has reached it) still needs to render as
 * an idle station, not be missing from the floor. */
export const ROLES = [
  { name: "product_manager", badge: "PM" },
  { name: "solution_architect", badge: "AR" },
  { name: "frontend_engineer", badge: "FE" },
  { name: "backend_engineer", badge: "BE" },
  { name: "devops_engineer", badge: "OP" },
  { name: "qa_engineer", badge: "QA" },
  { name: "uat_reviewer", badge: "UT" },
  { name: "release_reporter", badge: "RR" },
] as const;

export type StationLifecycle = "idle" | "working" | "done" | "failed";

export interface StationState {
  lifecycle: StationLifecycle;
  statusLine: string;
  loopCount: number;
}

function firstLine(text: string): string {
  const line = text.split("\n").find((l) => l.trim());
  return line ? line.replace(/^#+\s*/, "") : "done";
}

export function deriveStations(events: RunEvent[]): Record<string, StationState> {
  const result: Record<string, StationState> = {};

  for (const role of ROLES) {
    const roleEvents = events.filter((e) => e.source === role.name);
    const turnStarts = roleEvents.filter((e) => e.event_type === "turn_started").length;
    const last = roleEvents[roleEvents.length - 1];

    if (!last) {
      result[role.name] = { lifecycle: "idle", statusLine: "waiting", loopCount: 0 };
      continue;
    }

    if (last.event_type === "turn_started") {
      const startIdx = roleEvents.lastIndexOf(last);
      const toolCallsThisTurn = roleEvents.slice(startIdx).filter((e) => e.event_type === "tool_call");
      const latestTool = toolCallsThisTurn[toolCallsThisTurn.length - 1];
      result[role.name] = {
        lifecycle: "working",
        statusLine: latestTool ? `▶ ${latestTool.detail}` : "starting…",
        loopCount: turnStarts,
      };
    } else if (last.event_type === "error") {
      result[role.name] = { lifecycle: "failed", statusLine: last.detail || "session failed", loopCount: turnStarts };
    } else {
      result[role.name] = { lifecycle: "done", statusLine: firstLine(last.detail), loopCount: turnStarts };
    }
  }

  return result;
}
