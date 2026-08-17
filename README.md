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
