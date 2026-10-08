import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  api,
  type ActionInfo,
  type CatalogueEntry,
  type IntegrationInfo,
  type ParameterType,
} from "../api";
import { BrandIcon } from "../components/BrandIcon";
import { Link } from "../components/Link";
import { href, navigate } from "../router";

/* Integrations: where the outside world gets connected.
 *
 * Two entities, and the split is the point.
 *
 *   an integration is a named connection that holds credentials —
 *   "Acme Slack". Two Slack workspaces are two integrations.
 *
 *   an action is one REST call on it: method, path, headers, body, with
 *   {{ placeholders }} for whatever the rule supplies.
 *
 * The catalogue is a head start, never a gate. Anything in it can equally be
 * typed in by hand, and what that produces is the same rows — which is the
 * only way the claim "add your own API" survives contact with the second one.
 */

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

const METHODS: Method[] = ["GET", "POST", "PUT", "PATCH", "DELETE"];

interface ParameterRow {
  name: string;
  label: string;
  type: ParameterType;
  help: string;
  required: boolean;
}

interface ActionForm {
  name: string;
  description: string;
  method: Method;
  path: string;
  /** What the action asks for. Declared here, used by the rule screen. */
  parameters: ParameterRow[];
  /** Held as text so a half-typed object does not throw on every keystroke. */
  headers: string;
  body: string;
}

const PARAMETER_TYPES: ParameterType[] = ["string", "number", "boolean", "json"];

const BLANK_ACTION: ActionForm = {
  name: "",
  description: "",
  method: "POST",
  path: "",
  parameters: [],
  headers: "{}",
  body: "{}",
};

/**
 * `selected` and `action` come from the URL: /integrations/<name> and
 * /integrations/<name>/actions/<action>. Adding one is not a place of its
 * own -- it has no name yet -- so it stays local to this screen.
 */
export function Integrations({
  selected,
  action,
}: {
  selected: string | null;
  action: string | null;
}) {
  const [integrations, setIntegrations] = useState<IntegrationInfo[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [catalogue, setCatalogue] = useState<CatalogueEntry[]>([]);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [configured, known] = await Promise.all([api.integrations(), api.catalogue()]);
      setIntegrations(configured.integrations);
      setCatalogue(known.catalogue);
      setLoaded(true);
      setLoadError(null);
    } catch (exc) {
      setLoadError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Following a link to an integration closes the add form.
  useEffect(() => {
    if (selected !== null) setAdding(false);
  }, [selected]);

  const current = integrations.find((i) => i.name === selected) ?? null;
  const missing = loaded && selected !== null && current === null;

  async function remove(integration: IntegrationInfo) {
    setError(null);
    try {
      await api.deleteIntegration(integration.id);
      await load();
      navigate(href("integrations"), { replace: true });
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }

  return (
    <div className="rules">
      <aside className="rule-list">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 14 }}>
          <h2>All integrations</h2>
          <button
            className="btn primary"
            onClick={() => {
              setAdding(true);
              navigate(href("integrations"));
            }}
          >
            Add
          </button>
        </div>

        {integrations.length === 0 && (
          <p className="hint">
            None yet. Add Slack or Braze from the catalogue, or describe your own
            REST API.
          </p>
        )}

        {integrations.map((integration) => (
          <Link
            key={integration.id}
            to={href("integrations", integration.name)}
            className={`rule-card${selected === integration.name ? " active" : ""}`}
            aria-current={selected === integration.name ? "page" : undefined}
            onClick={() => setAdding(false)}
          >
            <div className="rule-card-head">
              <span className="with-icon">
                <BrandIcon name={integration.provider} size="sm" />
                <strong>{integration.name}</strong>
              </span>
              <span className="tag">{integration.provider}</span>
            </div>
            <div className="muted">
              {integration.actions.length} action
              {integration.actions.length === 1 ? "" : "s"}
              {integration.credential_keys.length === 0 && integration.auth_kind !== "none" && (
                <> · no credentials</>
              )}
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

        {error && (
          <div className="notice bad">
            <div className="notice-head">
              <span aria-hidden="true">✕</span> Not saved
            </div>
            {error}
          </div>
        )}

        {adding && (
          <AddIntegration
            catalogue={catalogue}
            onCancel={() => setAdding(false)}
            onSaved={async (name) => {
              setAdding(false);
              await load();
              navigate(href("integrations", name));
            }}
          />
        )}

        {!adding && missing && (
          <div className="card">
            <h2>No integration called {selected}</h2>
            <p className="hint">
              It may have been renamed or deleted. Pick one from the list, or add
              it again.
            </p>
          </div>
        )}

        {!adding && current && (
          <IntegrationDetail
            // One editor per integration, so a half-edited action never
            // follows you to the next one.
            key={current.id}
            integration={current}
            action={action}
            onChanged={load}
            onDelete={() => remove(current)}
          />
        )}

        {!adding && !current && !missing && !loadError && (
          <div className="card">
            <h2>Integrations and actions</h2>
            <p className="hint">
              An <strong>integration</strong> is a named connection that holds
              credentials — your Slack workspace, your Braze account. An{" "}
              <strong>action</strong> is one REST call on it, described as data:
              method, path, headers and body.
            </p>
            <p className="hint">
              Nothing here is special-cased. Slack is defined in exactly the
              format you would use for your own API, so adding a system we have
              never heard of takes the same five minutes.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}

/* ------------------------------------------------------------------ add */

function AddIntegration({
  catalogue,
  onCancel,
  onSaved,
}: {
  catalogue: CatalogueEntry[];
  onCancel: () => void;
  onSaved: (name: string) => void;
}) {
  const [provider, setProvider] = useState<string>(catalogue[0]?.name ?? "custom");
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [authKind, setAuthKind] = useState<"none" | "bearer" | "header">("bearer");
  const [headerName, setHeaderName] = useState("X-API-Key");
  const [credentialKey, setCredentialKey] = useState("token");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  const entry = catalogue.find((c) => c.name === provider) ?? null;
  const custom = provider === "custom";

  useEffect(() => {
    // Picking a catalogue entry prefills everything it already knows, so the
    // only thing left to type is the credential.
    if (entry) {
      setBaseUrl(entry.base_url);
      setName((current) => current || entry.name);
    }
    setValues({});
  }, [provider, entry]);

  async function save() {
    setBusy(true);
    setErrors([]);
    try {
      const created = await api.saveIntegration({
        name,
        description: null,
        provider: custom ? null : provider,
        base_url: baseUrl,
        auth_kind: custom ? authKind : "bearer",
        auth_header_name: custom && authKind === "header" ? headerName : null,
        auth_credential: custom ? credentialKey : (entry?.auth_credential ?? "token"),
        credentials: values,
      });
      onSaved(created.name);
    } catch (exc) {
      setErrors(
        exc instanceof ApiError && exc.errors.length
          ? exc.errors
          : [exc instanceof ApiError ? exc.message : String(exc)],
      );
    } finally {
      setBusy(false);
    }
  }

  const canSave = Boolean(name && baseUrl);

  return (
    <div className="card">
      <h2>Add an integration</h2>
      <p className="hint">
        Start from something we know, or describe your own API. Both produce the
        same thing.
      </p>

      <span className="field" id="int-provider-label">
        System
      </span>
      <div className="source-picker" role="radiogroup" aria-labelledby="int-provider-label">
        {[...catalogue.map((c) => ({ name: c.name, description: c.description })),
          { name: "custom", description: "Any REST API" }].map((option) => (
          <button
            key={option.name}
            type="button"
            role="radio"
            aria-checked={option.name === provider}
            className={`source-option${option.name === provider ? " on" : ""}`}
            onClick={() => setProvider(option.name)}
          >
            <BrandIcon name={option.name} size="sm" label="" />
            <span>
              <strong>{option.name === "custom" ? "Custom" : option.name}</strong>
              {option.description && <span className="muted"> · {option.description}</span>}
            </span>
          </button>
        ))}
      </div>

      <div className="grid-2" style={{ marginTop: 12 }}>
        <div>
          <label className="field" htmlFor="int-name">
            Name
          </label>
          <input
            id="int-name"
            type="text"
            value={name}
            placeholder="acme-slack"
            onChange={(e) => setName(e.target.value)}
          />
          <p className="footnote">
            What you will pick from when attaching an action. Two Slack
            workspaces are two integrations, so name them apart.
          </p>
        </div>
      </div>

      <div style={{ marginTop: 12 }}>
        <label className="field" htmlFor="int-url">
          Base URL
        </label>
        <input
          id="int-url"
          type="text"
          value={baseUrl}
          placeholder="https://api.example.com/v2"
          onChange={(e) => setBaseUrl(e.target.value)}
        />
        {provider === "braze" && (
          <p className="footnote">
            Braze is regional — check which cluster your account is on and change
            this if it is not iad-01.
          </p>
        )}
      </div>

      {custom && (
        <div className="grid-2" style={{ marginTop: 12 }}>
          <div>
            <label className="field" htmlFor="int-auth">
              Authentication
            </label>
            <select
              id="int-auth"
              value={authKind}
              onChange={(e) => setAuthKind(e.target.value as typeof authKind)}
            >
              <option value="none">none</option>
              <option value="bearer">bearer token</option>
              <option value="header">custom header</option>
            </select>
            {authKind === "header" && (
              <input
                style={{ marginTop: 8 }}
                type="text"
                value={headerName}
                placeholder="X-API-Key"
                onChange={(e) => setHeaderName(e.target.value)}
              />
            )}
          </div>
          {authKind !== "none" && (
            <div>
              <label className="field" htmlFor="int-cred-key">
                Credential name
              </label>
              <input
                id="int-cred-key"
                type="text"
                value={credentialKey}
                onChange={(e) => setCredentialKey(e.target.value)}
              />
              <input
                style={{ marginTop: 8 }}
                type="password"
                autoComplete="off"
                placeholder="the value"
                value={values[credentialKey] ?? ""}
                onChange={(e) => setValues({ ...values, [credentialKey]: e.target.value })}
              />
            </div>
          )}
        </div>
      )}

      {!custom && entry && (
        <div style={{ marginTop: 12 }}>
          {entry.credentials.map((credential) => (
            <div key={credential.key} style={{ marginTop: 10 }}>
              <label className="field" htmlFor={`cred-${credential.key}`}>
                {credential.label}
                {!credential.required && <span className="muted"> · optional</span>}
              </label>
              <input
                id={`cred-${credential.key}`}
                type={credential.secret ? "password" : "text"}
                autoComplete="off"
                value={values[credential.key] ?? ""}
                onChange={(e) => setValues({ ...values, [credential.key]: e.target.value })}
              />
              {credential.help && <p className="footnote">{credential.help}</p>}
            </div>
          ))}
          <p className="footnote" style={{ marginTop: 10 }}>
            Comes with {entry.actions.length} action
            {entry.actions.length === 1 ? "" : "s"}:{" "}
            {entry.actions.map((a) => a.name).join(", ")}. You can edit them, and
            add your own.
          </p>
        </div>
      )}

      {errors.length > 0 && (
        <div className="notice bad">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </div>
      )}

      <p className="footnote" style={{ marginTop: 12 }}>
        Credentials are encrypted before they are stored, and no endpoint ever
        returns one — not even to this page.
      </p>

      <div className="row" style={{ marginTop: 14 }}>
        <button className="btn primary" onClick={save} disabled={!canSave || busy}>
          {busy ? "Saving…" : "Add integration"}
        </button>
        <button className="linkish" onClick={onCancel}>
          cancel
        </button>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- detail */

function IntegrationDetail({
  integration,
  action,
  onChanged,
  onDelete,
}: {
  integration: IntegrationInfo;
  /** The action named in the URL, open for editing. */
  action: string | null;
  onChanged: () => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState<ActionForm | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const here = href("integrations", integration.name);

  // The editor follows the URL for existing actions. A new action has no
  // name yet, so its form is local and the URL stays on the integration.
  const appliedFor = useRef<string | null>(null);
  useEffect(() => {
    if (appliedFor.current === action) return;
    if (action === null) {
      setEditing(null);
      appliedFor.current = null;
      return;
    }
    const found = integration.actions.find((a) => a.name === action);
    if (!found) return;
    open(found);
    appliedFor.current = action;
  }, [action, integration.actions]);

  const missingAction =
    action !== null && !integration.actions.some((a) => a.name === action);

  function startNewAction() {
    appliedFor.current = null;
    setErrors([]);
    setEditing({ ...BLANK_ACTION });
    navigate(here);
  }

  function close() {
    setEditing(null);
    navigate(here);
  }

  function open(action: ActionInfo) {
    setErrors([]);
    setEditing({
      name: action.name,
      description: action.description ?? "",
      method: action.method as Method,
      path: action.path,
      parameters: action.parameters.map((p) => ({
        name: p.name,
        label: p.label,
        type: p.type,
        help: p.help ?? "",
        required: p.required,
      })),
      headers: JSON.stringify(action.headers ?? {}, null, 2),
      body: JSON.stringify(action.body ?? {}, null, 2),
    });
  }

  async function saveAction() {
    if (!editing) return;
    setBusy(true);
    setErrors([]);

    let headers: Record<string, unknown>;
    let body: Record<string, unknown>;
    try {
      headers = JSON.parse(editing.headers || "{}");
      body = JSON.parse(editing.body || "{}");
    } catch (exc) {
      // Caught here rather than sent, so the message names JSON rather than
      // arriving as a generic 422 from the server.
      setErrors([`Headers and body must be valid JSON — ${String(exc)}`]);
      setBusy(false);
      return;
    }

    try {
      await api.saveAction(integration.id, editing.name, {
        description: editing.description || null,
        method: editing.method,
        path: editing.path,
        parameters: Object.fromEntries(
          editing.parameters
            .filter((p) => p.name.trim())
            .map((p) => [
              p.name.trim(),
              {
                label: p.label || null,
                type: p.type,
                help: p.help || null,
                required: p.required,
              },
            ]),
        ),
        headers,
        body,
        retry_on: [],
      });
      close();
      onChanged();
    } catch (exc) {
      setErrors(
        exc instanceof ApiError && exc.errors.length
          ? exc.errors
          : [exc instanceof ApiError ? exc.message : String(exc)],
      );
    } finally {
      setBusy(false);
    }
  }

  async function removeAction(name: string) {
    try {
      await api.deleteAction(integration.id, name);
      if (name === action) close();
      onChanged();
    } catch (exc) {
      setErrors([exc instanceof ApiError ? exc.message : String(exc)]);
    }
  }

  return (
    <>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div className="with-icon" style={{ gap: 14 }}>
            <BrandIcon name={integration.provider} size="lg" />
            <div>
              <h2 style={{ marginBottom: 2 }}>{integration.name}</h2>
              <p className="hint" style={{ marginBottom: 0 }}>
                {integration.base_url}
              </p>
            </div>
          </div>
          <button className="linkish" onClick={onDelete}>
            delete
          </button>
        </div>

        <div className="kpi-row" style={{ marginTop: 14 }}>
          <div className="stat">
            <div className="label">Provider</div>
            <div className="value" style={{ fontSize: 18 }}>
              {integration.provider}
            </div>
          </div>
          <div className="stat">
            <div className="label">Authentication</div>
            <div className="value" style={{ fontSize: 18 }}>
              {integration.auth_kind}
            </div>
            {integration.auth_kind !== "none" && (
              <div className="sub">via {integration.auth_credential}</div>
            )}
          </div>
          <div className="stat">
            <div className="label">Credentials</div>
            <div className="value" style={{ fontSize: 18 }}>
              {integration.credential_keys.length}
            </div>
            <div className="sub">
              {integration.credential_keys.join(", ") || "none stored"}
            </div>
          </div>
        </div>

        {integration.auth_kind !== "none" &&
          !integration.credential_keys.includes(integration.auth_credential) && (
            <div className="notice bad" style={{ marginTop: 12 }}>
              This integration signs with <code>{integration.auth_credential}</code>,
              but no such credential is stored. Every action on it will fail until
              you add one.
            </div>
          )}
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h2>Actions</h2>
            <p className="hint" style={{ marginBottom: 0 }}>
              One REST call each. Use <code>{"{{ name }}"}</code> for anything the
              rule supplies.
            </p>
          </div>
          <button className="btn" onClick={startNewAction}>
            New action
          </button>
        </div>

        {missingAction && (
          <div className="notice bad" style={{ marginTop: 12 }}>
            {integration.name} has no action called <code>{action}</code>. It may
            have been renamed or removed.
          </div>
        )}

        {integration.actions.length > 0 && (
          <div className="table-scroll" style={{ marginTop: 14 }}>
            <table>
              <thead>
                <tr>
                  <th>action</th>
                  <th>method</th>
                  <th>path</th>
                  <th>needs</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {integration.actions.map((action) => (
                  <tr key={action.id}>
                    <td>
                      <Link
                        to={href("integrations", integration.name, action.name)}
                        className="ref"
                      >
                        {action.name}
                      </Link>
                      {action.description && (
                        <div className="muted" style={{ fontSize: 11 }}>
                          {action.description}
                        </div>
                      )}
                    </td>
                    <td className="muted">{action.method}</td>
                    <td className="muted">{action.path || "—"}</td>
                    <td className="muted">{action.parameter_names.join(", ") || "nothing"}</td>
                    <td>
                      <Link
                        to={href("integrations", integration.name, action.name)}
                        className="linkish"
                      >
                        edit
                      </Link>{" "}
                      <button className="linkish" onClick={() => removeAction(action.name)}>
                        remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {editing && (
          <div style={{ marginTop: 16 }}>
            <div className="grid-2">
              <div>
                <label className="field" htmlFor="action-name">
                  Name
                </label>
                <input
                  id="action-name"
                  type="text"
                  value={editing.name}
                  placeholder="send_message"
                  onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                />
              </div>
              <div>
                <label className="field" htmlFor="action-method">
                  Method
                </label>
                <select
                  id="action-method"
                  value={editing.method}
                  onChange={(e) =>
                    setEditing({ ...editing, method: e.target.value as Method })
                  }
                >
                  {METHODS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div style={{ marginTop: 12 }}>
              <label className="field" htmlFor="action-path">
                Path
              </label>
              <input
                id="action-path"
                type="text"
                value={editing.path}
                placeholder="/contacts/{{ contact_id }}"
                onChange={(e) => setEditing({ ...editing, path: e.target.value })}
              />
              <p className="footnote">
                Appended to {integration.base_url}. Placeholders work here too.
              </p>
            </div>

            <div style={{ marginTop: 12 }}>
              <label className="field" htmlFor="action-desc">
                Description
              </label>
              <input
                id="action-desc"
                type="text"
                value={editing.description}
                placeholder="Post a message to a channel"
                onChange={(e) => setEditing({ ...editing, description: e.target.value })}
              />
            </div>

            <div style={{ marginTop: 16 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <label className="field" style={{ marginBottom: 0 }}>
                  Parameters
                </label>
                <button
                  type="button"
                  className="linkish"
                  onClick={() =>
                    setEditing({
                      ...editing,
                      parameters: [
                        ...editing.parameters,
                        { name: "", label: "", type: "string", help: "", required: true },
                      ],
                    })
                  }
                >
                  add parameter
                </button>
              </div>
              <p className="footnote">
                What this action asks for. Declaring them is what lets the rule
                screen ask properly — with a label and the right kind of input —
                and what lets a number be sent as a number rather than a string.
              </p>

              {editing.parameters.length === 0 && (
                <p className="hint">
                  None declared. The placeholders in the body will be used
                  instead, all treated as text.
                </p>
              )}

              {editing.parameters.map((parameter, index) => {
                const update = (patch: Partial<ParameterRow>) =>
                  setEditing({
                    ...editing,
                    parameters: editing.parameters.map((p, i) =>
                      i === index ? { ...p, ...patch } : p,
                    ),
                  });
                return (
                  <div
                    key={index}
                    className="picker"
                    style={{ marginTop: 8, display: "block", padding: 10 }}
                  >
                    <div className="grid-2">
                      <input
                        type="text"
                        value={parameter.name}
                        placeholder="channel"
                        aria-label="Parameter name"
                        onChange={(e) => update({ name: e.target.value })}
                      />
                      <select
                        value={parameter.type}
                        aria-label="Parameter type"
                        onChange={(e) => update({ type: e.target.value as ParameterType })}
                      >
                        {PARAMETER_TYPES.map((kind) => (
                          <option key={kind} value={kind}>
                            {kind}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="grid-2" style={{ marginTop: 8 }}>
                      <input
                        type="text"
                        value={parameter.label}
                        placeholder="Channel"
                        aria-label="Label shown on the rule screen"
                        onChange={(e) => update({ label: e.target.value })}
                      />
                      <input
                        type="text"
                        value={parameter.help}
                        placeholder="#ops, or a channel ID"
                        aria-label="Help text"
                        onChange={(e) => update({ help: e.target.value })}
                      />
                    </div>
                    <div className="row" style={{ marginTop: 8 }}>
                      <label className="pick">
                        <input
                          type="checkbox"
                          checked={parameter.required}
                          onChange={(e) => update({ required: e.target.checked })}
                        />
                        required
                      </label>
                      <span className="spacer" />
                      <button
                        type="button"
                        className="linkish"
                        onClick={() =>
                          setEditing({
                            ...editing,
                            parameters: editing.parameters.filter((_, i) => i !== index),
                          })
                        }
                      >
                        remove
                      </button>
                    </div>
                    {parameter.name && (
                      <p className="footnote" style={{ marginTop: 6 }}>
                        Reference it below as{" "}
                        <code>{`{{ ${parameter.name} }}`}</code>
                      </p>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="grid-2" style={{ marginTop: 12 }}>
              <div>
                <label className="field" htmlFor="action-headers">
                  Headers (JSON)
                </label>
                <textarea
                  id="action-headers"
                  className="sql-editor"
                  style={{ minHeight: 110 }}
                  spellCheck={false}
                  value={editing.headers}
                  onChange={(e) => setEditing({ ...editing, headers: e.target.value })}
                />
                <p className="footnote">
                  Authentication is added at send time — do not put a token here.
                </p>
              </div>
              <div>
                <label className="field" htmlFor="action-body">
                  Body (JSON)
                </label>
                <textarea
                  id="action-body"
                  className="sql-editor"
                  style={{ minHeight: 110 }}
                  spellCheck={false}
                  value={editing.body}
                  onChange={(e) => setEditing({ ...editing, body: e.target.value })}
                />
                <p className="footnote">
                  Placeholders work in keys as well as values, so{" "}
                  <code>{'{"{{ field }}": "{{ value }}"}'}</code> sets a field
                  chosen by the rule.
                </p>
              </div>
            </div>

            {errors.length > 0 && (
              <div className="notice bad">
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {errors.map((e) => (
                    <li key={e}>{e}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="row" style={{ marginTop: 14 }}>
              <button
                className="btn primary"
                onClick={saveAction}
                disabled={busy || !editing.name}
              >
                {busy ? "Saving…" : "Save action"}
              </button>
              <button className="linkish" onClick={close}>
                cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
