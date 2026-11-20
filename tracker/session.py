"""Passive attachment to YOUR Chrome over the DevTools protocol (CDP).

Read-only by construction:
  * The tracker never navigates, clicks, types or sends any request to the visa site.
  * It only enables the CDP *Network* domain on tabs of the dedicated Chrome profile and reads
    responses to visa-site (and login-page) requests that your own browsing produced.
  * It does not enable Runtime/Page domains or inject scripts.

Chrome must be started with --remote-debugging-port and a dedicated --user-data-dir
(Chrome 136+ refuses remote debugging on the default profile). `python -m tracker chrome`
does this for you. You log in by hand in that window; no credentials or cookies are stored.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import websockets

log = logging.getLogger("tracker.session")

INTERESTING_TYPES = {"XHR", "Fetch", "Document"}
MAX_BODY_BYTES = 2_000_000


@dataclass
class Exchange:
    at: datetime
    url: str
    method: str
    post_data: str | None
    resource_type: str
    status: int | None
    mime: str
    body: str | None
    tab_id: str


ExchangeHandler = Callable[[Exchange], Awaitable[None]]


class PageListener:
    """One websocket per tab; Network domain only."""

    def __init__(self, ws_url: str, tab_id: str, is_interesting: Callable[[str, str], bool],
                 on_exchange: ExchangeHandler) -> None:
        self.ws_url = ws_url
        self.tab_id = tab_id
        self.is_interesting = is_interesting
        self.on_exchange = on_exchange
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reqs: dict[str, dict[str, Any]] = {}
        self._ws: Any = None
        self._bg: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        # proxy=None: this is a loopback connection to your own Chrome; never route it via a proxy.
        async with websockets.connect(self.ws_url, max_size=32 * 1024 * 1024, open_timeout=10,
                                      ping_interval=20, proxy=None) as ws:
            self._ws = ws
            reader = asyncio.create_task(self._reader())
            try:
                await self._call("Network.enable", {"maxPostDataSize": 65536})
                log.info("Listening on tab %s", self.tab_id[:8])
                await reader
            finally:
                reader.cancel()
                for t in list(self._bg):
                    t.cancel()
                for f in self._pending.values():
                    if not f.done():
                        f.cancel()

    async def _call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> Any:
        mid = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    async def _reader(self) -> None:
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                fut = self._pending.get(msg["id"])
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(str(msg["error"].get("message", msg["error"]))))
                    else:
                        fut.set_result(msg.get("result", {}))
                continue
            self._on_event(msg.get("method", ""), msg.get("params", {}))

    def _on_event(self, method: str, p: dict[str, Any]) -> None:
        if method == "Network.requestWillBeSent":
            req = p.get("request", {})
            rtype = p.get("type", "Other")
            if not self.is_interesting(req.get("url", ""), rtype):
                self._reqs.pop(p.get("requestId", ""), None)
                return
            self._reqs[p["requestId"]] = {
                "url": req.get("url", ""), "method": req.get("method", "GET"),
                "post_data": req.get("postData"), "has_post": bool(req.get("hasPostData")),
                "type": rtype, "status": None, "mime": "",
            }
        elif method == "Network.responseReceived":
            r = self._reqs.get(p.get("requestId", ""))
            if r is not None:
                resp = p.get("response", {})
                r.update(status=resp.get("status"), mime=resp.get("mimeType", ""), url=resp.get("url", r["url"]))
        elif method == "Network.loadingFinished":
            r = self._reqs.pop(p.get("requestId", ""), None)
            if r is not None:
                t = asyncio.create_task(self._finish(p["requestId"], r, int(p.get("encodedDataLength") or 0)))
                self._bg.add(t)
                t.add_done_callback(self._bg.discard)
        elif method == "Network.loadingFailed":
            self._reqs.pop(p.get("requestId", ""), None)

    async def _finish(self, rid: str, r: dict[str, Any], size: int) -> None:
        body: str | None = None
        try:
            if size <= MAX_BODY_BYTES:
                res = await self._call("Network.getResponseBody", {"requestId": rid})
                body = res.get("body", "")
                if res.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8", errors="replace")
            if r["post_data"] is None and r["has_post"]:
                res = await self._call("Network.getRequestPostData", {"requestId": rid})
                r["post_data"] = res.get("postData")
        except Exception as e:  # noqa: BLE001 - body may be evicted; that's fine
            log.debug("No body for %s: %s", r["type"], e)
        ex = Exchange(at=datetime.now(timezone.utc), url=r["url"], method=r["method"], post_data=r["post_data"],
                      resource_type=r["type"], status=r["status"], mime=r["mime"], body=body, tab_id=self.tab_id)
        try:
            await self.on_exchange(ex)
        except Exception:  # noqa: BLE001
            log.exception("Exchange handler failed")


class CdpWatcher:
    """Finds tabs in the tracker's dedicated Chrome profile and attaches a PageListener to each."""

    def __init__(self, cdp_url: str, site_host: str, login_pattern: str, on_exchange: ExchangeHandler,
                 on_state: Callable[[bool, int], Awaitable[None]], scan_every: float = 1.5) -> None:
        self.cdp_url = cdp_url.rstrip("/")
        self.site_host = site_host.lower()
        self.login_rx = re.compile(login_pattern, re.I)
        self.on_exchange = on_exchange
        self.on_state = on_state
        self.scan_every = scan_every
        self.connected = False
        self.tabs: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()

    def _on_site(self, url: str) -> bool:
        return self.site_host in (urlsplit(url).hostname or "").lower()

    def is_interesting(self, url: str, rtype: str) -> bool:
        if rtype not in INTERESTING_TYPES:
            return False
        if self._on_site(url):
            return True
        return rtype == "Document" and bool(self.login_rx.search(url))

    def _list_targets(self) -> list[dict[str, Any]]:
        # Local loopback call to Chrome only; bypass any HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{self.cdp_url}/json/list", timeout=3) as resp:
            return json.loads(resp.read().decode())

    async def run(self) -> None:
        while not self._stopping.is_set():
            was = (self.connected, len(self.tabs))
            try:
                targets = await asyncio.to_thread(self._list_targets)
                self.connected = True
                for t in targets:
                    tid, url, ws = t.get("id"), t.get("url", ""), t.get("webSocketDebuggerUrl")
                    if t.get("type") != "page" or not ws or tid in self.tabs:
                        continue
                    # The profile is dedicated to the tracker, so attach to every tab (including blank
                    # new tabs) to avoid missing the first request after you navigate. Only visa-site
                    # requests are ever processed (see is_interesting).
                    if url.startswith(("devtools://", "chrome-extension://")):
                        continue
                    task = asyncio.create_task(self._run_tab(tid, ws))
                    self.tabs[tid] = task
            except Exception as e:  # noqa: BLE001
                if self.connected:
                    log.warning("Chrome DevTools endpoint not reachable (%s)", type(e).__name__)
                self.connected = False
            for tid in [k for k, v in self.tabs.items() if v.done()]:
                self.tabs.pop(tid, None)
            if (self.connected, len(self.tabs)) != was:
                await self.on_state(self.connected, len(self.tabs))
            try:
                await asyncio.wait_for(self._stopping.wait(), self.scan_every)
            except asyncio.TimeoutError:
                pass

    async def _run_tab(self, tid: str, ws: str) -> None:
        try:
            await PageListener(ws, tid, self.is_interesting, self.on_exchange).run()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("Tab %s detached (%s)", tid[:8], type(e).__name__)

    async def stop(self) -> None:
        self._stopping.set()
        for t in self.tabs.values():
            t.cancel()
        await asyncio.gather(*self.tabs.values(), return_exceptions=True)
        self.tabs.clear()
