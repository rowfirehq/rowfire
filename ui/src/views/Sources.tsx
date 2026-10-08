import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  api,
  NAME_PATTERN,
  type SchemaResponse,
  type SourceInfo,
  type SourceKind,
  type SuggestedSource,
} from "../api";
import { BrandIcon, engineLabel } from "../components/BrandIcon";
import { Link } from "../components/Link";
import { SchemaBrowser } from "../components/SchemaBrowser";
import { href, navigate } from "../router";
import { Review } from "../steps/Review";

/* Data sources: the databases Rowfire reads.
 *
 * Any number of them, Postgres or MySQL, each a named read-only connection.
 * A trigger names the one it reads; a trigger that names none reads the
 * default -- `primary`, or the first source added.
 *
 * Done once per database, then largely ignored, which is why it is its own
 * place rather than the first step of anything: the person composing rules
 * every day should not walk past a connection string to get to their work.
 */

/** Which engine a DSN points at, from its scheme -- the same rule the server applies. */
function kindOfDsn(dsn: string): SourceKind | null {
  const scheme = dsn.trim().split("://")[0]?.toLowerCase() ?? "";
  if (!dsn.includes("://")) return null;
  if (["postgres", "postgresql", "postgresql+psycopg"].includes(scheme)) return "postgres";
  if (["mysql", "mysql+pymysql", "mariadb"].includes(scheme)) return "mysql";
  return null;
}

/** `selected` comes from the URL: /sources/<name>. */
export function Sources({
  selected,
  onChanged,
}: {
  selected: string | null;
  /** Told whether any source exists, so the rest of the app can open up. */
  onChanged: (hasSources: boolean) => void;
}) {
  const [items, setItems] = useState<SourceInfo[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [suggested, setSuggested] = useState<SuggestedSource[]>([]);
  const [showRaw, setShowRaw] = useState(false);
  // The hosted demo connects every visitor to the sample databases, and the
  // server refuses any other; there is nothing to add or remove.
  const [fixed, setFixed] = useState(false);

  const load = useCallback(async () => {
    try {
      const listed = await api.sources();
      setItems(listed.sources);
      setLoaded(true);
      setLoadError(null);
      onChanged(listed.sources.length > 0);
    } catch (exc) {
      setLoadError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }, [onChanged]);

  useEffect(() => {
    load();
    api
      .health()
      .then((health) => {
        setSuggested(health.suggested_sources ?? []);
        setFixed(health.hosted);
      })
      .catch(() => undefined);
  }, [load]);

  // Following a link to a source closes the add form.
  useEffect(() => {
    if (selected !== null) setAdding(false);
  }, [selected]);

  const current = items.find((s) => s.name === selected) ?? null;
  const missing = loaded && selected !== null && current === null;
  // With nothing connected yet there is only one useful thing to show.
  const showAdd = !fixed && (adding || (loaded && items.length === 0));

  return (
    <div className="rules">
      <aside className="rule-list">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 14 }}>
          <h2>All data sources</h2>
          {!fixed && (
            <button
              className="btn primary"
              onClick={() => {
                setAdding(true);
                navigate(href("sources"));
              }}
            >
              Add
            </button>
          )}
        </div>
        {fixed && (
          <p className="hint">
            The demo's sample databases. To connect your own, run Rowfire yourself.
          </p>
        )}

        {loaded && items.length === 0 && (
          <p className="hint">None yet. Add a Postgres or MySQL database to start.</p>
        )}

        {items.map((source) => (
          <Link
            key={source.name}
            to={href("sources", source.name)}
            className={`rule-card${selected === source.name ? " active" : ""}`}
            aria-current={selected === source.name ? "page" : undefined}
            onClick={() => setAdding(false)}
          >
            <div className="rule-card-head">
              <span className="with-icon">
                <BrandIcon name={source.kind} size="sm" />
                <strong>{source.name}</strong>
              </span>
              {source.default && <span className="tag">default</span>}
            </div>
            <div className="muted">{source.summary}</div>
            <div className="muted" style={{ fontSize: 11 }}>
              {source.triggers.length === 0
                ? "no triggers yet"
                : `${source.triggers.length} trigger${source.triggers.length === 1 ? "" : "s"}`}
            </div>
          </Link>
        ))}
      </aside>

      <section className="rule-detail">
        {loadError && (
          <div className="notice bad">
            <div className="notice-head">
              <span aria-hidden="true">✕</span> Could not load
            </div>
            {loadError}
            <div className="row" style={{ marginTop: 10 }}>
              <button className="btn" onClick={() => load()}>
                Try again
              </button>
            </div>
          </div>
        )}

        {showAdd && (
          <AddSource
            first={items.length === 0}
            taken={items.map((s) => s.name)}
            suggested={suggested}
            onCancel={items.length > 0 ? () => setAdding(false) : undefined}
            onSaved={async (name) => {
              setAdding(false);
              await load();
              navigate(href("sources", name));
            }}
          />
        )}

        {!showAdd && missing && (
          <div className="card">
            <h2>No data source called {selected}</h2>
            <p className="hint">It may have been deleted. Pick one from the list, or add it.</p>
          </div>
        )}

        {!showAdd && current && (
          <SourceDetail
            key={current.name}
            source={current}
            fixed={fixed}
            onDeleted={async () => {
              await load();
              navigate(href("sources"), { replace: true });
            }}
          />
        )}

        {!showAdd && !current && !missing && loaded && (
          <>
            <div className="card">
              <h2>Data sources</h2>
              <p className="hint">
                A <strong>data source</strong> is a database Rowfire reads, through a
                read-only connection: PostgreSQL or MySQL, as many as you need. Each
                trigger names the source its query runs against, so one rule can
                watch your orders in Postgres while another watches tickets in MySQL.
              </p>
              <p className="hint">
                A trigger that names no source reads the default:{" "}
                <code>primary</code> when there is one, or the first source you added.
              </p>
            </div>

            <div className="card">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <div>
                  <h2>Definitions document</h2>
                  <p className="hint" style={{ marginBottom: 0 }}>
                    Triggers and rules as one document. Editing it here stores a
                    version exactly as the composer does; this is the escape hatch,
                    not the main road.
                  </p>
                </div>
                <button className="btn" onClick={() => setShowRaw((v) => !v)}>
                  {showRaw ? "Hide" : "Edit as YAML"}
                </button>
              </div>
            </div>
            {showRaw && <Review onSaved={() => undefined} />}
          </>
        )}
      </section>
    </div>
  );
}

/* ------------------------------------------------------------------ add */

function AddSource({
  first,
  taken,
  suggested,
  onCancel,
  onSaved,
}: {
  first: boolean;
  taken: string[];
  suggested: SuggestedSource[];
  onCancel?: () => void;
  onSaved: (name: string) => void;
}) {
  // The first source is `primary` unless said otherwise, which is the name a
  // trigger with no `source:` reads.
  const [name, setName] = useState(first ? "primary" : "");
  const [dsn, setDsn] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const kind = kindOfDsn(dsn);
  const replacing = taken.includes(name);
  const canSave = Boolean(NAME_PATTERN.test(name) && dsn.trim() && kind);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const added = await api.addSource(name, dsn.trim());
      onSaved(added.name);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h2>{first ? "Connect your first database" : "Add a data source"}</h2>
      <p className="hint">
        Point this at a read-only replica or a <code>SELECT</code>-only account.
        Every session is opened read-only — Postgres with{" "}
        <code>default_transaction_read_only</code>, MySQL with{" "}
        <code>SET SESSION TRANSACTION READ ONLY</code> — so a write is refused by
        the database itself, not by convention.
      </p>

      <div className="engine-row" aria-label="Supported databases">
        {(["postgres", "mysql"] as const).map((engine) => (
          <span key={engine} className={`engine-chip${kind === engine ? " on" : ""}`}>
            <BrandIcon name={engine} size="sm" label="" />
            {engineLabel(engine)}
          </span>
        ))}
      </div>

      <form onSubmit={submit}>
        <div className="grid-2">
          <div>
            <label className="field" htmlFor="source-name">
              Name
            </label>
            <input
              id="source-name"
              type="text"
              value={name}
              placeholder="support_mysql"
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setName(e.target.value)}
            />
            <p className="footnote" style={{ marginTop: 6 }}>
              {name && !NAME_PATTERN.test(name)
                ? "Lowercase letters, digits and _, starting with a letter."
                : replacing
                  ? `Replaces the connection stored as ${name}.`
                  : "What a trigger writes as source: to read it."}
            </p>
          </div>
          <div>
            <label className="field" htmlFor="source-dsn">
              Connection string
            </label>
            <div className="with-icon" style={{ width: "100%" }}>
              <BrandIcon name={kind ?? "custom"} size="md" label={kind ? engineLabel(kind) : "Not recognised yet"} />
              <input
                id="source-dsn"
                type="password"
                value={dsn}
                placeholder="postgresql://… or mysql://…"
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setDsn(e.target.value)}
                style={{ flex: 1, minWidth: 0 }}
              />
            </div>
            <p className="footnote" style={{ marginTop: 6 }}>
              {dsn && !kind
                ? "Start it with postgresql:// or mysql://."
                : "Stored encrypted. It is never shown again, or sent back to this page."}
            </p>
          </div>
        </div>

        {suggested.length > 0 && (
          <div className="row" style={{ marginTop: 4, flexWrap: "wrap", gap: 8 }}>
            {suggested.map((demo) => (
              <button
                key={demo.dsn}
                type="button"
                className="btn"
                onClick={() => {
                  setDsn(demo.dsn);
                  if (!name || taken.includes(name) || (first && name === "primary")) {
                    setName(first && demo.kind === "postgres" ? "primary" : demo.name);
                  }
                }}
              >
                <span className="with-icon">
                  <BrandIcon name={demo.kind} size="sm" label="" />
                  Use the demo {engineLabel(demo.kind)} database
                </span>
              </button>
            ))}
          </div>
        )}

        {error && (
          <div className="notice bad" style={{ marginTop: 12 }}>
            <div className="notice-head">
              <span aria-hidden="true">✕</span> Could not connect
            </div>
            {error}
          </div>
        )}

        <div className="row" style={{ marginTop: 16 }}>
          <button className="btn primary" disabled={!canSave || busy}>
            {busy ? "Connecting…" : replacing ? "Replace connection" : "Connect"}
          </button>
          {onCancel && (
            <button type="button" className="linkish" onClick={onCancel}>
              cancel
            </button>
          )}
        </div>
      </form>
    </div>
  );
}

/* --------------------------------------------------------------- detail */

function SourceDetail({
  source,
  onDeleted,
  fixed = false,
}: {
  source: SourceInfo;
  onDeleted: () => void;
  fixed?: boolean;
}) {
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .schema(source.name)
      .then(setSchema)
      .catch((exc) => setSchemaError(exc instanceof ApiError ? exc.message : String(exc)));
  }, [source.name]);

  async function remove() {
    setError(null);
    try {
      await api.deleteSource(source.name);
      onDeleted();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }

  const inUse = source.triggers.length > 0;

  return (
    <>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div className="with-icon" style={{ gap: 14 }}>
            <BrandIcon name={source.kind} size="lg" />
            <div>
              <h2 style={{ marginBottom: 2 }}>
                {source.name}{" "}
                {source.default && (
                  <span className="tag" style={{ verticalAlign: "middle" }}>
                    default
                  </span>
                )}
              </h2>
              <p className="hint" style={{ margin: 0 }}>
                {source.label} · {source.summary}
              </p>
            </div>
          </div>
          {!fixed && (
            <button
              className="linkish"
              onClick={remove}
              disabled={inUse}
              title={inUse ? "Point its triggers at another source first" : undefined}
            >
              delete
            </button>
          )}
        </div>

        {error && (
          <div className="notice bad" style={{ marginTop: 12 }}>
            {error}
          </div>
        )}

        <div className="kpi-row" style={{ marginTop: 14 }}>
          <div className="stat">
            <div className="label">Engine</div>
            <div className="value" style={{ fontSize: 18 }}>
              {source.label}
            </div>
            <div className="sub">read-only session</div>
          </div>
          <div className="stat">
            <div className="label">Tables</div>
            <div className="value" style={{ fontSize: 18 }}>
              {schema ? schema.table_count : "…"}
            </div>
            <div className="sub">{schema ? `in ${schema.schema}` : "reading the schema"}</div>
          </div>
          <div className="stat">
            <div className="label">Triggers</div>
            <div className="value" style={{ fontSize: 18 }}>
              {source.triggers.length}
            </div>
            <div className="sub">
              {source.default ? "including any that name no source" : "that name this source"}
            </div>
          </div>
        </div>

        {inUse && (
          <p className="hint" style={{ marginTop: 14, marginBottom: 0 }}>
            Read by{" "}
            {source.triggers.map((name, i) => (
              <span key={name}>
                {i > 0 && ", "}
                <Link to={href("triggers", name)} className="ref">
                  {name}
                </Link>
              </span>
            ))}
            .
          </p>
        )}
      </div>

      <div className="card">
        <h2>What it can see</h2>
        <p className="hint">
          Read live from the database through this connection, so it is exactly what
          a trigger's query can join.
        </p>
        {schemaError && <div className="notice bad">{schemaError}</div>}
        {schema && <SchemaBrowser tables={schema.tables} />}
      </div>
    </>
  );
}
