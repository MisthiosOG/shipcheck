# shipcheck

[![skills.sh](https://skills.sh/b/MisthiosOG/shipcheck)](https://skills.sh/MisthiosOG/shipcheck)

Post-deploy verification for AI coding agents. Your agent just said "deployed successfully" — but is the app actually up, error-free, and fast? shipcheck checks.

## Install

```
npx skills add MisthiosOG/shipcheck
```

## What it does

After any deploy, the agent calls the check API and reports:

- ✅ Page up + HTTP status + title
- ❌ Console errors (JS errors, failed fetches, broken resources)
- ⚡ LCP load time
- 📱 Mobile screenshot

```json
{"ok": true, "status": 200, "title": "Example Domain", "lcp_ms": 1360, "errors": [], "warnings": []}
```

## API

```bash
curl -X POST https://shipcheck-production-5d2a.up.railway.app/check \
  -H "Authorization: Bearer $SHIPCHECK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-app.vercel.app"}'
```

Works with any URL — Vercel, Netlify, Railway, custom domains. Free tier available; `SHIPCHECK_API_KEY` required.

## Local mode (no API)

```bash
pip install -r requirements.txt && python -m playwright install chromium
python check.py https://your-app.vercel.app
```

## Guards

- **SSRF** — URL must resolve to a public IP; private ranges (10/8, 192.168/16, 172.16/12), loopback, link-local (incl. cloud metadata 169.254.169.254), reserved & multicast are rejected. Ports 80/443 only. The validated IP is pinned into Chromium (`--host-resolver-rules`) to close the DNS-rebinding window.
- **Hard timeout** — 15s per page, browser killed on overhang.
- **Rate limit** — 3 checks/hour per IP on the free tier (in-memory sliding window), 60/hour on paid keys. Blocked requests also count against the quota.
- **Logging** — one JSON line per request (domain, result, IP, tier, ms) to stdout for usage accounting.

## Cost per check (estimates)

| Metric | Per check |
|---|---|
| RAM | ~150–300 MB (one headless Chromium page) |
| Wall time | ~5–15 s (~2–8 CPU-sec) |
| Capacity on a $5 Railway tier | ~2 concurrent, ~3–6 checks/min peak |
| Ballpark cost | ~$0.0005–0.002/check |

One $9 subscriber covers roughly 2,500–10,000 free-tier checks — verify against the Railway dashboard after the first ~100 requests.

