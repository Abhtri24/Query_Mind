const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function req<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const opts: RequestInit = {
    method,
    credentials: "include",
    headers: {},
  };
  if (body) {
    (opts.headers as Record<string, string>)["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
 const res = await fetch(BASE + path, opts);
const data = await res.json();

if (!res.ok) {
    throw new Error(data.error || `HTTP ${res.status}`);
}

return data as T;
}

export const api = {
  // Auth
  signup: (username: string, password: string, email?: string) =>
    req("/auth/signup", "POST", { username, password, email }),
  login: (username: string, password: string) =>
    req("/auth/login", "POST", { username, password }),
  logout: () => req("/auth/logout", "POST"),
  me: () => req<{ user_id: number; username: string }>("/auth/me"),

  // Budget
  budget: () => req<{ tokens_used: number; budget: number; remaining: number; date: string }>("/budget"),

  // Connections
  addConnection: (alias: string, uri: string) =>
    req<{ id: number; alias: string; dialect: string }>("/connections", "POST", { alias, uri }),
  listConnections: () =>
    req<Connection[]>("/connections").then(d => Array.isArray(d) ? d : []),
  deleteConnection: (id: number) => req(`/connections/${id}`, "DELETE"),
  exploreConnection: (id: number, payload?: { api_key?: string; provider?: string }) =>
    req<{ message: string; table_count: number; db_summary: string; explored_at: string }>(
      `/connections/${id}/explore`, "POST", payload
    ),
  getConnectionMemory: (id: number) =>
    req<{ exists: boolean; [key: string]: unknown }>(`/connections/${id}/memory`),
  deleteConnectionMemory: (id: number) =>
    req(`/connections/${id}/memory`, "DELETE"),

  // Query
  query: (payload: {
    question: string;
    connection_id?: number;
    uri?: string;
    api_key?: string;
    provider?: string;
    model?: string;
  }) => req<{
    success: boolean;
    sql: string | null;
    results: unknown;
    explanation: string | null;
    error: string | null;
    retries: number;
    healing_log: string[];
    response_time_s: number;
    message_id: number;
    results_truncated: boolean;
    schema_source: string;
    clarification_needed: string | null;
    plan: string[];
    cached: boolean;
  }>("/query", "POST", payload),

  // Sessions
  listSessions: () =>
    req<{ id: number; connection_id: number | null; started_at: string; message_count: number }[]>("/sessions").then(d => Array.isArray(d) ? d : []),
  getSession: (id: number) =>
    req<{ session_id: number; messages: Message[] }>(`/sessions/${id}`),
  deleteSession: (id: number) => req(`/sessions/${id}`, "DELETE"),
  clearSession: () => req("/sessions/current/clear", "POST"),
};

export interface Message {
  id: number;
  question: string;
  sql: string | null;
  answer: string | null;
  error: string | null;
  retries: number;
  response_time: number | null;
  created_at: string | null;
  results_truncated?: boolean;
}

export interface Connection {
  id: number;
  alias: string;
  dialect: string;
  created_at: string;
  has_memory?: boolean;
}
