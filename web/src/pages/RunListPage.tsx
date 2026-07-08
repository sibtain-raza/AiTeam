import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, type RunSummary } from "../lib/api";
import { useAuth } from "../lib/AuthContext";

function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function RunListPage() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [goal, setGoal] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { logout } = useAuth();
  const navigate = useNavigate();

  async function refresh() {
    const list = await api.listRuns();
    setRuns(list);
  }

  useEffect(() => {
    refresh().catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load runs"));
    const interval = setInterval(() => refresh().catch(() => {}), 5000);
    return () => clearInterval(interval);
  }, []);

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    if (!goal.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const run = await api.createRun(goal.trim());
      setGoal("");
      navigate(`/runs/${run.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to start run");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="app-shell">
      <nav className="nav">
        <span className="wordmark">AI<span>TEAM</span></span>
        <div className="nav-right">
          <span>Web UI · Phase 1 (shared Claude Code login — see README)</span>
          <button className="btn" onClick={logout}>Sign out</button>
        </div>
      </nav>

      <div className="page-body">
        <form className="new-run-card" onSubmit={onCreate}>
          <div className="field">
            <label htmlFor="goal">New run — goal</label>
            <input
              id="goal"
              placeholder="Build a URL shortener with custom aliases and click analytics."
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
            />
          </div>
          <button type="submit" className="btn btn-primary" disabled={creating || !goal.trim()}>
            {creating ? "Starting…" : "Start run"}
          </button>
        </form>

        {error && <div className="error-text">{error}</div>}

        <div className="run-table">
          <div className="run-row head">
            <span>Goal</span>
            <span>Status</span>
            <span>Started</span>
          </div>
          {runs === null && <div className="empty-state">Loading…</div>}
          {runs !== null && runs.length === 0 && (
            <div className="empty-state">No runs yet — start one above.</div>
          )}
          {runs?.map((run) => (
            <div className="run-row" key={run.id}>
              <Link className="goal-cell" to={`/runs/${run.id}`}>{run.goal}</Link>
              <span className={`status-chip ${run.status}`}>
                <span className="dot" />
                {run.status}
              </span>
              <span className="time-cell">{relativeTime(run.created_at)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
