import { useCallback, useEffect, useState, type ReactElement } from "react";

import { ApiError, EXPIRED_FLAG, api, type Banner } from "./api";
import { Link } from "./components/Link";
import { Logo } from "./components/Logo";
import { feedbackHandled } from "./moments";
import { href, isAlias, navigate, useRoute, type View } from "./router";
import { Live } from "./steps/Live";
import { Connect } from "./views/Connect";
import { Inbox } from "./views/Inbox";
import { Integrations } from "./views/Integrations";
import { Rules } from "./views/Rules";
import { Start } from "./views/Start";
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
    id: "connect",
    label: "Connect",
    title: "Connect",
    blurb: "Where Rowfire hears about events: your app sends them, or Rowfire reads your database.",
    icon: (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M5.5 1.5v3.5M10.5 1.5v3.5" />
        <path d="M3.5 5h9v2.5a4.5 4.5 0 0 1-9 0z" />
        <path d="M8 12v2.5" />
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
    id: "actions",
    label: "Actions",
    title: "Actions",
    blurb: "What a rule can do — post to Slack, open a ticket, call an API — grouped by the account it runs through.",
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
];

export function App() {
  const route = useRoute();
  // Whether anything is connected. Until it is, only Connect (and Get started) opens.
  const [connected, setConnected] = useState(false);
  // Nothing is drawn until the first health check answers, so a deep link
  // does not flash the Connect screen on its way to where it points.
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
  // `local`, `demo`, or an extension's: decides which places are offered.
  const [realm, setRealm] = useState("local");
  const [banner, setBanner] = useState<Banner | null>(null);

  useEffect(() => {
    const load = async () => {
      let health = await api.health();
      // Someone an extension signs in, who is not signed in yet: its page.
      if (!health.session && health.signin_url) {
        window.location.assign(health.signin_url);
        return;
      }
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
      setRealm(health.realm);
      setBanner(health.banner ?? null);
    };
    load()
      .catch((exc) => setFault(exc instanceof ApiError ? exc.message : String(exc)))
      .finally(() => setReady(true));
  }, []);

  // An extension's banner can count things (a trial's events, say) that
  // change while the page is open, so it is asked again now and then.
  const hasBanner = banner !== null;
  useEffect(() => {
    if (!hasBanner) return;
    const timer = window.setInterval(() => {
      api
        .health()
        .then((health) => setBanner(health.banner ?? null))
        .catch(() => undefined);
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [hasBanner]);

  // A request an extension claimed: a shared server with no sample data to
  // tour, where apps send events instead.
  const sharedServer = realm !== "local" && realm !== "demo";
  const offered = PLACES.filter((place) =>
    place.id === "start" ? !sharedServer : true,
  );

  // `/` has no page of its own: open where the work happens once set up,
  // and at Connect or Get started otherwise. Older addresses (/sources,
  // /integrations, /setup) are corrected to their current ones.
  useEffect(() => {
    if (ready && route.view === null) {
      // Nothing connected yet means a first visit: start with the guided
      // setup rather than an empty form.
      // A demo visitor arrives already connected, and is there to try it.
      navigate(href(sharedServer ? "connect" : connected && !demo ? "rules" : "start"), {
        replace: true,
      });
    } else if (route.view !== "unknown" && route.view !== null && isAlias(window.location.pathname)) {
      // An older address (/sources, /integrations, /setup): its current one,
      // keeping the query, which Supabase's sign-in returns with.
      const current = href(route.view, route.item, route.action) + window.location.search;
      navigate(current, { replace: true });
    }
  }, [ready, route.view, route.item, connected, demo, sharedServer]);

  const onSourcesChanged = useCallback((has: boolean) => setConnected(has), []);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const reachable = (id: View) => id === "start" || id === "connect" || connected;
  const view: View | null = route.view === null || route.view === "unknown" ? null : route.view;
  // A link into the app before a database is connected keeps its URL, shows
  // the connection form, and carries on to where it pointed once connected.
  const shown: View | null = view && !reachable(view) ? "connect" : view;
  const place = offered.find((p) => p.id === shown) ?? null;

  useEffect(() => {
    // Only name the item when its page is what is actually on screen, not
    // while the link is held at Connect.
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
          {offered.map((entry) => (
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
        {banner && <BannerLine banner={banner} />}
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
              <Link to={href(connected ? "rules" : "connect")} className="ref">
                Go to {connected ? "Rules" : "Connect"}
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
        {ready && shown === "actions" && (
          <Integrations selected={route.item} action={route.action} />
        )}
        {ready && shown === "activity" && <Live />}
        {ready && shown === "inbox" && <Inbox />}
        {ready && shown === "connect" && (
          // A link held here while nothing was connected carries on to where
          // it pointed once a connection exists: `shown` follows `connected`.
          <Connect
            selected={view === "connect" ? route.item : null}
            platform={view === "connect" ? route.action : null}
            onChanged={onSourcesChanged}
          />
        )}

        <p className="footnote page-foot">
          {sharedServer
            ? "Only the events you subscribe to reach Rowfire, with only the fields you name. "
            : "Read-only throughout — this never writes to a database it reads. "}
          Definitions and credentials live in the control plane: credentials are stored
          encrypted, every change to a definition is kept as a version, and rules can be
          promoted to live.
        </p>
      </main>
    </div>
  );
}

/** An extension's line across the top: text, then links or actions. */
function BannerLine({ banner }: { banner: Banner }) {
  return (
    <div className="demo-banner" role="note">
      <span>{banner.text}</span>
      {banner.actions && banner.actions.length > 0 && (
        <span className="row banner-actions">
          {banner.actions.map((action) =>
            "href" in action ? (
              <a key={action.label} className="btn" href={action.href}>
                {action.label}
              </a>
            ) : (
              <button
                key={action.label}
                className="btn"
                onClick={() => {
                  api
                    .post(action.post)
                    .catch(() => undefined)
                    .finally(() => window.location.assign(action.then ?? "/"));
                }}
              >
                {action.label}
              </button>
            ),
          )}
        </span>
      )}
    </div>
  );
}
