import { useState } from "react";

import { ApiError, api } from "../api";

/* "Simulate new activity": a burst of rows in the sample database.
 *
 * Only rendered where the server says it is configured -- a demo deployment.
 * Rowfire never writes to a database it reads; this runs a fixed script
 * through a separate login the demo provides, and polls straight after, so
 * whatever it sets off shows up in seconds.
 */
export function SimulateButton({
  primary = false,
  onDone,
}: {
  primary?: boolean;
  onDone?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.simulateActivity();
      setNote("Added new rows to the sample database. Watch for what fires.");
      onDone?.();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="simulate">
      <button
        className={primary ? "btn primary" : "btn"}
        onClick={() => void run()}
        disabled={busy}
      >
        {busy ? "Adding activity…" : "Simulate new activity"}
      </button>
      {note && !error && <span className="muted simulate-note">{note}</span>}
      {error && <span className="bad-text simulate-note">{error}</span>}
    </span>
  );
}
