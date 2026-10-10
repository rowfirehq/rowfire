/* A few named moments in the UI, as browser events a deployment can listen for.
 *
 * Rowfire tracks nothing itself. A deployment that adds its own markup through
 * ROWFIRE_HEAD_HTML (the public demo adds analytics there) can listen on
 * `window`; with no listener, these do nothing.
 *
 *   rowfire:moment    detail { name }, at most once per name per page load:
 *                       "first_delivery"  a live rule delivered to the Demo inbox
 *   rowfire:feedback  "Give feedback" was clicked. A listener that handles it
 *                     in place calls preventDefault(), and the link is not
 *                     followed.
 */

const sent = new Set<string>();

export function moment(name: string): void {
  if (sent.has(name)) return;
  sent.add(name);
  window.dispatchEvent(new CustomEvent("rowfire:moment", { detail: { name } }));
}

/** Whether a listener took over the feedback click. */
export function feedbackHandled(): boolean {
  return !window.dispatchEvent(new CustomEvent("rowfire:feedback", { cancelable: true }));
}
