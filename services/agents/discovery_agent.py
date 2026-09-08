"""Discovery agent: finds which documents exist, without a URL template.

A URL template would be simpler and would break. AAI's April 2026 release
ships `April2k26Annex4.pdf` next to `April2k26Anex5.pdf`, and January's
files carry a `_0` suffix. Templates encode an assumption about naming;
this agent instead *looks*, then reflects when it finds nothing and
relaxes its pattern before giving up.

Loop: fetch index -> extract links -> filter by pattern -> if empty,
broaden the pattern and retry -> register what survived.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from services.agents.base import Agent, Decision
from services.agents.policy import default_policy
from services.common.logging import get_logger
from services.common.models import SourceDocument, ToolCall
from services.ingestion.fetcher import fetch
from services.ingestion.registry import Source

log = get_logger(__name__)

_DOC_LINK = re.compile(r'href=["\']([^"\']+\.(?:pdf|xlsx|xls|csv))["\']', re.I)

# Progressively looser patterns. The agent falls down this ladder only
# when the tighter rung returns nothing.
_FALLBACK_LADDER = [r"an+ex\s*-?\s*4", r"an+ex", r"freight|cargo", r".+"]


class DiscoveryAgent(Agent):
    name = "discovery"

    def __init__(self, source: Source) -> None:
        super().__init__(policy=default_policy(self._plan))
        self.source = source
        self._register_tools()

    # ----------------------------------------------------------- tools --

    def _register_tools(self) -> None:
        @self.tool("fetch_index", "Download the source's index page.", url="page URL")
        def fetch_index(url: str) -> str:
            res = fetch(url)
            if not res.ok:
                raise RuntimeError(f"HTTP {res.status}")
            self.context["index_html"] = res.payload.decode("utf-8", "ignore")
            self.context["index_url"] = url
            return f"{res.size} bytes, {res.media_type}"

        @self.tool("extract_links", "Pull every document link out of the fetched page.")
        def extract_links() -> int:
            html = self.context.get("index_html", "")
            links = {
                urljoin(self.context.get("index_url", ""), m)
                for m in _DOC_LINK.findall(html)
            }
            self.context["all_links"] = sorted(links)
            return len(links)

        @self.tool(
            "filter_links",
            "Keep links matching a regex. Returns how many matched.",
            pattern="regular expression",
        )
        def filter_links(pattern: str) -> int:
            rx = re.compile(pattern, re.I)
            matched = [u for u in self.context.get("all_links", []) if rx.search(u)]
            self.context["matched"] = matched
            self.context["last_pattern"] = pattern
            return len(matched)

        @self.tool(
            "probe_archive",
            "Try constructed URLs for months the index no longer links.",
            months="how many months back to probe",
        )
        def probe_archive(months: int = 24) -> int:
            """Recover the history the index has dropped.

            The publisher lists only recent files but keeps older ones
            served, so the archive is reachable by constructing its URLs
            and checking which actually return a document. Content type
            decides that, not the status code - this server answers 200
            with an HTML error page for a file that does not exist.
            """
            template = self.source.archive_template
            if not template:
                return 0
            from datetime import date

            known = {u for u in self.context.get("all_links", [])}
            candidates: list[str] = []
            today = date.today()
            for back in range(1, int(months) + 1):
                total = today.year * 12 + (today.month - 1) - back
                y, m = divmod(total, 12)
                url = template.format(
                    mon=_MON_ABBR[m + 1], month=_MON_NAME[m + 1],
                    yy=f"{y % 100:02d}", yyyy=str(y),
                )
                if url not in known:
                    candidates.append(url)

            found: list[str] = []
            for url in candidates:
                res = fetch(url)
                # A PDF is a PDF because its bytes say so.
                if res.ok and res.media_type == "application/pdf":
                    found.append(url)
            self.context["archive_links"] = found
            self.context["matched"] = sorted(
                set(self.context.get("matched", [])) | set(found)
            )
            log.info(f"archive probe: {len(found)}/{len(candidates)} months recovered")
            return len(found)

        @self.tool("register_documents", "Turn matched links into SourceDocuments.")
        def register_documents() -> int:
            docs = [
                SourceDocument(
                    publisher=self.source.publisher,
                    source_url=url,
                    hints={
                        "source_key": self.source.key,
                        "matched_pattern": self.context.get("last_pattern"),
                        "period_hint": _guess_period(url),
                    },
                )
                for url in self.context.get("matched", [])
            ]
            self.context["documents"] = docs
            return len(docs)

    # ---------------------------------------------------------- policy --

    def _plan(self, history: list[ToolCall], context: dict[str, Any]) -> Decision:
        done = [c.tool for c in history if c.ok]

        if "fetch_index" not in done:
            return Decision("fetch_index", {"url": self.source.index_url or ""},
                            "need the index page")
        if "extract_links" not in done:
            return Decision("extract_links", {}, "enumerate candidate documents")

        # Reflection: did the current pattern actually find anything?
        attempts = [c for c in history if c.tool == "filter_links"]
        if context.get("matched"):
            # The index lists only recent files. If the source declares an
            # archive pattern, the months it has dropped are worth probing
            # before settling for what is linked.
            if self.source.archive_template and "probe_archive" not in done:
                return Decision(
                    "probe_archive",
                    {"months": self.source.archive_months_back or 24},
                    f"index yielded {len(context['matched'])} document(s); "
                    "probing the archive for months it no longer links",
                )
            if "register_documents" not in done:
                return Decision("register_documents", {}, "matches found; register them")
            return Decision(None, {}, "documents registered")

        rung = len(attempts)
        ladder = (self.source.link_patterns or []) + _FALLBACK_LADDER
        if rung < len(ladder):
            reason = ("first attempt" if rung == 0
                      else f"pattern {rung} found nothing; broadening")
            return Decision("filter_links", {"pattern": ladder[rung]}, reason)

        return Decision(None, {}, "exhausted every pattern; nothing matched")

    # ------------------------------------------------------------------ #

    def is_goal_met(self, context: dict[str, Any]) -> bool:
        return bool(context.get("documents"))

    def summarise(self, context: dict[str, Any]) -> str:
        docs = context.get("documents") or []
        return (
            f"{len(docs)} document(s) discovered for {self.source.key} "
            f"via pattern {context.get('last_pattern')!r}"
        )


_PERIOD_RX = re.compile(
    r"(Jan|Feb|Mar|Apr|April|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    r"\s*2?k?(\d{2,4})", re.I)

_MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

_MON_ABBR = {v: k.capitalize() for k, v in _MON.items()}
_MON_NAME = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November",
    12: "December",
}


def _guess_period(url: str) -> str | None:
    """Read 'April2k26Annex4.pdf' as 2026-04. A hint only; the parser
    reads the authoritative period from inside the document."""
    m = _PERIOD_RX.search(url.rsplit("/", 1)[-1])
    if not m:
        return None
    mon = _MON.get(m.group(1)[:3].lower())
    yr = m.group(2)
    year = int(yr) if len(yr) == 4 else 2000 + int(yr)
    return f"{year}-{mon:02d}" if mon else None
