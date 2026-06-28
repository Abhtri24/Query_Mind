"use client";
import { useEffect, useRef, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { api, type Connection, type Message } from "@/lib/api";

// ── Types ──────────────────────────────────────────────────────────────────
interface QueryResult {
  success: boolean;
  sql: string | null;
  results: unknown;
  explanation: string | null;
  error: string | null;
  retries: number;
  healing_log: string[];
  response_time_s: number;
}

interface ChatEntry {
  question: string;
  result: QueryResult | null;
  loading: boolean;
}

// ── SQL highlighter ────────────────────────────────────────────────────────
function highlightSql(sql: string) {
  const kws = /\b(SELECT|FROM|WHERE|JOIN|ON|GROUP BY|ORDER BY|LIMIT|AS|AND|OR|INNER|LEFT|RIGHT|COUNT|SUM|AVG|MAX|MIN|HAVING|DISTINCT|LIKE|IN|NOT|IS|NULL|BY|WITH|UNION|CASE|WHEN|THEN|ELSE|END)\b/g;
  return sql.replace(kws, '<span class="sql-kw">$1</span>');
}

// ── Result table ───────────────────────────────────────────────────────────
function ResultTable({ raw }: { raw: unknown }) {
  if (!raw) return null;
  if (typeof raw === "string" && raw.length > 2) {
    try {
      const rowStrs = raw.replace(/^\[|\]$/g, "").split(/\),\s*\(/);
      const rows = rowStrs.map(r =>
        r.replace(/^\(?|\)?$/g, "").split(/,(?=(?:[^']*'[^']*')*[^']*$)/).map(s => s.trim().replace(/^'|'$/g, ""))
      );
      if (rows.length && rows[0].length) {
        const cols = rows[0].map((_, i) => `col${i + 1}`);
        return (
          <div style={{ overflowX: "auto", marginTop: 8 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
              <thead>
                <tr>{cols.map(c => <th key={c} style={{ textAlign: "left", padding: "6px 9px", fontWeight: 700, color: "var(--ink)", borderBottom: "2px solid var(--hairline)", background: "var(--surface)", fontSize: 10, letterSpacing: ".04em" }}>{c}</th>)}</tr>
              </thead>
              <tbody>
                {rows.slice(0, 30).map((row, i) => (
                  <tr key={i}>{row.map((v, j) => <td key={j} style={{ padding: "6px 9px", borderBottom: "1px solid var(--hairline)", color: "var(--body)", fontSize: 11 }}>{v}</td>)}</tr>
                ))}
                {rows.length > 30 && <tr><td colSpan={cols.length} style={{ padding: "6px 9px", fontSize: 10, color: "var(--mute)" }}>…{rows.length - 30} more rows</td></tr>}
              </tbody>
            </table>
          </div>
        );
      }
    } catch {}
    return <div style={{ marginTop: 6, background: "var(--surface-card)", borderRadius: 4, padding: "7px 10px", fontSize: 11, color: "var(--mute)", wordBreak: "break-all" }}>{String(raw).substring(0, 600)}</div>;
  }
  if (Array.isArray(raw) && raw.length) {
    const cols = Object.keys(raw[0] as object);
    return (
      <div style={{ overflowX: "auto", marginTop: 8 }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
          <thead><tr>{cols.map(c => <th key={c} style={{ textAlign: "left", padding: "6px 9px", fontWeight: 700, color: "var(--ink)", borderBottom: "2px solid var(--hairline)", background: "var(--surface)", fontSize: 10 }}>{c}</th>)}</tr></thead>
          <tbody>
            {(raw as Record<string, unknown>[]).slice(0, 30).map((row, i) => (
              <tr key={i}>{cols.map(c => <td key={c} style={{ padding: "6px 9px", borderBottom: "1px solid var(--hairline)", color: "var(--body)" }}>{String(row[c] ?? "")}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return null;
}

// ── Main dashboard ─────────────────────────────────────────────────────────
export default function Dashboard() {
  const router = useRouter();
  const [user, setUser] = useState<string>("");
  const [tab, setTab] = useState<"query" | "connect" | "history">("query");

  // Connections
  const [connections, setConnections] = useState<Connection[]>([]);
  const [activeConn, setActiveConn] = useState<Connection | null>(null);

  // Connect form
  const [dialect, setDialect] = useState<"mysql" | "postgresql" | "sqlite">("mysql");
  const [alias, setAlias] = useState("");
  const [mHost, setMHost] = useState("localhost");
  const [mUser, setMUser] = useState("");
  const [mPass, setMPass] = useState("");
  const [mDb, setMDb] = useState("");
  const [pgHost, setPgHost] = useState("localhost");
  const [pgUser, setPgUser] = useState("");
  const [pgPass, setPgPass] = useState("");
  const [pgDb, setPgDb] = useState("");
  const [sqlitePath, setSqlitePath] = useState("");
  const [uri, setUri] = useState("");
  const [uriManual, setUriManual] = useState(false);
  const [connStatus, setConnStatus] = useState<{ msg: string; type: "ok" | "err" | "info" } | null>(null);

  // Query
  const [provider, setProvider] = useState<"groq" | "gemini">("groq");
  const [apiKey, setApiKey] = useState("");
  const [question, setQuestion] = useState("");
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [budget, setBudget] = useState<{ remaining: number; budget: number } | null>(null);

  // History
  const [sessions, setSessions] = useState<{ id: number; message_count: number; started_at: string }[]>([]);

  // Notify
  const [notif, setNotif] = useState<{ msg: string; show: boolean }>({ msg: "", show: false });

  const messagesEnd = useRef<HTMLDivElement>(null);

  const notify = (msg: string) => {
    setNotif({ msg, show: true });
    setTimeout(() => setNotif(n => ({ ...n, show: false })), 2800);
  };

  useEffect(() => {
    api.me().then(d => setUser(d.username)).catch(() => router.push("/login"));
    api.listConnections().then(setConnections).catch(() => {});
    api.budget().then(setBudget).catch(() => {});
  }, [router]);

  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [chat]);

  // Build URI from fields
  useEffect(() => {
    if (uriManual) return;
    let u = "";
    if (dialect === "mysql") u = `mysql+pymysql://${mUser}:${mPass}@${mHost || "localhost"}/${mDb}`;
    else if (dialect === "postgresql") u = `postgresql+psycopg2://${pgUser}:${pgPass}@${pgHost || "localhost"}/${pgDb}`;
    else u = `sqlite:///${sqlitePath}`;
    setUri(u);
  }, [dialect, mHost, mUser, mPass, mDb, pgHost, pgUser, pgPass, pgDb, sqlitePath, uriManual]);

  const loadConnections = useCallback(() => {
    api.listConnections().then(setConnections).catch(() => {});
  }, []);

  const saveConn = async () => {
    if (!alias || !uri) { setConnStatus({ msg: "alias and URI are required", type: "err" }); return; }
    try {
      setConnStatus({ msg: "connecting…", type: "info" });
      const c = await api.addConnection(alias, uri) as Connection;
      setConnStatus({ msg: `[+] "${c.alias}" saved`, type: "ok" });
      loadConnections();
      notify("saved: " + c.alias);
    } catch (e: unknown) {
      setConnStatus({ msg: "[-] " + (e instanceof Error ? e.message : "error"), type: "err" });
    }
  };

  const deleteConn = async (id: number) => {
    await api.deleteConnection(id);
    if (activeConn?.id === id) setActiveConn(null);
    loadConnections();
    notify("connection removed");
  };

  const sendQuery = async () => {
    if (busy || !question.trim()) return;
    if (!activeConn) { setTab("connect"); notify("select a database first"); return; }
    setBusy(true);
    const q = question.trim();
    setQuestion("");
    setChat(c => [...c, { question: q, result: null, loading: true }]);
    try {
      const r = await api.query({ question: q, connection_id: activeConn.id, api_key: apiKey || undefined, provider });
      setChat(c => c.map((e, i) => i === c.length - 1 ? { ...e, result: r, loading: false } : e));
      api.budget().then(setBudget).catch(() => {});
    } catch (e: unknown) {
      const errResult: QueryResult = { success: false, sql: null, results: null, explanation: null, error: e instanceof Error ? e.message : "error", retries: 0, healing_log: [], response_time_s: 0 };
      setChat(c => c.map((e, i) => i === c.length - 1 ? { ...e, result: errResult, loading: false } : e));
    }
    setBusy(false);
  };

  const loadHistory = () => {
    api.listSessions().then(setSessions).catch(() => {});
  };

  const replaySession = async (id: number) => {
    try {
      const d = await api.getSession(id);
      const entries: ChatEntry[] = d.messages.map((m: Message) => ({
        question: m.question,
        loading: false,
        result: { success: !m.error, sql: m.sql, results: null, explanation: m.answer, error: m.error, retries: m.retries || 0, healing_log: [], response_time_s: m.response_time || 0 },
      }));
      setChat(entries);
      setTab("query");
    } catch { notify("could not load session"); }
  };

  // ── Styles ────────────────────────────────────────────────────────────────
  const mono = "var(--font-mono)";
  const S = {
    wrap:    { fontFamily: mono, background: "var(--canvas)", color: "var(--ink)", minHeight: "100vh", display: "flex", flexDirection: "column" as const },
    nav:     { position: "sticky" as const, top: 0, zIndex: 100, background: "var(--canvas)", borderBottom: "1px solid var(--hairline)", padding: "0 28px", height: 50, display: "flex", alignItems: "center", justifyContent: "space-between" },
    wm:      { fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", gap: 6 },
    br:      { color: "var(--mute)", fontWeight: 400 },
    tabBar:  { display: "flex", gap: 2 },
    tab:     (active: boolean): React.CSSProperties => ({ fontSize: 12, fontWeight: 500, padding: "5px 12px", borderRadius: 4, background: active ? "var(--surface)" : "none", color: active ? "var(--ink)" : "var(--mute)", border: "none", cursor: "pointer", fontFamily: mono }),
    navR:    { display: "flex", alignItems: "center", gap: 8 },
    user:    { fontSize: 11, color: "var(--mute)" },
    logout:  { fontSize: 11, color: "var(--mute)", background: "none", border: "none", cursor: "pointer", fontFamily: mono },
    main:    { flex: 1, display: "flex", overflow: "hidden" as const },
    // Query layout
    sidebar: { width: 200, borderRight: "1px solid var(--hairline)", padding: "20px 16px", flexShrink: 0 as const, overflowY: "auto" as const },
    sblabel: { fontSize: 10, fontWeight: 700, color: "var(--mute)", letterSpacing: ".07em", marginBottom: 6 },
    connBox: { background: "var(--surface)", borderRadius: 4, padding: "9px 11px", border: "1px solid var(--hairline)", marginBottom: 8 },
    btnSm:   (variant: "ink" | "ghost" | "green"): React.CSSProperties => ({
      fontFamily: mono, fontSize: 11, fontWeight: 500, padding: "4px 10px", borderRadius: 4, cursor: "pointer", border: "none",
      ...(variant === "ink" ? { background: "var(--ink)", color: "#fff" } : variant === "green" ? { background: "var(--success)", color: "#fff" } : { background: "none", color: "var(--mute)", border: "1px solid var(--hairline)" }),
    }),
    pTabs:   { display: "flex", gap: 4, marginTop: 6 },
    pTab:    (active: boolean): React.CSSProperties => ({ flex: 1, fontFamily: mono, fontSize: 10, fontWeight: 500, padding: "4px", borderRadius: 4, border: "1px solid var(--hairline)", background: active ? "var(--ink)" : "none", color: active ? "#fff" : "var(--mute)", cursor: "pointer" }),
    budgetB: { marginTop: 10, padding: "7px 9px", background: "var(--surface)", borderRadius: 4 },
    track:   { background: "var(--surface-card)", borderRadius: 2, height: 3, margin: "4px 0" },
    fill:    (pct: number): React.CSSProperties => ({ height: 3, borderRadius: 2, width: `${pct}%`, background: pct > 50 ? "var(--success)" : pct > 20 ? "var(--warning)" : "var(--danger)", transition: "width .4s" }),
    // Chat
    chatArea:  { flex: 1, display: "flex", flexDirection: "column" as const, overflow: "hidden" as const },
    chatScroll:{ flex: 1, overflowY: "auto" as const, padding: "20px 24px" },
    inputWrap: { borderTop: "1px solid var(--hairline)", padding: "12px 20px", background: "var(--canvas)" },
    inputBox:  { border: "1px solid var(--hairline)", borderRadius: 4, background: "var(--canvas)", transition: "border-color .15s" },
    textarea:  { width: "100%", resize: "none" as const, border: "none", outline: "none", background: "none", fontFamily: mono, fontSize: 13, color: "var(--ink)", padding: "10px 12px", minHeight: 60, borderBottom: "1px solid var(--hairline)", display: "block" },
    inputFtr:  { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 10px" },
    sendBtn:   (disabled: boolean): React.CSSProperties => ({ background: "var(--ink)", color: "#fff", fontFamily: mono, fontSize: 11, fontWeight: 500, padding: "5px 13px", borderRadius: 4, border: "none", cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1 }),
    // Message
    msg:       { border: "1px solid var(--hairline)", borderRadius: 4, overflow: "hidden", marginBottom: 12 },
    msgQ:      { padding: "9px 12px", background: "var(--surface)", fontSize: 12, fontWeight: 500, color: "var(--ink)" },
    msgBody:   { padding: "12px" },
    sqlBox:    { background: "var(--dark)", borderRadius: 4, padding: "10px 12px", fontSize: 11, color: "#d0d0d0", marginBottom: 10, whiteSpace: "pre-wrap" as const, lineHeight: 1.65, overflowX: "auto" as const },
    sqlLabel:  { fontSize: 9, color: "var(--ash)", marginBottom: 6, letterSpacing: ".06em", fontWeight: 700 },
    expl:      { fontSize: 13, color: "var(--body)", marginBottom: 10, lineHeight: 1.7 },
    errBox:    { background: "rgba(255,59,48,.06)", border: "1px solid rgba(255,59,48,.15)", borderRadius: 4, padding: "8px 10px", fontSize: 12, color: "#cc2d22", marginBottom: 8 },
    healLog:   { marginTop: 6, padding: "7px 10px", background: "var(--surface)", borderRadius: 4 },
    healLabel: { fontSize: 9, fontWeight: 700, color: "var(--mute)", letterSpacing: ".06em", marginBottom: 3 },
    healEntry: { fontSize: 10, color: "var(--mute)", padding: "2px 0", borderBottom: "1px solid var(--hairline)" },
    metaRow:   { display: "flex", alignItems: "center", gap: 8, marginTop: 8, paddingTop: 8, borderTop: "1px solid var(--hairline)" },
    badge:     (type: "ok" | "heal" | "err"): React.CSSProperties => ({
      fontSize: 9, fontWeight: 700, padding: "2px 6px", borderRadius: 4, letterSpacing: ".04em",
      ...(type === "ok" ? { background: "rgba(48,209,88,.1)", color: "#1a7a3a", border: "1px solid rgba(48,209,88,.2)" } : type === "heal" ? { background: "rgba(255,159,10,.1)", color: "#7a4000", border: "1px solid rgba(255,159,10,.2)" } : { background: "rgba(255,59,48,.07)", color: "#cc2d22", border: "1px solid rgba(255,59,48,.18)" }),
    }),
    // Connect
    page:    { padding: "28px 32px", maxWidth: 600, overflowY: "auto" as const, flex: 1 },
    ph:      { borderBottom: "1px solid var(--hairline)", paddingBottom: 16, marginBottom: 24 },
    pTitle:  { fontSize: 14, fontWeight: 700 },
    pSub:    { fontSize: 11, color: "var(--mute)", marginTop: 3 },
    fg:      { marginBottom: 16 },
    lbl:     { display: "block" as const, fontSize: 11, fontWeight: 500, marginBottom: 4 },
    inp:     { width: "100%", background: "var(--surface)", color: "var(--ink)", fontFamily: mono, fontSize: 12, border: "1px solid var(--hairline)", borderRadius: 4, padding: "8px 10px", outline: "none" },
    dTabs:   { display: "flex", border: "1px solid var(--hairline)", borderRadius: 4, overflow: "hidden", marginBottom: 4 },
    dTab:    (a: boolean): React.CSSProperties => ({ flex: 1, fontFamily: mono, fontSize: 11, fontWeight: 500, background: a ? "var(--ink)" : "none", color: a ? "#fff" : "var(--mute)", border: "none", padding: "6px", cursor: "pointer" }),
    statMsg: (t: "ok" | "err" | "info"): React.CSSProperties => ({ fontSize: 11, padding: "8px 10px", borderRadius: 4, marginTop: 8, ...(t === "ok" ? { background: "rgba(48,209,88,.1)", color: "#1a7a3a", border: "1px solid rgba(48,209,88,.2)" } : t === "err" ? { background: "rgba(255,59,48,.07)", color: "#cc2d22", border: "1px solid rgba(255,59,48,.18)" } : { background: "var(--surface)", color: "var(--mute)", border: "1px solid var(--hairline)" }) }),
    connItem:{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "11px 0", borderBottom: "1px solid var(--hairline)" },
    // History
    sessRow: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "11px 0", borderBottom: "1px solid var(--hairline)" },
  };

  const budgetPct = budget ? Math.max(0, Math.round(budget.remaining / budget.budget * 100)) : 100;

  return (
    <div style={S.wrap}>
      {/* NAV */}
      <nav style={S.nav}>
        <div style={S.wm}><span style={S.br}>[</span>QM<span style={S.br}>]</span> QueryMind</div>
        <div style={S.tabBar}>
          {(["query", "connect", "history"] as const).map(t => (
            <button key={t} style={S.tab(tab === t)} onClick={() => { setTab(t); if (t === "history") loadHistory(); }}>{t}</button>
          ))}
        </div>
        <div style={S.navR}>
          {user && <span style={S.user}>[{user}]</span>}
          <button style={S.logout} onClick={async () => { await api.logout(); router.push("/"); }}>logout</button>
        </div>
      </nav>

      <div style={S.main}>

        {/* ── QUERY TAB ── */}
        {tab === "query" && (
          <>
            {/* Sidebar */}
            <div style={S.sidebar}>
              <div style={S.sblabel}>active db</div>
              <div style={S.connBox}>
                <div style={{ fontSize: 12, fontWeight: 700 }}>{activeConn?.alias || "none"}</div>
                <div style={{ fontSize: 10, color: "var(--mute)", marginTop: 2 }}>{activeConn?.dialect || "select from connect tab"}</div>
              </div>
              <button style={{ ...S.btnSm("ghost"), width: "100%", marginBottom: 16, fontSize: 10 }} onClick={() => setTab("connect")}>change database</button>

              <div style={S.sblabel}>llm provider</div>
              <div style={S.pTabs}>
                {(["groq", "gemini"] as const).map(p => (
                  <button key={p} style={S.pTab(provider === p)} onClick={() => setProvider(p)}>{p}</button>
                ))}
              </div>
              <input
                style={{ ...S.inp, fontSize: 10, marginTop: 6 }}
                type="password"
                placeholder="api key (optional)"
                value={apiKey}
                onChange={e => setApiKey(e.target.value)}
              />
              <div style={{ fontSize: 9, color: "var(--mute)", marginTop: 4 }}>no key → uses hosted budget</div>

              {budget && (
                <div style={S.budgetB}>
                  <div style={{ fontSize: 9, fontWeight: 700, color: "var(--mute)", letterSpacing: ".06em" }}>HOSTED BUDGET</div>
                  <div style={S.track}><div style={S.fill(budgetPct)} /></div>
                  <div style={{ fontSize: 9, color: "var(--mute)" }}>{budget.remaining.toLocaleString()} / {budget.budget.toLocaleString()} tokens</div>
                </div>
              )}

              <button style={{ ...S.btnSm("ghost"), width: "100%", marginTop: 12, fontSize: 10 }}
                onClick={async () => { await api.clearSession(); setChat([]); notify("cleared [x]"); }}>
                clear conversation [x]
              </button>
            </div>

            {/* Chat area */}
            <div style={S.chatArea}>
              <div style={S.chatScroll}>
                {chat.length === 0 ? (
                  <div style={{ textAlign: "center", padding: "60px 0" }}>
                    <div style={{ fontSize: 22, color: "var(--surface-card)", letterSpacing: 4, marginBottom: 10 }}>[_]</div>
                    <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 4 }}>ready to query</div>
                    <div style={{ fontSize: 12, color: "var(--mute)" }}>
                      {activeConn ? `connected to ${activeConn.alias} — ask anything` : "connect a database from the connect tab"}
                    </div>
                  </div>
                ) : (
                  chat.map((entry, i) => (
                    <div key={i} style={S.msg} className="anim-fade-up">
                      <div style={S.msgQ}>&gt; {entry.question}</div>
                      <div style={S.msgBody}>
                        {entry.loading ? (
                          <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--mute)" }}>
                            <span style={{ width: 12, height: 12, border: "2px solid var(--hairline)", borderTopColor: "var(--ink)", borderRadius: "50%", display: "inline-block", animation: "spin .7s linear infinite" }} />
                            generating SQL…
                          </div>
                        ) : entry.result ? (
                          <>
                            {entry.result.sql && (
                              <div style={S.sqlBox}>
                                <div style={S.sqlLabel}>SQL</div>
                                <div dangerouslySetInnerHTML={{ __html: highlightSql(entry.result.sql) }} />
                              </div>
                            )}
                            {entry.result.explanation && <div style={S.expl}>{entry.result.explanation}</div>}
                            {!entry.result.success && entry.result.error && <div style={S.errBox}>[-] {entry.result.error}</div>}
                            <ResultTable raw={entry.result.results} />
                            {entry.result.healing_log && entry.result.healing_log.length > 1 && (
                              <div style={S.healLog}>
                                <div style={S.healLabel}>SELF-HEAL LOG</div>
                                {entry.result.healing_log.map((l, j) => <div key={j} style={{ ...S.healEntry, borderBottom: j === entry.result!.healing_log.length - 1 ? "none" : "1px solid var(--hairline)" }}>{l}</div>)}
                              </div>
                            )}
                            <div style={S.metaRow}>
                              <span style={S.badge(entry.result.retries > 0 ? "heal" : entry.result.success ? "ok" : "err")}>
                                {entry.result.retries > 0 ? `[healed: ${entry.result.retries} ${entry.result.retries === 1 ? "retry" : "retries"}]` : entry.result.success ? "[ok]" : "[failed]"}
                              </span>
                              {entry.result.response_time_s > 0 && <span style={{ fontSize: 10, color: "var(--mute)" }}>{entry.result.response_time_s}s</span>}
                              <span style={{ fontSize: 10, color: "var(--mute)" }}>{provider}</span>
                            </div>
                          </>
                        ) : null}
                      </div>
                    </div>
                  ))
                )}
                <div ref={messagesEnd} />
              </div>

              {/* Input */}
              <div style={S.inputWrap}>
                <div style={S.inputBox}>
                  <textarea
                    style={S.textarea}
                    placeholder={`ask anything about your database…\ne.g. "how many users signed up last week?"`}
                    value={question}
                    onChange={e => setQuestion(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendQuery(); } }}
                  />
                  <div style={S.inputFtr}>
                    <span style={{ fontSize: 10, color: "var(--ash)" }}>shift+enter = new line · enter = send</span>
                    <button style={S.sendBtn(busy || !question.trim())} onClick={sendQuery} disabled={busy || !question.trim()}>send →</button>
                  </div>
                </div>
              </div>
            </div>
          </>
        )}

        {/* ── CONNECT TAB ── */}
        {tab === "connect" && (
          <div style={S.page}>
            <div style={S.ph}>
              <div style={S.pTitle}>connect a database</div>
              <div style={S.pSub}>connection is tested before saving</div>
            </div>

            <div style={S.fg}>
              <label style={S.lbl}>database type</label>
              <div style={S.dTabs}>
                {(["mysql", "postgresql", "sqlite"] as const).map(d => (
                  <button key={d} style={S.dTab(dialect === d)} onClick={() => { setDialect(d); setUriManual(false); }}>{d}</button>
                ))}
              </div>
            </div>

            <div style={S.fg}>
              <label style={S.lbl}>alias</label>
              <input style={S.inp} placeholder="my-prod-db" value={alias} onChange={e => setAlias(e.target.value)} />
            </div>

            {dialect === "mysql" && <>
              <div style={S.fg}><label style={S.lbl}>host</label><input style={S.inp} value={mHost} onChange={e => setMHost(e.target.value)} /></div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div style={S.fg}><label style={S.lbl}>user</label><input style={S.inp} value={mUser} onChange={e => setMUser(e.target.value)} /></div>
                <div style={S.fg}><label style={S.lbl}>password</label><input style={S.inp} type="password" value={mPass} onChange={e => setMPass(e.target.value)} /></div>
              </div>
              <div style={S.fg}><label style={S.lbl}>database</label><input style={S.inp} value={mDb} onChange={e => setMDb(e.target.value)} /></div>
            </>}

            {dialect === "postgresql" && <>
              <div style={S.fg}><label style={S.lbl}>host</label><input style={S.inp} value={pgHost} onChange={e => setPgHost(e.target.value)} /></div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div style={S.fg}><label style={S.lbl}>user</label><input style={S.inp} value={pgUser} onChange={e => setPgUser(e.target.value)} /></div>
                <div style={S.fg}><label style={S.lbl}>password</label><input style={S.inp} type="password" value={pgPass} onChange={e => setPgPass(e.target.value)} /></div>
              </div>
              <div style={S.fg}><label style={S.lbl}>database</label><input style={S.inp} value={pgDb} onChange={e => setPgDb(e.target.value)} /></div>
            </>}

            {dialect === "sqlite" && (
              <div style={S.fg}><label style={S.lbl}>file path</label><input style={S.inp} placeholder="/data/mydb.db" value={sqlitePath} onChange={e => setSqlitePath(e.target.value)} /></div>
            )}

            <div style={S.fg}>
              <label style={S.lbl}>URI <span style={{ color: "var(--mute)", fontWeight: 400 }}>— or paste directly</span></label>
              <input style={S.inp} value={uri} onChange={e => { setUri(e.target.value); setUriManual(true); }} placeholder="postgresql+psycopg2://user:pass@host/db" />
            </div>

            {connStatus && <div style={S.statMsg(connStatus.type)}>{connStatus.msg}</div>}

            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button style={{ ...S.btnSm("ink"), padding: "8px 16px", fontSize: 12 }} onClick={saveConn}>test + save</button>
            </div>

            {/* Saved connections */}
            <div style={{ marginTop: 32 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: "var(--mute)", letterSpacing: ".06em", marginBottom: 12 }}>SAVED CONNECTIONS</div>
              {connections.length === 0 ? (
                <div style={{ textAlign: "center", padding: "24px 0", fontSize: 12, color: "var(--mute)" }}>no connections yet</div>
              ) : connections.map(c => (
                <div key={c.id} style={S.connItem}>
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 700 }}>{c.alias}</div>
                    <div style={{ fontSize: 11, color: "var(--mute)", marginTop: 2 }}>{c.dialect}</div>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    <button style={S.btnSm(activeConn?.id === c.id ? "green" : "ink")} onClick={() => { setActiveConn(c); setTab("query"); notify("active: " + c.alias); }}>
                      {activeConn?.id === c.id ? "active ✓" : "use this"}
                    </button>
                    <button style={S.btnSm("ghost")} onClick={() => deleteConn(c.id)}>remove</button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── HISTORY TAB ── */}
        {tab === "history" && (
          <div style={S.page}>
            <div style={S.ph}>
              <div style={S.pTitle}>session history</div>
              <div style={S.pSub}>click any session to replay it in the query view</div>
            </div>
            {sessions.length === 0 ? (
              <div style={{ textAlign: "center", padding: "40px 0", fontSize: 12, color: "var(--mute)" }}>no sessions yet</div>
            ) : sessions.map(s => (
              <div key={s.id} style={S.sessRow}>
                <div>
                  <div style={{ fontSize: 12, fontWeight: 500 }}>session #{s.id}</div>
                  <div style={{ fontSize: 11, color: "var(--mute)", marginTop: 2 }}>{s.message_count} {s.message_count === 1 ? "query" : "queries"} · {new Date(s.started_at).toLocaleString()}</div>
                </div>
                <button style={{ fontSize: 11, color: "var(--accent)", background: "none", border: "none", cursor: "pointer", fontFamily: mono }} onClick={() => replaySession(s.id)}>view →</button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Notify */}
      <div style={{ position: "fixed", bottom: 20, right: 20, zIndex: 9999, background: "var(--ink)", color: "#fff", fontFamily: mono, fontSize: 11, padding: "9px 16px", borderRadius: 4, opacity: notif.show ? 1 : 0, transform: notif.show ? "translateY(0)" : "translateY(5px)", transition: "all .2s", pointerEvents: "none" }}>
        {notif.msg}
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
