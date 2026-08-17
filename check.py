"""Local fallback checker — same logic as the API, no server needed.
Usage: python check.py https://your-app.vercel.app
"""
import asyncio, json, sys, base64
from app import check

async def main():
    if len(sys.argv) < 2:
        print("usage: python check.py <url>", file=sys.stderr); sys.exit(1)
    r = await check(sys.argv[1], screenshot=True, timeout=20)
    shot = r.pop("screenshot_mobile", None)
    if shot:
        open("shipcheck-screenshot.jpg", "wb").write(base64.b64decode(shot))
        r["screenshot"] = "shipcheck-screenshot.jpg"
    print(json.dumps(r, indent=1))
    sys.exit(0 if r["ok"] else 1)

asyncio.run(main())
