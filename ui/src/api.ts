// Thin fetch wrapper plus the shapes the API returns.
//
// The HTTP surface is the contract between this front end and the Python side.
// Keep it explicit here so the UI stays replaceable.
//
// The central split: a **trigger** is a SQL query with a clock and a grain, and
// a **rule** points at a trigger and says how often it may fire per key. One
// trigger feeds many rules, which is why scheduling state hangs off the trigger
// and mode hangs off the rule.

/** The database engines a data source can be. */
export type SourceKind = "postgres" | "mysql";

export interface ConnectResponse {
  connected: boolean;
  /** The data source's name, e.g. `primary`. */
  name: string;
  kind: SourceKind;
  server_version: string;
  database: string;
  table_count: number;
  read_only: boolean;
  host_summary: string;
  /** True when the credential was persisted encrypted, not just held in memory. */
  stored: boolean;
}

/** A stored database a trigger can read. Never carries a credential. */
export interface SourceInfo {
  name: string;
  kind: SourceKind | null;
  label: string;
  /** host:port/database, with no user or password. */
  summary: string;
  /** The source a trigger that names none reads. */
  default: boolean;
  /** Triggers that read this source, explicitly or as the default. */
  triggers: string[];
  created_at: string;
}

/** A one-click demo database, offered when adding a source. */
export interface SuggestedSource {
  name: string;
  kind: SourceKind;
  dsn: string;
}

/** Scheduling state for one trigger. No mode here -- that belongs to rules. */
export interface TriggerState {
  name: string;
  enabled: boolean;
  watermark: string | null;
  next_run_at: string | null;
  last_run_at: string | null;
  last_error: string | null;
  poll_interval_seconds: number;
  lookback_seconds: number;
  /** Every rule reading this trigger, and the subset that is live. */
  rules: string[];
  live_rules: string[];
  total_fires: number;
}

export interface TriggerStates {
  halted: boolean;
  halted_reason: string | null;
  triggers: TriggerState[];
}

/** Runtime state for one rule: is it live, and what has it fired. */
export interface RuleState {
  name: string;
  trigger: string;
  mode: "shadow" | "live";
  enabled: boolean;
  watermark: string | null;
  next_run_at: string | null;
  last_error: string | null;
  total_fires: number;
  last_fired_at: string | null;
}

export interface RuleStates {
  halted: boolean;
  halted_reason: string | null;
  rules: RuleState[];
}

export interface Activity {
  runs: {
    trigger: string;
    status: string;
    matched_rows: number;
    fires_new: number;
    fires_suppressed: number;
    /** Deliveries from this run that actually went out. */
    sent: number;
    started_at: string;
    error: string | null;
  }[];
  fires: {
    rule: string;
    entity_id: string | null;
    event_time: string | null;
    fired_at: string;
  }[];
}

/** One thing a rule put in the Demo inbox. */
export interface InboxItem {
  id: string;
  rule: string;
  integration: string;
  mode: "shadow" | "live";
  status: "pending" | "sent" | "failed" | "suppressed";
  /** Why it was not delivered: shadow mode, a frequency cap, or an error. */
  reason: string | null;
  /** How to draw it, from the request path: /messages or /tickets. */
  kind: "message" | "ticket" | "other";
  body: unknown;
  created_at: string;
  sent_at: string | null;
}

export interface Inbox {
  /** Names of the integrations that deliver here. Empty until one is added. */
  installed: string[];
  items: InboxItem[];
}

// ------------------------------------------------------------- authoring

export interface TriggerDraft {
  name: string;
  description: string | null;
  sql: string;
  /** An output column of the query. Required to run live. */
  event_time: string | null;
  /** The grain: which output columns identify one row. */
  key: string[];
  /** The data source it reads; null for the default one. */
  source: string | null;
}

export type Policy = "once_ever" | "once_per_period" | "once_per_n";

export interface RuleDraft {
  name: string;
  trigger: string;
  description: string | null;
  policy: Policy;
  period: "day" | "week" | "month" | null;
  n: number | null;
}

/** What a query returns, read from the cursor rather than parsed. */
export interface OutputColumn {
  name: string;
  type: string;
}

export interface CheckResult {
  valid: boolean;
  errors: string[];
  /** Present whenever the query itself ran, even if the clock or key is wrong. */
  columns: OutputColumn[];
  sql: string | null;
}

export type ParameterType = "string" | "number" | "boolean" | "json";

/** One value an action asks for, declared rather than inferred. */
export interface ParameterInfo {
  name: string;
  label: string;
  type: ParameterType;
  help: string | null;
  required: boolean;
  default: unknown;
}

/** One REST call on an integration, described entirely as data. */
export interface ActionInfo {
  id: string;
  name: string;
  description: string | null;
  method: string;
  path: string;
  headers: Record<string, unknown>;
  body: Record<string, unknown>;
  retry_on: number[];
  /** The declared contract, required first. Row columns are filled at send time. */
  parameters: ParameterInfo[];
  parameter_names: string[];
}

/** A named, configured connection to an outside system. */
export interface IntegrationInfo {
  id: string;
  name: string;
  description: string | null;
  /** The catalogue template it came from, or "custom". */
  provider: string;
  base_url: string;
  auth_kind: "none" | "bearer" | "header";
  auth_header_name: string | null;
  auth_credential: string;
  /** Which credentials are stored — never their values. */
  credential_keys: string[];
  enabled: boolean;
  timeout_ms: number;
  actions: ActionInfo[];
}

/** A system we already know how to talk to. Only ever a head start. */
export interface CatalogueEntry {
  name: string;
  description: string | null;
  base_url: string;
  auth_kind: string;
  auth_credential: string;
  credentials: {
    key: string;
    label: string;
    help: string | null;
    secret: boolean;
    required: boolean;
  }[];
  actions: {
    name: string;
    description: string | null;
    method: string;
    path: string;
    parameters: ParameterInfo[];
  }[];
}

export interface IntegrationDraft {
  name: string;
  description: string | null;
  provider: string | null;
  base_url: string;
  auth_kind: "none" | "bearer" | "header";
  auth_header_name: string | null;
  auth_credential: string;
  /** Write-only: sent on save, never returned by any endpoint. */
  credentials: Record<string, string>;
}

export interface ParameterDraft {
  label: string | null;
  type: ParameterType;
  help: string | null;
  required: boolean;
}

export interface ActionDraft {
  description: string | null;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  parameters: Record<string, ParameterDraft>;
  headers: Record<string, unknown>;
  body: Record<string, unknown>;
  retry_on: number[];
}

export interface BindingInfo {
  id: string;
  rule_name: string;
  integration: string;
  action: string;
  parameters: Record<string, string>;
  /** What the frequency cap counts against. Null means the channel as a whole. */
  recipient_template: string | null;
  enabled: boolean;
}

export interface Limits {
  cap_per_recipient: number;
  cap_window_hours: number;
}

// ---------------------------------------------------------------- schema

export interface ColumnInfo {
  name: string;
  pg_type: string;
  semantic_type: string;
  nullable: boolean;
}

export interface ForeignKeyInfo {
  column: string;
  target_table: string;
  target_column: string;
}

export interface TableInfo {
  name: string;
  primary_key: string | null;
  timestamps: string[];
  columns: ColumnInfo[];
  foreign_keys: ForeignKeyInfo[];
}

export interface SchemaResponse {
  source: string;
  kind: SourceKind;
  schema: string;
  tables: TableInfo[];
  table_count: number;
}

export interface TriggerInfo {
  name: string;
  description: string | null;
  sql: string;
  event_time: string | null;
  key: string[];
  /** The data source it reads; null for the default one. */
  source: string | null;
}

export interface RuleInfo {
  name: string;
  trigger: string;
  description: string | null;
  policy: string;
  period: string | null;
  n: number | null;
}

export interface DefinitionsResponse {
  yaml_text: string;
  triggers: TriggerInfo[];
  rules: RuleInfo[];
}

export interface SeriesPoint {
  date: string;
  count: number;
}

export interface BacktestResult {
  rule: string;
  trigger: string;
  days: number;
  since: string | null;
  until: string | null;
  fires: number;
  matched_rows: number;
  dedup_removed: number;
  unique_keys: number;
  null_event_time_rows: number;
  mean_per_day: number;
  query_ms: number;
  timeless: boolean;
  busiest_day: { date: string; count: number } | null;
  quietest_day: { date: string; count: number } | null;
  series: SeriesPoint[];
  zero_days: number;
  columns: string[];
  /** Semantic type per output column, read from the cursor. */
  column_types: Record<string, string>;
  sample: Record<string, unknown>[];
  sql: string | null;
}

/** Render a value using what the column *means*, not its JS type. */
export function formatCell(value: unknown, semanticType: string | undefined): string {
  if (value === null || value === undefined) return "null";

  switch (semanticType) {
    case "money":
      return typeof value === "number"
        ? value.toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })
        : String(value);
    case "timestamp": {
      const parsed = new Date(String(value));
      if (Number.isNaN(parsed.getTime())) return String(value);
      return parsed.toISOString().replace("T", " ").slice(0, 16);
    }
    case "boolean":
      return value ? "true" : "false";
    case "identifier":
      // Never grouped or formatted: an id is a label that happens to be numeric.
      return String(value);
    case "integer":
      return typeof value === "number" ? value.toLocaleString() : String(value);
    default:
      return String(value);
  }
}

/** Turn one FastAPI validation entry into a sentence naming the field. */
function describeValidationError(entry: {
  loc?: unknown[];
  msg?: string;
  ctx?: { pattern?: string };
}): string {
  // loc is like ["body", "name"]; the wrapper is noise to the reader.
  const field = (entry.loc ?? [])
    .filter((part) => typeof part === "string" && part !== "body")
    .join(".");
  const message = entry.msg ?? "is not valid";

  // The pattern cases are the ones people actually hit, and a raw regex is
  // not an error message anybody can act on.
  if (entry.ctx?.pattern === NAME_PATTERN.source) {
    return `${field || "name"} must be lowercase letters, numbers and underscores, starting with a letter`;
  }
  return field ? `${field}: ${message}` : message;
}

/** The shape every trigger and rule name has to take.
 *
 * These are identifiers: YAML keys, the thing a rule's `trigger` points at,
 * and the name the fire ledger is keyed on. Keeping them to one form is what
 * lets a name be renamed and followed rather than guessed at.
 */
export const NAME_PATTERN = /^[a-z][a-z0-9_]*$/;

/** The name someone probably meant. "Thank The Customer" -> thank_the_customer. */
export function slugifyName(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^([0-9])/, "n$1");
}

export class ApiError extends Error {
  readonly status: number;
  readonly errors: string[];

  constructor(status: number, message: string, errors: string[] = []) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

/** Set across the reload that replaces an expired demo workspace. */
export const EXPIRED_FLAG = "rowfire-demo-expired";

/**
 * A hosted demo visitor's workspace is gone (deleted after a day idle), so
 * every request from this page will now fail. Reload once: the app makes a
 * fresh workspace on load and says why. The flag keeps it to one reload --
 * if the new page fails the same way, it shows the error instead of looping.
 */
function sessionExpired(): void {
  try {
    if (sessionStorage.getItem(EXPIRED_FLAG)) return;
    sessionStorage.setItem(EXPIRED_FLAG, "1");
  } catch {
    return;
  }
  window.location.reload();
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { "content-type": "application/json" },
    ...init,
  });

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    let errors: string[] = [];
    try {
      const body = await response.json();
      const detail = body?.detail;
      if (response.status === 401 && body?.session_required) {
        sessionExpired();
      }
      if (typeof detail === "string") {
        message = detail;
      } else if (Array.isArray(detail)) {
        // FastAPI's own request validation, which has a different shape from
        // ours: a list of {loc, msg}. Without this branch the reason was
        // thrown away and the user got a bare "422 Unprocessable Entity" --
        // the one message that cannot be acted on, in place of one that said
        // exactly which field was wrong.
        errors = detail.map(describeValidationError);
        message = `${errors.length} problem${errors.length === 1 ? "" : "s"} found`;
      } else if (detail?.errors) {
        // The API reports every problem at once rather than the first.
        errors = detail.errors;
        message = `${errors.length} problem${errors.length === 1 ? "" : "s"} found`;
      }
    } catch {
      // Non-JSON error body; keep the status line.
    }
    throw new ApiError(response.status, message, errors);
  }

  return (await response.json()) as T;
}

export const api = {
  health: () =>
    request<{
      ok: boolean;
      connected: boolean;
      allow_write: boolean;
      suggested_dsn: string | null;
      suggested_sources: SuggestedSource[];
      /** Whether "Simulate new activity" is configured. Demo deployments only. */
      demo_activity: boolean;
      /** A public demo, where each visitor has a private workspace. */
      hosted: boolean;
      /** Whether this browser has its workspace yet (always true off hosted). */
      session: boolean;
      /** Hosted: hours without a visit before a workspace is deleted. */
      idle_hours: number | null;
      /** Where "Give feedback" points, when the deployment sets one. */
      feedback_url: string | null;
      /** The definitions version in force, or null when nothing is stored. */
      definitions_version: number | null;
      halted: string | null;
      /** Set when the control plane is not answering. */
      degraded: string | null;
    }>("/health"),

  /** Hosted demo: make this visitor's workspace (idempotent). */
  startSession: () =>
    request<{ workspace: string; created: boolean }>("/session", { method: "POST" }),

  sources: () =>
    request<{ sources: SourceInfo[]; default: string | null; kinds: SourceKind[] }>("/sources"),

  /** Add a data source, or replace the DSN of one with the same name. */
  addSource: (name: string, dsn: string) =>
    request<ConnectResponse>("/sources", {
      method: "POST",
      body: JSON.stringify({ name, dsn }),
    }),

  deleteSource: (name: string) =>
    request<{ deleted: boolean }>(`/sources/${encodeURIComponent(name)}`, { method: "DELETE" }),

  /** A source's live schema; the default source when none is named. */
  schema: (source?: string | null) =>
    request<SchemaResponse>(
      source ? `/schema?source=${encodeURIComponent(source)}` : "/schema",
    ),

  draft: () =>
    request<{ yaml_text: string; table_count: number }>("/definitions/draft", {
      method: "POST",
    }),

  saveDefinitions: (yamlText: string) =>
    request<{ saved: boolean; version: number; triggers: number; rules: number }>(
      "/definitions/save",
      { method: "POST", body: JSON.stringify({ yaml_text: yamlText }) },
    ),

  definitions: () => request<DefinitionsResponse>("/definitions"),

  validate: () => request<{ valid: boolean; errors: string[] }>("/validate"),

  /** Run a draft query with LIMIT 0 and report the columns it returns. */
  checkSql: (sql: string, event_time: string | null, key: string[], source: string | null) =>
    request<CheckResult>("/triggers/check", {
      method: "POST",
      body: JSON.stringify({ sql, event_time, key, source }),
    }),

  upsertTrigger: (originalName: string, draft: TriggerDraft) =>
    request<{ saved: boolean; name: string; version?: number }>(
      `/triggers/${encodeURIComponent(originalName)}`,
      { method: "PUT", body: JSON.stringify(draft) },
    ),

  deleteTrigger: (name: string) =>
    request<{ deleted: boolean }>(`/triggers/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  upsertRule: (originalName: string, draft: RuleDraft) =>
    request<{ saved: boolean; name: string; version?: number }>(
      `/rules/${encodeURIComponent(originalName)}`,
      { method: "PUT", body: JSON.stringify(draft) },
    ),

  deleteRule: (name: string) =>
    request<{ deleted: boolean }>(`/rules/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  catalogue: () => request<{ catalogue: CatalogueEntry[] }>("/catalogue"),

  integrations: () => request<{ integrations: IntegrationInfo[] }>("/integrations"),

  saveIntegration: (draft: IntegrationDraft) =>
    request<{ created: boolean; id: string; name: string }>("/integrations", {
      method: "POST",
      body: JSON.stringify(draft),
    }),

  deleteIntegration: (id: string) =>
    request<{ deleted: boolean }>(`/integrations/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  saveAction: (integrationId: string, name: string, draft: ActionDraft) =>
    request<{ saved: boolean; action: ActionInfo }>(
      `/integrations/${encodeURIComponent(integrationId)}/actions/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify(draft) },
    ),

  deleteAction: (integrationId: string, name: string) =>
    request<{ deleted: boolean }>(
      `/integrations/${encodeURIComponent(integrationId)}/actions/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),

  bindings: () => request<{ bindings: BindingInfo[] }>("/bindings"),

  createBinding: (
    rule_name: string,
    integration: string,
    action: string,
    parameters: Record<string, string>,
    recipient_template?: string | null,
  ) =>
    request<{ created: boolean; id: string }>("/bindings", {
      method: "POST",
      body: JSON.stringify({
        rule_name,
        integration,
        action,
        parameters,
        recipient_template: recipient_template || null,
      }),
    }),

  limits: () => request<Limits>("/limits"),

  setLimits: (limits: Limits) =>
    request<Limits>("/limits", { method: "POST", body: JSON.stringify(limits) }),

  deleteBinding: (id: string) =>
    request<{ deleted: boolean }>(`/bindings/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  /** Scheduling state, per trigger. */
  triggers: () => request<TriggerStates>("/triggers"),

  /** Runtime state, per rule. */
  rules: () => request<RuleStates>("/rules"),

  /** Ask for a poll on the next worker tick. Does not widen the window. */
  runTrigger: (name: string) =>
    request<{ queued: boolean }>(`/triggers/${encodeURIComponent(name)}/run`, {
      method: "POST",
    }),

  /** Move the watermark back so the next poll re-reads older rows. */
  rewindTrigger: (name: string, days: number) =>
    request<{ trigger: string; watermark: string; days: number }>(
      `/triggers/${encodeURIComponent(name)}/rewind`,
      { method: "POST", body: JSON.stringify({ days }) },
    ),

  setMode: (name: string, mode: "shadow" | "live") =>
    request<{ name: string; mode: string }>(`/rules/${encodeURIComponent(name)}/mode`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),

  halt: (reason: string) =>
    request<{ halted: boolean }>("/halt", {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),

  resume: () => request<{ halted: boolean }>("/resume", { method: "POST" }),

  activity: () => request<Activity>("/activity"),

  inbox: () => request<Inbox>("/inbox"),

  /** Demo only: add a burst of rows to the sample database, then poll. */
  simulateActivity: () =>
    request<{ ok: boolean; queued: string[] }>("/demo/activity", { method: "POST" }),

  backtest: (rule: string, days: number, sample: number) =>
    request<BacktestResult>("/backtest", {
      method: "POST",
      body: JSON.stringify({ rule, days, sample }),
    }),
};
