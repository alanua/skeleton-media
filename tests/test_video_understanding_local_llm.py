from __future__ import annotations

import json

import pytest

from core.video_understanding.local_llm import (
    LocalLlmClient,
    LocalLlmConfig,
    UnderstandingInput,
    local_llm_policy,
)
from core.video_understanding.models import ProcessingMode, VideoUnderstandingError


SECTIONS = {
    "ABOUT": {},
    "STRUCTURE": [],
    "METHOD": [],
    "ENTITIES": [],
    "CLAIMS": [],
    "VISUAL_EVIDENCE": [],
    "TIMESTAMPS": [],
    "ACTIONS": [],
    "CONFLICTS": [],
    "CONFIDENCE": {},
}


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_ollama_backend_uses_loopback_api_chat_and_json_mode() -> None:
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response({"message": {"role": "assistant", "content": json.dumps(SECTIONS)}})

    client = LocalLlmClient(
        LocalLlmConfig(endpoint="http://127.0.0.1:11434", model="synthetic-model"),
        opener=opener,
    )
    result = client.understand(
        UnderstandingInput(
            mode=ProcessingMode.STANDARD,
            transcript_segments=({"start": 0, "end": 1, "text": "synthetic"},),
            visual_evidence=(),
            ocr_evidence=(),
        )
    )
    assert result == SECTIONS
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["body"]["stream"] is False
    assert captured["body"]["format"] == "json"
    assert captured["body"]["options"]["temperature"] == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://ollama.com/api/chat",
        "http://example.org:11434",
        "http://10.0.0.5:11434",
        "file:///tmp/socket",
    ],
)
def test_ollama_backend_rejects_non_loopback_endpoints(endpoint: str) -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        LocalLlmConfig(endpoint=endpoint, model="synthetic")
    assert exc.value.reason_code == "LOCAL_LLM_ENDPOINT_REQUIRED"


def test_incomplete_ollama_result_fails_closed() -> None:
    def opener(_request, timeout):
        del timeout
        return _Response({"message": {"content": json.dumps({"ABOUT": {}})}})

    client = LocalLlmClient(
        LocalLlmConfig(endpoint="http://localhost:11434", model="synthetic"),
        opener=opener,
    )
    with pytest.raises(VideoUnderstandingError) as exc:
        client.understand(
            UnderstandingInput(ProcessingMode.DEEP, (), (), ())
        )
    assert exc.value.reason_code == "LOCAL_LLM_INCOMPLETE_RESULT"


def test_local_llm_policy_by_processing_mode() -> None:
    assert local_llm_policy("QUICK") == "OPTIONAL_WITH_DETERMINISTIC_FALLBACK"
    assert local_llm_policy("STANDARD") == "REQUIRED_FOR_SYNTHESIS"
    assert local_llm_policy("DEEP") == "REQUIRED_FOR_SYNTHESIS"
    assert local_llm_policy("TARGETED") == "REQUIRED_FOR_SYNTHESIS"
    assert local_llm_policy("ARCHIVE") == "NOT_REQUIRED"
