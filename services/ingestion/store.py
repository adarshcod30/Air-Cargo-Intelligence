"""Content-addressed raw store plus an append-only provenance ledger.

Raw artefacts are archived under their own content hash *before* anything
parses them. That is what lets a parser change be replayed over the whole
history without re-hitting a government endpoint - which matters when the
endpoint is slow, rate-limited, or liable to remove old files.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.common.models import DocStatus, SourceDocument

log = get_logger(__name__)

_EXT = {
    "application/pdf": ".pdf",
    "application/zip": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/json": ".json",
    "text/html": ".html",
    "text/csv": ".csv",
    "text/plain": ".txt",
}


class RawStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or SETTINGS.raw_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.root / "_ledger.jsonl"

    def archive(self, doc: SourceDocument, payload: bytes, media_type: str, sha256: str) -> Path:
        """Write bytes under their content hash and update the document."""
        folder = self.root / doc.publisher.value.lower()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sha256[:16]}{_EXT.get(media_type, '.bin')}"
        if not path.exists():
            path.write_bytes(payload)
        doc.sha256 = sha256
        doc.media_type = media_type
        doc.byte_size = len(payload)
        doc.raw_path = str(path.relative_to(self.root.parent.parent))
        doc.status = DocStatus.FETCHED
        return path

    def already_have(self, sha256: str) -> bool:
        """Content-hash dedup: the same bytes never get ingested twice."""
        return any(sha256[:16] in p.name for p in self.root.rglob("*") if p.is_file())

    def record(self, doc: SourceDocument) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc.to_dict(), default=str) + "\n")

    def ledger(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        with self.ledger_path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
