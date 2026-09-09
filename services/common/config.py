"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_env_file() -> None:
    """Read .env into the environment on import, if one exists.

    Without this every entry point needs the caller to export the file
    first, which is a step that works on the machine where it was written
    and nowhere else. Real environment variables always win: a hosted
    deployment sets them directly and has no .env, and a stale local file
    must never override what the platform supplies.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


_load_env_file()


def _flag(env: str, default: bool) -> bool:
    raw = os.getenv(env)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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

    # Managed-model access. Region-scoped credentials come from the standard
    # AWS environment variables, so boto3 resolves them the same way the CLI
    # does and no key is ever passed through application config.
    aws_region: str
    bedrock_enabled: bool
    bedrock_model_id: str
    bedrock_fast_model_id: str
    bedrock_embed_model_id: str
    rag_enabled: bool
    rag_top_k: int

    database_url_readonly: str | None
    data_gov_in_api_key: str | None
    agent_max_steps: int
    extraction_confidence_floor: float

    @property
    def llm_available(self) -> bool:
        """Whether an LLM policy can be used.

        This used to return True whenever a base URL and model name were
        merely *set*, which is why traces recorded `policy: llm` for runs in
        which every model call had failed into the deterministic fallback.
        Configuration is not reachability. The managed path now verifies
        that credentials actually resolve; the self-hosted path is still
        best-effort, and the policy relabels a fallback either way.

        When this is False the agents still run on the deterministic
        policy, which is what keeps the pipeline testable in CI with no
        credentials at all.
        """
        if self.bedrock_enabled:
            try:
                import boto3

                creds = boto3.Session().get_credentials()
                if creds is not None and creds.access_key:
                    return True
            except Exception:
                pass
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
        aws_region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1",
        bedrock_enabled=_flag("BEDROCK_ENABLED", True),
        # Nova Lite is the default because the policy loop makes one short
        # structured call per step; a larger model costs more without
        # choosing better tools. The A/B harness measures whether that
        # holds rather than assuming it.
        bedrock_model_id=os.getenv("BEDROCK_MODEL_ID") or "us.amazon.nova-lite-v1:0",
        bedrock_fast_model_id=os.getenv("BEDROCK_FAST_MODEL_ID") or "us.amazon.nova-micro-v1:0",
        bedrock_embed_model_id=os.getenv("BEDROCK_EMBED_MODEL_ID")
        or "amazon.titan-embed-text-v2:0",
        rag_enabled=_flag("RAG_ENABLED", True),
        rag_top_k=int(os.getenv("RAG_TOP_K", "5")),
        # The serving path connects as a role that can read the
        # allowlisted views and nothing else. Falls back to the owner
        # connection so a fresh checkout still runs, but the fallback is
        # logged as a warning rather than passing silently.
        database_url_readonly=os.getenv("DATABASE_URL_READONLY") or None,
        data_gov_in_api_key=os.getenv("DATA_GOV_IN_API_KEY") or None,
        agent_max_steps=int(os.getenv("ACI_AGENT_MAX_STEPS", "12")),
        extraction_confidence_floor=float(os.getenv("ACI_CONFIDENCE_FLOOR", "0.60")),
    )


SETTINGS = load_settings()
