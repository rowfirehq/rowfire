import { useEffect, useState } from "react";

import { ApiError, api } from "../api";

export function Review({ onSaved }: { onSaved: () => void }) {
  const [yamlText, setYamlText] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    // Prefer whatever is already on disk, so a demo that already has triggers
    // does not lose them behind a freshly generated draft.
    api
      .definitions()
      .then((existing) => setYamlText(existing.yaml_text))
      .catch(() => api.draft().then((draft) => setYamlText(draft.yaml_text)))
      .catch((exc) => setErrors([exc instanceof ApiError ? exc.message : String(exc)]))
      .finally(() => setLoading(false));
  }, []);

  async function regenerate() {
    setBusy(true);
    setErrors([]);
    try {
      setYamlText((await api.draft()).yaml_text);
      setMessage(
        "Regenerated from the live schema — one trigger per table, each with " +
          "no WHERE clause so it matches every row. Narrow them, then save.",
      );
    } catch (exc) {
      setErrors([exc instanceof ApiError ? exc.message : String(exc)]);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    setErrors([]);
    setMessage(null);
    try {
      const saved = await api.saveDefinitions(yamlText);
      setMessage(
        `Stored as version ${saved.version} — ${saved.triggers} triggers, ` +
          `${saved.rules} rules.`,
      );
      onSaved();
    } catch (exc) {
      if (exc instanceof ApiError) {
        setErrors(exc.errors.length ? exc.errors : [exc.message]);
      } else {
        setErrors([String(exc)]);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h2>Definitions</h2>
      <p className="hint">
        The whole definition set as one document: triggers with their SQL, and
        the rules that hang off them. Saving stores a new version in the control
        plane — nothing is accepted until it parses, and every version is kept.
      </p>

      {loading ? (
        <p className="hint">Loading…</p>
      ) : (
        <textarea
          value={yamlText}
          spellCheck={false}
          onChange={(event) => setYamlText(event.target.value)}
          aria-label="definitions document"
        />
      )}

      <div className="row" style={{ marginTop: 14 }}>
        <button className="btn primary" onClick={save} disabled={busy || loading}>
          {busy ? "Saving…" : "Save definitions"}
        </button>
        <button className="btn" onClick={regenerate} disabled={busy || loading}>
          Regenerate from schema
        </button>
      </div>

      {errors.length > 0 && (
        <div className="notice bad">
          <div className="notice-head">
            <span aria-hidden="true">✕</span> Not saved — {errors.length} problem
            {errors.length === 1 ? "" : "s"}
          </div>
          <ul>
            {errors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        </div>
      )}

      {message && !errors.length && (
        <div className="notice good">
          <div className="notice-head">
            <span aria-hidden="true">✓</span> {message}
          </div>
        </div>
      )}
    </div>
  );
}
