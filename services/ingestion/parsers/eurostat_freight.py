"""Parser for Eurostat `avia_gooa` (freight and mail by main airport).

Eurostat returns JSON-stat: dimensions are described separately from the
data, and `value` is a flat map from a single integer index to a number.
Decoding means converting that integer back into one coordinate per
dimension using the `size` array - row-major, last dimension fastest.

Dimensions for this dataset: freq, unit, tra_meas, schedule, tra_cov,
rep_airp, time. We keep unit=T (tonnes) and the freight measures, and map
`tra_cov` (NAT / INTL / TOTAL) onto our Direction.
"""

from __future__ import annotations

import json

from services.common.logging import get_logger
from services.common.models import (
    CargoFact,
    Direction,
    ExtractionResult,
    Publisher,
    SourceDocument,
)
from services.ingestion.normalise import to_kilograms

log = get_logger(__name__)

_COVERAGE = {
    "NAT": Direction.DOMESTIC,
    "INTL": Direction.INTERNATIONAL,
    "TOTAL": Direction.TOTAL,
}

# Freight loaded and unloaded is the closest analogue to the AAI measure.
_PREFERRED_MEASURES = {"FRM_LD_NLD", "FRM_BRD", "FRM_LD"}


class EurostatFreightParser:
    name = "eurostat_avia_gooa"

    def can_handle(self, doc: SourceDocument, payload: bytes) -> float:
        if payload[:1] not in (b"{", b"["):
            return 0.0
        try:
            data = json.loads(payload.decode("utf-8", "ignore"))
        except Exception:
            return 0.0
        dims = set((data.get("dimension") or {}).keys())
        return 0.95 if {"rep_airp", "tra_meas"} <= dims else 0.1

    def parse(self, doc: SourceDocument, payload: bytes) -> ExtractionResult:
        result = ExtractionResult(parser=self.name)
        try:
            data = json.loads(payload.decode("utf-8", "ignore"))
        except Exception as exc:
            result.warnings.append(f"invalid JSON: {exc}")
            return result

        if "value" not in data:
            result.warnings.append(f"no value block: {str(data)[:160]}")
            return result

        dim_ids: list[str] = data.get("id") or list(data["dimension"].keys())
        sizes: list[int] = data.get("size") or [
            len(data["dimension"][d]["category"]["index"]) for d in dim_ids
        ]
        # position -> code, per dimension
        decoders = []
        for d in dim_ids:
            index = data["dimension"][d]["category"]["index"]
            decoders.append({v: k for k, v in index.items()})
        labels = {
            d: data["dimension"][d]["category"].get("label", {}) for d in dim_ids
        }

        # Row-major strides: last dimension varies fastest.
        strides = [1] * len(sizes)
        for i in range(len(sizes) - 2, -1, -1):
            strides[i] = strides[i + 1] * sizes[i + 1]

        for flat_idx, value in data["value"].items():
            if value is None:
                continue
            rem = int(flat_idx)
            coords = {}
            for dim, stride, dec in zip(dim_ids, strides, decoders, strict=True):
                coords[dim] = dec.get(rem // stride)
                rem %= stride

            # A JSON-stat payload is a dense cube: most cells are dimension
            # combinations we never asked for (other units, other measures).
            # Counting those as "rows seen" made yield look catastrophic and
            # sank an otherwise clean extraction below the confidence floor.
            # Only cells that survive the dimension filter are candidates.
            if coords.get("unit") != "T":
                continue
            if coords.get("tra_meas") not in _PREFERRED_MEASURES:
                continue
            direction = _COVERAGE.get(coords.get("tra_cov") or "")
            if direction is None:
                continue
            result.rows_seen += 1

            airport_code = coords.get("rep_airp") or ""
            airport_label = labels.get("rep_airp", {}).get(airport_code, airport_code)
            period = coords.get("time") or "unknown"
            # Eurostat annual periods are a bare year.
            if len(period) == 4:
                period = f"{period}-A"

            icao = airport_code.split("_", 1)[1] if "_" in airport_code else None
            country = airport_code.split("_", 1)[0] if "_" in airport_code else None

            result.facts.append(
                CargoFact(
                    airport_name_raw=airport_label,
                    period=period,
                    direction=direction,
                    tonnage_kg=to_kilograms(float(value), "tonnes"),
                    source_document_id=doc.doc_id,
                    publisher=Publisher.EUROSTAT,
                    airport_icao=icao,
                    airport_name=airport_label,
                    country=country,
                    resolution_confidence=1.0,
                    resolution_method="eurostat-code",
                )
            )
            result.rows_kept += 1

        from services.ingestion.parsers.base import score_extraction

        result.confidence = score_extraction(result)
        log.info(f"{self.name}: {result.summary()}")
        return result
