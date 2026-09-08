"""HTTP retrieval that does not trust the server.

Built from an observed failure, not a hypothetical one: AAI serves
`April2k26Annex5.pdf` as an HTML error page with **HTTP 200**, while the
real file lives at `April2k26Anex5.pdf` (missing an "n"). A fetcher that
believes status codes and file extensions ingests that error page as
cargo data and never notices.

So content type is decided by sniffing magic bytes, and a mismatch with
the URL's extension is surfaced as a warning the extraction agent can act
on rather than being silently accepted.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx

from services.common.config import SETTINGS
from services.common.logging import get_logger, redact

log = get_logger(__name__)

# Magic-byte signatures, checked in order.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),      # xlsx/docx are zip containers
    (b"\xd0\xcf\x11\xe0", "application/vnd.ms-excel"),  # legacy .xls
    (b"{", "application/json"),
    (b"[", "application/json"),
]

_HTML_MARKERS = (b"<html", b"<!doctype html", b"<head", b"<body")


def sniff_media_type(payload: bytes, declared: str | None = None) -> str:
    """Identify content from its bytes. The declared type is a hint only."""
    head = payload[:2048]
    for sig, media in _SIGNATURES:
        if head.startswith(sig):
            # A JSON guess from a bare brace is weak; confirm it parses.
            if media == "application/json":
                try:
                    import json

                    json.loads(payload.decode("utf-8", "ignore"))
                except Exception:
                    continue
            return media
    lowered = head.lower()
    if any(m in lowered for m in _HTML_MARKERS):
        return "text/html"
    if declared:
        return declared.split(";")[0].strip()
    return "application/octet-stream"


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int
    payload: bytes
    media_type: str
    declared_type: str | None
    sha256: str
    warnings: list[str]

    @property
    def size(self) -> int:
        return len(self.payload)

    def __repr__(self) -> str:
        return (
            f"FetchResult(status={self.status}, media={self.media_type}, "
            f"bytes={self.size}, warnings={len(self.warnings)})"
        )


_EXT_EXPECTATION = {
    ".pdf": "application/pdf",
    ".xlsx": "application/zip",
    ".xls": "application/vnd.ms-excel",
    ".json": "application/json",
}


def fetch(url: str, *, client: httpx.Client | None = None) -> FetchResult:
    """Retrieve a URL with retries, then verify what actually came back."""
    owned = client is None
    client = client or httpx.Client(
        timeout=SETTINGS.request_timeout,
        follow_redirects=True,
        headers={"User-Agent": SETTINGS.user_agent},
    )
    warnings: list[str] = []
    last_exc: Exception | None = None
    resp = None

    try:
        for attempt in range(1, SETTINGS.max_retries + 1):
            try:
                resp = client.get(url)
                if resp.status_code < 400:
                    break
                warnings.append(f"attempt {attempt}: HTTP {resp.status_code}")
            except Exception as exc:
                last_exc = exc
                warnings.append(f"attempt {attempt}: {type(exc).__name__}")
            time.sleep(SETTINGS.polite_delay_s * attempt)

        if resp is None:
            return FetchResult(
                url, False, 0, b"", "none", None, "",
                warnings + [f"exhausted retries: {last_exc}"],
            )

        payload = resp.content
        declared = resp.headers.get("content-type")
        media = sniff_media_type(payload, declared)
        digest = hashlib.sha256(payload).hexdigest()

        # The check that catches the AAI trap.
        for ext, expected in _EXT_EXPECTATION.items():
            if url.lower().split("?")[0].endswith(ext) and media != expected:
                warnings.append(
                    f"content mismatch: URL ends {ext} (expects {expected}) "
                    f"but bytes are {media}"
                )
        if media == "text/html" and not url.lower().rstrip("/").endswith((".html", "/")):
            warnings.append("served HTML for a non-HTML URL - likely an error page")

        ok = resp.status_code < 400 and bool(payload)
        result = FetchResult(url, ok, resp.status_code, payload, media, declared, digest, warnings)
        log.debug(f"fetched {redact(url)} -> {result!r}")
        time.sleep(SETTINGS.polite_delay_s)
        return result
    finally:
        if owned:
            client.close()
