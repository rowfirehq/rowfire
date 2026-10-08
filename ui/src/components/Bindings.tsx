import { useMemo, useState } from "react";

import {
  ApiError,
  api,
  type BindingInfo,
  type IntegrationInfo,
  type ParameterInfo,
} from "../api";
import { href } from "../router";
import { BrandIcon } from "./BrandIcon";
import { Link } from "./Link";

/* What happens when a rule fires.
 *
 * One action per fired row. A rule can have several actions and each produces
 * its own request; nothing is chained and no value is carried between them.
 *
 * Bound to the rule, not the trigger. Two rules on one query usually want
 * different messages -- ops wants a channel post, the customer wants a text --
 * and a binding on the trigger would send both from both.
 */
/** A hint shaped like the thing being asked for. */
function placeholderFor(parameter: ParameterInfo): string {
  switch (parameter.type) {
    case "number":
      return "{{ total_amount }}";
    case "boolean":
      return "true";
    case "json":
      return '{"key": "value"}';
    default:
      return parameter.name === "channel" ? "#ops" : "order {{ id }}";
  }
}

export function Bindings({
  ruleName,
  integrations,
  bindings,
  templateColumns,
  onChanged,
}: {
  ruleName: string;
  integrations: IntegrationInfo[];
  bindings: BindingInfo[];
  /** Columns the trigger's query returns, from a backtest's cursor. */
  templateColumns: string[];
  onChanged: () => void;
}) {
  const [integrationName, setIntegrationName] = useState("");
  const [actionName, setActionName] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [recipient, setRecipient] = useState("");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  const integration = integrations.find((c) => c.name === integrationName);
  const action = integration?.actions.find((a) => a.name === actionName);
  const parameters: ParameterInfo[] = useMemo(() => action?.parameters ?? [], [action]);

  async function add() {
    setBusy(true);
    setErrors([]);
    try {
      await api.createBinding(ruleName, integrationName, actionName, values, recipient);
      setValues({});
      setRecipient("");
      setActionName("");
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

  async function remove(id: string) {
    try {
      await api.deleteBinding(id);
      onChanged();
    } catch (exc) {
      setErrors([exc instanceof ApiError ? exc.message : String(exc)]);
    }
  }

  return (
    <div className="card">
      <h2>Then do this</h2>
      <p className="hint">
        Actions run in shadow until the rule is promoted — fully rendered and
        recorded, but not sent.
      </p>

      {bindings.length > 0 && (
        <div className="table-scroll" style={{ marginBottom: 16 }}>
          <table>
            <thead>
              <tr>
                <th>integration</th>
                <th>action</th>
                <th>parameters</th>
                <th>capped per</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {bindings.map((binding) => (
                <tr key={binding.id}>
                  <td>
                    <span className="with-icon">
                      <BrandIcon
                        name={integrations.find((c) => c.name === binding.integration)?.provider}
                        size="sm"
                        label=""
                      />
                      <Link to={href("integrations", binding.integration)} className="ref">
                        {binding.integration}
                      </Link>
                    </span>
                  </td>
                  <td>
                    <Link
                      to={href("integrations", binding.integration, binding.action)}
                      className="ref"
                    >
                      {binding.action}
                    </Link>
                  </td>
                  <td className="muted" style={{ whiteSpace: "normal" }}>
                    {Object.entries(binding.parameters)
                      .map(([k, v]) => `${k}: ${v}`)
                      .join(" · ")}
                  </td>
                  <td className="muted">
                    {binding.recipient_template || "the whole channel"}
                  </td>
                  <td>
                    <button className="linkish" onClick={() => remove(binding.id)}>
                      remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {integrations.length === 0 ? (
        <div className="notice warn">
          No integrations yet. Add one under <strong>Integrations</strong> — pick
          Slack or Braze from the catalogue, or describe your own REST API.
        </div>
      ) : (
        <>
          <div className="grid-2">
            <div>
              <span className="field" id="binding-integration-label">
                Integration
              </span>
              <div
                className="source-picker"
                role="radiogroup"
                aria-labelledby="binding-integration-label"
              >
                {integrations.map((c) => (
                  <button
                    key={c.name}
                    type="button"
                    role="radio"
                    aria-checked={c.name === integrationName}
                    className={`source-option${c.name === integrationName ? " on" : ""}`}
                    onClick={() => {
                      setIntegrationName(c.name);
                      setActionName("");
                      setValues({});
                    }}
                  >
                    <BrandIcon name={c.provider} size="sm" label="" />
                    <strong>{c.name}</strong>
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="field" htmlFor="binding-action">
                Action
              </label>
              <select
                id="binding-action"
                value={actionName}
                disabled={!integration}
                onChange={(e) => {
                  setActionName(e.target.value);
                  setValues({});
                }}
              >
                <option value="">choose…</option>
                {(integration?.actions ?? []).map((a) => (
                  <option key={a.name} value={a.name}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {parameters.map((parameter) => (
            <div key={parameter.name} style={{ marginTop: 12 }}>
              <label className="field" htmlFor={`p-${parameter.name}`}>
                {parameter.label}
                {!parameter.required && <span className="muted"> · optional</span>}
                <span className="muted"> · {parameter.type}</span>
              </label>
              <input
                id={`p-${parameter.name}`}
                type="text"
                value={values[parameter.name] ?? ""}
                placeholder={placeholderFor(parameter)}
                onChange={(e) =>
                  setValues({ ...values, [parameter.name]: e.target.value })
                }
              />
              {parameter.help && <p className="footnote">{parameter.help}</p>}

              {/* The columns this rule's rows actually carry, one click away.
                  Typing {{ customer_id }} correctly from memory is the kind of
                  thing nobody should have to do. */}
              {templateColumns.length > 0 && (
                <div className="picker" style={{ marginTop: 6 }}>
                  {templateColumns.map((column) => (
                    <button
                      key={column}
                      type="button"
                      className="tag"
                      onClick={() =>
                        setValues({
                          ...values,
                          [parameter.name]: `${values[parameter.name] ?? ""}{{ ${column} }}`,
                        })
                      }
                    >
                      {column}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}

          {action && (
            <div style={{ marginTop: 14 }}>
              <label className="field" htmlFor="binding-recipient">
                Count the frequency cap per…
              </label>
              <input
                id="binding-recipient"
                type="text"
                value={recipient}
                placeholder="{{ customer_id }}"
                onChange={(e) => setRecipient(e.target.value)}
              />
              <p className="footnote">
                Leave blank and the cap applies to this integration and action as
                a whole, which is right for an ops channel and wrong for anything
                addressed to a person — one busy day and everyone after the tenth
                is suppressed. Put the customer here and "at most one a week"
                means what it sounds like.
              </p>
            </div>
          )}

          {action && templateColumns.length === 0 && (
            <p className="footnote" style={{ marginTop: 8 }}>
              Run the backtest above and the columns this rule's rows carry will
              appear here, ready to drop in.
            </p>
          )}
          {action && templateColumns.length > 0 && (
            <p className="footnote" style={{ marginTop: 8 }}>
              Click a column to insert it. A template naming something the row
              does not have fails the delivery rather than sending a message with
              a hole in it.
            </p>
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

          <div className="row" style={{ marginTop: 14 }}>
            <button
              className="btn"
              onClick={add}
              disabled={busy || !integrationName || !actionName}
            >
              {busy ? "Adding…" : "Add action"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
