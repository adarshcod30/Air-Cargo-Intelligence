"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _path(env: str, default: str) -> Path:
    p = Path(os.getenv(env, REPO_ROOT / default))
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass(frozen=True)
class Settings:
    raw_dir: Path
    interim_dir: Path
    processed_dir: Path
    seeds_dir: Path

    user_agent: str
    request_timeout: float
    max_retries: int
    polite_delay_s: float

    llm_base_url: str | None
    llm_api_key: str | None
    llm_model: str | None

    data_gov_in_api_key: str | None
    agent_max_steps: int
    extraction_confidence_floor: float

    @property
    def llm_available(self) -> bool:
        """Whether an LLM policy can be used.

        When this is False the agents still run - they fall back to the
        deterministic policy. The agent architecture does not depend on a
        model being reachable, which is also what makes it testable in CI.
        """
        return bool(self.llm_base_url and self.llm_model)


def load_settings() -> Settings:
    return Settings(
        raw_dir=_path("ACI_RAW_DIR", "data/raw"),
        interim_dir=_path("ACI_INTERIM_DIR", "data/interim"),
        processed_dir=_path("ACI_PROCESSED_DIR", "data/processed"),
        seeds_dir=_path("ACI_SEEDS_DIR", "db/seeds"),
        user_agent=os.getenv(
            "ACI_USER_AGENT",
            "AirCargoIntelligence/0.1 (research project; contact via repository issues)",
        ),
        request_timeout=float(os.getenv("ACI_REQUEST_TIMEOUT", "60")),
        max_retries=int(os.getenv("ACI_MAX_RETRIES", "3")),
        polite_delay_s=float(os.getenv("ACI_POLITE_DELAY", "0.5")),
        llm_base_url=os.getenv("LLM_BASE_URL") or None,
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL") or None,
        data_gov_in_api_key=os.getenv("DATA_GOV_IN_API_KEY") or None,
        agent_max_steps=int(os.getenv("ACI_AGENT_MAX_STEPS", "12")),
        extraction_confidence_floor=float(os.getenv("ACI_CONFIDENCE_FLOOR", "0.60")),
    )


SETTINGS = load_settings()
