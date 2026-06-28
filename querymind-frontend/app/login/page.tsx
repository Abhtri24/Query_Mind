"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";

export default function Login() {
  const router = useRouter();
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    if (!u || !p) { setErr("username and password required"); return; }
    setLoading(true); setErr("");
    try {
      await api.login(u, p);
      router.push("/dashboard");
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "login failed");
    }
    setLoading(false);
  };

  const inp: React.CSSProperties = {
    width: "100%", background: "var(--surface)", color: "var(--ink)",
    fontFamily: "var(--font-mono)", fontSize: 13,
    border: "1px solid var(--hairline)", borderRadius: 4,
    padding: "9px 12px", outline: "none",
  };

  return (
    <div style={{ minHeight: "100vh", background: "var(--canvas)", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "var(--font-mono)" }}>
      <div style={{ width: 340, border: "1px solid var(--hairline)", borderRadius: 4, padding: "36px 32px", background: "var(--canvas)" }}>
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 4 }}>[QM] login</div>
        <div style={{ fontSize: 12, color: "var(--mute)", marginBottom: 24 }}>welcome back</div>

        {err && <div style={{ fontSize: 12, color: "var(--danger)", background: "rgba(255,59,48,.07)", border: "1px solid rgba(255,59,48,.18)", borderRadius: 4, padding: "8px 10px", marginBottom: 14 }}>{err}</div>}

        <div style={{ marginBottom: 14 }}>
          <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>username</label>
          <input style={inp} value={u} onChange={e => setU(e.target.value)} onKeyDown={e => e.key === "Enter" && submit()} placeholder="abhinav" />
        </div>
        <div style={{ marginBottom: 20 }}>
          <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>password</label>
          <input style={inp} type="password" value={p} onChange={e => setP(e.target.value)} onKeyDown={e => e.key === "Enter" && submit()} placeholder="••••••" />
        </div>

        <button onClick={submit} disabled={loading} style={{ width: "100%", background: "var(--ink)", color: "#fff", fontFamily: "var(--font-mono)", fontSize: 13, fontWeight: 500, padding: "9px 0", borderRadius: 4, border: "none", cursor: loading ? "not-allowed" : "pointer", opacity: loading ? 0.6 : 1 }}>
          {loading ? "logging in…" : "login →"}
        </button>

        <div style={{ textAlign: "center", marginTop: 16, fontSize: 12, color: "var(--mute)" }}>
          no account?{" "}
          <span style={{ color: "var(--ink)", cursor: "pointer", fontWeight: 500 }} onClick={() => router.push("/signup")}>sign up</span>
        </div>
        <div style={{ textAlign: "center", marginTop: 8, fontSize: 12, color: "var(--mute)" }}>
          <span style={{ cursor: "pointer" }} onClick={() => router.push("/")}>← back to home</span>
        </div>
      </div>
    </div>
  );
}
