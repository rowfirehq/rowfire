# rowfire-sdk (Python, prototype)

Business events from inside the app, so Rowfire never needs a database login.

Installed once, the SDK tags each database write with the operation that caused
it, such as a route or a job. The same `charges.status → refunded` is then a
*goodwill refund* when it comes from `POST /admin/charges/{id}/refund` and a
*failed-shipment refund* when it comes from `job AutoRefundWorker`.
Subscriptions are fetched from Rowfire at runtime and matched **inside the
app**, so only the events a subscription asks for leave the process, with only
the fields it names. Adding an event is a config change, not a deploy.

So that someone can write a subscription without reading the code, the SDK
also sends a **catalog**: the operations the app has, the tables each one
writes, the columns that change and the values they take.

This is a prototype of the app side only. Rowfire has no `/v1/events`,
`/v1/config` or `/v1/catalog` endpoint yet; the tests and the benchmark run against
`rowfire_sdk.testing.FakeIngest`.

## Using it

```python
import rowfire_sdk as rowfire
from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy
from rowfire_sdk.integrations.asgi import RowfireMiddleware

rowfire.init(
    endpoint="https://rowfire.internal",
    api_key=os.environ["ROWFIRE_API_KEY"],
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

An optional **ceiling**, set in code and never by config, bounds what any
subscription can reach: `rowfire.Ceiling(allow_operations=[...],
deny_fields=["*email*"])`. An operation outside `allow_operations` matches
nothing. A subscription that asks for a denied field, or tests one in `where`,
is rejected and listed in `stats()["rejected_subscriptions"]`. Testing counts
because an event that fires only for `@bigcorp.com` addresses reveals
something about the address. None is set by default; deciding the defaults is
left for later.

## Catalog

What the dashboard needs so that people pick instead of type. Sent to
`POST /v1/catalog` every `catalog_interval` seconds (60 by default), on the same
background thread as events:

```json
{
  "operations": {
    "POST /admin/charges/{id}/refund": {
      "declared": true, "seen": 41, "errors": 0, "statuses": {"200": 41},
      "params": {"id": {"many_values": true}},
      "actor": {"role": {"values": [{"value": "support", "seen": 41}]}},
      "writes": [{"table": "charges", "kind": "update", "seen": 38, "sampled": 38,
                  "changed": {"status": 38, "refunded_at": 38}}]
    }
  },
  "tables": {
    "charges": {
      "columns": {
        "status": {"type": "string", "nullable": false, "primary_key": false,
                   "values": [{"value": "paid", "seen": 12}, {"value": "refunded", "seen": 38}]},
        "plan":   {"type": "enum", "declared_values": ["free", "team", "enterprise"], "...": "..."}
      },
      "transitions": {"status": {"values": [{"from": "paid", "to": "refunded", "seen": 38}]}}
    }
  }
}
```

Where each part comes from:

- **Declared**, from code: every route the router knows, listed on the first
  request even if nobody has called it yet; each table's columns, types,
  foreign keys and enum values, from the ORM's models.
- **Counted**: every operation (calls, errors, statuses) and every committed
  write, by operation, table and kind. A write that rolls back is not counted.
- **Observed**, from data: on sampled writes, the values each column takes and,
  for updates, what it changed from and to; and the values of actor attributes
  and path parameters.

Observed values are real data, and nothing restricts them yet. Two settings
keep the catalog useful:

- `catalog_max_values` (20): a column with more distinct values than this
  (ids, emails, amounts) is marked `many_values` and its list is dropped.
- `catalog_samples_per_minute` (100): how many writes per operation, table and
  kind are read for values each minute. Every write is still counted.

Counts are cumulative per process, tagged with an instance id, so Rowfire keeps
the latest snapshot per instance and adds them up; a failed send is carried by
the next one. `catalog=False` turns it off.

## Delivery

The request thread only matches and appends to a list. One daemon thread per
process does everything else. The design follows `sentry-python`
(`sentry_sdk/_batcher.py`, `worker.py`):

- **Copied from Sentry:** a lazy thread owned by a pid, `register_at_fork`
  resets, a re-entry guard, Sentry's timing (send every 5 seconds plus a random
  0 to 1 second, or as soon as a batch fills; `flush_interval` and
  `flush_jitter`), gzip, a kept-alive
  connection, respect for `Retry-After`, and loss counts that ride along with
  the next batch.
- **Different from Sentry:**
  - A failed send is **retried** with backoff; Sentry drops it.
  - A batch waiting for a retry goes to a **spool**, in memory or on disk. Disk
    spool files carry the writer's pid, so workers sharing a directory each send
    their own, and adopt a dead process's. `spool_dir` is checked at startup by
    writing a file; if it cannot be used (read-only filesystem, permissions),
    the SDK logs why and keeps retries in memory instead of failing the app.
  - Events are **held until the transaction commits** and dropped on rollback.
  - Nothing is sampled.
- **A backlog moves to the spool instead of being dropped.** Past `max_queue`
  (10,000 queued events) the background thread sends the backlog through the
  spool, where an event takes about 266 bytes of JSON instead of about 624 as
  Python objects (or none, on disk). The request thread only drops past
  `max_queue_hard` (50,000, about 30 MB at worst), so a job that updates
  15,000 bookings in one transaction delivers all 15,000. A single commit
  larger than `max_queue_hard` still loses the excess, counted; raise it for
  such jobs. While batches wait in the spool, newer events queue behind them,
  so events arrive in the order they happened.
- **Events are not lost silently.** Every event carries a random id, so Rowfire
  can drop the copy a retry makes. When an event is lost (the in-memory queue
  is full, the spool is over its cap, or Rowfire rejected the batch), the count
  and reason go to Rowfire with the next batch and to `stats()`.

## Cost

`PYTHONPATH=sdk/python/src uv run python sdk/python/bench/bench_overhead.py`,
one run on a shared cloud container (Python 3.13), so read these as orders of
magnitude:

| | catalog off | catalog on |
| --- | ---: | ---: |
| operation nobody subscribes to | 2.3 µs | 4.0 µs |
| operation that matches: event built + queued | 8.5 µs | 15 µs |
| encode + gzip, per event, background thread | 4.5 µs | |

The catalog's share is recording each operation's actor and parameters.

For ORM writes (an update and commit on in-memory SQLite, about 700 µs), the
hook's cost is smaller than this machine's run-to-run spread. The benchmark
shows +35 to +50 µs for a write nobody subscribes to and +75 to +100 µs for a
matched one, while an interleaved comparison of catalog on and off came out
within noise. A profile puts the SDK's own work at roughly 10 to 20 µs per
write. Against a real database, where a commit is a network round trip, it is
smaller still in relative terms.

With Rowfire taking a full second to answer each batch, per-call latency for
matched operations matched the latency against an instant Rowfire (p50 within
a few µs). So the network is off the request's path. The gap between those
p50s and the table above is the benchmark loop pacing itself (the control case
in the same loop measures that). The background thread's own work
holds the GIL, so at very high event rates delivery does compete with the app
for CPU.

## Not done yet

- Rowfire's side: the ingest, config and catalog endpoints, dedup by event id,
  an event store that triggers can query, and the picker UI over the catalog.
- Limits on what the catalog observes (opt-in per column, for instance), and a
  default ceiling.
- Core SQL (`session.execute(update(...))`) bypasses the ORM's unit of work and
  is not seen.
- An event matched inside a SAVEPOINT that rolls back while the outer
  transaction commits is still sent.
- Django, Celery and other integrations, and an outbox option for
  events that must survive a crash between commit and send.

## Walkthrough

```bash
PYTHONPATH=sdk/python/src uv run python sdk/python/examples/walkthrough.py
```

Follows refunds through the SDK: delivered, retried after a 503, rolled back,
and ignored. It prints each component as it acts and the thread it runs on,
then the catalog the dashboard would receive.

## Seeing the traffic

```bash
PYTHONPATH=sdk/python/src uv run python sdk/python/examples/send_to_endpoint.py URL
```

Runs a small FastAPI app with made-up data and sends its real traffic to `URL`:
event batches to `URL/v1/events` and the catalog to `URL/v1/catalog`. Point it
at a request inspector (Pipedream, webhook.site) to read every request. Bodies
go as plain JSON there; `--gzip` compresses them as the SDK does by default,
and `--show` prints each one locally too.

Two client options exist for this and for real networks:

- `compress=False` sends plain JSON instead of gzip, for request inspectors.
- `proxy` (default: from the environment) is read like curl and requests do:
  `HTTPS_PROXY`, skipped for hosts in `NO_PROXY`, with credentials in the URL
  sent as `Proxy-Authorization`. The SDK tunnels with CONNECT, so TLS runs end
  to end with Rowfire. Pass a URL to choose one, or `False` to go direct.

## Tests

```bash
uv run pytest sdk/python/tests
```

They also run as part of the root suite. They need SQLAlchemy and FastAPI from
the root environment; the SDK itself uses only the standard library and
supports Python 3.9+.
