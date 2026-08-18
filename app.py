"""shipcheck — post-deploy verification API.

POST /check {url} -> {ok, status, title, errors, warnings, lcp_ms, screenshot}.
POST /monitor {url, webhook} (Pro) -> hourly monitoring + webhook alerts on up/down.
Guards baked in: SSRF (private/metadata/link-local IPs, ports 80/443 only, DNS pin
against rebinding), hard 15s timeout, in-memory rate limit (3/hr/IP free), one JSON
log line per request for usage accounting.
"""
import asyncio
import base64
import html
import ipaddress
import json
import os
import secrets
import socket
import time
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, field_validator
from playwright.async_api import async_playwright

# ponytail: single shared key via env for MVP; per-user keys + Stripe metering when paying users exist
API_KEY = os.environ.get("SHIPCHECK_API_KEY", "sk-test-123")
FREE_KEY = "sk-free-demo"
HARD_TIMEOUT = 15           # seconds, hard ceiling per check (kills stuck pages)
FREE_LIMIT_PER_HOUR = 3
PAID_LIMIT_PER_HOUR = 60
MONITOR_CAP = 20
MONITOR_INTERVAL = int(os.environ.get("SHIPCHECK_INTERVAL", "3600"))  # seconds; lower for testing
# ponytail: /data only persists if a Railway volume is attached (one-time); without it this
# degrades to the container-local file (lost on redeploy, same as old in-memory behavior).
STORE = os.environ.get("SHIPCHECK_STORE") or (
    "/data/shipcheck_state.json" if os.path.isdir("/data")
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipcheck_state.json"))
PUBLIC_URL = os.environ.get("SHIPCHECK_PUBLIC_URL", "https://shipcheck-production-5d2a.up.railway.app")


class SSRFError(ValueError):
    pass


def assert_safe_url(url: str) -> tuple:
    """Validate scheme/port/host, resolve DNS, reject blocked ranges. Returns (host, pinned_ip).

    Blocked: non-http(s), ports other than 80/443, and anything resolving to private,
    loopback, link-local (incl. cloud metadata 169.254.169.254), reserved, multicast,
    or unspecified addresses. IPv4-mapped IPv6 is unwrapped before checking.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise SSRFError(f"scheme {p.scheme!r} not allowed (http/https only)")
    if p.port is not None and p.port not in (80, 443):
        raise SSRFError(f"port {p.port} not allowed (80/443 only)")
    host = p.hostname
    if not host:
        raise SSRFError("no host in url")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFError(f"dns resolve failed for {host}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped:
            ip = mapped
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
           or ip.is_multicast or ip.is_unspecified:
            raise SSRFError(f"{host} resolves to blocked address {ip}")
    return host, infos[0][4][0]


class RateLimiter:
    """Sliding-window counter per key, in-memory.
    ponytail: resets on redeploy, no sync across instances — move to Redis if >1 instance or abuse appears."""

    def __init__(self):
        self.windows = {}

    def hit(self, key: str, limit: int, window: int = 3600) -> int:
        """Record a hit. Returns remaining quota; negative = blocked (abs value = retry_after seconds)."""
        now = time.time()
        q = self.windows.setdefault(key, deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return -max(1, int(window - (now - q[0])))
        q.append(now)
        return limit - len(q)


RATE = RateLimiter()

# --- 24/7 monitoring (Pro feature) ---
# State lives in one JSON file so paying customers' monitors survive restarts.
# ponytail: no locking/queuing — single writer (monitor loop) + register endpoint is
# rare; move to SQLite when concurrent writes or >1 instance exists.
STATE = {"customers": {}}  # token -> {"webhook": str, "urls": {url: record}}
_monitor_task = None


def _new_monitor(url: str) -> dict:
    return {"last_ok": None, "last_status": None, "last_checked": None,
            "checks": 0, "oks": 0}


def save_state():
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f)
    os.replace(tmp, STORE)


def load_state():
    try:
        with open(STORE, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded.get("customers"), dict):
            STATE["customers"] = loaded["customers"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def find_or_create_customer(webhook: str) -> str:
    for token, c in STATE["customers"].items():
        if c.get("webhook") == webhook:
            return token
    token = secrets.token_urlsafe(8)
    STATE["customers"][token] = {"webhook": webhook, "urls": {}}
    return token


def total_monitors() -> int:
    return sum(len(c.get("urls", {})) for c in STATE["customers"].values())


def _send_webhook(webhook: str, text: str):
    """Fire-and-forget Discord webhook alert (blocks are cheap here)."""
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps({"content": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "note": f"webhook_send_failed: {e}"}), flush=True)


async def monitor_loop():
    """Every MONITOR_INTERVAL seconds, check each monitored URL; alert webhook on state change."""
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        dirty = False
        for token, c in list(STATE["customers"].items()):
            for url, rec in list(c.get("urls", {}).items()):
                try:
                    r = await check(url, screenshot=False, timeout=HARD_TIMEOUT)
                except SSRFError:
                    del c["urls"][url]
                    dirty = True
                    continue
                now_ok = bool(r["ok"])
                prev_ok = rec["last_ok"]
                rec.update(last_ok=now_ok, last_status=r["status"],
                           last_checked=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           checks=rec.get("checks", 0) + 1,
                           oks=rec.get("oks", 0) + (1 if now_ok else 0))
                dirty = True
                _log(urlparse(url).hostname, now_ok, r["status"], "monitor", "pro", 0, "monitor")
                if prev_ok is None:
                    continue  # first check after (re)start — establish baseline, no alert
                if now_ok != prev_ok:
                    state = "✅ back UP" if now_ok else "🔴 is DOWN"
                    extra = "" if now_ok else f" — status {r['status']}, {r['errors'][:1]}"
                    _send_webhook(c["webhook"], f"shipcheck: `{url}` {state}{extra}")
        if dirty:
            save_state()


@asynccontextmanager
async def lifespan(app):
    global _monitor_task
    load_state()
    _monitor_task = asyncio.create_task(monitor_loop())
    yield
    if _monitor_task:
        _monitor_task.cancel()


app = FastAPI(title="shipcheck", lifespan=lifespan)


class MonitorRequest(BaseModel):
    url: str
    webhook: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("url must be http(s) with a host")
        return v

    @field_validator("webhook")
    @classmethod
    def validate_webhook(cls, v: str) -> str:
        # alerts only go to Discord webhooks — prevents turning shipcheck into a webhook spam relay
        if not v.startswith("https://discord.com/api/webhooks/") and \
           not v.startswith("https://discordapp.com/api/webhooks/"):
            raise ValueError("webhook must be a Discord webhook URL")
        return v


async def _visit(url: str, host: str, pinned_ip: str, screenshot: bool, timeout: int) -> dict:
    """Run headless Chromium against url with DNS pinned to pinned_ip."""
    errors, warnings = [], []
    result = {"url": url, "ok": False, "status": None, "title": None,
              "lcp_ms": None, "errors": errors, "warnings": warnings}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=[
            "--no-sandbox",
            # DNS pin: Chromium must connect to the exact IP we validated — closes the
            # rebinding window between assert_safe_url() and the browser's own connect.
            # ponytail: redirects to OTHER hosts still resolve via Chromium's DNS; add a
            # context.route() validator on every hostname if this ever gets targeted.
            f"--host-resolver-rules=MAP {host} {pinned_ip}",
        ])
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})

            def _on_console(m):
                if m.type in ("error", "warning"):
                    loc = (m.location or {}).get("url", "")
                    text = f"{m.text} @ {loc}" if loc else m.text  # name the failing resource, not just the symptom
                    (errors if m.type == "error" else warnings).append(text)

            page.on("console", _on_console)
            t0 = time.time()
            try:
                resp = await page.goto(url, wait_until="load", timeout=timeout * 1000)
                result["status"] = resp.status if resp else None
                result["title"] = await page.title()
                result["lcp_ms"] = await page.evaluate(
                    "() => new Promise(r => { let v = null;"
                    "new PerformanceObserver(l => { const e = l.getEntries();"
                    "if (e.length) v = e[e.length-1].startTime; }).observe({type:'largest-contentful-paint', buffered:true});"
                    "setTimeout(() => r(v), 800); })"
                ) or round((time.time() - t0) * 1000)
                result["ok"] = bool(resp and resp.ok)
                if screenshot:
                    # screenshot lives only in this JSON response (RAM, ~40-100KB) — never written to disk
                    shot = await page.screenshot(type="jpeg", quality=70, full_page=False)
                    result["screenshot_mobile"] = base64.b64encode(shot).decode()
            except Exception as e:
                errors.append(f"navigation failed: {type(e).__name__}: {e}")
        finally:
            await browser.close()
    return result


async def check(url: str, screenshot: bool = True, timeout: int = HARD_TIMEOUT) -> dict:
    """SSRF-validate, then visit with hard timeout. Raises SSRFError for blocked targets."""
    timeout = min(timeout, HARD_TIMEOUT)
    host, ip = assert_safe_url(url)
    try:
        return await asyncio.wait_for(_visit(url, host, ip, screenshot, timeout), timeout + 5)
    except asyncio.TimeoutError:
        return {"url": url, "ok": False, "status": None, "title": None, "lcp_ms": None,
                "errors": [f"hard timeout: page did not finish within {timeout + 5}s"], "warnings": []}


class CheckRequest(BaseModel):
    url: str
    screenshot: bool = True
    timeout: int = 15

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("url must be http(s) with a host")
        return v


def _log(domain: str, ok: bool, status, ip: str, tier: str, ms: int, note: str = ""):
    """One JSON line per request to stdout — Railway captures it; feed usage decisions."""
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "domain": domain, "ok": ok, "status": status,
                      "ip": ip, "tier": tier, "ms": ms, "note": note}), flush=True)


@app.post("/check")
async def api_check(req: CheckRequest, request: Request, authorization: str = Header(default="")):
    tier = "paid" if authorization == f"Bearer {API_KEY}" \
        else "free" if authorization == f"Bearer {FREE_KEY}" else None
    if tier is None:
        raise HTTPException(401, "missing or invalid api key")
    # ponytail: trusts leftmost X-Forwarded-For (set by Railway proxy); validate against real proxy list if self-hosted elsewhere
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "?")
    rate_key = API_KEY if tier == "paid" else ip
    limit = PAID_LIMIT_PER_HOUR if tier == "paid" else FREE_LIMIT_PER_HOUR
    remaining = RATE.hit(rate_key, limit)
    if remaining < 0:
        _log(urlparse(req.url).hostname, False, 429, ip, tier, 0, "rate_limited")
        raise HTTPException(429, {"detail": "rate limited", "retry_after": -remaining,
                                  "limit": f"{limit}/hour"})
    t0 = time.time()
    try:
        result = await check(req.url, req.screenshot, req.timeout)
    except SSRFError as e:
        _log(urlparse(req.url).hostname, False, 403, ip, tier, int((time.time() - t0) * 1000), f"ssrf_blocked: {e}")
        raise HTTPException(403, f"blocked: {e}")
    _log(urlparse(req.url).hostname, result["ok"], result["status"], ip, tier, int((time.time() - t0) * 1000))
    return result


@app.post("/monitor")
async def api_monitor(req: MonitorRequest, authorization: str = Header(default="")):
    """Pro-only: register a URL for hourly monitoring + Discord alerts on up/down."""
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(402, "monitoring is a Pro feature — get an API key at " + PUBLIC_URL)
    if total_monitors() >= MONITOR_CAP:
        raise HTTPException(503, f"monitor capacity full ({MONITOR_CAP})")
    try:
        assert_safe_url(req.url)
    except SSRFError as e:
        raise HTTPException(403, f"blocked: {e}")
    token = find_or_create_customer(req.webhook)
    STATE["customers"][token]["urls"].setdefault(req.url, _new_monitor(req.url))
    save_state()
    _log(urlparse(req.url).hostname, True, 200, "?", "pro", 0, "monitor_registered")
    return {"registered": req.url, "interval_seconds": MONITOR_INTERVAL,
            "active_monitors": total_monitors(),
            "status_page": f"{PUBLIC_URL}/status/{token}"}


@app.get("/monitors")
async def api_monitors(authorization: str = Header(default="")):
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(401, "invalid api key")
    return {"count": total_monitors(),
            "urls": {u: {"ok": rec.get("last_ok"), "status": rec.get("last_status")}
                     for c in STATE["customers"].values()
                     for u, rec in c.get("urls", {}).items()}}


@app.get("/status/{token}", response_class=HTMLResponse)
async def status_page(token: str):
    """Public per-customer status page — token is the secret, page needs no login.
    Ponytail: plain server-rendered HTML, no auth; add real accounts when customers exist."""
    c = STATE["customers"].get(token)
    if not c:
        raise HTTPException(404, "unknown status page")
    rows = ""
    for url, rec in c.get("urls", {}).items():
        ok, st, n, oks, last = rec.get("last_ok"), rec.get("last_status"), rec.get("checks", 0), rec.get("oks", 0), rec.get("last_checked")
        badge = "✅ UP" if ok else ("🔴 DOWN" if ok is False else "⏳ awaiting first check")
        up_pct = f"{oks / n * 100:.1f}%" if n else "—"
        rows += (f"<tr><td>{html.escape(url)}</td><td>{badge}</td>"
                 f"<td>{html.escape(str(st))}</td><td>{up_pct}</td>"
                 f"<td>{html.escape(last or '—')}</td></tr>")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>shipcheck status</title>
<style>
body {{ background:#0a0a0f; color:#e8e8ed; font:16px/1.6 system-ui,sans-serif; margin:0; padding:32px 20px }}
h1 {{ font-size:1.3rem }} h1 a {{ color:#4ade80; text-decoration:none }}
table {{ width:100%; border-collapse:collapse; margin-top:16px; font-size:.9rem }}
th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid #26262f }}
th {{ color:#8b8b96; font-weight:500 }} td {{ word-break:break-all }}
</style></head><body>
<h1>⚓ <a href="{PUBLIC_URL}">shipcheck</a> status</h1>
<table><tr><th>Site</th><th>Status</th><th>Last HTTP</th><th>Uptime</th><th>Last checked (UTC)</th></tr>
{rows}</table></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def landing():
    return open(os.path.join(os.path.dirname(__file__), "landing.html"), encoding="utf-8").read()


@app.get("/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    import sys
    # --- guard self-checks (no network) ---
    blocked = ["http://127.0.0.1/", "http://localhost/", "http://10.0.0.1/",
               "http://192.168.1.1/", "http://172.16.0.1/", "http://172.31.255.255/",
               "http://169.254.169.254/", "http://[::1]/", "http://0.0.0.0/",
               "http://example.com:8080/", "ftp://example.com/"]
    for bad in blocked:
        try:
            assert_safe_url(bad)
            raise AssertionError(f"should have blocked: {bad}")
        except SSRFError:
            pass
    host, ip = assert_safe_url("https://example.com")
    assert host == "example.com" and ipaddress.ip_address(ip).is_global
    rl = RateLimiter()
    assert rl.hit("t", 3) == 2 and rl.hit("t", 3) == 1 and rl.hit("t", 3) == 0
    assert rl.hit("t", 3) < 0
    # --- persistence self-check: save → wipe → load round-trip ---
    t1 = find_or_create_customer("https://discord.com/api/webhooks/1/abc")
    STATE["customers"][t1]["urls"]["https://x.test"] = _new_monitor("https://x.test")
    save_state()
    assert find_or_create_customer("https://discord.com/api/webhooks/1/abc") == t1  # same webhook → same token
    kept = STATE["customers"]
    STATE["customers"] = {}
    load_state()
    assert STATE["customers"].get(t1, {}).get("urls", {}).get("https://x.test") is not None
    os.remove(STORE)
    STATE["customers"] = kept
    print(f"GUARD CHECKS PASSED ({len(blocked)} SSRF cases + rate limiter + persistence)", file=sys.stderr)
    # --- live check ---
    r = asyncio.run(check("https://example.com", screenshot=False))
    print(json.dumps(r, indent=1))
    assert r["ok"] and r["status"] == 200, f"checker self-check failed: {r}"
    print("SELF-CHECK PASSED", file=sys.stderr)
