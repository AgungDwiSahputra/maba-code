"""web_fetch: ambil halaman web publik -> Markdown. Jawabannya dibuat oleh sub-agen GPT-6 Luna (lihat session.py).

Perilaku mengikuti WebFetch Claude Code: http di-upgrade ke https, redirect ke host lain dikembalikan (tidak
diikuti), cache 15 menit, isi dipotong 100k karakter. Pagar tambahan MABA: host lokal/privat ditolak setelah
resolusi DNS (mencegah agen dipakai mengakses jaringan internal), kredensial di URL ditolak.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from maba_code import __version__

MAX_BYTES = 5 * 1024 * 1024
MAX_MARKDOWN_CHARS = 100_000
TIMEOUT_SECONDS = 20.0
CACHE_TTL_SECONDS = 15 * 60
CACHE_SIZE = 50
MAX_REDIRECTS = 5
TEXT_TYPES = (
    "text/", "application/json", "application/ld+json", "application/xhtml+xml", "application/xml",
    "application/javascript", "application/x-yaml", "application/yaml",
)
_STRIP_BLOCKS = re.compile(r"<(script|style|noscript|svg|template|iframe)\b[^>]*>.*?</\1\s*>", re.I | re.S)

Resolver = Callable[[str], Awaitable[list[str]]]


class FetchError(LookupError):
    """Kesalahan yang dilaporkan ke model sebagai hasil tool berstatus error."""


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    text: str
    truncated: bool = False
    redirect: str | None = None


async def default_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def normalize_url(raw: str) -> httpx.URL:
    try:
        url = httpx.URL(raw.strip())
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise FetchError(f"Invalid URL: {raw!r} ({exc})") from None
    if url.scheme == "http":
        url = url.copy_with(scheme="https")
    if url.scheme != "https":
        raise FetchError("Only http(s) URLs are supported.")
    if url.userinfo:
        raise FetchError("URLs containing credentials are not allowed.")
    host = (url.host or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or ("." not in host and ":" not in host):
        raise FetchError(f"Refusing to fetch local host '{host}'.")
    return url


def check_address(ip: str) -> None:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        raise FetchError(f"Unexpected address {ip!r}.") from None
    if not addr.is_global:
        raise FetchError(f"Refusing to fetch private/local network address {ip}.")


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section", "article", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag in {"h1", "h2", "h3"}:
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_markdown(html: str) -> str:
    html = _STRIP_BLOCKS.sub("", html)
    try:
        from markdownify import markdownify

        text = markdownify(html, heading_style="ATX")
    except Exception:  # noqa: BLE001 - markdownify opsional; cadangan stdlib
        parser = _TextExtractor()
        parser.feed(html)
        text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class WebFetcher:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None):
        self.transport = transport  # hanya untuk pengujian (httpx.MockTransport)
        self.resolver = resolver or default_resolver
        self.cache: dict[str, tuple[float, Page]] = {}

    async def _check_host(self, url: httpx.URL) -> None:
        host = url.host
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            try:
                addresses = await self.resolver(host)
            except OSError as exc:
                raise FetchError(f"Cannot resolve host {host}: {exc}") from None
        if not addresses:
            raise FetchError(f"Cannot resolve host {host}.")
        for address in addresses:
            check_address(address)

    async def fetch(self, raw_url: str) -> Page:
        url = normalize_url(raw_url)
        key = str(url)
        hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        try:
            page = await self._fetch(url)
        except httpx.HTTPError as exc:
            raise FetchError(f"Request to {url} failed: {type(exc).__name__}: {exc}") from None
        if len(self.cache) >= CACHE_SIZE:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = (time.monotonic(), page)
        return page

    async def _fetch(self, url: httpx.URL) -> Page:
        headers = {
            "User-Agent": f"MABA-Code/{__version__} (web_fetch)",
            "Accept": "text/markdown, text/html;q=0.9, text/plain;q=0.8, application/json;q=0.7, */*;q=0.1",
        }
        async with httpx.AsyncClient(
            transport=self.transport, follow_redirects=False, timeout=TIMEOUT_SECONDS, headers=headers
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                await self._check_host(url)
                async with client.stream("GET", url) as resp:
                    if resp.is_redirect:
                        target = url.join(resp.headers.get("location", ""))
                        if target.scheme == "http":
                            target = target.copy_with(scheme="https")
                        if target.host != url.host:
                            return Page(str(url), resp.status_code, "", "", redirect=str(target))
                        url = target
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(f"HTTP {resp.status_code} for {url}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith(TEXT_TYPES):
                        raise FetchError(f"Unsupported content type '{ctype}' (only text, HTML and JSON pages).")
                    body = bytearray()
                    truncated = False
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            del body[MAX_BYTES:]
                            truncated = True
                            break
                    try:
                        text = bytes(body).decode(resp.charset_encoding or "utf-8", errors="replace")
                    except LookupError:
                        text = bytes(body).decode("utf-8", errors="replace")
                    if "html" in ctype or (not ctype and "<html" in text[:2000].lower()):
                        text = html_to_markdown(text)
                    if len(text) > MAX_MARKDOWN_CHARS:
                        text = text[:MAX_MARKDOWN_CHARS]
                        truncated = True
                    return Page(str(url), resp.status_code, ctype or "text/plain", text, truncated)
        raise FetchError(f"Too many redirects (more than {MAX_REDIRECTS}).")
