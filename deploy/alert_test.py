"""End-to-end check that each alert is delivered exactly once.

Needs the local stack: docker compose -f compose.yml -f compose.local.yml up -d

Creates N checks through Caddy (HTTPS, verified against Caddy's local CA),
pings them all at the same moment so they all go down together and the two
sendalerts daemons (one per web container) race for them. Then it asserts
exactly one DOWN email and one DOWN webhook per check, and still exactly one
after waiting extra sendalerts cycles. Finally it pings one check back up and
asserts exactly one UP email and one UP webhook.

Usage (from deploy/): python3 -I alert_test.py [N]
"""

from __future__ import annotations

import json
import os
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
from collections import Counter
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = ["docker", "compose", "-f", "compose.yml", "-f", "compose.local.yml"]
SITE = "https://localhost"
MAILPIT = "http://localhost:8025"
SINK = "http://localhost:8099"
TIMEOUT, GRACE = 60, 60  # the minimums healthchecks allows


def compose(*args: str, input: str | None = None) -> str:
    r = subprocess.run(COMPOSE + list(args), cwd=HERE, input=input, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"{' '.join(args)} failed:\n{r.stderr}")
    return r.stdout


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None,
         ctx: ssl.SSLContext | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method, headers=headers or {})
    # Everything is on localhost: ignore any HTTP(S)_PROXY in the environment.
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ctx))
    with opener.open(req, timeout=30) as r:
        raw = r.read()
        if raw and r.headers.get_content_type() == "application/json":
            return json.loads(raw)
        return raw.decode()


SETUP = """
import json, uuid
from django.contrib.auth.models import User
from hc.accounts.models import Project, Profile
from hc.api.models import Channel
u = User.objects.create_user("alert-test-{tag}", "alert-test-{tag}@localhost")
Profile.objects.for_user(u)
p = Project.objects.create(owner=u, name="alert-test-{tag}", api_key="{key}",
    badge_key=str(uuid.uuid4()))
Channel.objects.create(project=p, kind="email", email_verified=True,
    value=json.dumps({{"value": "oncall-{tag}@localhost", "up": True, "down": True}}))
spec = lambda s: {{"method_" + s: "POST", "url_" + s: "http://webhook-sink:8080/{tag}/" + s + "/$CODE",
    "body_" + s: "$NAME $STATUS", "headers_" + s: {{}}}}
Channel.objects.create(project=p, kind="webhook", value=json.dumps({{**spec("down"), **spec("up")}}))
"""


def deliveries(tag: str) -> tuple[Counter, Counter]:
    """Return (emails, webhooks), each a Counter keyed by (status, check name or code)."""
    emails: Counter = Counter()
    msgs = http("GET", f"{MAILPIT}/api/v1/search?query=to:oncall-{tag}@localhost&limit=1000")
    for m in msgs["messages"]:
        status, _, name = m["Subject"].partition(" | ")
        emails[(status, name)] += 1

    hooks: Counter = Counter()
    for r in http("GET", f"{SINK}/_requests"):
        parts = r["path"].strip("/").split("/")
        if parts[0] == tag:
            hooks[(parts[1].upper(), parts[2])] += 1
    return emails, hooks


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    tag = secrets.token_hex(3)
    key = secrets.token_urlsafe(24)

    # Trust Caddy's internal root CA rather than disabling verification.
    root = compose("exec", "-T", "caddy", "cat", "/data/caddy/pki/authorities/local/root.crt")
    with tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False) as f:
        f.write(root)
    ctx = ssl.create_default_context(cafile=f.name)

    running = compose("ps", "--status", "running", "--services").split()
    web = next(svc for svc in ("web1", "web2") if svc in running)
    compose("exec", "-T", web, "./manage.py", "shell", input=SETUP.format(tag=tag, key=key))
    api = {"X-Api-Key": key, "Content-Type": "application/json"}

    checks = []
    for i in range(n):
        c = http("POST", f"{SITE}/api/v3/checks/", {
            "name": f"{tag}-{i:03d}", "timeout": TIMEOUT, "grace": GRACE, "channels": "*",
        }, api, ctx)
        checks.append(c)
    print(f"[{tag}] created {n} checks, each with 1 email + 1 webhook channel")

    for c in checks:
        http("GET", c["ping_url"], ctx=ctx)
    t0 = time.time()
    print(f"[{tag}] pinged all; they go down after {TIMEOUT + GRACE}s, waiting...")

    deadline = t0 + TIMEOUT + GRACE + 90
    while time.time() < deadline:
        statuses = Counter(c["status"] for c in http("GET", f"{SITE}/api/v3/checks/", None, api, ctx)["checks"])
        emails, hooks = deliveries(tag)
        print(f"  t+{time.time() - t0:5.0f}s statuses={dict(statuses)} "
              f"emails={sum(emails.values())} webhooks={sum(hooks.values())}")
        if statuses.get("down") == n and sum(emails.values()) >= n and sum(hooks.values()) >= n:
            break
        time.sleep(5)

    # Give both sendalerts loops several more cycles to produce any duplicate.
    time.sleep(20)
    emails, hooks = deliveries(tag)

    failures = []
    for c in checks:
        e = emails[("DOWN", c["name"])]
        w = hooks[("DOWN", c["uuid"])]
        if e != 1 or w != 1:
            failures.append(f"{c['name']}: {e} DOWN emails, {w} DOWN webhooks")

    # Bring one check back up: expect exactly one UP of each.
    up = checks[0]
    http("GET", up["ping_url"], ctx=ctx)
    for _ in range(30):
        emails, hooks = deliveries(tag)
        if emails[("UP", up["name"])] and hooks[("UP", up["uuid"])]:
            break
        time.sleep(1)
    time.sleep(10)
    emails, hooks = deliveries(tag)
    e, w = emails[("UP", up["name"])], hooks[("UP", up["uuid"])]
    if e != 1 or w != 1:
        failures.append(f"{up['name']}: {e} UP emails, {w} UP webhooks")

    # Which container's sendalerts sent the DOWN alerts (proves both raced).
    by_web = {}
    for svc in ("alerts1", "alerts2"):
        logs = compose("logs", "--no-log-prefix", svc)
        by_web[svc] = sum(1 for line in logs.splitlines()
                          if " goes down" in line and any(c["uuid"] in line for c in checks))
    print(f"[{tag}] DOWN flips processed per container: {by_web}")

    if failures:
        print(f"[{tag}] FAIL: {len(failures)} problem(s)")
        for line in failures:
            print("  " + line)
        return 1
    print(f"[{tag}] PASS: {n} checks -> exactly {n} DOWN emails + {n} DOWN webhooks; "
          f"1 UP email + 1 UP webhook")
    return 0


if __name__ == "__main__":
    sys.exit(main())
