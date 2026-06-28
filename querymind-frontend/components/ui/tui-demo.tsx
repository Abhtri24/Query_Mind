"use client";
import { useEffect, useState } from "react";

const SEQUENCES = [
  {
    query: "show me authors ranked by total engagement time",
    steps: [
      { type: "sql",    text: "SELECT a.username, SUM(ee.read_duration_seconds) AS total\nFROM authors a\nJOIN books b ON a.id = b.author_id\nJOIN snippets s ON b.id = s.book_id\nJOIN engagement_events ee ON s.id = ee.snippet_id\nGROUP BY a.username ORDER BY total DESC;" },
      { type: "result", text: "testauthor  →  80s\njanesmith   →  64s\nbobwriter   →  41s" },
    ],
  },
  {
    query: "which users signed up in the last 7 days?",
    steps: [
      { type: "sql",    text: "SELECT username, email, created_at\nFROM nl2db_users\nWHERE created_at >= NOW() - INTERVAL '7 days'\nORDER BY created_at DESC;" },
      { type: "result", text: "abhinav  ·  abhinav@example.com  ·  2 days ago\ntestuser ·  test@example.com    ·  5 days ago" },
    ],
  },
  {
    query: "average read duration per book?",
    steps: [
      { type: "retry",  text: "empty result — relaxing filter → retrying [1/3]" },
      { type: "sql",    text: "SELECT b.title, AVG(ee.read_duration_seconds) AS avg_read\nFROM books b\nJOIN snippets s ON b.id = s.book_id\nJOIN engagement_events ee ON s.id = ee.snippet_id\nGROUP BY b.title ORDER BY avg_read DESC;" },
      { type: "result", text: "Deep Work        →  42.3s avg\nAtomic Habits    →  38.1s avg\nThe Lean Startup →  31.7s avg" },
    ],
  },
];

const TYPE_COLORS: Record<string, string> = {
  sql:    "color: #8b8bdb;",
  retry:  "color: #ff9f0a;",
  result: "color: #30d158;",
};

const TYPE_LABELS: Record<string, string> = {
  sql:    "sql",
  retry:  "heal",
  result: "result",
};

export function TuiDemo() {
  const [seqIdx, setSeqIdx] = useState(0);
  const [stepIdx, setStepIdx] = useState(-1);
  const [charIdx, setCharIdx] = useState(0);
  const [queryDone, setQueryDone] = useState(false);
  const [phase, setPhase] = useState<"query" | "steps" | "pause">("query");

  const seq = SEQUENCES[seqIdx];
  const currentStep = stepIdx >= 0 ? seq.steps[stepIdx] : null;

  // Phase: type the query
  useEffect(() => {
    if (phase !== "query") return;
    if (charIdx < seq.query.length) {
      const t = setTimeout(() => setCharIdx(c => c + 1), 38);
      return () => clearTimeout(t);
    } else {
      const t = setTimeout(() => {
        setQueryDone(true);
        setPhase("steps");
        setStepIdx(0);
        setCharIdx(0);
      }, 500);
      return () => clearTimeout(t);
    }
  }, [phase, charIdx, seq.query.length]);

  // Phase: show steps one by one
  useEffect(() => {
    if (phase !== "steps") return;
    if (stepIdx >= seq.steps.length) {
      const t = setTimeout(() => {
        setPhase("pause");
      }, 2200);
      return () => clearTimeout(t);
    }
    const step = seq.steps[stepIdx];
    if (charIdx < step.text.length) {
      const delay = step.type === "sql" ? 12 : 18;
      const t = setTimeout(() => setCharIdx(c => c + 1), delay);
      return () => clearTimeout(t);
    } else {
      const t = setTimeout(() => {
        setStepIdx(s => s + 1);
        setCharIdx(0);
      }, 300);
      return () => clearTimeout(t);
    }
  }, [phase, stepIdx, charIdx, seq.steps]);

  // Phase: pause then next sequence
  useEffect(() => {
    if (phase !== "pause") return;
    const t = setTimeout(() => {
      setSeqIdx(i => (i + 1) % SEQUENCES.length);
      setStepIdx(-1);
      setCharIdx(0);
      setQueryDone(false);
      setPhase("query");
    }, 3000);
    return () => clearTimeout(t);
  }, [phase]);

  return (
    <div style={{
      background: "var(--dark)",
      borderRadius: "6px",
      padding: "28px 24px 20px",
      position: "relative",
      fontFamily: "var(--font-mono)",
      fontSize: "12px",
      lineHeight: "1.7",
      width: "100%",
      maxWidth: "580px",
    }}>
      {/* traffic lights */}
      <div style={{ position: "absolute", top: 12, left: 16, display: "flex", gap: 6 }}>
        {["#ff5f57","#ffbd2e","#28c840"].map((c, i) => (
          <span key={i} style={{ width: 10, height: 10, borderRadius: "50%", background: c, display: "inline-block" }} />
        ))}
      </div>

      {/* prompt row */}
      <div style={{
        background: "var(--dark-prompt)",
        borderRadius: "4px",
        padding: "8px 12px",
        marginBottom: 14,
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        marginTop: 8,
      }}>
        <span style={{ color: "var(--accent)", fontWeight: 700, marginTop: 1 }}>›</span>
        <span style={{ color: "#e0e0e0", flex: 1, minHeight: "1.4em" }}>
          {seq.query.slice(0, charIdx)}
          {phase === "query" && <span className="anim-blink" style={{ color: "var(--accent)" }}>▌</span>}
        </span>
      </div>

      {/* steps */}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {seq.steps.slice(0, stepIdx).map((s, i) => (
          <div key={i} style={{ display: "flex", gap: 10, opacity: 0.85 }}>
            <span style={{ minWidth: 48, fontSize: 10, fontWeight: 700, marginTop: 2, ...(s.type === "sql" ? { color: "#8b8bdb" } : s.type === "retry" ? { color: "var(--warning)" } : { color: "var(--success)" }) }}>
              [{TYPE_LABELS[s.type]}]
            </span>
            <pre style={{ color: "#c8c8c8", fontSize: 11, whiteSpace: "pre-wrap", flex: 1 }}>{s.text}</pre>
          </div>
        ))}
        {currentStep && stepIdx < seq.steps.length && (
          <div style={{ display: "flex", gap: 10 }}>
            <span style={{ minWidth: 48, fontSize: 10, fontWeight: 700, marginTop: 2, ...(currentStep.type === "sql" ? { color: "#8b8bdb" } : currentStep.type === "retry" ? { color: "var(--warning)" } : { color: "var(--success)" }) }}>
              [{TYPE_LABELS[currentStep.type]}]
            </span>
            <pre style={{ color: "#c8c8c8", fontSize: 11, whiteSpace: "pre-wrap", flex: 1 }}>
              {currentStep.text.slice(0, charIdx)}
              <span className="anim-blink" style={{ color: "var(--ash)" }}>▌</span>
            </pre>
          </div>
        )}
      </div>

      {/* footer */}
      {phase === "pause" && (
        <div style={{ display: "flex", gap: 10, marginTop: 12, paddingTop: 10, borderTop: "1px solid rgba(255,255,255,0.06)", fontSize: 10, color: "var(--ash)", opacity: 0.7 }}>
          <span style={{ color: "var(--success)" }}>[ok]</span>
          <span>groq llama-3.3-70b · {(Math.random() * 0.8 + 0.7).toFixed(2)}s</span>
        </div>
      )}
    </div>
  );
}
