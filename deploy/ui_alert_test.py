"""Alert delivery test that goes through the UI the way a person would.

Needs the local stack: docker compose -f compose.yml -f compose.local.yml up -d

In a real (headless) Chrome, over HTTPS through Caddy:
  1. sign up with a new address and log in with the magic link from Mailpit
  2. add an email integration for a second address (on-call), confirm it is
     "Unconfirmed", then verify it via the link in the email
  3. add a webhook integration in the webhook form
  4. set "My First Check" to 1 minute period / 1 minute grace in the schedule modal
  5. ping the check's URL once (as a cron job would), then let it go down

Then asserts exactly one DOWN email to each address and one DOWN webhook, still
exactly one after extra sendalerts cycles, and then exactly one of each UP after
pinging the check again.

The browser trusts Caddy's local root CA: the server's chain is verified
against that root in Python, and Chrome is told to accept only those
verified certificates (--ignore-certificate-errors-spki-list).

Usage (from deploy/):
  .venv/bin/python ui_alert_test.py [--headed]
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections import Counter
from urllib.parse import quote
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from playwright.sync_api import Page, expect, sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = ["docker", "compose", "-f", "compose.yml", "-f", "compose.local.yml"]
SITE = "https://localhost"
MAILPIT = "http://localhost:8025"
SINK = "http://localhost:8099"
CHECK_NAME = "My First Check"


def compose(*args: str) -> str:
    r = subprocess.run(COMPOSE + list(args), cwd=HERE, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"{' '.join(args)} failed:\n{r.stderr}")
    return r.stdout


def http(url: str, ctx: ssl.SSLContext | None = None):
    # Everything is on localhost: ignore any HTTP(S)_PROXY in the environment.
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ctx))
    with opener.open(Request(url), timeout=30) as r:
        raw = r.read()
        if r.headers.get_content_type() == "application/json":
            return json.loads(raw)
        return raw.decode()


def caddy_trust() -> tuple[ssl.SSLContext, list[str]]:
    """Return an SSL context trusting Caddy's root, and SPKI pins for Chrome."""
    root = compose("exec", "-T", "caddy", "cat", "/data/caddy/pki/authorities/local/root.crt")
    with tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False) as f:
        f.write(root)
    ctx = ssl.create_default_context(cafile=f.name)

    with socket.create_connection(("localhost", 443)) as sock:
        with ctx.wrap_socket(sock, server_hostname="localhost") as tls:
            chain = tls.get_verified_chain()

    pins = []
    for der in chain:  # DER-encoded certs, leaf first
        pem = ssl.DER_cert_to_PEM_cert(der)
        pubkey = subprocess.run(["openssl", "x509", "-pubkey", "-noout"], input=pem.encode(),
                                capture_output=True, check=True).stdout
        spki = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "der"], input=pubkey,
                              capture_output=True, check=True).stdout
        digest = subprocess.run(["openssl", "dgst", "-sha256", "-binary"], input=spki,
                                capture_output=True, check=True).stdout
        pins.append(base64.b64encode(digest).decode())
    return ctx, pins


def mail_subjects(to: str) -> Counter:
    found = http(f"{MAILPIT}/api/v1/search?query={quote(f'to:{to}')}&limit=1000")
    return Counter(m["Subject"] for m in found["messages"])


def wait_for_mail(to: str, subject: str, timeout: float = 30) -> str:
    """Wait for a message to `to` whose subject starts with `subject`; return its text body."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = http(f"{MAILPIT}/api/v1/search?query={quote(f'to:{to}')}&limit=50")
        for m in found["messages"]:
            if m["Subject"].startswith(subject):
                return http(f"{MAILPIT}/api/v1/message/{m['ID']}")["Text"]
        time.sleep(1)
    sys.exit(f"no '{subject}' email to {to} within {timeout}s")


def webhook_hits(tag: str) -> Counter:
    hits: Counter = Counter()
    for r in http(f"{SINK}/_requests"):
        parts = r["path"].strip("/").split("/")
        if parts[0] == tag:
            hits[parts[1]] += 1
    return hits


def step(msg: str) -> None:
    print(f"  - {msg}", flush=True)


def go_to_integrations(page: Page) -> None:
    page.get_by_role("link", name="Integrations").first.click()
    expect(page).to_have_url(re.compile(r"/projects/[0-9a-f-]+/integrations/$"))


def main() -> int:
    headed = "--headed" in sys.argv
    tag = secrets.token_hex(3)
    me = f"owner-{tag}@localhost"
    oncall = f"oncall-{tag}@localhost"
    print(f"[{tag}] owner={me} oncall={oncall}")

    ctx, pins = caddy_trust()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=not headed,
            args=["--no-proxy-server", "--ignore-certificate-errors-spki-list=" + ",".join(pins)],
        )
        page = browser.new_page()
        page.set_default_timeout(15_000)

        # 1. Sign up, log in via the emailed magic link
        page.goto(SITE + "/")
        expect(page).to_have_url(SITE + "/accounts/login/")
        page.click("#nav-sign-up")
        page.fill("#signup-email", me)
        page.click("#signup-go")
        expect(page.locator("#signup-result")).to_contain_text("Please check your email!")
        step("signed up over HTTPS")

        text = wait_for_mail(me, "Log in to")
        login_link = re.search(r"https://localhost/accounts/check_token/\S+", text).group(0)
        page.goto(login_link)
        expect(page).to_have_url(re.compile(r"/projects/[0-9a-f-]+/checks/$"))
        expect(page.get_by_text(CHECK_NAME).first).to_be_visible()
        project = re.search(r"/projects/([0-9a-f-]+)/", page.url).group(1)
        step(f"logged in via emailed link, project {project}")

        # 2. Add an email integration for the on-call address and verify it
        go_to_integrations(page)
        page.click(f'a[href="/projects/{project}/add_email/"]')
        page.fill('input[name="value"]', oncall)
        page.check('input[name="down"]')
        page.check('input[name="up"]')
        page.get_by_role("button", name="Save Integration").click()
        expect(page).to_have_url(f"{SITE}/projects/{project}/integrations/")
        row = page.locator("tr", has_text=oncall)
        expect(row.locator(".label", has_text="Unconfirmed")).to_be_visible()
        step(f"added email integration for {oncall}: shows Unconfirmed")

        text = wait_for_mail(oncall, "Verify email address")
        verify_link = re.search(r"https://localhost/integrations/\S+/verify/\S+/", text).group(0)
        page.goto(verify_link)
        expect(page.get_by_role("heading", name="Email Address Verified!")).to_be_visible()
        page.goto(f"{SITE}/projects/{project}/integrations/")
        expect(page.locator("tr", has_text=oncall).locator(".label", has_text="Unconfirmed")).to_have_count(0)
        step("verified it via the emailed link")

        # 3. Add a webhook integration
        page.click(f'a[href="/projects/{project}/add_webhook/"]')
        page.fill("#webhook-form-name", f"sink {tag}")
        for status in ("down", "up"):
            page.select_option(f"#method-{status}", "POST")
            page.fill(f'input[name="url_{status}"]', f"http://webhook-sink:8080/{tag}/{status}/$CODE")
            page.fill(f'textarea[name="body_{status}"]', "$NAME is $STATUS")
        page.locator("#add-webhook-form button[type=submit]").click()
        expect(page).to_have_url(f"{SITE}/projects/{project}/integrations/")
        expect(page.get_by_text(f"sink {tag}")).to_be_visible()
        step("added webhook integration")

        # 4. Set the check's schedule to 1 minute period, 1 minute grace
        page.get_by_role("link", name="Checks").first.click()
        page.locator("tr.checks-row", has_text=CHECK_NAME).locator('a[title="Show Details"]').click()
        expect(page).to_have_url(re.compile(r"/checks/[0-9a-f-]+/details/$"))
        page.click("#edit-timeout")
        modal = page.locator("#update-timeout-modal")
        expect(modal).to_be_visible()
        modal.locator("#period-unit").select_option("60")
        modal.locator("#period-value").fill("1")
        modal.locator("#grace-unit").select_option("60")
        modal.locator("#grace-value").fill("1")
        modal.locator("#update-timeout-form button[type=submit]").click()
        expect(page.locator("#details-schedule")).to_contain_text("1 minute")
        step("set period 1 min, grace 1 min in the schedule modal")

        # 5. Ping it once, the way a cron job would, then wait for it to go down
        ping_url = page.locator("#how-to-ping code.click-to-copy", has_text="/ping/").inner_text().strip()
        assert http(ping_url, ctx) == "OK"
        t0 = time.time()
        step(f"pinged {ping_url}; expecting DOWN about 120s from now")

        status = ""
        while time.time() < t0 + 210:
            page.reload()
            classes = page.locator("#current-status-icon").get_attribute("class")
            status = re.search(r"ic-(\w+)", classes).group(1)
            down_mails = sum(n for s, n in (mail_subjects(me) + mail_subjects(oncall)).items()
                             if s.startswith("DOWN"))
            print(f"    t+{time.time() - t0:4.0f}s UI status={status} down_emails={down_mails} "
                  f"webhooks={dict(webhook_hits(tag))}", flush=True)
            if status == "down" and down_mails >= 2 and webhook_hits(tag)["down"] >= 1:
                break
            time.sleep(5)

        # Give both sendalerts loops several more cycles to produce any duplicate.
        time.sleep(20)
        down = f"DOWN | {CHECK_NAME}"
        up = f"UP | {CHECK_NAME}"
        results = {
            f"UI shows {CHECK_NAME} down": (status == "down", status),
            f"'{down}' emails to {me}": (mail_subjects(me)[down] == 1, mail_subjects(me)[down]),
            f"'{down}' emails to {oncall}": (mail_subjects(oncall)[down] == 1, mail_subjects(oncall)[down]),
            "DOWN webhooks": (webhook_hits(tag)["down"] == 1, webhook_hits(tag)["down"]),
        }

        # 6. Ping again: expect exactly one UP of each
        assert http(ping_url, ctx) == "OK"
        deadline = time.time() + 30
        while time.time() < deadline and not (
            mail_subjects(me)[up] and mail_subjects(oncall)[up] and webhook_hits(tag)["up"]
        ):
            time.sleep(1)
        time.sleep(10)
        results[f"'{up}' emails to {me}"] = (mail_subjects(me)[up] == 1, mail_subjects(me)[up])
        results[f"'{up}' emails to {oncall}"] = (mail_subjects(oncall)[up] == 1, mail_subjects(oncall)[up])
        results["UP webhooks"] = (webhook_hits(tag)["up"] == 1, webhook_hits(tag)["up"])

        browser.close()

    ok = True
    for name, (passed, got) in results.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}: {got}")
        ok &= passed
    print(f"[{tag}] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
