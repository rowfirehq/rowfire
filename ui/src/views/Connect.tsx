import { useCallback, useEffect, useState, type FormEvent } from "react";

import { ApiError, api, type AppEvent, type AppEventKey } from "../api";

/* Connect your app: send events with the in-app SDK instead of letting
 * Rowfire read a database.
 *
 * Four steps, in the order someone does them: install, make a key, paste the
 * snippet, see the first event land. The snippet is filled in with this
 * server's address and, right after it is made, the key itself -- the one
 * moment the key can be shown, so the one moment it can be pasted.
 */

const REFRESH_MS = 3000;
const INSTALL =
  'pip install "rowfire-sdk[sqlalchemy] @ git+https://github.com/rowfirehq/rowfire-sdk-python"';

function ago(iso: string | null): string {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function snippet(endpoint: string, key: string | null): string {
  const apiKey = key ? `"${key}"` : 'os.environ["ROWFIRE_API_KEY"]';
  return [
    "import os",
    "",
    "import rowfire_sdk as rowfire",
    "from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy",
    "from rowfire_sdk.integrations.asgi import RowfireMiddleware",
    "",
    "rowfire.init(",
    `    endpoint="${endpoint}",`,
    `    api_key=${apiKey},`,
    ")",
    "rowfire_sqlalchemy.instrument()          # every SQLAlchemy session",
    "app.add_middleware(RowfireMiddleware)    # FastAPI / Starlette",
  ].join("\n");
}

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn"
      onClick={() => {
        navigator.clipboard
          ?.writeText(text)
          .then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          })
          .catch(() => undefined);
      }}
    >
      {copied ? "Copied" : label}
    </button>
  );
}

function Code({ text }: { text: string }) {
  return (
    <div className="code-block">
      <pre>{text}</pre>
      <CopyButton text={text} />
    </div>
  );
}

export function Connect({ ingestHere }: { ingestHere: boolean }) {
  const endpoint = ingestHere ? window.location.origin : null;
  const [keys, setKeys] = useState<AppEventKey[] | null>(null);
  const [name, setName] = useState("my-app");
  const [fresh, setFresh] = useState<{ prefix: string; key: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<{ total: number; events: AppEvent[] } | null>(null);
  const [lost, setLost] = useState(0);

  const loadKeys = useCallback(() => {
    api
      .appEventKeys()
      .then((answer) => setKeys(answer.keys))
      .catch((exc) => setError(exc instanceof ApiError ? exc.message : String(exc)));
  }, []);

  useEffect(loadKeys, [loadKeys]);

  // Waiting for the first event is the point of the page, so it keeps asking.
  useEffect(() => {
    let live = true;
    const tick = () => {
      api
        .appEventsRecent(10)
        .then((answer) => live && setEvents(answer))
        .catch(() => undefined);
      api
        .appEventLosses()
        .then(
          (answer) => live && setLost(answer.losses.reduce((sum, row) => sum + row.count, 0)),
        )
        .catch(() => undefined);
    };
    tick();
    const timer = window.setInterval(tick, REFRESH_MS);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, []);

  const create = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const made = await api.createAppEventKey(name.trim() || "my-app");
      setFresh({ prefix: made.prefix, key: made.key });
      loadKeys();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (prefix: string) => {
    if (!window.confirm(`Revoke ${prefix}…? Apps sending with it will be refused.`)) return;
    try {
      await api.revokeAppEventKey(prefix);
      if (fresh?.prefix === prefix) setFresh(null);
      loadKeys();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  };

  const active = (keys ?? []).filter((key) => !key.revoked);
  const arrived = (events?.total ?? 0) > 0;

  return (
    <div className="connect">
      <section className="card">
        <h2>1. Install the SDK</h2>
        <p className="hint">
          It runs inside your app, sees each write your ORM makes, and sends only the events
          you subscribe to — with only the fields you name. Nothing needs access to your
          database.
        </p>
        <Code text={INSTALL} />
      </section>

      <section className="card">
        <h2>2. Create an API key</h2>
        <p className="hint">
          One per app is plenty. The key is shown once, here, right after you make it.
        </p>
        <form className="row" onSubmit={create}>
          <input
            type="text"
            aria-label="Key name"
            value={name}
            maxLength={100}
            onChange={(e) => setName(e.target.value)}
            style={{ maxWidth: 260 }}
          />
          <button className="btn primary" type="submit" disabled={busy}>
            {busy ? "Creating…" : "Create key"}
          </button>
        </form>

        {fresh && (
          <div className="notice good" style={{ marginTop: 14 }}>
            <div className="notice-head">Your new key — copy it now, it won't be shown again</div>
            <div className="code-block">
              <pre>{fresh.key}</pre>
              <CopyButton text={fresh.key} />
            </div>
          </div>
        )}

        {error && (
          <p className="bad-text" role="alert" style={{ marginTop: 10 }}>
            {error}
          </p>
        )}

        {active.length > 0 && (
          <div className="table-scroll" style={{ marginTop: 14 }}>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Key</th>
                  <th>Created</th>
                  <th>Last used</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {active.map((key) => (
                  <tr key={key.prefix}>
                    <td>{key.name}</td>
                    <td className="mono">{key.prefix}…</td>
                    <td>{ago(key.created_at)}</td>
                    <td>{ago(key.last_used_at)}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="linklike" onClick={() => revoke(key.prefix)}>
                        Revoke
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2>3. Start it in your app</h2>
        {endpoint ? (
          <>
            <p className="hint">
              Once, at startup. {fresh ? "Your new key is already filled in" : "Put the key in "}
              {fresh ? (
                " — move it to an environment variable before you commit this."
              ) : (
                <>
                  <code>ROWFIRE_API_KEY</code>.
                </>
              )}
            </p>
            <Code text={snippet(endpoint, fresh?.key ?? null)} />
            <p className="footnote" style={{ marginTop: 10 }}>
              Jobs and commands name themselves with{" "}
              <code>with rowfire.operation("job NightlyBilling"):</code>. A subscription
              without <code>write:</code> fires when its operation finishes successfully, so
              it needs no ORM integration at all.
            </p>
          </>
        ) : (
          <p className="hint">
            Apps send events to Rowfire's ingest server, which this server isn't running.
            Start it with <code>rowfire ingest --host 0.0.0.0 --port 8100</code> and use its
            address as the endpoint.
          </p>
        )}
      </section>

      <section className="card">
        <h2>4. Watch events arrive</h2>
        {!arrived ? (
          <div className="waiting">
            <span className="pulse" aria-hidden="true" />
            <span className="muted">
              Waiting for the first event… Use your app (or run a job) after starting it with
              the key above.
            </span>
          </div>
        ) : (
          <>
            <p className="hint">
              <strong>{events!.total.toLocaleString()}</strong> event
              {events!.total === 1 ? "" : "s"} received. A trigger reads them as the{" "}
              <code>events</code> table, like any other query.
            </p>
            <div className="table-scroll scroll-y">
              <table>
                <thead>
                  <tr>
                    <th>Received</th>
                    <th>Subscription</th>
                    <th>Operation</th>
                    <th>Data</th>
                  </tr>
                </thead>
                <tbody>
                  {events!.events.map((event) => (
                    <tr key={event.event_id}>
                      <td>{ago(event.received_at)}</td>
                      <td className="mono">{event.subscription}</td>
                      <td className="mono">{event.operation ?? "—"}</td>
                      <td className="mono muted" title={JSON.stringify(event.data)}>
                        {JSON.stringify(event.data).slice(0, 80)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
        {lost > 0 && (
          <p className="bad-text" style={{ marginTop: 10 }}>
            Your apps reported {lost.toLocaleString()} event{lost === 1 ? "" : "s"} they could
            not deliver in the last 7 days.
          </p>
        )}
        <p className="footnote" style={{ marginTop: 12 }}>
          What the SDK sends is set by <code>subscriptions:</code> in your definitions: an
          operation, optionally a table write in it, and the fields to carry. Apps fetch them
          from this server, so changing one needs no deploy.
        </p>
      </section>
    </div>
  );
}
