import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  api,
  NAME_PATTERN,
  type SchemaResponse,
  type SourceInfo,
  type SourceKind,
  type SuggestedSource,
  type SupabaseProject,
} from "../api";
import { BrandIcon, engineLabel } from "../components/BrandIcon";
import { Link } from "../components/Link";
import { SchemaBrowser } from "../components/SchemaBrowser";
import { href, navigate } from "../router";
import { Review } from "../steps/Review";

/* Data sources: the databases Rowfire reads.
 *
 * Any number of them, Postgres, MySQL or Supabase, each a named read-only
 * connection. A Supabase project can also be connected with OAuth instead of
 * a connection string: Supabase sends the browser back here with a grant, and
 * the person picks which project to read.
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
  if (scheme === "supabase") return "supabase";
  return null;
}

const SUPABASE_OAUTH_HINT =
  "Not set up on this server: set SUPABASE_OAUTH_CLIENT_ID and " +
  "SUPABASE_OAUTH_CLIENT_SECRET to sign in with Supabase.";
const SUPABASE_OFF_HINT =
  "Supabase is not set up on this server: set SUPABASE_OAUTH_CLIENT_ID and " +
  "SUPABASE_OAUTH_CLIENT_SECRET, or SUPABASE_ACCESS_TOKEN.";

/** What Supabase's OAuth redirect left in the URL, read once and then cleared. */
interface SupabaseReturn {
  grant: string | null;
  name: string | null;
  error: string | null;
}

function readSupabaseReturn(): SupabaseReturn | null {
  const params = new URLSearchParams(window.location.search);
  const grant = params.get("supabase_grant");
  const error = params.get("supabase_error");
  if (!grant && !error) return null;
  return { grant, name: params.get("name"), error };
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
  const [supabaseReturn, setSupabaseReturn] = useState<SupabaseReturn | null>(readSupabaseReturn);
  const picking = supabaseReturn?.grant ? supabaseReturn : null;

  // Out of the address bar once read, so a reload does not replay a used grant.
  // An effect rather than the initializer, which must stay pure.
  useEffect(() => {
    if (window.location.search.includes("supabase_")) {
      window.history.replaceState(window.history.state, "", window.location.pathname);
    }
  }, []);

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
  // A Supabase error waits for the list too: until it loads, `first` would be
  // guessed, and a wrong guess names the new source `primary` and replaces it.
  const showAdd =
    !fixed && !picking && (adding || (loaded && (supabaseReturn !== null || items.length === 0)));

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
          <p className="hint">None yet. Add a Postgres, MySQL or Supabase database to start.</p>
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

        {picking && (
          <PickSupabaseProject
            grant={picking.grant ?? ""}
            name={picking.name ?? (items.length === 0 ? "primary" : "supabase")}
            taken={items.map((s) => s.name)}
            onCancel={() => setSupabaseReturn(null)}
            onSaved={async (name) => {
              setSupabaseReturn(null);
              await load();
              navigate(href("sources", name));
            }}
          />
        )}

        {showAdd && (
          <AddSource
            first={items.length === 0}
            taken={items.map((s) => s.name)}
            suggested={suggested}
            initialName={supabaseReturn?.name || null}
            supabaseError={supabaseReturn?.error ?? null}
            onCancel={
              items.length > 0
                ? () => {
                    setAdding(false);
                    setSupabaseReturn(null);
                  }
                : undefined
            }
            onSaved={async (name) => {
              setAdding(false);
              setSupabaseReturn(null);
              await load();
              navigate(href("sources", name));
            }}
          />
        )}

        {!showAdd && !picking && missing && (
          <div className="card">
            <h2>No data source called {selected}</h2>
            <p className="hint">It may have been deleted. Pick one from the list, or add it.</p>
          </div>
        )}

        {!showAdd && !picking && current && (
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

        {!showAdd && !picking && !current && !missing && loaded && (
          <>
            <div className="card">
              <h2>Data sources</h2>
              <p className="hint">
                A <strong>data source</strong> is a database Rowfire reads, through a
                read-only connection: PostgreSQL, MySQL or Supabase, as many as you need. Each
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
  initialName,
  supabaseError,
  onCancel,
  onSaved,
}: {
  first: boolean;
  taken: string[];
  suggested: SuggestedSource[];
  /** The name typed before a "Connect Supabase" that came back with an error. */
  initialName: string | null;
  /** Why the last "Connect Supabase" did not finish, from the redirect back. */
  supabaseError: string | null;
  onCancel?: () => void;
  onSaved: (name: string) => void;
}) {
  // The first source is `primary` unless said otherwise, which is the name a
  // trigger with no `source:` reads.
  const [name, setName] = useState(initialName ?? (first ? "primary" : ""));
  const [dsn, setDsn] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // What the server can do with Supabase; null until it has answered, so the
  // options are not shown dimmed for a moment on every open.
  const [supabaseStatus, setSupabaseStatus] = useState<{
    oauth: boolean;
    access_token: boolean;
  } | null>(null);
  const [redirecting, setRedirecting] = useState(false);

  useEffect(() => {
    api
      .supabaseStatus()
      .then(setSupabaseStatus)
      .catch(() => setSupabaseStatus({ oauth: false, access_token: false }));
  }, []);
  const supabaseOAuth = supabaseStatus?.oauth ?? false;
  // Neither a sign-in nor a server token: Supabase cannot be read at all.
  const supabaseOff =
    supabaseStatus !== null && !supabaseStatus.oauth && !supabaseStatus.access_token;
  const tokenMissing = supabaseStatus !== null && !supabaseStatus.access_token;

  const kind = kindOfDsn(dsn);
  const replacing = taken.includes(name);
  const canSave = Boolean(
    NAME_PATTERN.test(name) && dsn.trim() && kind && !(kind === "supabase" && tokenMissing),
  );

  async function connectSupabase() {
    setRedirecting(true);
    setError(null);
    try {
      const { url } = await api.supabaseAuthorize(name);
      window.location.assign(url);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
      setRedirecting(false);
    }
  }

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

      {supabaseError && (
        <div className="notice bad">
          <div className="notice-head">
            <span aria-hidden="true">✕</span> Supabase was not connected
          </div>
          {supabaseError}
        </div>
      )}

      {supabaseStatus !== null && (
        <div className="row" style={{ alignItems: "center", gap: 12, marginBottom: 14 }}>
          <button
            type="button"
            className="btn"
            disabled={!supabaseOAuth || !NAME_PATTERN.test(name) || redirecting}
            title={supabaseOAuth ? undefined : SUPABASE_OAUTH_HINT}
            onClick={connectSupabase}
          >
            <span className="with-icon">
              <BrandIcon name="supabase" size="sm" label="" />
              {redirecting ? "Opening Supabase…" : "Connect Supabase"}
            </span>
          </button>
          <span className="footnote">
            {supabaseOAuth
              ? "No password: approve read-only access in Supabase, then pick a project. " +
                "Supabase runs every query as its own read-only user."
              : SUPABASE_OAUTH_HINT}
          </span>
        </div>
      )}

      <div className="engine-row" aria-label="Supported databases">
        {(["postgres", "mysql", "supabase"] as const).map((engine) => (
          <span
            key={engine}
            className={`engine-chip${kind === engine ? " on" : ""}${
              engine === "supabase" && supabaseOff ? " off" : ""
            }`}
            title={engine === "supabase" && supabaseOff ? SUPABASE_OFF_HINT : undefined}
          >
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
                placeholder="postgresql://…, mysql://… or supabase://<project ref>"
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setDsn(e.target.value)}
                style={{ flex: 1, minWidth: 0 }}
              />
            </div>
            <p className="footnote" style={{ marginTop: 6 }}>
              {dsn && !kind
                ? "Start it with postgresql://, mysql:// or supabase://."
                : kind === "supabase" && tokenMissing
                  ? supabaseOAuth
                    ? "This server has no SUPABASE_ACCESS_TOKEN. Use Connect Supabase instead."
                    : SUPABASE_OFF_HINT
                : kind === "supabase"
                  ? "Read with the server's SUPABASE_ACCESS_TOKEN. Use Connect Supabase to sign in instead."
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
                  Use the demo {engineLabel(demo.kind)}{" "}
                  {demo.kind === "supabase" ? "project" : "database"}
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

/* ------------------------------------------------------------ supabase */

/** After Supabase's consent screen: which project should this source read? */
function PickSupabaseProject({
  grant,
  name: initialName,
  taken,
  onCancel,
  onSaved,
}: {
  grant: string;
  name: string;
  taken: string[];
  onCancel: () => void;
  onSaved: (name: string) => void;
}) {
  const [name, setName] = useState(initialName);
  const [projects, setProjects] = useState<SupabaseProject[] | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .supabaseProjects(grant)
      .then((listed) => {
        setProjects(listed.projects);
        if (listed.projects.length === 1) setChosen(listed.projects[0].ref);
      })
      .catch((exc) => setError(exc instanceof ApiError ? exc.message : String(exc)));
  }, [grant]);

  const replacing = taken.includes(name);
  const canSave = Boolean(NAME_PATTERN.test(name) && chosen) && !busy;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!chosen) return;
    setBusy(true);
    setError(null);
    try {
      const added = await api.addSupabaseSource(name, grant, chosen);
      onSaved(added.name);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="with-icon" style={{ gap: 12, marginBottom: 6 }}>
        <BrandIcon name="supabase" size="md" label="" />
        <h2 style={{ margin: 0 }}>Pick a Supabase project</h2>
      </div>
      <p className="hint">
        Supabase granted read-only access. Choose the project this source reads; its
        queries run through Supabase as <code>supabase_read_only_user</code>, so a write
        is refused by Supabase itself.
      </p>

      <form onSubmit={submit}>
        {projects === null && !error && <p className="hint">Loading your projects…</p>}
        {projects !== null && projects.length === 0 && (
          <p className="hint">This Supabase account has no projects Rowfire can see.</p>
        )}
        {projects !== null && projects.length > 0 && (
          <div role="radiogroup" aria-label="Supabase projects" style={{ marginBottom: 12 }}>
            {projects.map((project) => (
              <label
                key={project.ref}
                className={`rule-card${chosen === project.ref ? " active" : ""}`}
                style={{ display: "block", cursor: "pointer" }}
              >
                <input
                  type="radio"
                  name="supabase-project"
                  value={project.ref}
                  checked={chosen === project.ref}
                  onChange={() => setChosen(project.ref)}
                  style={{ marginRight: 8 }}
                />
                <strong>{project.name ?? project.ref}</strong>
                <div className="muted" style={{ fontSize: 12 }}>
                  {[project.organization, project.region, project.ref].filter(Boolean).join(" · ")}
                  {project.status && project.status !== "ACTIVE_HEALTHY" && ` · ${project.status}`}
                </div>
              </label>
            ))}
          </div>
        )}

        <label className="field" htmlFor="supabase-source-name">
          Name
        </label>
        <input
          id="supabase-source-name"
          type="text"
          value={name}
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

        {error && (
          <div className="notice bad" style={{ marginTop: 12 }}>
            <div className="notice-head">
              <span aria-hidden="true">✕</span> Could not connect
            </div>
            {error}
          </div>
        )}

        <div className="row" style={{ marginTop: 16 }}>
          <button className="btn primary" disabled={!canSave}>
            {busy ? "Connecting…" : replacing ? "Replace connection" : "Connect project"}
          </button>
          <button type="button" className="linkish" onClick={onCancel}>
            cancel
          </button>
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
            <div className="sub">
              {source.kind === "supabase" ? "Supabase's read-only user" : "read-only session"}
            </div>
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
