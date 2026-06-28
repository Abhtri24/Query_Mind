"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { DottedGlowBackground } from "@/components/ui/dotted-glow-background";
import { TuiDemo } from "@/components/ui/tui-demo";
import { api } from "@/lib/api";

const FEATURES = [
  { mark: "[+]", title: "Self-healing SQL", desc: "Fails? It diagnoses, retries, fixes — up to 3 times autonomously." },
  { mark: "[+]", title: "Any database", desc: "MySQL, PostgreSQL, SQLite. Paste a URI, done." },
  { mark: "[+]", title: "Bring your own key", desc: "Groq or Gemini free tier. Or use the hosted budget." },
  { mark: "[x]", title: "Read-only, always", desc: "INSERT, UPDATE, DROP — blocked at the validation layer, not just the prompt." },
];

const COMING = [
  { mark: "[-]", title: "Schema memory", desc: "First-connect exploration cached. Subsequent sessions load instantly." },
  { mark: "[-]", title: "Query planner", desc: "Compound questions decomposed into chained sub-queries." },
  { mark: "[-]", title: "Chart agent", desc: "Autonomous visualisation after every query." },
];

export default function Landing() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    api.me().then(() => setAuthed(true)).catch(() => {});
    setTimeout(() => setVisible(true), 50);
  }, []);

  const s: Record<string, React.CSSProperties> = {
    page:      { fontFamily: "var(--font-mono)", background: "var(--canvas)", color: "var(--ink)", minHeight: "100vh" },
    nav:       { position: "sticky", top: 0, zIndex: 100, background: "var(--canvas)", borderBottom: "1px solid var(--hairline)", padding: "0 32px", height: 52, display: "flex", alignItems: "center", justifyContent: "space-between" },
    wordmark:  { fontWeight: 700, fontSize: 14, letterSpacing: ".04em", display: "flex", alignItems: "center", gap: 6 },
    bracket:   { color: "var(--mute)", fontWeight: 400 },
    navLinks:  { display: "flex", gap: 4, alignItems: "center" },
    navLink:   { fontSize: 13, color: "var(--mute)", fontWeight: 500, padding: "5px 10px", borderRadius: 4, background: "none", border: "none", cursor: "pointer", fontFamily: "var(--font-mono)" },
    cta:       { background: "var(--ink)", color: "#fff", fontSize: 13, fontWeight: 500, fontFamily: "var(--font-mono)", padding: "6px 16px", borderRadius: 4, border: "none", cursor: "pointer" },
    ctaGhost:  { background: "var(--canvas)", color: "var(--ink)", fontSize: 13, fontWeight: 500, fontFamily: "var(--font-mono)", padding: "5px 16px", borderRadius: 4, border: "1px solid var(--hairline-strong)", cursor: "pointer" },
    container: { maxWidth: 920, margin: "0 auto", padding: "0 32px" },
    hero:      { padding: "80px 0 72px", position: "relative", overflow: "hidden" },
    eyebrow:   { display: "inline-flex", alignItems: "center", gap: 8, fontSize: 11, color: "var(--mute)", fontWeight: 500, letterSpacing: ".06em", marginBottom: 20 },
    dot:       { width: 6, height: 6, borderRadius: "50%", background: "var(--success)", animation: "pulse 2s ease-in-out infinite" },
    h1:        { fontSize: "clamp(28px, 4vw, 40px)", fontWeight: 700, lineHeight: 1.2, marginBottom: 16, maxWidth: 560 },
    muted:     { color: "var(--mute)" },
    sub:       { fontSize: 14, color: "var(--body)", maxWidth: 420, marginBottom: 32, lineHeight: 1.75 },
    actions:   { display: "flex", gap: 10, alignItems: "center", marginBottom: 56 },
    divider:   { border: "none", borderTop: "1px solid var(--hairline)", margin: "72px 0" },
    secLabel:  { fontSize: 12, fontWeight: 700, color: "var(--ink)", marginBottom: 20, letterSpacing: ".04em" },
    featRow:   { display: "flex", gap: 14, padding: "13px 0", borderBottom: "1px solid var(--hairline)" },
    featMark:  { color: "var(--mute)", fontWeight: 700, fontSize: 12, minWidth: 22, marginTop: 2 },
    featTitle: { fontWeight: 700, fontSize: 13, color: "var(--ink)", marginBottom: 2 },
    featDesc:  { fontSize: 12, color: "var(--body)", lineHeight: 1.65 },
    footer:    { borderTop: "1px solid var(--hairline)", padding: "24px 32px", display: "flex", justifyContent: "space-between", alignItems: "center", maxWidth: 920, margin: "0 auto" },
    footerTxt: { fontSize: 12, color: "var(--mute)" },
  };

  return (
    <div style={s.page}>
      {/* NAV */}
      <nav style={s.nav}>
        <div style={s.wordmark}>
          <span style={s.bracket}>[</span>QM<span style={s.bracket}>]</span> QueryMind
        </div>
        <div style={s.navLinks}>
          {authed ? (
            <>
              <button style={s.navLink} onClick={() => router.push("/dashboard")}>dashboard</button>
              <button style={s.cta} onClick={() => router.push("/dashboard")}>open app →</button>
            </>
          ) : (
            <>
              <button style={s.navLink} onClick={() => router.push("/login")}>login</button>
              <button style={s.cta} onClick={() => router.push("/signup")}>get started →</button>
            </>
          )}
        </div>
      </nav>

      <div style={s.container}>
        {/* HERO */}
        <div style={{ ...s.hero, opacity: visible ? 1 : 0, transition: "opacity 0.4s ease" }}>
          {/* Dotted background — subtle, top portion only */}
          <div style={{ position: "absolute", inset: 0, zIndex: 0, pointerEvents: "none", maskImage: "radial-gradient(ellipse 80% 60% at 60% 40%, black 0%, transparent 100%)", opacity: 0.4 }}>
            <DottedGlowBackground gap={18} radius={1.2} speedMin={0.15} speedMax={0.6} opacity={0.6} />
          </div>

          <div style={{ position: "relative", zIndex: 1 }}>
            <div style={{ display: "flex", gap: 48, alignItems: "flex-start", flexWrap: "wrap" }}>
              {/* Left — copy */}
              <div style={{ flex: "1 1 320px", minWidth: 280 }}>
                <div style={s.eyebrow}>
                  <span style={s.dot} />
                  self-healing sql · multi-db · any llm
                </div>
                <h1 style={s.h1}>
                  Talk to your<br />
                  database.<br />
                  <span style={s.muted}>In plain English.</span>
                </h1>
                <p style={s.sub}>
                  Connect any database. Ask questions naturally.
                  QueryMind writes SQL, fixes its own mistakes,
                  and explains what it found.
                </p>
                <div style={s.actions}>
                  <button style={s.cta} onClick={() => router.push(authed ? "/dashboard" : "/signup")}>
                    {authed ? "open dashboard →" : "connect a database →"}
                  </button>
                  {!authed && (
                    <button style={s.ctaGhost} onClick={() => router.push("/login")}>
                      login
                    </button>
                  )}
                </div>
              </div>

              {/* Right — animated TUI */}
              <div style={{ flex: "1 1 360px", minWidth: 300, paddingTop: 8 }}
                className="anim-fade-up" >
                <TuiDemo />
              </div>
            </div>
          </div>
        </div>

        <hr style={s.divider} />

        {/* FEATURES */}
        <div style={{ marginBottom: 72 }}>
          <div style={s.secLabel}>[+] what it does</div>
          {FEATURES.map((f, i) => (
            <div key={i} style={{ ...s.featRow, borderBottom: i === FEATURES.length - 1 ? "none" : "1px solid var(--hairline)" }}>
              <span style={s.featMark}>{f.mark}</span>
              <div>
                <div style={s.featTitle}>{f.title}</div>
                <div style={s.featDesc}>{f.desc}</div>
              </div>
            </div>
          ))}
        </div>

        <hr style={s.divider} />

        {/* COMING */}
        <div style={{ marginBottom: 72 }}>
          <div style={s.secLabel}>[-] coming next</div>
          {COMING.map((f, i) => (
            <div key={i} style={{ ...s.featRow, borderBottom: i === COMING.length - 1 ? "none" : "1px solid var(--hairline)" }}>
              <span style={s.featMark}>{f.mark}</span>
              <div>
                <div style={s.featTitle}>{f.title}</div>
                <div style={s.featDesc}>{f.desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* FOOTER */}
      <div style={{ borderTop: "1px solid var(--hairline)", marginTop: 0 }}>
        <div style={s.footer}>
          <span style={s.footerTxt}><span style={{ color: "var(--mute)" }}>[QM]</span> QueryMind — alpha</span>
          <span style={s.footerTxt}>© 2026</span>
        </div>
      </div>
    </div>
  );
}
