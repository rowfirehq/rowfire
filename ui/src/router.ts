import { useSyncExternalStore, type MouseEvent } from "react";

/* Deep links, on the History API and nothing else.
 *
 * A URL names a thing that exists: a trigger, a rule, a connection, an
 * action. Forms for something not yet created live at the bare list path
 * (/rules is the new-rule form), because a name like `new` is a perfectly
 * valid rule name and a reserved segment would make that rule unlinkable.
 *
 *   /start                         Get started: a first rule, end to end
 *   /connect                       what is connected, and the choice of how
 *                                  to connect more: an app, or a database
 *   /connect/<name>                one connection: a database and what it can
 *                                  see, or an app and how to send from it
 *   /connect/<name>/<platform>     an app's steps on one platform (python, frappe)
 *   /triggers                      list, new-trigger form
 *   /triggers/<name>               one trigger
 *   /rules                         list, new-rule form
 *   /rules/<name>                  one rule
 *   /actions                       what rules can do, by the account it runs through
 *   /actions/<name>                one account (an integration: Slack, an API)
 *   /actions/<name>/<action>       one of its actions, open for editing
 *   /activity
 *   /inbox                         what rules posted to the Demo inbox
 *
 * Older addresses still resolve: /sources and /setup to /connect, and
 * /integrations (with its /<name>/actions/<action> form) to /actions.
 *
 * Integrations are addressed by name rather than id so a link survives the
 * integration being deleted and re-created, and reads as what it points at.
 */

export type View = "start" | "connect" | "triggers" | "rules" | "actions" | "activity" | "inbox";

export const VIEWS: View[] = ["start", "connect", "triggers", "rules", "actions", "activity", "inbox"];

/** Old paths that still resolve, and where they went. */
export const ALIASES: Record<string, View> = {
  setup: "connect",
  sources: "connect",
  integrations: "actions",
};

export interface Route {
  /** null for `/`, which the shell redirects; "unknown" for anything else. */
  view: View | null | "unknown";
  /** The connection, trigger, rule or integration named in the path. */
  item: string | null;
  /** The action named under an integration, or the platform under an app. */
  action: string | null;
}

function decode(segment: string): string | null {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
}

export function parse(pathname: string): Route {
  const raw = pathname.split("/").filter(Boolean);
  const segments = raw.map(decode);
  if (segments.some((s) => s === null)) return { view: "unknown", item: null, action: null };
  const [first, item, sub, action, ...rest] = segments as string[];
  const head = first !== undefined ? (ALIASES[first] ?? first) : undefined;

  if (head === undefined) return { view: null, item: null, action: null };
  if (!(VIEWS as string[]).includes(head) || rest.length > 0) {
    return { view: "unknown", item: null, action: null };
  }
  const view = head as View;

  if (item === undefined) return { view, item: null, action: null };
  if (view === "activity" || view === "inbox" || view === "start") return { view: "unknown", item: null, action: null };

  if (sub === undefined) return { view, item, action: null };
  if (view === "actions" || view === "connect") {
    // /actions/<name>/<action>, and the older /integrations/<name>/actions/<action>.
    if (action === undefined) return { view, item, action: sub };
    if (view === "actions" && sub === "actions") return { view, item, action };
  }
  return { view: "unknown", item: null, action: null };
}

export function href(view: View, item?: string | null, action?: string | null): string {
  let path = `/${view}`;
  if (item) path += `/${encodeURIComponent(item)}`;
  if (item && action) path += `/${encodeURIComponent(action)}`;
  return path;
}

/** Whether `pathname` is an older address, to be replaced by its current one. */
export function isAlias(pathname: string): boolean {
  const first = pathname.split("/").filter(Boolean)[0];
  return first !== undefined && first in ALIASES;
}

/* ------------------------------------------------------------- the store */

const CHANGE = "rowfire:navigate";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("popstate", onChange);
  window.addEventListener(CHANGE, onChange);
  return () => {
    window.removeEventListener("popstate", onChange);
    window.removeEventListener(CHANGE, onChange);
  };
}

function snapshot(): string {
  return window.location.pathname;
}

export function navigate(path: string, { replace = false } = {}): void {
  if (path === window.location.pathname) return;
  if (replace) window.history.replaceState(null, "", path);
  else window.history.pushState(null, "", path);
  window.dispatchEvent(new Event(CHANGE));
}

/** The current route. Re-renders on navigation and on back/forward. */
export function useRoute(): Route {
  const pathname = useSyncExternalStore(subscribe, snapshot);
  return parse(pathname);
}

/**
 * Click handler for an <a href> that navigates in-page, while leaving
 * modified clicks (new tab, new window, download) to the browser.
 */
export function follow(event: MouseEvent<HTMLAnchorElement>, path: string): void {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  ) {
    return;
  }
  event.preventDefault();
  navigate(path);
}
