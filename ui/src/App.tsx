import { useCallback, useEffect, useState, type ReactElement } from "react";

import { ApiError, EXPIRED_FLAG, api } from "./api";
import { Link } from "./components/Link";
import { Logo } from "./components/Logo";
import { feedbackHandled } from "./moments";
import { href, navigate, useRoute, type View } from "./router";
import { Live } from "./steps/Live";
import { Inbox } from "./views/Inbox";
import { Integrations } from "./views/Integrations";
import { Rules } from "./views/Rules";
import { Start } from "./views/Start";
import { Sources } from "./views/Sources";
import { Triggers } from "./views/Triggers";

/* One surface, seven places to be.
 *
 * The nav is a left rail rather than a row of tabs because these are places,
 * not steps: somebody lives in Rules and visits Data sources once. A rail also leaves
 * the full width of the window for the work, which matters when the work is
 * reading SQL and tables of rows.
 */

interface Place {
  id: View;
  label: string;
  title: string;
  blurb: string;
  icon: ReactElement;
}

const PLACES: Place[] = [
  {
    id: "start",
    label: "Get started",
    title: "Get started",
    blurb: "Your first automation, on sample data, in about two minutes.",
    icon: (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M4 2.5v11l9-5.5z" />
      </svg>
    ),
  },
  {
    id: "triggers",
    label: "Triggers",
    title: "Triggers",
    blurb: "A query that says when something happened, and what makes one row one thing.",
    icon: (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M2 4h12M2 8h8M2 12h5" />
      </svg>
    ),
  },
  {
    id: "rules",
    label: "Rules",
    title: "Rules",
    blurb: "What happens when a trigger's rows appear, and how often.",
    icon: (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M8 1.5 14 5v6l-6 3.5L2 11V5z" />
      </svg>
    ),
  },
  {
    id: "integrations",
    label: "Integrations",
    title: "Integrations",
    blurb: "Where actions are sent, and the shape of each request.",
    icon: (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M6.5 9.5a3 3 0 0 0 4.2 0l2.1-2.1a3 3 0 0 0-4.2-4.2l-.9.9" />
        <path d="M9.5 6.5a3 3 0 0 0-4.2 0L3.2 8.6a3 3 0 0 0 4.2 4.2l.9-.9" />
      </svg>
    ),
  },
  {
    id: "activity",
    label: "Activity",
    title: "Activity",
    blurb: "What is running, how far it has got, and what it has sent.",
    icon: (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M1.5 9.5h3l2-5 3 9 2-5h3" />
      </svg>
    ),
  },
  {
    id: "inbox",
    label: "Demo inbox",
    title: "Demo inbox",
    blurb: "Where rules deliver when nothing real is connected yet.",
    icon: (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M2.5 2.5h11l1 6v5h-13v-5z" />
        <path d="M1.5 8.5h4a2.5 2.5 0 0 0 5 0h4" />
      </svg>
    ),
  },
  {
    id: "sources",
    label: "Data sources",
    title: "Data sources",
    blurb: "The databases this reads — Postgres, MySQL or Supabase, as many as you need.",
    icon: (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <ellipse cx="8" cy="3.8" rx="5.5" ry="2.3" />
        <path d="M2.5 3.8v8.4c0 1.3 2.5 2.3 5.5 2.3s5.5-1 5.5-2.3V3.8" />
        <path d="M2.5 8c0 1.3 2.5 2.3 5.5 2.3s5.5-1 5.5-2.3" />
      </svg>
    ),
  },
];

export function App() {
  const route = useRoute();
  // Whether any data source exists. Until one does, only Data sources opens.
  const [connected, setConnected] = useState(false);
  // Nothing is drawn until the first health check answers, so a deep link
  // does not flash the data sources screen on its way to where it points.
  const [ready, setReady] = useState(false);
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [halted, setHalted] = useState<string | null>(null);
  const [fault, setFault] = useState<string | null>(null);
  // The public demo: shown a banner, and given a workspace on first load.
  const [demo, setDemo] = useState<{
    idleHours: number | null;
    feedback: string | null;
    renewed: boolean;
  } | null>(null);

  useEffect(() => {
    const load = async () => {
      let health = await api.health();
      // A hosted visitor's first request: make their private workspace
      // (sample data, sources and definitions included) before anything else
      // asks for it.
      if (health.hosted && !health.session) {
        await api.startSession();
        health = await api.health();
      }
      // Set just before the reload that replaced an expired workspace.
      let renewed = false;
      try {
        renewed = sessionStorage.getItem(EXPIRED_FLAG) !== null;
        sessionStorage.removeItem(EXPIRED_FLAG);
      } catch {
        // Storage blocked: the banner simply does not mention it.
      }
      if (health.hosted) {
        setDemo({ idleHours: health.idle_hours, feedback: health.feedback_url, renewed });
      }
      setHalted(health.halted);
      setFault(health.degraded);
      setConnected(health.connected);
    };
    load()
      .catch((exc) => setFault(exc instanceof ApiError ? exc.message : String(exc)))
      .finally(() => setReady(true));
  }, []);

  // `/` has no page of its own: open where the work happens once set up,
  // and at the data sources otherwise. `/setup` is the old name for the
  // latter, so its address is corrected too.
  useEffect(() => {
    if (ready && route.view === null) {
      // Nothing connected yet means a first visit: start with the guided
      // setup rather than an empty form.
      // A demo visitor arrives already connected, and is there to try it.
      navigate(href(connected && !demo ? "rules" : "start"), { replace: true });
    } else if (window.location.pathname.split("/")[1] === "setup") {
      navigate(href("sources", route.item), { replace: true });
    }
  }, [ready, route.view, route.item, connected, demo]);

  const onSourcesChanged = useCallback((has: boolean) => setConnected(has), []);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const reachable = (id: View) => id === "sources" || id === "start" || connected;
  const view: View | null = route.view === null || route.view === "unknown" ? null : route.view;
  // A link into the app before a database is connected keeps its URL, shows
  // the connection form, and carries on to where it pointed once connected.
  const shown: View | null = view && !reachable(view) ? "sources" : view;
  const place = PLACES.find((p) => p.id === shown) ?? null;

  useEffect(() => {
    // Only name the item when its page is what is actually on screen, not
    // while the link is held at Data sources.
    const named = shown === view ? [route.action, route.item] : [];
    document.title = [...named, place?.title, "Rowfire"].filter(Boolean).join(" · ");
  }, [route.action, route.item, place, shown, view]);

  return (
    <div className="app">
      <nav className="rail" aria-label="Sections">
        <div className="brand">
          <span className="brand-mark">
            <Logo />
          </span>
          Rowfire
        </div>

        <ul className="rail-nav">
          {PLACES.map((entry) => (
            <li key={entry.id}>
              {reachable(entry.id) ? (
                <Link
                  to={href(entry.id)}
                  className="rail-item"
                  aria-current={entry.id === shown ? "page" : undefined}
                >
                  <span className="rail-icon">{entry.icon}</span>
                  {entry.label}
                </Link>
              ) : (
                <button className="rail-item" disabled>
                  <span className="rail-icon">{entry.icon}</span>
                  {entry.label}
                </button>
              )}
            </li>
          ))}
        </ul>

        <div className="rail-foot">
          <span className="conn-pill">
            <span className={connected ? "dot" : "dot off"} aria-hidden="true" />
            {connected ? "connected" : "not connected"}
          </span>
          <button
            className="icon-btn"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label="Toggle colour theme"
            title="Toggle colour theme"
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </div>
      </nav>

      <main className="main">
        {demo && (
          <div className="demo-banner" role="note">
            <span>
              {demo.renewed && (
                <>
                  <strong>Your earlier demo workspace had expired, so this is a fresh one.</strong>{" "}
                </>
              )}
              <strong>Public demo.</strong> Your workspace is private to this browser and is
              deleted after {demo.idleHours ?? 24} hours without a visit. Everything runs on sample
              data and delivers to the Demo inbox.
            </span>
            {demo.feedback && (
              <a
                className="btn"
                href={demo.feedback}
                target="_blank"
                rel="noreferrer"
                onClick={(event) => {
                  if (feedbackHandled()) event.preventDefault();
                }}
              >
                Give feedback
              </a>
            )}
          </div>
        )}
        {place && (
          <header className="page-head">
            <h1>{place.title}</h1>
            <p>{place.blurb}</p>
          </header>
        )}

        {ready && route.view === "unknown" && (
          <div className="card">
            <h2>Nothing here</h2>
            <p className="hint">
              <code>{window.location.pathname}</code> is not a page in Rowfire.{" "}
              <Link to={href(connected ? "rules" : "sources")} className="ref">
                Go to {connected ? "Rules" : "Data sources"}
              </Link>
              .
            </p>
          </div>
        )}

        {fault && (
          <div className="notice bad">
            <div className="notice-head">
              <span aria-hidden="true">✕</span> Cannot reach the server
            </div>
            {fault}
            <br />
            Until this clears, this page cannot tell you whether credentials are
            being stored.
          </div>
        )}

        {halted && (
          <div className="notice bad">
            <div className="notice-head">
              <span aria-hidden="true">■</span> Everything is halted
            </div>
            {halted} — nothing will fire until you resume it under Activity.
          </div>
        )}

        {ready && shown === "start" && <Start onConnected={onSourcesChanged} />}
        {ready && shown === "triggers" && <Triggers selected={route.item} />}
        {ready && shown === "rules" && <Rules selected={route.item} />}
        {ready && shown === "integrations" && (
          <Integrations selected={route.item} action={route.action} />
        )}
        {ready && shown === "activity" && <Live />}
        {ready && shown === "inbox" && <Inbox />}
        {ready && shown === "sources" && (
          // A link held here while nothing was connected carries on to where
          // it pointed once a source exists: `shown` follows `connected`.
          <Sources selected={view === "sources" ? route.item : null} onChanged={onSourcesChanged} />
        )}

        <p className="footnote page-foot">
          Read-only throughout — this never writes to the database you are
          reading. Definitions and the connection live in the control plane: the
          credential is stored encrypted, every change to a definition is kept as
          a version, and rules can be promoted to live.
        </p>
      </main>
    </div>
  );
}
