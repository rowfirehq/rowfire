import { useSyncExternalStore, type MouseEvent } from "react";

/* Deep links, on the History API and nothing else.
 *
 * A URL names a thing that exists: a trigger, a rule, an integration, one
 * action on it. Forms for something not yet created live at the bare list
 * path (/rules is the new-rule form), because a name like `new` is a perfectly
 * valid rule name and a reserved segment would make that rule unlinkable.
 *
 *   /start                         Get started: a first rule, end to end
 *   /triggers                      list, new-trigger form
 *   /triggers/<name>               one trigger
 *   /rules                         list, new-rule form
 *   /rules/<name>                  one rule
 *   /integrations                  list
 *   /integrations/<name>           one integration
 *   /integrations/<name>/actions/<action>   one action, open for editing
 *   /activity
 *   /inbox                         what rules posted to the Demo inbox
 *   /sources                       data sources, and the form to add one
 *   /sources/<name>                one data source and what it can see
 *
 * `/setup` was the single-database page; it still resolves, to /sources.
 *
 * Integrations are addressed by name rather than id so a link survives the
 * integration being deleted and re-created, and reads as what it points at.
 */

export type View =
  | "start"
  | "triggers"
  | "rules"
  | "integrations"
  | "activity"
  | "inbox"
  | "sources";

export const VIEWS: View[] = [
  "start",
  "triggers",
  "rules",
  "integrations",
  "activity",
  "inbox",
  "sources",
];

/** Old paths that still resolve, and where they went. */
const ALIASES: Record<string, View> = { setup: "sources" };

export interface Route {
  /** null for `/`, which the shell redirects; "unknown" for anything else. */
  view: View | null | "unknown";
  /** The trigger, rule or integration named in the path. */
  item: string | null;
  /** The action named under an integration. */
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
  if (view === "integrations" && sub === "actions" && action !== undefined) {
    return { view, item, action };
  }
  return { view: "unknown", item: null, action: null };
}

export function href(view: View, item?: string | null, action?: string | null): string {
  let path = `/${view}`;
  if (item) path += `/${encodeURIComponent(item)}`;
  if (item && action) path += `/actions/${encodeURIComponent(action)}`;
  return path;
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
