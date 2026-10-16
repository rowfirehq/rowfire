# rowfire-sdk (Python, prototype)

Business events from inside the app, so Rowfire never needs a database login.

Installed once, the SDK tags each database write with the operation that caused
it, such as a route or a job. The same `charges.status → refunded` is then a
*goodwill refund* when it comes from `POST /admin/charges/{id}/refund` and a
*failed-shipment refund* when it comes from `job AutoRefundWorker`.
Subscriptions are fetched from Rowfire at runtime and matched **inside the
app**, so only the events a subscription asks for leave the process, with only
the fields it names. Adding an event is a config change, not a deploy.

This is a prototype of the app side only. Rowfire has no `/v1/events` or
`/v1/config` endpoint yet; the tests and the benchmark run against
`rowfire_sdk.testing.FakeIngest`.

## Using it

```python
import rowfire_sdk as rowfire
from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy
from rowfire_sdk.integrations.asgi import RowfireMiddleware

rowfire.init(
    endpoint="https://rowfire.internal",
    api_key=os.environ["ROWFIRE_API_KEY"],
    # The app's side of the contract: no subscription can reach past it.
    ceiling=rowfire.Ceiling(
        allow_operations=["POST /admin/*", "POST /checkout", "job *"],
        deny_fields=["*email*", "*password*", "*token*", "ssn"],
    ),
    spool_dir="/var/lib/myapp/rowfire",  # optional: survive restarts
)
rowfire_sqlalchemy.instrument()  # every Session
app.add_middleware(RowfireMiddleware)  # FastAPI / Starlette

# In auth middleware, once:
rowfire.set_actor(id=user.id, role=user.role)

# Jobs and commands name themselves:
with rowfire.operation("job AutoRefundWorker", actor={"role": "system"}):
    ...
```

The middleware names a request by its route *template*
(`POST /charges/{id}/refund`), not the URL.

## Subscriptions

```json
{
  "id": "goodwill_refund",
  "operation": "POST /admin/charges/{id}/refund",
  "on": {"table": "charges", "kind": ["update"], "changed": "status"},
  "where": {"row.status": "refunded", "actor.role": "support"},
  "fields": ["row.id", "row.amount", "old.status", "actor.id", "params.id"]
}
```

- `operation`: an exact name or a glob (`POST /admin/*`).
- `on`: a table write inside that operation. Without it, the subscription fires
  when the operation ends without an exception and with a status below 400.
- `where`: equality conditions, or a list meaning "any of".
- Field paths: `row.<column>` (after the write), `old.<column>` (before it),
  `actor.<attr>`, `params.<name>`, `operation`, `response.status`.

The **ceiling** is set in code, never by config. An operation outside
`allow_operations` matches nothing. A subscription that asks for a denied
field, or tests one in `where`, is rejected and listed in
`stats()["rejected_subscriptions"]`. Testing counts because an event that fires
only for `@bigcorp.com` addresses reveals something about the address.

## Delivery

The request thread only matches and appends to a list. One daemon thread per
process does everything else. The design follows `sentry-python`
(`sentry_sdk/_batcher.py`, `worker.py`):

- **Copied from Sentry:** a lazy thread owned by a pid, `register_at_fork`
  resets, a re-entry guard, a jittered flush interval, gzip, a kept-alive
  connection, respect for `Retry-After`, and loss counts that ride along with
  the next batch.
- **Different from Sentry:**
  - A failed send is **retried** with backoff; Sentry drops it.
  - A batch waiting for a retry goes to a **spool**, in memory or on disk. Disk
    spool files carry the writer's pid, so workers sharing a directory each send
    their own, and adopt a dead process's.
  - Events are **held until the transaction commits** and dropped on rollback.
  - Nothing is sampled.
- **Events are not lost silently.** Every event carries a random id, so Rowfire
  can drop the copy a retry makes. When an event is lost (the in-memory queue
  is full, the spool is over its cap, or Rowfire rejected the batch), the count
  and reason go to Rowfire with the next batch and to `stats()`.

## Cost

`PYTHONPATH=sdk/python/src uv run python sdk/python/bench/bench_overhead.py`,
one run on a shared cloud container (Python 3.13), so read these as orders of
magnitude:

| | µs |
| --- | ---: |
| operation nobody subscribes to | 2.0 |
| operation that matches: event built + queued | 7.5 |
| encode + gzip, per event, background thread | 4.4 |
| ORM update + commit (SQLite in memory), not instrumented | 552 |
| same, instrumented, operation nobody watches | +1.7 (within noise) |
| same, instrumented, matched | +48 |

With Rowfire taking a full second to answer each batch, per-call latency for
matched operations was p50 36 µs and p99 127 µs. Against an instant Rowfire it
was p50 31 µs and p99 122 µs. So the network is off the request's path. The
remaining gap to the 7.5 µs above is the benchmark loop pacing itself (the
control case in the same loop measures that). The background thread's own work
holds the GIL, so at very high event rates delivery does compete with the app
for CPU.

## Not done yet

- Rowfire's side: the ingest and config endpoints, dedup by event id, and an
  event store that triggers can query.
- Core SQL (`session.execute(update(...))`) bypasses the ORM's unit of work and
  is not seen.
- An event matched inside a SAVEPOINT that rolls back while the outer
  transaction commits is still sent.
- HTTP proxies, Django, Celery and other integrations, and an outbox option for
  events that must survive a crash between commit and send.

## Walkthrough

```bash
PYTHONPATH=sdk/python/src uv run python sdk/python/examples/walkthrough.py
```

Follows refunds through the SDK: delivered, retried after a 503, rolled back,
and ignored. It prints each component as it acts and the thread it runs on.

## Tests

```bash
uv run pytest sdk/python/tests
```

They also run as part of the root suite. They need SQLAlchemy and FastAPI from
the root environment; the SDK itself uses only the standard library and
supports Python 3.9+.
