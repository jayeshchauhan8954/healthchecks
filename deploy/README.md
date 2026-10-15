# Docker Compose deployment: 2 web containers behind Caddy

```
            :443 (TLS)                 ┌── web1 (uWSGI, HTTP only) ──┐
client ───► caddy ── round robin ──────┤                             ├──► postgres
                                       └── web2 (uWSGI, HTTP only) ──┘      ▲
                                                                            │
           alerts1, alerts2 (sendalerts) + reports (sendreports --loop) ────┘
migrate: one-shot, runs before everything else
```

Files: `compose.yml` (production stack), `caddy/Caddyfile`, `uwsgi/web.ini`,
`.env.example`, `compose.local.yml` (local-only Mailpit + webhook sink),
`compose.killtest.yml` + `slow_smtp.py` (crash test), `ui_alert_test.py`,
`alert_test.py`, `kill_test.py`.

## Alert delivery

* `sendalerts` runs in two worker containers. Each flip is claimed with an
  atomic `UPDATE ... WHERE processed IS NULL`, so only one worker sends it. If
  one worker dies, the other keeps sending. This needs the shared Postgres:
  never use SQLite here. `sendreports` (reports and repeat-alert nags) uses the
  same claim pattern.
* The workers are separate from the web containers on purpose. In the upstream
  image they are uWSGI attach-daemons. On `docker stop`, uWSGI (PID 1) exits
  within about a second and the kernel SIGKILLs the daemons, dropping the alert
  being sent. Here each worker is its container's main process behind tini,
  with `stop_grace_period: 10m`. On SIGTERM it stops claiming work, finishes the
  send in progress, and exits.
* The grace period has to cover the slowest possible in-flight flip. A flip's
  channels are notified one after another, at up to ~90s per channel (3
  attempts x 30s timeout). sendalerts can also claim one more flip after
  SIGTERM. So the bound is 2 x channels x ~92s, and 10m covers 3 channels.
  Raise it if your checks have more. A normal stop doesn't wait that long: it
  waits only as long as the sends actually take.
* On the production host, make sure a reboot also waits that long. Docker
  stores the grace period as the container's StopTimeout (600). The systemd
  unit's `TimeoutStopSec` (`systemctl show docker -p TimeoutStopUSec`) still
  needs to be at least that, or a reboot SIGKILLs the workers anyway. I haven't
  verified this one: it depends on the host.
* Caddy retries a request on another web container only if it couldn't
  connect (the request never got through), and it buffers request bodies so a
  retried POST ping keeps its body.
* Webhooks get up to 3 attempts on a timeout (30s) or a non-2xx response. Make
  sure your webhook receiver answers 2xx quickly, or it may see repeats.

What happens when a worker dies in the middle of a batch (`kill_test.py`:
checks with 2 email channels + 1 webhook all go down at once, one worker is
stopped mid-batch, then started again):

| How it dies | Setup | Result |
|---|---|---|
| `docker compose stop` (SIGTERM) | SMTP 3s, webhook OK | 30/30 exactly once |
| `docker compose stop` | SMTP 3s, webhook hangs (stop issued as it starts hanging) | exited after 96s, nothing lost |
| `docker compose stop`, grace **2m** | SMTP 20s, webhook hangs | in-flight flip needed ~130s: SIGKILLed at 120s, **1 email lost** |
| `docker compose stop`, grace **10m** | SMTP 20s, webhook hangs | exited after 130s, nothing lost |
| `docker kill`, OOM kill, crash, power loss (SIGKILL) | SMTP 3s | 29/30. The alert being sent at that moment is **lost**, never duplicated |

The "webhook hangs" runs show `webhook=3` for the affected checks. That's hc
retrying a receiver that never answered, which is the expected behaviour.

A hard kill loses at most the alert being sent in that container at that
moment (sendalerts handles one flip at a time). The flip was already marked
processed, so no worker retries it. That covers every channel of the flip that
hadn't been notified yet, not just the one being sent. In one run the kill
landed during the first of two emails: the webhook had gone out, and both
emails were lost.

It leaves a trace for the channel being sent: a row in `api_notification` with
`error = 'Sending'` that never got updated. Channels later in the same flip
were never started, so they leave no row. Treat a stuck row as "this check's
alert may not have reached any channel after this one". To find these, run:

```sql
select n.created, c.name, ch.kind from api_notification n
join api_check c on c.id = n.owner_id join api_channel ch on ch.id = n.channel_id
where n.error = 'Sending' and n.created < now() - interval '5 minutes';
```

## Run locally

All commands below run from `deploy/`.

```sh
cd deploy
cp .env.example .env    # then set HC_DOMAIN=localhost, SITE_ROOT=https://localhost,
                        # ALLOWED_HOSTS=localhost, and random SECRET_KEY and DB_PASSWORD, e.g.
                        # python3 -c 'import secrets; print(secrets.token_urlsafe(50))'
export COMPOSE_FILE=compose.yml:compose.local.yml
docker compose up -d --build
docker compose exec web1 ./manage.py createsuperuser   # or add --email ... --password ...
```

`compose.local.yml` handles the rest for local use: mail goes to Mailpit,
webhooks may target the private Docker network, and sign-up is open (the
production `.env` keeps it closed).

* App: https://localhost (cert from Caddy's local CA, which browsers will warn
  about. Trust it with
  `docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .`)
* Mailpit: http://localhost:8025. Webhook sink: http://localhost:8099/_requests

Alert tests. Each takes about 3 minutes, since 60s period + 60s grace are the
minimums:

* **UI test.** Signs up and logs in through the emailed link, adds an email
  integration and verifies it from the email, adds a webhook in the form, sets
  the schedule, pings, and waits for DOWN. Drives your installed Google Chrome
  through Caddy's HTTPS. Then asserts exactly 1 DOWN and 1 UP per email address
  and per webhook.

  ```sh
  python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
  .venv/bin/python ui_alert_test.py        # --headed to watch it
  ```

* **Race test.** `python3 -I alert_test.py 40` creates 40 checks that go
  down at the same moment, so the two `sendalerts` workers compete for them.
  It takes setup shortcuts: the user and channels come from `manage.py shell`,
  with the email channel pre-verified. Use it for the concurrency question, and
  the UI test for the real user path.

* **Crash test.** `kill_test.py` stops a worker mid-batch and reports what was
  lost or duplicated. Each check has 2 email channels + 1 webhook. It needs the
  slow-SMTP override:

  ```sh
  export COMPOSE_FILE=compose.yml:compose.local.yml:compose.killtest.yml
  docker compose up -d
  python3 kill_test.py 30              # SIGKILL alerts1: expect 1 lost, 0 twice
  python3 kill_test.py 30 --graceful   # docker compose stop alerts1: expect 0 lost
  # webhook sink hangs; stop alerts1 just as its webhook starts hanging:
  SMTP_DELAY=20 docker compose up -d slow-smtp
  python3 kill_test.py 6 --graceful --hang-webhook   # expect 0 lost
  # back to the normal local stack afterwards:
  export COMPOSE_FILE=compose.yml:compose.local.yml
  docker compose up -d --remove-orphans
  ```

  The SIGKILL run exits non-zero on purpose: losing the in-flight alert is the
  known limitation it demonstrates (see "Alert delivery" above).

## Production

* `.env`: real `HC_DOMAIN`/`SITE_ROOT`/`ALLOWED_HOSTS`, SMTP settings, and
  `INTEGRATIONS_ALLOW_PRIVATE_IPS=False`. Ports 80 and 443 must be reachable
  for Let's Encrypt.
* Run `compose.yml` alone (no `compose.local.yml`).
* Upgrading without downtime. A plain `up -d` after a rebuild recreates both
  web containers at the same time, so roll them one at a time:

  ```sh
  docker compose build
  docker compose run --rm migrate
  docker compose up -d --no-deps web1   # wait until `docker compose ps` shows healthy
  docker compose up -d --no-deps web2
  docker compose up -d --no-deps alerts1   # stops gracefully: finishes its in-flight send
  docker compose up -d --no-deps alerts2
  docker compose up -d --no-deps reports
  ```

  Migrations must stay compatible with the old code still running in the
  other container while the rollout is in progress.
