"""Bedrock access for the reasoning layer.

Two capabilities, one client:

  converse()  chat completion, used by the LLM policy to choose the next
              tool and by the narrative agent to write prose.
  embed()     Titan text embeddings, used to index source documents so a
              citation can point at a paragraph rather than a whole PDF.

Why the Converse API rather than each model's native payload: Converse
normalises the request and response shape across Nova, Llama and Mistral,
so swapping the model is a config change instead of a code change. That
matters here because the policy A/B harness runs the same goal through
several models and compares step counts.

The grounding invariant still holds. Nothing in this module is allowed to
produce a measurement; callers constrain the model to choosing a tool name
or narrating figures it was handed. See services/semantic/nl.py for the
verification that enforces it.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from services.common.config import SETTINGS
from services.common.logging import get_logger

log = get_logger(__name__)

# Models that must be addressed through a cross-region inference profile
# rather than the bare model id. Bedrock rejects the bare id with a
# ValidationException naming the profile, so we prefix and retry once.
_PROFILE_PREFIX = "us."

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


@dataclass
class Usage:
    """Token accounting, surfaced in the agent console as budget burn."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, i: int, o: int) -> None:
        self.input_tokens += i
        self.output_tokens += o
        self.calls += 1

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "calls": self.calls,
        }


@dataclass
class BedrockUnavailable(Exception):
    reason: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.reason


class BedrockClient:
    """Thin, synchronous wrapper. Thread-safe lazy client construction."""

    def __init__(self, model_id: str | None = None, region: str | None = None) -> None:
        self.model_id = model_id or SETTINGS.bedrock_model_id
        self.embed_model_id = SETTINGS.bedrock_embed_model_id
        self.region = region or SETTINGS.aws_region
        self._client: Any = None
        self._lock = threading.Lock()
        self.usage = Usage()
        self._last_error: str | None = None

    # -- plumbing ---------------------------------------------------------

    def _runtime(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        import boto3
                        from botocore.config import Config
                    except ImportError as exc:
                        raise BedrockUnavailable(f"boto3 not installed: {exc}") from exc
                    self._client = boto3.client(
                        "bedrock-runtime",
                        region_name=self.region,
                        config=Config(
                            retries={"max_attempts": 3, "mode": "adaptive"},
                            read_timeout=SETTINGS.request_timeout,
                        ),
                    )
        return self._client

    @property
    def available(self) -> bool:
        """Whether a call could plausibly succeed.

        Deliberately checks credentials rather than only configuration.
        The previous design treated 'a base URL is set' as 'a model is
        reachable', so every call failed into a silent fallback while the
        trace still recorded the model as the decision-maker.
        """
        if not SETTINGS.bedrock_enabled:
            return False
        try:
            import boto3
        except ImportError:
            return False
        try:
            creds = boto3.Session().get_credentials()
            return creds is not None and creds.access_key is not None
        except Exception:
            return False

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # -- chat -------------------------------------------------------------

    def converse(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        model_id: str | None = None,
    ) -> str:
        """Single-turn completion. Raises BedrockUnavailable on failure."""
        mid = model_id or self.model_id
        body: dict[str, Any] = {
            "modelId": mid,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system:
            body["system"] = [{"text": system}]

        try:
            resp = self._runtime().converse(**body)
        except Exception as exc:
            msg = str(exc)
            # Nova and Llama in us-east-1 are served through a cross-region
            # inference profile; the bare model id is rejected with a
            # message naming the profile. Retry once with the prefix rather
            # than making every caller know which models need it.
            if "inference profile" in msg.lower() and not mid.startswith(_PROFILE_PREFIX):
                log.info(f"retrying {mid} via inference profile {_PROFILE_PREFIX}{mid}")
                body["modelId"] = _PROFILE_PREFIX + mid
                try:
                    resp = self._runtime().converse(**body)
                except Exception as exc2:
                    self._last_error = str(exc2)
                    raise BedrockUnavailable(str(exc2)) from exc2
            else:
                self._last_error = msg
                raise BedrockUnavailable(msg) from exc

        u = resp.get("usage") or {}
        self.usage.add(int(u.get("inputTokens", 0)), int(u.get("outputTokens", 0)))
        self._last_error = None
        return resp["output"]["message"]["content"][0]["text"]

    def converse_json(self, prompt: str, system: str | None = None, **kw: Any) -> dict:
        """Completion parsed as JSON.

        Not every non-Anthropic model on Bedrock honours a structured
        output flag, so we ask for JSON and extract the object rather than
        depending on a response_format parameter that some models ignore.
        """
        raw = self.converse(prompt, system=system, **kw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = _JSON_BLOCK.search(raw)
            if not m:
                raise BedrockUnavailable(f"model did not return JSON: {raw[:200]}") from None
            return json.loads(m.group(0))

    # -- embeddings -------------------------------------------------------

    def embed(self, text: str, dimensions: int = 1024) -> list[float]:
        """Embed one passage with Titan v2.

        Normalised vectors, so cosine similarity reduces to a dot product
        and pgvector's inner-product operator can be used directly.
        """
        payload = {"inputText": text, "dimensions": dimensions, "normalize": True}
        try:
            resp = self._runtime().invoke_model(
                modelId=self.embed_model_id, body=json.dumps(payload)
            )
            return json.loads(resp["body"].read())["embedding"]
        except Exception as exc:
            self._last_error = str(exc)
            raise BedrockUnavailable(str(exc)) from exc

    def embed_many(
        self, texts: list[str], dimensions: int = 1024, workers: int = 8
    ) -> list[list[float]]:
        """Embed a batch, concurrently.

        Titan exposes no batch endpoint, so a corpus of this size is 1,410
        separate round trips. Done sequentially that is dominated entirely
        by latency rather than by any work either side is doing. A small
        pool cuts the rebuild from minutes to under one, and stays well
        inside the per-account request rate.

        Order is preserved: results are placed by index, not appended, so a
        chunk cannot be stored against another chunk's vector.
        """
        if not texts:
            return []
        if len(texts) == 1 or workers <= 1:
            return [self.embed(t, dimensions=dimensions) for t in texts]

        from concurrent.futures import ThreadPoolExecutor

        out: list[list[float] | None] = [None] * len(texts)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self.embed, t, dimensions): i for i, t in enumerate(texts)
            }
            for fut, i in futures.items():
                out[i] = fut.result()
        return [v for v in out if v is not None]


_default: BedrockClient | None = None
_default_lock = threading.Lock()


def get_client() -> BedrockClient:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = BedrockClient()
    return _default
