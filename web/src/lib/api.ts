export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

const TOKEN_KEY = "aiteam_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // response wasn't JSON — fall back to statusText
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export interface RunSummary {
  id: string;
  goal: string;
  status: "running" | "completed" | "failed";
  stop_reason: string | null;
  created_at: string;
  updated_at: string;
}

export const api = {
  signup: (email: string, password: string) =>
    request<{ access_token: string }>("/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  login: (email: string, password: string) =>
    request<{ access_token: string }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  createRun: (goal: string) =>
    request<RunSummary>("/runs", { method: "POST", body: JSON.stringify({ goal }) }),
  listRuns: () => request<RunSummary[]>("/runs"),
  getRun: (id: string) => request<RunSummary>(`/runs/${id}`),
  eventsUrl: (runId: string) => {
    const token = getToken() ?? "";
    return `${API_BASE}/runs/${runId}/events?token=${encodeURIComponent(token)}`;
  },
};
