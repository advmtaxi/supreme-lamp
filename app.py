import asyncio
import re
from urllib.parse import urlparse, urlencode, quote
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse
import httpx
from playwright.async_api import async_playwright

app = FastAPI()

def reconstruct_url(raw_url: str) -> str:

    # Strip existing fragments
    base = raw_url.split("#")[0]
    # Ensure https://
    if not base.startswith("http"):
        base = "https://" + base
    return base + "#player=clappr#autoplay=true"


async def grab_m3u8(embed_url: str) -> str:
    """Launch headless Chromium, intercept the m3u8 request."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium",
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
            ],
            headless=True,
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        m3u8_url = None
        found = asyncio.Event()

        def on_request(request):
            nonlocal m3u8_url
            if ".m3u8" in request.url and not found.is_set():
                m3u8_url = request.url
                found.set()

        page.on("request", on_request)

        await page.goto(embed_url, wait_until="domcontentloaded", timeout=30000)

        # Wait up to 15s for m3u8 to appear
        try:
            await asyncio.wait_for(found.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass

        await browser.close()

    if not m3u8_url:
        raise HTTPException(status_code=404, detail="No m3u8 found in page requests")

    return m3u8_url


async def fetch_m3u8_content(m3u8_url: str, origin: str, referer: str) -> str:
    """Fetch the m3u8 with proper headers."""
    headers = {
        "Referer": referer,
        "Origin": origin,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(m3u8_url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.text


def rewrite_m3u8(content: str, m3u8_url: str) -> str:
    """
    Return the m3u8 as-is — segments are served directly from origin.
    We only ensure relative URLs are made absolute.
    """
    base = m3u8_url.rsplit("/", 1)[0] + "/"
    lines = content.splitlines()
    output = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            # It's a segment line — make absolute if relative
            if stripped.startswith("http"):
                output.append(stripped)
            else:
                output.append(base + stripped)
        else:
            output.append(line)

    return "\n".join(output)


@app.get("/getm3u8")
async def get_m3u8(url: str = Query(..., description="embedindia.st embed URL")):
    # 1. Reconstruct URL with clappr + autoplay
    embed_url = reconstruct_url(url)

    parsed = urlparse(embed_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    referer = embed_url

    # 2. Launch browser, intercept m3u8
    m3u8_url = await grab_m3u8(embed_url)

    # 3. Fetch the m3u8 with embedindia headers
    content = await fetch_m3u8_content(m3u8_url, origin=origin, referer=referer)

    # 4. Rewrite relative segment URLs to absolute (segments NOT proxied)
    rewritten = rewrite_m3u8(content, m3u8_url)

    return PlainTextResponse(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Access-Control-Allow-Origin": "*",
            "X-M3U8-Source": m3u8_url,
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
