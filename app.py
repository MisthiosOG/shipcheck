"""shipcheck — post-deploy verification API.

POST /check {url} -> {ok, status, title, errors, warnings, lcp_ms, screenshot}.
Guards baked in: SSRF (private/metadata/link-local IPs, ports 80/443 only, DNS pin
against rebinding), hard 15s timeout, in-memory rate limit (3/hr/IP free), one JSON
log line per request for usage accounting.
"""
import asyncio
import base64
import ipaddress
import json
import os
import socket
import time
from collections import deque
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, field_validator
from playwright.async_api import async_playwright

app = FastAPI(title="shipcheck")

# ponytail: single shared key via env for MVP; per-user keys + Stripe metering when paying users exist
API_KEY = os.environ.get("SHIPCHECK_API_KEY", "sk-test-123")
FREE_KEY = "sk-free-demo"
HARD_TIMEOUT = 15           # seconds, hard ceiling per check (kills stuck pages)
FREE_LIMIT_PER_HOUR = 3
PAID_LIMIT_PER_HOUR = 60


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
    print(f"GUARD CHECKS PASSED ({len(blocked)} SSRF cases + rate limiter)", file=sys.stderr)
    # --- live check ---
    r = asyncio.run(check("https://example.com", screenshot=False))
    print(json.dumps(r, indent=1))
    assert r["ok"] and r["status"] == 200, f"checker self-check failed: {r}"
    print("SELF-CHECK PASSED", file=sys.stderr)
