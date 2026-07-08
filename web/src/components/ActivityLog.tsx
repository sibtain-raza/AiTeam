import type { ReactNode } from "react";
import type { RunEvent } from "../hooks/useRunEvents";

function timeOf(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderDetail(event: RunEvent): ReactNode {
  const firstLine = (event.detail || "").split("\n").find((l) => l.trim()) ?? "";
  if (event.event_type === "turn_completed" && /\bQA_FAIL\b/.test(event.detail)) {
    return <span className="fail">QA_FAIL</span>;
  }
  if (event.event_type === "turn_completed" && /\b(QA_PASS|UAT_APPROVED)\b/.test(event.detail)) {
    return <span className="pass">{firstLine || "done"}</span>;
  }
  if (event.event_type === "tool_call") {
    return <span className="live">▶ {event.detail}</span>;
  }
  if (event.event_type === "run_failed" || event.event_type === "error") {
    return <span className="fail">{firstLine || event.detail}</span>;
  }
  return firstLine || event.event_type;
}

export function ActivityLog({ events }: { events: RunEvent[] }) {
  const ordered = [...events].reverse();
  return (
    <div className="log-panel">
      <div className="log-head">
        <span>Activity</span>
        <span>{events.length} events</span>
      </div>
      <div className="log-body">
        {ordered.length === 0 && <div className="empty-state">Waiting for the first event…</div>}
        {ordered.map((event, i) => (
          <div className={`log-line ${i === 0 ? "now" : ""}`} key={event.id}>
            <span className="log-t">{timeOf(event.at)}</span>
            <span className="log-who">{event.source}</span>
            <span className="log-msg">{renderDetail(event)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
