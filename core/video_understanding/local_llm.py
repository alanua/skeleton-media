from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from core.video_understanding.models import ProcessingMode, VideoUnderstandingError


_REQUIRED_SECTIONS = (
    "ABOUT",
    "STRUCTURE",
    "METHOD",
    "ENTITIES",
    "CLAIMS",
    "VISUAL_EVIDENCE",
    "TIMESTAMPS",
    "ACTIONS",
    "CONFLICTS",
    "CONFIDENCE",
)


@dataclass(frozen=True)
class LocalLlmConfig:
    endpoint: str
    model: str
    provider: str = "ollama"
    timeout_seconds: float = 120.0
    max_input_chars: int = 120_000
    max_output_chars: int = 32_000
    supports_vision: bool = False
    keep_alive: str = "5m"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise VideoUnderstandingError(
                "LOCAL_LLM_ENDPOINT_REQUIRED",
                "local LLM endpoint must be loopback HTTP",
            )
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise VideoUnderstandingError("INVALID_LLM_ENDPOINT", "local LLM endpoint is unsafe")
        if parsed.query:
            raise VideoUnderstandingError("INVALID_LLM_ENDPOINT", "local LLM endpoint cannot contain query")
        if self.provider not in {"ollama", "openai_compatible"}:
            raise VideoUnderstandingError("INVALID_LLM_PROVIDER", "local LLM provider is unsupported")
        if not self.model.strip() or len(self.model) > 256:
            raise VideoUnderstandingError("INVALID_LLM_MODEL", "local LLM model is invalid")
        if not 1 <= self.timeout_seconds <= 900:
            raise VideoUnderstandingError("INVALID_LLM_TIMEOUT", "local LLM timeout is invalid")
        if not 1_000 <= self.max_input_chars <= 1_000_000:
            raise VideoUnderstandingError("INVALID_LLM_LIMIT", "local LLM input limit is invalid")
        if not 1_000 <= self.max_output_chars <= 200_000:
            raise VideoUnderstandingError("INVALID_LLM_LIMIT", "local LLM output limit is invalid")
        if not self.keep_alive or len(self.keep_alive) > 32:
            raise VideoUnderstandingError("INVALID_KEEP_ALIVE", "keep_alive is invalid")


@dataclass(frozen=True)
class UnderstandingInput:
    mode: ProcessingMode
    transcript_segments: tuple[Mapping[str, Any], ...]
    visual_evidence: tuple[Mapping[str, Any], ...]
    ocr_evidence: tuple[Mapping[str, Any], ...]
    question: str | None = None
    domain_hint: str | None = None


class LocalLlmClient:
    """Bounded loopback LLM client for synthesis; never a canonical authority."""

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
        if self.config.provider == "ollama":
            request, parser = self._ollama_request(prompt)
        else:
            request, parser = self._openai_compatible_request(prompt)
        try:
            response = self._opener(request, timeout=self.config.timeout_seconds)
            raw = response.read(self.config.max_output_chars + 1)
        except Exception as exc:
            raise VideoUnderstandingError("LOCAL_LLM_UNAVAILABLE", "local LLM request failed") from exc
        if len(raw) > self.config.max_output_chars:
            raise VideoUnderstandingError("LOCAL_LLM_OUTPUT_TOO_LARGE", "local LLM output exceeded limit")
        result = parser(raw)
        _validate_understanding_result(result)
        return result

    def _ollama_request(self, prompt: str) -> tuple[Request, Callable[[bytes], dict[str, Any]]]:
        endpoint = urljoin(self.config.endpoint.rstrip("/") + "/", "api/chat")
        body = {
            "model": self.config.model,
            "stream": False,
            "format": "json",
            "keep_alive": self.config.keep_alive,
            "options": {"temperature": 0},
            "messages": _messages(prompt),
        }
        return _json_request(endpoint, body), _parse_ollama

    def _openai_compatible_request(
        self, prompt: str
    ) -> tuple[Request, Callable[[bytes], dict[str, Any]]]:
        endpoint = urljoin(self.config.endpoint.rstrip("/") + "/", "v1/chat/completions")
        body = {
            "model": self.config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": _messages(prompt),
        }
        return _json_request(endpoint, body), _parse_openai_compatible


def _messages(prompt: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Return strict JSON with ABOUT, STRUCTURE, METHOD, ENTITIES, CLAIMS, "
                "VISUAL_EVIDENCE, TIMESTAMPS, ACTIONS, CONFLICTS, CONFIDENCE. "
                "Never claim visual confirmation without linked visual evidence. "
                "Mark every inference explicitly and do not invent missing evidence."
            ),
        },
        {"role": "user", "content": prompt},
    ]


def _json_request(endpoint: str, body: Mapping[str, Any]) -> Request:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return Request(
        endpoint,
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def _parse_ollama(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
        content = payload["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise VideoUnderstandingError("LOCAL_LLM_INVALID_RESPONSE", "Ollama response is invalid") from exc
    if not isinstance(result, dict):
        raise VideoUnderstandingError("LOCAL_LLM_INVALID_RESPONSE", "Ollama result must be an object")
    return result


def _parse_openai_compatible(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
    except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise VideoUnderstandingError(
            "LOCAL_LLM_INVALID_RESPONSE", "OpenAI-compatible local response is invalid"
        ) from exc
    if not isinstance(result, dict):
        raise VideoUnderstandingError("LOCAL_LLM_INVALID_RESPONSE", "local LLM result must be an object")
    return result


def _validate_understanding_result(result: Mapping[str, Any]) -> None:
    missing = [section for section in _REQUIRED_SECTIONS if section not in result]
    if missing:
        raise VideoUnderstandingError(
            "LOCAL_LLM_INCOMPLETE_RESULT",
            "local LLM omitted required understanding sections",
        )


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
