# Hosting the public demo

A public Rowfire demo that anyone can open with a link: no install, no
account, no waiting for a container to build. Each visitor gets a private
workspace of their own, and every merge to `main` updates it by itself.

There are two ways to run it:

- **[On Render](#on-render-recommended)**: managed Postgres with backups,
  HTTPS and deploys handled for you. About $20 a month, and nothing to
  maintain. Recommended.
- **[On your own server](#on-your-own-server)**: one VM running
  `docker compose`. €0–5 a month, but the server, its updates and its
  backups are yours to look after.

## What a visitor gets, and what keeps it safe

On their first visit the app makes them a **workspace**: their own rules,
integrations, Demo inbox and history, plus **their own copy of the sample
Postgres tables** (a schema of their own), so *Simulate new activity* fires
their rules and nobody else's. A signed, HttpOnly cookie is the only thing
that names it. After `ROWFIRE_WORKSPACE_IDLE_HOURS` (24 by default) without a
visit, the worker deletes the workspace, its history and its copy of the
data.

A public instance is a different risk from a local one, so `ROWFIRE_HOSTED=1`
turns on a set of locks (`src/rowfire/hosted.py`):

| risk | what stops it |
| --- | --- |
| A visitor points the server at an address of their choosing | Data sources are fixed to the sample databases; adding or replacing one is refused. |
| An integration used to call other sites, or the cloud's metadata endpoint | `ROWFIRE_EGRESS=inbox-only`: only the Demo inbox can be delivered to. |
| One visitor reading or changing another's work | Every query and every lookup by id is scoped to the visitor's workspace. Sample data is per visitor. |
| A page on another site using a visitor's cookie | `SameSite=Lax`, and state-changing requests from another origin are refused. |
| Expensive queries | A 10-second statement timeout on every visitor's queries. |
| Floods | A cap on live workspaces (`ROWFIRE_MAX_WORKSPACES`, 200) and on new sessions per address per hour. |

Trigger SQL still runs, because writing it is the point. It runs as a
read-only login against sample data only.

## On Render (recommended)

`render.yaml` describes the whole demo: the web app, the worker and a managed
Postgres, in Frankfurt. Render reads it and creates all three.

**1. Create it.** Open
<https://render.com/deploy?repo=https://github.com/rowfirehq/rowfire>,
sign in with GitHub, add a payment method (always-on services are not on the
free tier), and approve the plan. The first deploy builds the image and
takes about ten minutes. Render generates the master key itself.

**2. Open it.** The web service's page shows its address,
`https://rowfire-demo.onrender.com` or similar. The first visit makes a
workspace and lands on **Get started**.

That's all. From then on every merge to `main` redeploys both services once
CI has passed on it, and each deploy reseeds the sample data so its
timestamps stay recent. Visitors' workspaces survive deploys.

| what | Render plan | about |
| --- | --- | --- |
| web app (`rowfire-demo`) | Starter | $7/month |
| worker (`rowfire-demo-worker`) | Starter | $7/month |
| Postgres (`rowfire-db`) | Basic 256 MB, 1 GB disk | about $6.30/month |

The database holds everything, a schema each: `rowfire_platform` for Rowfire
itself, `sample` for the template, and `visitor_*` for each visitor's copy.
It accepts connections only from the two services (`ipAllowList: []`).

**Optional:**

- *A custom domain* such as `demo.example.com`: add it under the web
  service's **Settings → Custom Domains**, and set `ROWFIRE_PUBLIC_HOSTNAME`
  to it in the `rowfire-demo` environment group so Rowfire accepts it. Point
  a `CNAME` at the service's `onrender.com` address. On Cloudflare, leave it
  **DNS only** (grey cloud): proxied, or before the domain is added on
  Render, it fails with Cloudflare's *Error 1000, DNS points to prohibited
  IP*.
- *A spending notification* under **Billing**, so a burst of traffic
  cannot surprise you.

**A Supabase source (optional).** Visitors can also get a third data
source: the sample product's own usage data, read from a Supabase project
through Supabase's read-only endpoint, with a sample trigger
(`free_workspace_near_quota`) and rule (`nudge_upgrade`) on it. Every
visitor shares it, read-only, as they share the MySQL support desk.

1. Create a Supabase project and run
   [`examples/saas/supabase.sql`](../examples/saas/supabase.sql) in its SQL
   editor. It makes two tables with row level security on and no policies,
   seeds sixty days of history, and schedules an hourly pg_cron job that
   keeps adding usage and the occasional signup, so the trigger keeps firing.
2. Create a personal access token under *Account → Access tokens*. It can do
   anything its account can, so make it from a Supabase account that is a
   member of nothing but the demo project's organization. Rowfire only ever
   sends it to the read-only query endpoint, but a token on a public server
   should open nothing else if it leaks.
3. Set `ROWFIRE_DEMO_SUPABASE_DSN` to `supabase://<project ref>` and
   `SUPABASE_ACCESS_TOKEN` to the token on both services, the web app and
   the worker (Render asks for them on the first deploy). New visitors get the
   source; existing workspaces keep what they had.

Without both settings the demo has no Supabase source, and the Supabase
trigger and rule are left out of each visitor's sample.

**If a deploy fails,** the web service's **Events** tab shows which step.
`rowfire cloud predeploy` names what it could not do. The likeliest is the
database user lacking `CREATEROLE`, which the demo needs for the read-only
role visitors' queries run as.

**What not to change:** `ROWFIRE_MASTER_KEY`, once the demo is running.
Every stored credential is encrypted under it. Visitors' workspaces would
become unreadable, and the only fix is wiping them.

## On your own server

You need a Linux server with Docker, a domain name (or subdomain) and about
fifteen minutes.

### One-time setup

**1. A server.** Any small VM works. 2 vCPU and 4 GB of RAM is plenty.

- *Hetzner* CX22 or similar, about €4–5 a month. Simplest.
- *Oracle Cloud* "Always Free" Ampere (ARM) VM, $0. The image is published
  for ARM as well as x86. Open ports 80 and 443 in the VCN security list as
  well as on the VM's own firewall.

Install Docker (`curl -fsSL https://get.docker.com | sh`) and add your deploy
user to the `docker` group.

**2. DNS.** Point an `A` record (and `AAAA` for IPv6) for your demo's name,
say `demo.example.com`, at the server. Caddy gets the HTTPS certificate by
itself once DNS resolves.

**3. The code and settings.**

```bash
sudo git clone https://github.com/rowfirehq/rowfire /opt/rowfire
sudo chown -R "$USER" /opt/rowfire
cd /opt/rowfire
cp deploy/hosted/.env.example deploy/hosted/.env
# Set DOMAIN, and generate the master key:
#   openssl rand -base64 32 | tr '+/' '-_'
nano deploy/hosted/.env
docker compose -f deploy/hosted/compose.yaml --env-file deploy/hosted/.env up -d
```

Open `https://<your domain>`. The first visit makes a workspace and lands on
**Get started**.

**4. Make the image public.** On GitHub: **Packages → rowfire → Package
settings → Change visibility → Public**. The server pulls it without logging
in.

**5. Deploy on every merge.** In the repository's **Settings → Secrets and
variables → Actions**, add:

| secret | value |
| --- | --- |
| `DEMO_SSH_HOST` | the server's address |
| `DEMO_SSH_USER` | the user that owns `/opt/rowfire` and can run docker |
| `DEMO_SSH_KEY` | a private key made for this, e.g. `ssh-keygen -t ed25519 -f demo_deploy -N ""`, with `demo_deploy.pub` added to that user's `~/.ssh/authorized_keys` |
| `DEMO_SSH_KNOWN_HOSTS` | the output of `ssh-keyscan <server address>`, so the workflow checks it is talking to your server |

From then on, `.github/workflows/deploy-demo.yml` runs after every image
published from `main`. It moves the checkout to `origin/main`, pulls the new
image and restarts what changed. You can also run it by hand from the
**Actions** tab. Until the secrets exist it skips with a notice instead of
failing.

### Running it

```bash
cd /opt/rowfire
C="docker compose -f deploy/hosted/compose.yaml --env-file deploy/hosted/.env"

$C ps                    # what is running
$C logs -f ui worker     # the server, and the worker polling and reaping
$C down && $C up -d      # restart everything; visitors' workspaces survive
$C down -v               # wipe everything, every visitor's workspace included
```

`ROWFIRE_FEEDBACK_URL` puts a **Give feedback** button in the demo's banner,
pointing at GitHub Discussions by default.

`ROWFIRE_HEAD_HTML` adds markup to the end of the page's `<head>`, such as an
analytics tag, so it lives in your instance's environment rather than in the
repository. On Render, set it in the `rowfire-demo` environment group; here,
in `deploy/hosted/.env`. Unset, the page is served exactly as built, which is
how every self-hosted instance starts: Rowfire itself tracks nothing. The value
goes into the page verbatim, so treat it like any other code you deploy, and
tell your visitors what it collects.

The app announces a few moments as browser events on `window`, so that markup
can react to them without Rowfire knowing what listens (`ui/src/moments.ts`):

| event | when |
| --- | --- |
| `rowfire:moment`, `detail.name` = `first_delivery` | a live rule delivered to the Demo inbox, once per page load |
| `rowfire:feedback` | **Give feedback** was clicked; call `preventDefault()` to handle it in place, and the link is not followed |

The rowfire.com demo uses them to show a PostHog survey, along these lines:

```html
<script>
  let surveys = false;  // stays false when PostHog is blocked: the link is followed
  posthog.onSurveysLoaded(() => { surveys = true; });
  window.addEventListener('rowfire:moment', (e) => posthog.capture('rowfire_' + e.detail.name));
  window.addEventListener('rowfire:feedback', (e) => {
    if (!surveys) return;
    e.preventDefault();
    posthog.displaySurvey('<survey id>', { displayType: 'popover', ignoreConditions: true, ignoreDelay: true });
  });
</script>
```

## What it does not do yet

- **Visitors are anonymous.** Their workspace lives in one browser. There
  are no accounts, so there is no way back to it from another device. That
  is the next step towards a hosted product, and it sits on top of the
  workspaces this already has.
- **One read-only login for every visitor's queries.** Each visitor's
  triggers read their own copy of the tables by default, but a hand-written
  query could name another visitor's schema and read it. Those copies hold
  only generated sample data, and nothing a visitor types is ever written
  into them, so there is nothing personal to read. A role per visitor would
  close it if that ever changes.
- **One server.** The worker serves every workspace in turn, which is plenty
  for a demo. For more, run more workers: `$C up -d --scale worker=3`. They
  coordinate through the database.

## The website

[rowfire.com](https://rowfire.com) is a static page kept in its own
repository and hosted on Cloudflare Pages. It is not part of Rowfire and
nothing here builds or serves it.
