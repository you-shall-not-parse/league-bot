"""Run with python -m league_web.server; publish through Cloudflare Tunnel."""
import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web
from league_web.data import public_data

STATIC = Path(__file__).parent / "static"


@web.middleware
async def headers(request, handler):
    response = await handler(request)
    response.headers.update({
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        "Cache-Control": "no-cache",
    })
    return response


async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def snapshot(request):
    try:
        payload = await asyncio.to_thread(public_data, request.app[DATA_DIR])
    except Exception:
        logging.exception("Could not read league data")
        return web.json_response({"error": "League data is temporarily unavailable. Please retry."}, status=503)
    return web.json_response(payload)


DATA_DIR = web.AppKey("data_dir", object)


def create_app(data_dir=None):
    app = web.Application(middlewares=[headers])
    app[DATA_DIR] = data_dir
    app.router.add_get("/", index)
    app.router.add_get("/api/league", snapshot)
    app.router.add_static("/assets/", STATIC, show_index=False)
    return app


if __name__ == "__main__":
    web.run_app(create_app(os.environ.get("LEAGUE_DATA_DIR")), host="127.0.0.1", port=int(os.environ.get("LEAGUE_WEB_PORT", "7030")))
