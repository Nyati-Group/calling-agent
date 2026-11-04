import { useEffect, useState } from "react";

const STORAGE_KEY = "calls";
const POLL_MS = 2000;
const FINAL_STATUSES = ["completed", "busy", "failed", "no-answer", "canceled"];
const FAILED_STATUSES = ["busy", "failed", "no-answer", "canceled"];

function loadCalls() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || [];
  } catch {
    return [];
  }
}

function saveCalls(calls) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(calls.slice(0, 20)));
  } catch {
    // Storage unavailable; the list just won't survive a reload.
  }
}

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}

function formatDuration(seconds) {
  const s = Number(seconds) || 0;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function formatTime(timestamp) {
  if (!timestamp) return "";
  return new Date(timestamp).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit"
  });
}

function PhoneIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path
        fill="currentColor"
        d="M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1l-2.3 2.2Z"
      />
    </svg>
  );
}

function Health() {
  const [health, setHealth] = useState(null);

  useEffect(() => {
    api("/health").then(setHealth).catch(() => setHealth(false));
  }, []);

  const ready =
    health &&
    health.sarvam_configured &&
    health.twilio_configured &&
    health.public_url_configured;

  const state = health === null ? "" : ready ? "ok" : "bad";
  const label =
    health === null
      ? "Checking"
      : !health
        ? "Server offline"
        : ready
          ? "Online"
          : "Check .env";

  return (
    <span className={"pill health " + state}>
      <i /> {label}
    </span>
  );
}

function CallCard({ call, onUpdate, onRemove }) {
  const [open, setOpen] = useState(!FINAL_STATUSES.includes(call.status));
  const done = FINAL_STATUSES.includes(call.status);
  const failed = FAILED_STATUSES.includes(call.status);

  useEffect(() => {
    if (done) return;

    const timer = setInterval(async () => {
      try {
        onUpdate(await api("/calls/" + call.sid));
      } catch {
        // Keep polling; the server may be restarting.
      }
    }, POLL_MS);

    return () => clearInterval(timer);
  }, [call.sid, done]);

  const turns = call.transcript || [];
  const statusClass = done ? (failed ? "bad" : "") : "live";

  return (
    <li className={"call" + (open ? " open" : "")}>
      <button
        type="button"
        className="call-head"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className="avatar">
          <PhoneIcon />
        </span>
        <span className="call-meta">
          <span className="number">{call.to}</span>
          <span className="sub">
            {formatTime(call.startedAt)}
            {call.duration ? ` · ${formatDuration(call.duration)}` : ""}
          </span>
        </span>
        <span className={"pill " + statusClass}>
          {!done && <i />}
          {call.status}
        </span>
        <span className="chevron" aria-hidden="true" />
      </button>

      {open && (
        <div className="transcript">
          {turns.length === 0 ? (
            <p className="empty-note">
              {done ? "No conversation recorded." : "Waiting for the call to connect…"}
            </p>
          ) : (
            turns.map((turn, i) => (
              <div key={i} className={"bubble " + turn.role}>
                <span className="who">
                  {turn.role === "assistant" ? "Aditya" : "Caller"}
                </span>
                {turn.content}
              </div>
            ))
          )}
          <div className="transcript-foot">
            <button
              type="button"
              className="text-btn"
              onClick={() => onRemove(call.sid)}
            >
              Remove from list
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

export default function App() {
  const [number, setNumber] = useState("");
  const [calls, setCalls] = useState(loadCalls);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => saveCalls(calls), [calls]);

  async function startCall(e) {
    e.preventDefault();
    setBusy(true);
    setError("");

    try {
      const result = await api("/make-call", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to_number: number.trim() })
      });

      setCalls((prev) => [
        {
          sid: result.call_sid,
          to: result.to,
          status: result.status,
          startedAt: Date.now(),
          transcript: []
        },
        ...prev
      ]);
      setNumber("");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function updateCall(update) {
    setCalls((prev) =>
      prev.map((call) => (call.sid === update.sid ? { ...call, ...update } : call))
    );
  }

  function removeCall(sid) {
    setCalls((prev) => prev.filter((call) => call.sid !== sid));
  }

  const liveCount = calls.filter((c) => !FINAL_STATUSES.includes(c.status)).length;

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="mark">S</span>
          <span>SyncAI</span>
        </div>
        <Health />
      </header>

      <main className="layout">
        <section className="dialer">
          <p className="eyebrow">Outbound calling agent</p>
          <p className="lede">
            Enter a number and Aditya will call to talk about Sunrise Residency
            in Whitefield, Bengaluru, in the caller's own language.
          </p>

          <form onSubmit={startCall} className="dial-form">
            <label htmlFor="number">Phone number</label>
            <div className="dial-row">
              <input
                id="number"
                type="tel"
                inputMode="tel"
                autoComplete="tel"
                placeholder="+91 98765 43210"
                value={number}
                onChange={(e) => setNumber(e.target.value)}
                required
              />
              <button className="call-btn" disabled={busy || !number.trim()}>
                <PhoneIcon />
                {busy ? "Calling…" : "Call"}
              </button>
            </div>
            <p className="hint">Include the country code, e.g. +91.</p>
            {error && <p className="error">{error}</p>}
          </form>
        </section>

        <section className="activity">
          <div className="activity-head">
            <h2>Recent calls</h2>
            {calls.length > 0 && (
              <span className="count">
                {liveCount > 0 ? `${liveCount} live · ` : ""}
                {calls.length} total
              </span>
            )}
          </div>

          {calls.length === 0 ? (
            <div className="empty">
              <span className="avatar large">
                <PhoneIcon />
              </span>
              <p>No calls yet</p>
              <span>Calls you place will appear here with a live transcript.</span>
            </div>
          ) : (
            <ul className="calls">
              {calls.map((call) => (
                <CallCard
                  key={call.sid}
                  call={call}
                  onUpdate={updateCall}
                  onRemove={removeCall}
                />
              ))}
            </ul>
          )}
        </section>
      </main>
    </div>
  );
}
