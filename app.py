"""shipcheck — post-deploy verification API.

One endpoint: POST /check {url} -> {ok, status, title, errors, warnings, lcp_ms, screenshots}.
Runs a headless browser, reports console errors + basic vitals. Free tier & billing are
enforced upstream (API key check) — keep the core dependency-free of auth so it stays testable.
"""
import base64
import os
import re
import time
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, field_validator
from playwright.async_api import async_playwright

app = FastAPI(title="shipcheck")

# ponytail: single shared key via env for MVP; per-user keys + Stripe metering when paying users exist
API_KEY = os.environ.get("SHIPCHECK_API_KEY", "sk-test-123")

CONSOLE_RE = re.compile(r"failed to load resource|net::ERR_", re.I)


class CheckRequest(BaseModel):
    url: str
    screenshot: bool = True
    timeout: int = 20

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("url must be http(s) with a host")
        return v


async def check(url: str, screenshot: bool = True, timeout: int = 20) -> dict:
    """Visit url in headless Chromium, return vitals + console errors + screenshots."""
    errors, warnings = [], []
    result = {
        "url": url, "ok": False, "status": None, "title": None,
        "lcp_ms": None, "errors": errors, "warnings": warnings,
    }
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else warnings.append(m.text) if m.type == "warning" else None)
        t0 = time.time()
        try:
            resp = await page.goto(url, wait_until="load", timeout=timeout * 1000)
            result["status"] = resp.status if resp else None
            result["title"] = await page.title()
            # LCP via PerformanceObserver — fall back to load time if unavailable
            result["lcp_ms"] = await page.evaluate(
                "() => new Promise(r => { let v = null;"
                "new PerformanceObserver(l => { const e = l.getEntries();"
                "if (e.length) v = e[e.length-1].startTime; }).observe({type:'largest-contentful-paint', buffered:true});"
                "setTimeout(() => r(v), 800); })"
            ) or round((time.time() - t0) * 1000)
            result["ok"] = bool(resp and resp.ok)
            if screenshot:
                shot = await page.screenshot(type="jpeg", quality=70, full_page=False)
                result["screenshot_mobile"] = base64.b64encode(shot).decode()
        except Exception as e:
            errors.append(f"navigation failed: {type(e).__name__}: {e}")
        finally:
            await browser.close()
    return result


@app.post("/check")
async def api_check(req: CheckRequest, authorization: str = Header(default="")):
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(401, "missing or invalid api key")
    return await check(req.url, req.screenshot, req.timeout)


@app.get("/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    import asyncio, json, sys
    # self-check: verify example.com end-to-end, assert the basics hold
    r = asyncio.run(check("https://example.com", screenshot=False, timeout=15))
    print(json.dumps(r, indent=1))
    assert r["ok"] and r["status"] == 200, f"checker self-check failed: {r}"
    print("SELF-CHECK PASSED", file=sys.stderr)
