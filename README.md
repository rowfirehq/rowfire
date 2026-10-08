# RowFire

Turn what happens in your database into Slack messages, Zendesk tickets and API
calls, without asking engineering to build each one.

**[Try the live demo](https://demo.rowfire.com)** on sample data: nothing to
install, no accounts to connect. More at [rowfire.com](https://rowfire.com).

Engineering describes an event once, as a SQL query: *a charge was declined*,
*an Enterprise account signed up*. From then on, whoever owns the workflow
(support, billing, sales, growth) subscribes to it. They pick how often it may
fire per customer, attach an action, and switch it on. No application changes,
no new service to deploy, and nothing is ever written to the database being
read.

- **Triggers are the events.** One `SELECT`, a clock column (`event_time`) and
  a key that says what counts as one thing, written by whoever knows the
  schema.
- **Rules are the subscriptions.** How often one key may fire, and what
  happens when it does. Many rules can subscribe to one trigger, which is
  polled once and its rows fanned out, so one automation can go live while
  another on the same query is still running in shadow.
- **Actions are the destinations.** Slack, Zendesk, Braze, or any REST API,
  configured as data rather than code.

Before a rule goes live you can see what it *would* have done: a **backtest**
replays it over the last N days, and **shadow mode** renders every request
without sending it.

The split follows who knows what. Whoever wrote the query knows where
duplication can happen, so the key is a property of the trigger. Whoever owns
the customer experience decides how often to act, so the cadence belongs to the
rule.

**Data sources.** PostgreSQL and MySQL, as many databases as you have: each
trigger names the one it reads. A trigger is a read-only query, polled on a
clock, so new rows are picked up on each poll (within a minute by default)
rather than streamed from the database's change log.

## Demo: a SaaS company, Slack and Zendesk

A five-minute tour on a made-up B2B SaaS product wired to **Slack** and
**Zendesk**. Its data lives in two databases, as it would in real life: the
app's **PostgreSQL** (accounts, billing attempts, trials, an NPS survey) and a
support desk in **MySQL** (tickets). Everything below runs against data
in [`examples/saas`](examples/saas) and [`fixtures/mysql`](fixtures/mysql).

### In your browser

Open **[demo.rowfire.com](https://demo.rowfire.com)**. Each visitor gets a
private workspace with this sample loaded, and it opens at **Get started**,
which sets up a first rule in a couple of minutes. It delivers to the **Demo
inbox**, so nothing is sent anywhere, and **Simulate new activity** gives it
something to fire on. The demo runs the latest `main`.

To run a public demo of your own that anyone can open with a link (a private
workspace per visitor, updated on every merge to `main`), deploy
[`render.yaml`](render.yaml) to Render in one click, or run it on a server of
your own. See [docs/hosting.md](docs/hosting.md).

### On your machine

```bash
cp .env.example .env
echo "ROWFIRE_MASTER_KEY=$(openssl rand -base64 32 | tr '+/' '-_')" >> .env

docker compose -f docker-compose.yml -f examples/saas/compose.yaml up -d
sh examples/saas/connect-actions.sh        # Slack, Zendesk and the Demo inbox; no credentials needed
```

Open <http://127.0.0.1:8000>. It starts at **Get started**, which walks one
rule end to end — connect the sample databases, pick an event, say how often,
write the message, backtest it, turn it on — and delivers to the **Demo
inbox**, so it needs no accounts. The rest of this tour does the same by hand.

To connect by hand instead, open **Data sources**: click *Use the demo
PostgreSQL database*, then **Connect**. Then **Add**, *Use the demo MySQL
database* (it is named `support`), and **Connect** again. If you have run the
stack before, start with `docker compose down -v` first. Otherwise the control
plane keeps the definitions it already has instead of loading the demo's.

![Data sources: a PostgreSQL and a MySQL database, and what each can see](docs/screenshots/data-sources.png)

### 1. Describe the event as SQL

This is the one step for whoever knows the schema. A trigger is one `SELECT`. It can join anything and compute anything; you then
pick which column is the clock and which columns make one row "one thing".
Here, a declined charge, keyed by account. Three declines on one invoice are
still one billing problem.

![Trigger editor: payment_failed](docs/screenshots/trigger-editor.png)

Each trigger names the database it reads. The support desk's trigger reads
`support`, so its query is checked against MySQL, in MySQL's dialect, and the
columns, the clock and the key come from there:

![Trigger editor: a MySQL trigger on the support source](docs/screenshots/trigger-mysql.png)

### 2. Subscribe a rule and attach Slack or Zendesk

This is the part that needs no engineering. A rule points at a trigger and
says how often one key may fire; an action says what to do when it does.
Slack, Zendesk and Braze come from a built-in catalogue, along with a Demo
inbox for trying rules without an account. An integration is
plain data (base URL, auth, and a REST call per action), so your own API
works the same way. Message templates pull any column from the trigger's row
with `{{ column }}`.

![Integrations: Zendesk with its actions](docs/screenshots/integrations.png)

The demo wires up seven rules:

| rule | trigger | fires at most | action | backtest, 120 days |
| --- | --- | --- | --- | --- |
| `announce_enterprise_signup` | Enterprise sign-up | once per account | Slack `#sales` | 26 |
| `billing_heads_up` | charge declined | once per account per day | Slack `#billing` | 126 of 211 rows |
| `billing_ticket` | charge declined | once per account per week | Zendesk ticket, high | 105 of 211 rows |
| `rescue_stalled_trial` | trial ends in 3 days, never activated | once per account | Zendesk ticket for CS | 87 |
| `detractor_follow_up` | NPS score of 6 or below | once per account per month | Zendesk ticket, high | 65 of 120 rows |
| `detractor_to_slack` | NPS score of 6 or below | once per account per day | Slack `#customer-voice` | 120 |
| `escalate_unanswered_urgent` | urgent ticket with no first response (MySQL) | once per ticket | Slack `#support` | 7 |

(Counts are for a freshly seeded database. The screenshots were taken after one
run of `simulate.sql`, so a few of theirs are slightly higher.)

### 3. Check what a rule would have done

**Backtest** runs a rule over the last 120 days without sending anything.
`billing_ticket` (one Zendesk ticket per account per week) turns 211 declined charges into 105
tickets. The other 106 are retries that would have been duplicate tickets.

![Backtest of billing_ticket with a Zendesk action attached](docs/screenshots/rule-backtest.png)

The backtest is also where a subtly wrong query shows itself. The obvious
"Enterprise sign-up" query forgets internal QA tenants, which are Enterprise
too. Backtested side by side, the naive rule would have posted **38**
announcements to `#sales`; the corrected one posts **26**. The example rows
show the `Internal QA` accounts that make up the difference.

| naive query | corrected query |
| --- | --- |
| ![Naive enterprise sign-up rule](docs/screenshots/rule-backtest-naive.png) | ![Corrected rule with a Slack action](docs/screenshots/rule-slack.png) |

### 4. Watch it run in shadow, then promote

The poller starts every trigger at *now*, so history never fires. To see it
react to new rows, add some:

```bash
docker compose exec -T postgres psql -U rowfire -d rowfire_fixture < examples/saas/simulate.sql
docker compose exec -T mysql mysql -uroot -prowfire rowfire_support < examples/saas/simulate-support.sql
```

(In the UI, **Simulate new activity** on Get started and the Demo inbox does
something similar with `examples/saas/activity.sql`, which picks different
accounts each time, and polls straight after.)

Within a minute, **Activity** shows each rule firing in shadow, each trigger
polled against its own database. The request is rendered and recorded but not
sent. Four declined charges for two accounts become two `#billing` messages and
two Zendesk tickets, the QA tenant never reaches `#sales`, and of three urgent
tickets in MySQL, the two that are not spam reach `#support`:

![Activity: rules in shadow, polls and the fire ledger](docs/screenshots/activity.png)

This is what shadow recorded for two of them with real credentials exported
(the token is added only at send time, so it is never stored). Without them the
same bodies go to `inbox://demo/messages` and `inbox://demo/tickets`:

```json
{
  "method": "POST",
  "url": "https://slack.com/api/chat.postMessage",
  "body": {
    "channel": "#sales",
    "text": ":tada: New Enterprise account: *Halcyon Freight* (halcyonfreight.example) — 250 seats in eu-central. Owner: Rosa Lindqvist <rosa@halcyonfreight.example>"
  }
}
```

```json
{
  "method": "POST",
  "url": "https://acme.zendesk.com/api/v2/tickets.json",
  "body": {
    "ticket": {
      "subject": "Payment failed for Kite Labs",
      "comment": { "body": "Hi Femi Adams,\n\nWe could not charge the card on file for invoice INV-5-LIVE ($49.00, expired_card). Could you update your payment details?" },
      "requester": { "name": "Femi Adams", "email": "femi@kitelabs.example" },
      "priority": "high",
      "tags": ["billing", "payment_failed"]
    }
  }
}
```

With no credentials exported, the script points every rule at the **Demo
inbox** instead of Slack and Zendesk. Press **promote to live** on a rule, add
activity again, and its messages and tickets appear on the **Demo inbox** page,
drawn the way Slack and Zendesk would show them, without anything leaving the
machine. Shadow rules show up there too, labelled with what they *would* have
posted.

To use the real systems, export credentials (see the top of
`connect-actions.sh`), run it again, and promote. **Stop everything** is the
kill switch.

## Quickstart

```bash
cp .env.example .env
```

Put a master key in it — it wraps every stored credential, so keep the real one
somewhere durable:

```bash
echo "ROWFIRE_MASTER_KEY=$(openssl rand -base64 32 | tr '+/' '-_')" >> .env
```

```bash
docker compose up -d
```

That brings up two fixture databases (PostgreSQL and MySQL), the control
plane, the UI and the poller, and migrates and seeds the control plane on the
way. Open <http://127.0.0.1:8000> (set `ROWFIRE_UI_PORT` if 8000 is taken) and
click *Use the demo PostgreSQL database*.

### Local development

```bash
uv sync && docker compose up -d postgres mysql controlplane
```

The fixture marketplace database lands on host port **5433** (5432 is too often
already taken; override with `ROWFIRE_PG_PORT`), the MySQL support desk on
**3307** (`ROWFIRE_MYSQL_PORT`) and the control plane on **5434**. Then:

```bash
export ROWFIRE_PLATFORM_DSN='postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform'
export DATABASE_URL='postgresql://rowfire_ro:rowfire_ro@localhost:5433/rowfire_fixture'
uv run alembic upgrade head
uv run rowfire init
uv run rowfire run order_row_once --days 120
```

## The UI

A local web surface for the same engine, for when a terminal is the wrong thing
to put in front of someone. The places are separate jobs rather than steps in a
wizard — except the first, which is one:

| tab | for |
| --- | --- |
| **Get started** | A first rule on the sample data, end to end, delivering to the Demo inbox. Where a fresh install opens. |
| **Triggers** | Write the query. It runs with `LIMIT 0` as you type and reports the columns it returns — aliases and joined columns included — and you name the clock and the key from that list. |
| **Rules** | Pick a trigger, set the cadence, backtest it, attach an action. |
| **Integrations** | Where actions go — Slack, Zendesk, Braze or any REST API — and the shape of each request. |
| **Activity** | Modes, watermarks, what has fired, the kill switch. |
| **Demo inbox** | What rules delivered to the Demo inbox, drawn as chat messages and tickets — shadow and live. |
| **Data sources** | The databases it reads — PostgreSQL or MySQL, as many as you need — and what each can see. |

Every data source and integration is shown with its mark, so which database a
trigger reads, or which system a rule talks to, is answered at a glance.

Every page has its own URL, so anything you are looking at can be bookmarked,
opened in a new tab or sent to someone with the same setup:

| URL | shows |
| --- | --- |
| `/triggers/<name>` | one trigger, open in the editor |
| `/rules/<name>` | one rule, with its backtest and actions |
| `/integrations/<name>` | one integration |
| `/integrations/<name>/actions/<action>` | one action, open for editing |
| `/sources/<name>` | one data source, and what it can see |
| `/activity`, `/inbox`, `/start` | those pages |

`/triggers` and `/rules` on their own open the form for a new one. A rule
named `new` is a valid rule, so no name is reserved for the form.

```bash
docker compose up -d
```

Then open <http://127.0.0.1:8000> (set `ROWFIRE_UI_PORT` if 8000 is taken) and
click *Use the demo PostgreSQL database*.

Against your own database:

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  -e ROWFIRE_PLATFORM_DSN='postgresql+psycopg://...' \
  -e ROWFIRE_MASTER_KEY="$ROWFIRE_MASTER_KEY" \
  rowfire serve --host 0.0.0.0 --port 8000 --allow-non-loopback
```

### The control plane is the only store

There is no file mode and no fallback. Definitions live in the control plane as
immutable versions; the customer connection lives there encrypted. The CLI, the
UI and the poller all read the same rows, so they cannot disagree about what is
defined or which database it runs against — a CLI answering from a file while
the scheduler read the database would report on the wrong data while looking
perfectly healthy.

`rowfire serve` refuses to start without a reachable control plane. A page that
loads and then reports every panel as broken is a worse error message than not
starting.

A YAML file is now an **import/export artifact**, never a source of truth:

```bash
rowfire platform pull > definitions.yaml      # export, e.g. into version control
rowfire platform push -f definitions.yaml     # import as a new version
rowfire platform push -f definitions.yaml --if-empty   # idempotent seeding
```

`--if-empty` is what `docker compose up` uses to seed a blank install, which is
why it exits 0 when there is already something there.

Saving through the UI goes through exactly the same path as `platform push`, so
editing a live rule in the browser demotes it to shadow the same way it would
from the CLI.

### Why it is local-only, and stays that way

The whole risk-removal in a first conversation is "a read-only user, nothing
leaves your network, nothing sends". A hosted UI where someone pastes a
production DSN into another company's website reintroduces exactly the objection
the backtest exists to remove. So:

- the DSN is stored **encrypted** (the poller needs it to run without you
  present) — never logged, never returned to the browser, and never written as
  plaintext anywhere
- the server binds loopback, and **rejects any request whose `Host` header is not
  loopback**. Binding to 127.0.0.1 alone does not stop a page you happen to be
  browsing from driving the server against your replica (DNS rebinding); the Host
  check does.
- `--allow-non-loopback` exists only for the container case, where the published
  port is pinned to the host's `127.0.0.1`
- `ROWFIRE_ALLOWED_HOSTS` names extra hosts to accept, exactly — for a forwarded
  port or a proxy that reaches the server under another name, such as the
  public demo's own domain (`rowfire cloud` sets it there). Exact names only:
  a wildcard would let any page served under the same suffix drive the server
- there is no auth, because there is no multi-user surface to authenticate

**The one exception is the public demo** (`ROWFIRE_HOSTED=1`, see
[docs/hosting.md](docs/hosting.md)). It is a multi-user surface on purpose,
and it never sees a customer's DSN: data sources are fixed to sample
databases, each visitor's work lives in a workspace of their own named by a
signed cookie, and actions can only reach the Demo inbox.

This is also why the SQL a trigger carries is parsed rather than
pattern-matched: a web form that accepts SQL and runs it against a production
replica is exactly where a regex denylist would be indefensible.

### Front end

React + Vite + TypeScript in `ui/`, **built inside Docker** (`node:20-alpine`),
so the build does not depend on whatever Node is installed locally — Vite 8
requires `>=20.19`. FastAPI serves the built assets from the same origin as the
API, so there is no CORS in production.

For hot-reload development, run the two apart:

```bash
uv run rowfire serve --dev-origin http://localhost:5173
```

```bash
cd ui && npm run dev
```

Vite proxies `/api` to the Python server, so the browser still sees one origin.

## Data sources

A **data source** is a database RowFire reads: a name, and a read-only DSN
stored encrypted. There can be any number, on either engine:

| engine | DSN | driver |
| --- | --- | --- |
| PostgreSQL | `postgresql://user:pass@host:5432/db` | psycopg |
| MySQL (and MariaDB) | `mysql://user:pass@host:3306/db` | PyMySQL |

The scheme picks the engine; nothing else needs to say which it is. Add sources
under **Data sources** in the UI, or from the CLI:

```bash
export SUPPORT_DSN='mysql://reader:…@support-db:3306/helpdesk'
uv run rowfire platform connect --dsn-env SUPPORT_DSN --name support
uv run rowfire platform sources
```

A trigger names the source it reads:

```yaml
triggers:
  urgent_ticket_unanswered:
    source: support          # a stored source's name
    sql: |
      SELECT id, subject, created_at FROM tickets
      WHERE priority = 'urgent' AND first_response_at IS NULL
    event_time: created_at
    key: [id]
```

A trigger that names no source reads the **default**: the source called
`primary` if there is one (it is what a single-database install always used),
otherwise the first source you added. Adding a source never changes what an
existing trigger reads. Rules need nothing: a rule reads whatever its trigger
reads.

Everything that touches a database follows the trigger's source. Its SQL is
checked and re-rendered in that engine's dialect, so backticks and MySQL
functions are fine on a MySQL source. The backtest runs there, and the worker
polls each trigger against its own database. Moving a trigger to another source
changes what it means, so like any other change to its query, it sends the
trigger's rules back to shadow. A source a trigger still reads cannot be deleted.

### MySQL specifics

- **Read-only, three ways.** The account should only have `SELECT`. The session
  runs `SET SESSION TRANSACTION READ ONLY`, so even an account that *can* write
  is refused by MySQL itself. The query checker refuses anything but a single
  `SELECT`. The tests prove each layer on its own.
- **Statement timeout.** Set with `max_execution_time` (MariaDB:
  `max_statement_time`), from the same `statement_timeout_ms`.
- **Times are UTC.** The session is pinned to `+00:00`, and a `DATETIME`, which
  carries no zone of its own, is read as UTC. A table that stores local
  wall-clock time in a `DATETIME` would place its rows hours off; convert in the
  query (`CONVERT_TZ(col, 'Europe/Cairo', '+00:00')`) if yours does.
- **TLS** follows the MySQL client's `ssl-mode`:
  `mysql://…/db?ssl-mode=VERIFY_IDENTITY` (also `REQUIRED`, `VERIFY_CA`,
  and `ssl-ca=/path/to/ca.pem`).

## The control plane

The platform's own database — **Postgres**, separate from the customer's in
every sense: that one is read-only forever, this one we own, write to, and
migrate.

```bash
docker compose up -d controlplane
```

```bash
export ROWFIRE_PLATFORM_DSN='postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform'
export ROWFIRE_MASTER_KEY="$(uv run python -c 'from rowfire.platform.crypto import generate_master_key; print(generate_master_key())')"
uv run alembic upgrade head
```

Single-tenant for now — one deployment per design partner — but every table
carries `workspace_id`, and it is part of the fire ledger's unique key. That
one column is what keeps going multi-tenant later from being a unique-index
rebuild on the largest, most write-hot table in the system.

### The fire ledger is the reliability guarantee

The rule, and everything depends on it holding:

> A send happens if, and only if, `claim()` **inserted a row**.

Not "if the row looked new" — if the `INSERT` reported that it inserted.
`SELECT`-then-`INSERT` has a window between the two statements where a second
worker does the same thing and both send. `INSERT … ON CONFLICT DO NOTHING
RETURNING` has no such window; Postgres resolves it under the unique index.

This is measured, not asserted. Racing 16 workers at one row:

| strategy | workers that would send |
| --- | --- |
| `SELECT`-then-`INSERT` | **16** |
| `ON CONFLICT` (`ledger.claim`) | **1** |

`dedup_bucket` puts all three policies on that one constraint — `''` for
`once_ever`, `2026-08-03` for `once_per_period`, the ordinal for `once_per_n`.

`once_ever` and `once_per_period` are pure uniqueness, so replaying a crashed
run is harmless. `once_per_n` *counts*, so a run that died after counting but
before sending would shift every subsequent nth on retry — `dedup_occurrence`
makes the increment conditional on the occurrence being genuinely new.

### Running triggers live

```bash
export ROWFIRE_MASTER_KEY="$(uv run rowfire platform init-key | head -1)"
uv run rowfire platform connect --dsn-env DATABASE_URL
uv run rowfire platform push
uv run rowfire worker
```

| command | what it does |
| --- | --- |
| `platform init-key` | Generate a master key for `ROWFIRE_MASTER_KEY` |
| `platform connect` | Store a data source's DSN, encrypted. `--name` (default `primary`) |
| `platform sources` | List the data sources, with engine and host, never a credential |
| `platform push` | Save definitions as a version and reconcile trigger state |
| `platform status` | Trigger modes, watermarks, last errors |
| `platform promote` / `demote` | Move a trigger between shadow and live |
| `platform halt` / `resume` | The kill switch |
| `worker` | Poll due triggers and deliver |

Everything starts in **shadow**, and every safety rule is a test rather than a
comment:

- **Cold start.** A new trigger's watermark is *now*, never the beginning of
  time. Pointed at the fixture's 1,485 orders, the first poll fires **zero**.
- **Any edit drops back to shadow.** The checksum covers only what changes a
  trigger's meaning, so editing one trigger does not demote the others — a
  demotion nobody can predict is a demotion everyone ignores.
- **The window overlaps on purpose.** Each poll re-scans `lookback_seconds`
  behind the watermark, because rows arrive with event_times slightly in the
  past. Re-scanning is safe because the ledger is permanent: re-polling the same
  120-day window a second time gives `matched=697 fired=0 already=697`.
- **The watermark only advances on success.** A failed poll re-covers its
  window rather than skipping it.
- **Shadow and live share one path.** The message is rendered, the cap is
  consumed, a `delivery` row is written either way; only whether the HTTP
  request is actually made differs.
  Budgets are scoped per mode, so watching a trigger in shadow does not spend
  the recipient's real budget before it goes live.

Workers scale with `docker compose up --scale worker=3` — `SKIP LOCKED` means
the replicas need no coordination.

### Stored credentials

v0 never persisted a DSN. A scheduler cannot work that way, so this is where
that property is deliberately given up — and `crypto.py` says so out loud.

Credentials are envelope-encrypted (AES-256-GCM, per-row data key wrapped by a
master key):

```
master key (env)  ->  wraps  ->  per-row data key  ->  encrypts  ->  DSN
```

There is no KMS yet and the master key comes from `ROWFIRE_MASTER_KEY`. The
envelope is still the right shape now, while there is no data: adopting a KMS
later only rewraps the data keys, and no stored ciphertext is ever decrypted or
rewritten. Encrypting directly under one key would make that swap a full data
migration.

**Losing `ROWFIRE_MASTER_KEY` makes every stored credential unrecoverable.**


## Integrations and actions

Three layers, and the bottom one is just HTTP:

```
integration          "Acme Slack" · base_url · auth kind · credentials (encrypted)
  └── action             send_message · POST /chat.postMessage · body template
        └── binding          tell_ops → send_message(channel=…, text=…)
```

An **integration** is an *instance*, not a type. Your production Slack and your
staging Slack are two integrations that happen to share a provider, each with
its own credentials and its own copy of the actions. An **action** is one REST
call on it, described entirely as data.

Both are managed under **Integrations** in the UI, or over the API:

```bash
# from the catalogue — prefills the base URL, the auth shape and the actions
curl -X POST localhost:8000/api/integrations -H 'content-type: application/json' \
  -d '{"name":"acme-slack","provider":"slack",
       "credentials":{"bot_token":"xoxb-…"}}'

# or entirely your own, with no catalogue entry and no code
curl -X POST localhost:8000/api/integrations -H 'content-type: application/json' \
  -d '{"name":"our-crm","base_url":"https://api.example.com/v2",
       "auth_kind":"header","auth_header_name":"X-API-Key",
       "auth_credential":"api_key","credentials":{"api_key":"k-…"}}'

curl -X PUT localhost:8000/api/integrations/<id>/actions/update_tier \
  -H 'content-type: application/json' \
  -d '{"method":"PATCH","path":"/contacts/{{ contact_id }}",
       "body":{"fields":{"{{ field }}":"{{ value }}"}}}'
```

| endpoint | what it does |
| --- | --- |
| `GET /api/catalogue` | Systems we already know, and what credentials each needs |
| `GET /api/integrations` | Configured integrations and their actions |
| `POST /api/integrations` | Create one, from a template or from scratch |
| `PUT /api/integrations/{id}/actions/{name}` | Define one REST call |
| `POST /api/bindings` | Attach an action to a rule, with parameters |

### Credentials are a map, and never come back

An integration holds a set of named credentials, encrypted as one envelope —
`{"api_key": "…", "app_id": "…"}` — because real APIs want more than one value
and some of them are configuration rather than secrets. `auth.credential` names
the one that signs.

No endpoint returns a credential. The listing reports which keys are *set*, so
the UI can say an integration needs reconnecting without ever being able to
leak one. Authentication is applied in `dispatch.send`, after the request has
been rendered and recorded, so the stored copy of a request never contains a
token.

### Integrations are data, not code

Slack lives in `src/rowfire/platform/catalogue/slack.yaml` and is loaded,
validated and executed by the same code as an integration you type into the UI
for your own API. Nothing about it is privileged — which is the only way to
know the format is actually general. Braze ships alongside it precisely because
its shape is different: a nested array body, a regional base URL, and a second
credential that is configuration rather than a secret.

The catalogue is a head start, never a gate. Deleting `catalogue/` would cost
you some prefilled forms and nothing else.

The model is deliberately flat: **one action per fired row**. A rule can
have several bindings and each produces its own request, but nothing is chained
and no value is carried from one to the next. Multi-step actions — a reply in
the thread of a message this run just posted — need response capture and
ordering guarantees, and are left for a later version rather than half-built
now.

### The Demo inbox needs no account

`catalogue/demo_inbox.yaml` is an integration like any other, with one
difference: its base URL is `inbox://demo`. The dispatcher completes a request
to that scheme in-process — it is recorded and marked sent, and never handed
to a transport — so nothing leaves the machine and there is nothing to
authenticate. Its two actions are shaped like Slack's `chat.postMessage` and
Zendesk's create ticket, so a rule built against the inbox reads the same as
one built against the real thing. The **Demo inbox** page reads the delivery
log back (`GET /api/inbox`) and draws `/messages` as chat messages and
`/tickets` as tickets.

### Templates are substitution, not a language

The whole template language is `{{ name }}` and `{{ a.b }}`. No calls, no
arithmetic, no filters, no attribute access into Python objects — a value is
looked up in a dict and inserted.

That is deliberate and not a gap to widen later. A template here turns customer
data into an outbound HTTP request, which is the same class of surface as the
`when` clause at the end that actually sends, and template sandboxes are
routinely escaped. Anything that is not a plain name stays literal text:
`{% for x in y %}` renders as those exact characters.

A template naming something the row does not have **fails the delivery** rather
than rendering an empty string, because a message with a hole in it is worse
than no message.

### Secrets are applied at send time, never stored

`dispatch.prepare` builds the request with no authentication; `dispatch.send`
adds the token at the moment of the call. Only the first result is written to
`delivery.rendered`, which the UI displays — merging the two would put an API
key in a database column and on a screen.

### Egress is currently unrestricted

A custom integration may point at any `http`/`https` URL, including private
addresses. That is defensible while every deployment is single-tenant and
self-hosted — the operator already controls the network — and indefensible the
day this runs in someone else's cloud, where an integration could reach the
instance metadata endpoint or the control plane itself. The check belongs in
`dispatch._check_url`, which exists and currently permits everything.

## Commands

Every command reads what is in force from the control plane. There is no
`--file` flag.

| command | what it does |
| --- | --- |
| `rowfire init` | Introspect the database and store a starting set of definitions. `--dry-run`, `--force` |
| `rowfire validate` | Check the stored definitions against the live schema, reporting every error at once |
| `rowfire list` | List the stored triggers and the rules hanging off them |
| `rowfire run <rule>` | The main command. `--days`, `--sample` |
| `rowfire explain <trigger>` | Print a trigger's compiled SQL without executing it |
| `rowfire serve` | Serve the local UI (loopback only) |
| `rowfire platform push -f <file>` | Import definitions as a new version. `--if-empty` |
| `rowfire platform pull` | Export the stored definitions. `-o <file>`, `--version N` |
| `rowfire platform promote/demote <rule>` | Move one rule between shadow and live |
| `rowfire platform halt <reason>` / `resume` | The kill switch |
| `rowfire worker` | Run the poller |

## What the number means, and what it does not

This is the part most likely to be misread.

The backtest evaluates rows **as they are now**, and uses `event_time` to place
them in the past. It does not replay history. So:

- An order completed in March that still shows `status = 4` is counted in March.
- An order completed in March and later refunded to `status = 6` is **invisible**.
  The backtest undercounts. Treat the result as a floor.
- A row the query returns with a null `event_time` cannot be placed on the
  timeline at all. These are counted and reported separately, never dropped —
  a large number usually means the trigger names the wrong output column as its
  clock.

`run` states all of this in its output rather than relying on you having read
this file.

## Safety

- PostgreSQL connections set `default_transaction_read_only = on` and psycopg's
  read-only transaction attribute; MySQL connections run
  `SET SESSION TRANSACTION READ ONLY`. Point either at a `SELECT`-only account
  and a write is refused three independent ways.
- A statement timeout from config is applied to every session.
- Sample queries always carry a `LIMIT`.
- The DSN is read from an environment variable named in the definitions file. It
  is never written to the file, never logged, and never included in an error
  message.

**The one exception is a demo button, and it is off unless you turn it on.**
**Simulate new activity** writes rows into the sample database so there is
something new to fire on. It exists only when both `ROWFIRE_DEMO_ACTIVITY_DSN`
(a write-capable login to the sample database) and `ROWFIRE_DEMO_ACTIVITY_SQL`
(a SQL file) are set, which only `examples/saas/compose.yaml` does. That login
is its own setting, never a data source, so every source stays read-only. It
runs that one file and never SQL from the browser. Without both settings the
button is not shown and `POST /api/demo/activity` is a 404. The code is
`src/rowfire/demo_activity.py`.

**Backtesting stores no credential. Scheduling one does.** The CLI and the demo
UI hold the DSN in memory only. A poller that runs on its own cannot — so
`rowfire platform connect` stores it encrypted at rest. That is a real change
in what you are trusted with, and it is called out here rather than left for
someone to discover in the schema.

### How a trigger's SQL is checked

A trigger is any single `SELECT`. That boundary is much looser than it used to
be, and the reframing is deliberate: the old validator refused subqueries,
joins and any function outside an allowlist, because it was protecting a surface
where a non-engineer typed a predicate. A trigger is written by whoever knows
the schema, against their own database, through a role they granted themselves.
Arbitrary `SELECT` is what they would write in any reporting tool.

What is still enforced, via `sqlglot`:

- exactly one statement — a `;` cannot smuggle a second one
- that statement reads: no `INSERT`/`UPDATE`/`DELETE`/DDL/`COPY`/`GRANT`,
  including one hidden inside a CTE
- what executes is re-rendered **from the validated syntax tree**, not from the
  original string, so there is no gap between what was checked and what runs

Underneath that: a read-only session, a statement timeout, and a hard row cap.

### The time window is added, not trusted

The window wraps the author's query rather than being written into it:

```sql
SELECT * FROM ( <their query> ) AS t
WHERE t.<event_time> >= :since AND t.<event_time> < :until
```

The watermark, the deliberate lookback overlap and the fire ledger all depend on
us owning those bounds; a query trusted to filter its own time range would take
that away. The one escape hatch is a query that mentions `:since` / `:until`
itself, which gets them bound directly — pushing the bound inside the query is
sometimes the difference between an index scan and a sequential one, and the
author knows better than we do where it belongs.

## Development

```bash
uv run pytest
```

Control-plane tests create and migrate their **own** database
(`rowfire_platform_test`) rather than sharing the one the app uses. That is not
tidiness: a running worker leases trigger rows every few seconds and holds
transactions that block `TRUNCATE`, so a shared database makes the suite fail or
hang depending on timing, with nothing pointing at the container as the cause.
The suite passes with `docker compose up` running.

Database-backed tests are marked `db` and skip cleanly when the fixture
databases are not running (`docker compose up -d postgres mysql controlplane`
starts all three):

```bash
uv run pytest -m "not db"
```

```bash
uv run ruff check . && uv run ruff format --check .
```

### Fixtures

`fixtures/seed.sql` is deterministic — modular arithmetic over
`generate_series`, no `random()` — so counts are exact and repeatable. It plants
six edge cases on purpose, each with a test that depends on it:

| case | what it exercises |
| --- | --- |
| 40 orders with `status = 4` but null `completed_at` | the timeline gap |
| 50 orders on `is_test = true` accounts | a naive predicate catches them |
| one customer, 40 orders in a week | dedup and frequency-cap pressure |
| `status` 4 and 7 both mean "done" | the silent soft-break |
| a 29-day stretch with zero volume | empty-window rendering |
| 5 orders with `completed_at` in the future | clock skew |

`fixtures/mysql/` is the second engine's fixture: a support desk of 480 tickets,
seeded just as deterministically (a recursive CTE, no `RAND()`). It has what a
MySQL schema really has: zone-less `DATETIME` columns, a `tinyint(1)` boolean, an
`ENUM`, a `JSON` column, and subjects with a literal `%` in them. The MySQL
tests replay the seed's arithmetic rather than paste in numbers.

`docker compose down` discards the database, so the next `up` reseeds from
scratch.

## License

Copyright (C) 2025-2026 Mostafa Saeed

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License, version 3, as published by
the Free Software Foundation. See [LICENSE](LICENSE) for the full text.

If you run a modified version of this software as a network service, the AGPL
requires you to make your modified source code available to its users.
