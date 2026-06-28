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
  if (!res.ok && res.status !== 401) throw new Error(data.error || `HTTP ${res.status}`);
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
    req<{ id: number; alias: string; dialect: string; created_at: string }[]>("/connections"),
  deleteConnection: (id: number) => req(`/connections/${id}`, "DELETE"),

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
  }>("/query", "POST", payload),

  // Sessions
  listSessions: () =>
    req<{ id: number; connection_id: number | null; started_at: string; message_count: number }[]>("/sessions"),
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
}

export interface Connection {
  id: number;
  alias: string;
  dialect: string;
  created_at: string;
}
