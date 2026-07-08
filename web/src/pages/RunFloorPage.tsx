import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type RunSummary } from "../lib/api";
import { useRunEvents } from "../hooks/useRunEvents";
import { deriveStations } from "../lib/stations";
import { Schematic } from "../components/Schematic";
import { ActivityLog } from "../components/ActivityLog";
import { useAuth } from "../lib/AuthContext";

function formatElapsed(startIso: string, endIso?: string): string {
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  const totalSec = Math.max(0, Math.floor((end - start) / 1000));
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

export function RunFloorPage() {
  const { runId } = useParams<{ runId: string }>();
  const [run, setRun] = useState<RunSummary | null>(null);
  const [now, setNow] = useState(Date.now());
  const { events, connected, ended } = useRunEvents(runId);
  const { logout } = useAuth();

  useEffect(() => {
    if (!runId) return;
    api.getRun(runId).then(setRun).catch(() => {});
  }, [runId]);

  // Re-fetch run status once the stream tells us it ended, so the header's
  // status pill/elapsed-time freeze at the right values.
  useEffect(() => {
    if (ended && runId) api.getRun(runId).then(setRun).catch(() => {});
  }, [ended, runId]);

  useEffect(() => {
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, []);

  const stations = useMemo(() => deriveStations(events), [events]);

  if (!run) {
    return (
      <div className="app-shell">
        <div className="page-body"><div className="empty-state">Loading run…</div></div>
      </div>
    );
  }

  const isRunning = run.status === "running";
  const elapsed = formatElapsed(run.created_at, isRunning ? undefined : run.updated_at);
  void now; // referenced only to force a re-render tick while running

  return (
    <div className="app-shell">
      <nav className="nav">
        <span className="wordmark">AI<span>TEAM</span></span>
        <div className="nav-right">
          <Link to="/runs">← All runs</Link>
          <button className="btn" onClick={logout}>Sign out</button>
        </div>
      </nav>

      <div className="page-body">
        <div className="strip">
          <div className="wordmark">RUN FLOOR</div>
          <div className="strip-div" />
          <div className="strip-field">
            <span className="strip-label">Run</span>
            <span className="strip-value">{run.id.slice(0, 12)}</span>
          </div>
          <div className="strip-field goal-field">
            <span className="strip-label">Goal</span>
            <span className="strip-value">{run.goal}</span>
          </div>
          <div className="strip-field">
            <span className="strip-label">Elapsed</span>
            <span className="strip-value">{elapsed}</span>
          </div>
          <span className={`status-chip ${run.status}`}>
            <span className="dot" />
            {isRunning ? (connected ? "running" : "reconnecting…") : run.status}
          </span>
        </div>

        <div className="floor">
          <Schematic stations={stations} />
          <ActivityLog events={events} />
        </div>

        <div className="legend">
          <span className="legend-item"><span className="legend-led" style={{ background: "var(--idle)" }} />Idle</span>
          <span className="legend-item"><span className="legend-led" style={{ background: "var(--brass)", boxShadow: "0 0 0 3px rgba(227,162,60,0.18)" }} />Working</span>
          <span className="legend-item"><span className="legend-led" style={{ background: "var(--teal)" }} />Done</span>
          <span className="legend-item"><span className="legend-led" style={{ background: "var(--brick)" }} />Failed</span>
          <span className="legend-item"><span className="legend-line" style={{ borderColor: "var(--brass)" }} />Rework loop (QA_FAIL)</span>
          <span className="legend-item"><span className="legend-line" style={{ borderColor: "var(--brick)" }} />Re-scope loop (UAT_REJECTED)</span>
        </div>

        {run.status === "failed" && run.stop_reason && (
          <div className="log-panel">
            <div className="log-head"><span>Failure reason</span></div>
            <div style={{ padding: "14px 18px", color: "var(--brick)", fontSize: 12.5 }}>{run.stop_reason}</div>
          </div>
        )}
      </div>
    </div>
  );
}
