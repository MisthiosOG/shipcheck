"""Local fallback checker — same guards as the API (SSRF, hard timeout), no server needed.
Usage: python check.py https://your-app.vercel.app
"""
import asyncio, json, sys, base64
from app import check, SSRFError

async def main():
    if len(sys.argv) < 2:
        print("usage: python check.py <url>", file=sys.stderr); sys.exit(2)
    try:
        r = await check(sys.argv[1], screenshot=True)
    except SSRFError as e:
        print(json.dumps({"ok": False, "error": f"blocked: {e}"})); sys.exit(2)
    shot = r.pop("screenshot_mobile", None)
    if shot:
        open("shipcheck-screenshot.jpg", "wb").write(base64.b64decode(shot))
        r["screenshot"] = "shipcheck-screenshot.jpg"
    print(json.dumps(r, indent=1))
    sys.exit(0 if r["ok"] else 1)

asyncio.run(main())
