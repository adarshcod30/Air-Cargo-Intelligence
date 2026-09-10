"""The README must agree with what the code measures.

This project has shipped false documentation twice. Five capabilities were
described that did not exist (Prophet, XGBoost, Optuna, MLflow, an
Isolation Forest ensemble), and the acceptance table has drifted from the
measured figures more than once, most recently claiming 198 documents where
the measurement said 261.

Prose can say anything. A test cannot, so the claims that are numbers are
checked here against the file the evaluator writes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
ACCEPTANCE = ROOT / "data" / "processed" / "acceptance.json"

# Named in the README at some point and never present in the code. Each
# was removed once; this stops any of them coming back.
NEVER_IMPLEMENTED = ["prophet", "xgboost", "optuna", "mlflow",
                     "isolation forest", "scikit-learn", "sklearn"]


def _readme() -> str:
    return README.read_text()


class TestNoUnbackedClaims:
    def test_the_readme_names_no_library_the_code_lacks(self):
        text = _readme().lower()
        code = " ".join(
            p.read_text().lower()
            for p in list((ROOT / "services").rglob("*.py")) + [ROOT / "pyproject.toml"]
        )
        for name in NEVER_IMPLEMENTED:
            if name in text:
                assert name in code, (
                    f"README names {name!r} and no code or dependency provides it")

    def test_the_srs_names_no_library_the_code_lacks(self):
        srs = ROOT / "docs" / "SRS.md"
        if not srs.exists():
            pytest.skip("no SRS")
        text = srs.read_text().lower()
        code = " ".join(p.read_text().lower() for p in (ROOT / "services").rglob("*.py"))
        for name in NEVER_IMPLEMENTED:
            if name in text:
                assert name in code, f"SRS names {name!r} and no code provides it"


class TestHouseStyle:
    def test_no_em_or_en_dashes_anywhere_tracked(self):
        """A standing instruction for this repo, so it is enforced not trusted."""
        offenders = []
        for pattern in ("*.md", "*.py", "*.js", "*.html", "*.css", "*.yml", "*.sql", "*.toml"):
            for path in ROOT.rglob(pattern):
                if any(part in {".venv", "node_modules", "__pycache__", ".git"}
                       for part in path.parts):
                    continue
                try:
                    body = path.read_text()
                except (UnicodeDecodeError, OSError):
                    continue
                # Written as escapes on purpose: a check that forbids a
                # character cannot contain it, or it fails on itself.
                if "\u2013" in body or "\u2014" in body:
                    offenders.append(str(path.relative_to(ROOT)))
        assert not offenders, f"en dash (2013) or em dash (2014) in: {sorted(set(offenders))}"


class TestAcceptanceTableMatchesMeasurement:
    """The table is generated evidence, not a summary someone maintains.

    These two skip in CI and are meant to. The measured figures live in
    `data/processed/acceptance.json`, which is derived data and not version
    controlled, and CI seeds a 151-fact warehouse whose numbers would not
    match the real one anyway. So this pair guards the local loop, where
    the figures are actually produced, and the static checks above are what
    run everywhere.
    """

    @pytest.fixture
    def measured(self):
        if not ACCEPTANCE.exists():
            pytest.skip("needs a measured warehouse: "
                        "python -m services.evaluation.acceptance")
        return json.loads(ACCEPTANCE.read_text())

    def test_every_measured_criterion_appears(self, measured):
        text = _readme()
        for row in measured:
            key = row["metric"].split("<")[0].strip()
            assert key in text, f"criterion {key!r} is measured but not documented"

    def test_every_figure_matches(self, measured):
        """The numbers, not the wording.

        The README bolds and annotates its cells, so this compares the
        digits the evaluator produced against the digits in the row.
        """
        lines = [ln for ln in _readme().split("\n") if ln.startswith("| ")]
        drift = []
        for row in measured:
            key = row["metric"].split("<")[0].strip()
            line = next((ln for ln in lines if key in ln), None)
            if line is None or line.count("|") < 5:
                continue
            digits = re.findall(r"[\d.]+", str(row["measured"]))
            cell = "".join(re.findall(r"[\d.,]+", line.split("|")[4])).replace(",", "")
            for d in digits:
                if d.replace(",", "") not in cell:
                    drift.append(f"{key}: measured {row['measured']!r}, "
                                 f"README shows {line.split('|')[4].strip()!r}")
                    break
        assert not drift, "acceptance table has drifted:\n  " + "\n  ".join(drift)
