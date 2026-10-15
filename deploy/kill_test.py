"""What happens to alerts when a sendalerts container dies mid-batch?

Needs: docker compose -f compose.yml -f compose.local.yml -f compose.killtest.yml up -d
(alert emails take ~3s each through slow-smtp, so a kill lands mid-send).

Creates N checks that all go down together, each with 3 channels: two email
addresses and a webhook (same setup shortcuts as alert_test.py: user and
channels are created via manage.py shell). Once alerts start flowing, kills
one sendalerts container (SIGKILL by default, or a graceful `docker compose
stop` with --graceful), starts it again, waits for delivery to settle, then
reports per check what arrived. It also shows the DB's view: unprocessed flips
and notifications stuck in "Sending".

--hang-webhook: the webhook sink stops answering (every attempt hits the 30s
timeout, so 3 attempts take ~90s). The stop is issued the moment the target
container's next webhook request starts hanging, which is the worst case for
how long it needs to finish.

Usage (from deploy/):
  python3 kill_test.py [N] [--graceful] [--hang-webhook] [--target alerts1]
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alert_test as base  # noqa: E402

base.COMPOSE = base.COMPOSE + ["-f", "compose.killtest.yml"]
compose, http = base.compose, base.http

# A second email channel, so each check has 3 channels like a real setup.
SETUP = base.SETUP + """
Channel.objects.create(project=p, kind="email", email_verified=True,
    value=json.dumps({{"value": "backup-{tag}@localhost", "up": True, "down": True}}))
"""


def deliveries(tag: str) -> tuple[Counter, Counter, list[dict]]:
    """Return (emails keyed by (address, subject), webhooks keyed by check code, raw webhook hits)."""
    emails: Counter = Counter()
    for who in ("oncall", "backup"):
        found = http("GET", f"{base.MAILPIT}/api/v1/search?query=to:{who}-{tag}@localhost&limit=1000")
        for m in found["messages"]:
            emails[(who, m["Subject"])] += 1
    hits = [r for r in http("GET", f"{base.SINK}/_requests") if r["path"].startswith(f"/{tag}/down/")]
    return emails, Counter(r["path"].rsplit("/", 1)[1] for r in hits), hits


def sql(query: str) -> list[list[str]]:
    out = compose("exec", "-T", "db", "psql", "-U", "postgres", "-d", "hc", "-tAF", "|", "-c", query)
    return [line.split("|") for line in out.splitlines() if line]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = int(args[0]) if args else 30
    graceful = "--graceful" in sys.argv
    hang = "--hang-webhook" in sys.argv
    target = sys.argv[sys.argv.index("--target") + 1] if "--target" in sys.argv else "alerts1"
    tag, key = secrets.token_hex(3), secrets.token_urlsafe(24)

    root =compose("exec", "-T", "caddy", "cat", "/data/caddy/pki/authorities/local/root.crt")
    with base.tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False) as f:
        f.write(root)
    ctx = base.ssl.create_default_context(cafile=f.name)

    running = compose("ps", "--status", "running", "--services").split()
    web = next(svc for svc in ("web1", "web2") if svc in running)
    compose("exec", "-T", web, "./manage.py", "shell", input=SETUP.format(tag=tag, key=key))
    api = {"X-Api-Key": key, "Content-Type": "application/json"}

    checks = [
        http("POST", f"{base.SITE}/api/v3/checks/", {
            "name": f"{tag}-{i:03d}", "timeout": base.TIMEOUT, "grace": base.GRACE, "channels": "*",
        }, api, ctx)
        for i in range(n)
    ]
    for c in checks:
        http("GET", c["ping_url"], ctx=ctx)
    t0 = time.time()
    how = "graceful stop (SIGTERM)" if graceful else "docker kill (SIGKILL)"
    print(f"[{tag}] {n} checks pinged (2 emails + 1 webhook each); "
          f"they go down at ~t+{base.TIMEOUT + base.GRACE}s")

    # Wait for the first alert.
    while not deliveries(tag)[2]:
        if time.time() > t0 + base.TIMEOUT + base.GRACE + 60:
            sys.exit("no alerts arrived")
        time.sleep(0.5)

    target_ip = subprocess.run(
        ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
         compose("ps", "-q", target).strip()], capture_output=True, text=True).stdout.strip()
    if hang:
        # Each attempt outlives the 30s curl timeout.
        http("POST", f"{base.SINK}/_hang/45")
        print(f"  t+{time.time() - t0:4.0f}s webhook sink now hangs; waiting for {target} "
              f"({target_ip}) to start a hanging webhook")
        while not any(r["client"] == target_ip and r["hung"] for r in deliveries(tag)[2]):
            time.sleep(0.2)
    else:
        time.sleep(6)

    emails, hooks, _ = deliveries(tag)
    print(f"  t+{time.time() - t0:4.0f}s {how} {target} "
          f"(so far: {sum(emails.values())} emails, {len(_)} webhook requests)")
    stop_start = time.time()
    compose("stop", target) if graceful else compose("kill", "-s", "SIGKILL", target)
    took = time.time() - stop_start
    cid = compose("ps", "-a", "-q", target).strip()
    exit_code = subprocess.run(["docker", "inspect", "-f", "{{.State.ExitCode}}", cid],
                               capture_output=True, text=True).stdout.strip()
    logs = compose("logs", "--no-log-prefix", "--since", f"{int(took) + 10}s", target)
    finished = "Done." in logs
    print(f"  t+{time.time() - t0:4.0f}s {target} exited after {took:.0f}s with code {exit_code}"
          f" ({'finished its sends: logged Done.' if finished else 'did NOT log Done.'})")
    if hang:
        http("POST", f"{base.SINK}/_hang/0")
        print(f"  t+{time.time() - t0:4.0f}s webhook sink answers normally again")
    time.sleep(2)
    compose("start", target)
    print(f"  t+{time.time() - t0:4.0f}s started {target} again")

    # Wait until every flip is processed and deliveries stop changing for 30s.
    last, stable_since = None, time.time()
    while time.time() - stable_since < 30:
        emails, hooks, _ = deliveries(tag)
        unprocessed = int(sql(
            "select count(*) from api_flip f join api_check c on c.id=f.owner_id "
            f"where c.name like '{tag}-%' and f.processed is null")[0][0])
        now_counts = (sum(emails.values()), sum(hooks.values()), unprocessed)
        if now_counts != last:
            print(f"  t+{time.time() - t0:4.0f}s emails={now_counts[0]} webhooks={now_counts[1]} "
                  f"unprocessed_flips={unprocessed}")
            last, stable_since = now_counts, time.time()
        time.sleep(2)

    lost, doubled, retried = [], [], []
    for c in checks:
        subject = f"DOWN | {c['name']}"
        got = {"oncall": emails[("oncall", subject)], "backup": emails[("backup", subject)],
               "webhook": hooks[c["uuid"]]}
        desc = f"{c['name']}: " + ", ".join(f"{k}={v}" for k, v in got.items())
        if 0 in got.values():
            lost.append(desc)
        elif got["oncall"] > 1 or got["backup"] > 1:
            doubled.append(desc)
        elif got["webhook"] > 1:
            retried.append(desc)

    stuck = sql(
        "select c.name, ch.kind, n.created from api_notification n "
        "join api_check c on c.id=n.owner_id join api_channel ch on ch.id=n.channel_id "
        f"where c.name like '{tag}-%' and n.error='Sending' order by c.name")
    flips = sql(
        "select count(*), count(processed) from api_flip f join api_check c on c.id=f.owner_id "
        f"where c.name like '{tag}-%' and f.new_status='down'")[0]
    down = sql(f"select count(*) from api_check where name like '{tag}-%' and status='down'")[0][0]

    print(f"\n[{tag}] {n} checks, {down} down in DB, {flips[0]} DOWN flips ({flips[1]} processed)")
    print(f"  every channel exactly once: {n - len(lost) - len(doubled) - len(retried)}")
    print(f"  LOST (a channel got nothing): {len(lost)}")
    for line in lost:
        print(f"    {line}")
    print(f"  email SENT TWICE: {len(doubled)}")
    for line in doubled:
        print(f"    {line}")
    print(f"  webhook retried (sink hung, so the client gave up and retried): {len(retried)}")
    for line in retried:
        print(f"    {line}")
    print(f"  notifications stuck in 'Sending' (died mid-send): {len(stuck)}")
    for name, kind, created in stuck:
        print(f"    {name} via {kind}, started {created}")
    return 0 if not lost and not doubled else 1


if __name__ == "__main__":
    sys.exit(main())
