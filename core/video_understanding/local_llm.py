from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from core.video_understanding.models import ProcessingMode, VideoUnderstandingError


@dataclass(frozen=True)
class LocalLlmConfig:
    endpoint: str
    model: str
    timeout_seconds: float = 120.0
    max_input_chars: int = 120_000
    max_output_chars: int = 32_000
    supports_vision: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise VideoUnderstandingError(
                "LOCAL_LLM_ENDPOINT_REQUIRED",
                "local LLM endpoint must be loopback HTTP",
            )
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise VideoUnderstandingError("INVALID_LLM_ENDPOINT", "local LLM endpoint is unsafe")
        if not self.model.strip() or len(self.model) > 256:
            raise VideoUnderstandingError("INVALID_LLM_MODEL", "local LLM model is invalid")
        if not 1 <= self.timeout_seconds <= 900:
            raise VideoUnderstandingError("INVALID_LLM_TIMEOUT", "local LLM timeout is invalid")
        if not 1_000 <= self.max_input_chars <= 1_000_000:
            raise VideoUnderstandingError("INVALID_LLM_LIMIT", "local LLM input limit is invalid")
        if not 1_000 <= self.max_output_chars <= 200_000:
            raise VideoUnderstandingError("INVALID_LLM_LIMIT", "local LLM output limit is invalid")


@dataclass(frozen=True)
class UnderstandingInput:
    mode: ProcessingMode
    transcript_segments: tuple[Mapping[str, Any], ...]
    visual_evidence: tuple[Mapping[str, Any], ...]
    ocr_evidence: tuple[Mapping[str, Any], ...]
    question: str | None = None
    domain_hint: str | None = None


class LocalLlmClient:
    """Bounded OpenAI-compatible loopback client for understanding, never authority."""

    def __init__(
        self,
        config: LocalLlmConfig,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.config = config
        self._opener = opener

    def understand(self, input_packet: UnderstandingInput) -> dict[str, Any]:
        prompt = build_understanding_prompt(input_packet, self.config.max_input_chars)
        body = {
            "model": self.config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return strict JSON with ABOUT, STRUCTURE, METHOD, ENTITIES, CLAIMS, "
                        "VISUAL_EVIDENCE, TIMESTAMPS, ACTIONS, CONFLICTS, CONFIDENCE. "
                        "Never claim visual confirmation without linked visual evidence. "
                        "Mark inference explicitly."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.config.endpoint,
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = self._opener(request, timeout=self.config.timeout_seconds)
            raw = response.read(self.config.max_output_chars + 1)
        except Exception as exc:
            raise VideoUnderstandingError("LOCAL_LLM_UNAVAILABLE", "local LLM request failed") from exc
        if len(raw) > self.config.max_output_chars:
            raise VideoUnderstandingError("LOCAL_LLM_OUTPUT_TOO_LARGE", "local LLM output exceeded limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            result = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise VideoUnderstandingError("LOCAL_LLM_INVALID_RESPONSE", "local LLM response is invalid") from exc
        if not isinstance(result, dict):
            raise VideoUnderstandingError("LOCAL_LLM_INVALID_RESPONSE", "local LLM result must be an object")
        return result


def build_understanding_prompt(input_packet: UnderstandingInput, max_chars: int) -> str:
    packet = {
        "mode": ProcessingMode(input_packet.mode).value,
        "question": input_packet.question,
        "domain_hint": input_packet.domain_hint,
        "transcript_segments": list(input_packet.transcript_segments),
        "visual_evidence": list(input_packet.visual_evidence),
        "ocr_evidence": list(input_packet.ocr_evidence),
    }
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True)
    if len(encoded) > max_chars:
        raise VideoUnderstandingError("LLM_INPUT_TOO_LARGE", "understanding input exceeded limit")
    return encoded


def local_llm_policy(mode: ProcessingMode | str) -> str:
    normalized = ProcessingMode(mode)
    if normalized in {ProcessingMode.STANDARD, ProcessingMode.DEEP, ProcessingMode.TARGETED}:
        return "REQUIRED_FOR_SYNTHESIS"
    if normalized is ProcessingMode.QUICK:
        return "OPTIONAL_WITH_DETERMINISTIC_FALLBACK"
    return "NOT_REQUIRED"
