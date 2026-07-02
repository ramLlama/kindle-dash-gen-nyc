"""Tests for the OpenRouter image-generation client."""

from __future__ import annotations

import base64
import json

import niquests_mock as nm
import pytest

from kindle_dash_gen_nyc.render.openrouter import (
    OpenRouterClient,
    OpenRouterError,
    nearest_aspect_ratio,
)

MODEL = "google/gemini-3.1-flash-lite-image"
ENDPOINTS_URL = f"https://openrouter.ai/api/v1/images/models/{MODEL}/endpoints"
IMAGES_URL = "https://openrouter.ai/api/v1/images"

# The live capability shape for google/gemini-3.1-flash-lite-image (decision doc in the plan).
ASPECT_RATIOS = [
    "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1",
    "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9",
]  # fmt: skip

_SUPPORTED_PARAMETERS = {
    "resolution": {"type": "enum", "values": ["1K"]},
    "aspect_ratio": {"type": "enum", "values": ASPECT_RATIOS},
    "n": {"type": "range", "min": 1, "max": 1},
    "input_references": {"type": "range", "min": 0, "max": 14},
}

ENDPOINTS_RESPONSE = {
    "id": MODEL,
    "endpoints": [
        {"provider_name": "google-vertex", "supported_parameters": _SUPPORTED_PARAMETERS},
        {"provider_name": "google-ai-studio", "supported_parameters": _SUPPORTED_PARAMETERS},
    ],
}

KNOWN_BYTES = b"PNGDATA"
IMAGES_RESPONSE = {"data": [{"b64_json": base64.b64encode(KNOWN_BYTES).decode()}]}


def _client(api_key: str | None = None) -> OpenRouterClient:
    return OpenRouterClient(MODEL, api_key=api_key)


def test_generate_returns_decoded_bytes_and_posts_expected_body() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(ENDPOINTS_URL).respond(json=ENDPOINTS_RESPONSE)
        images_route = router.post(IMAGES_URL).respond(json=IMAGES_RESPONSE)
        png = _client("sk-or-test").generate("draw a dashboard", aspect_ratio="4:3")

    assert png == KNOWN_BYTES
    request = images_route.calls[-1].request
    body = json.loads(request.body)
    assert body["model"] == MODEL
    assert body["prompt"] == "draw a dashboard"
    assert body["aspect_ratio"] == "4:3"
    assert "output_format" not in body  # not in this model's supported_parameters
    assert request.headers["Authorization"] == "Bearer sk-or-test"


def test_generate_includes_resolution_when_supported() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(ENDPOINTS_URL).respond(json=ENDPOINTS_RESPONSE)
        images_route = router.post(IMAGES_URL).respond(json=IMAGES_RESPONSE)
        _client("sk-or-test").generate("prompt", aspect_ratio="4:3", resolution="1K")

    body = json.loads(images_route.calls[-1].request.body)
    assert body["resolution"] == "1K"


def test_generate_resolution_not_in_enum_raises() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(ENDPOINTS_URL).respond(json=ENDPOINTS_RESPONSE)
        with pytest.raises(OpenRouterError):
            _client("sk-or-test").generate("prompt", aspect_ratio="4:3", resolution="4K")


def test_generate_resolution_unsupported_param_raises() -> None:
    # A model whose endpoints expose no "resolution" parameter at all.
    no_resolution = {
        "id": MODEL,
        "endpoints": [
            {
                "provider_name": "google-vertex",
                "supported_parameters": {"aspect_ratio": {"type": "enum", "values": ASPECT_RATIOS}},
            }
        ],
    }
    with nm.mock(assert_all_called=False) as router:
        router.get(ENDPOINTS_URL).respond(json=no_resolution)
        with pytest.raises(OpenRouterError):
            _client("sk-or-test").generate("prompt", aspect_ratio="4:3", resolution="1K")


def test_resolve_aspect_ratio_picks_nearest() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(ENDPOINTS_URL).respond(json=ENDPOINTS_RESPONSE)
        aspect = _client().resolve_aspect_ratio(1448, 1072)
    assert aspect == "4:3"


def test_resolve_aspect_ratio_override_accepted_when_supported() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(ENDPOINTS_URL).respond(json=ENDPOINTS_RESPONSE)
        aspect = _client().resolve_aspect_ratio(1448, 1072, override="16:9")
    assert aspect == "16:9"


def test_resolve_aspect_ratio_override_not_supported_raises() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(ENDPOINTS_URL).respond(json=ENDPOINTS_RESPONSE)
        with pytest.raises(OpenRouterError):
            _client().resolve_aspect_ratio(1448, 1072, override="7:11")


def test_generate_without_api_key_raises() -> None:
    with pytest.raises(OpenRouterError):
        _client(None).generate("prompt", aspect_ratio="4:3")


def test_generate_http_error_raises() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.post(IMAGES_URL).respond(status_code=500)
        with pytest.raises(OpenRouterError):
            _client("sk-or-test").generate("prompt", aspect_ratio="4:3")


def test_generate_missing_data_raises() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.post(IMAGES_URL).respond(json={})
        with pytest.raises(OpenRouterError):
            _client("sk-or-test").generate("prompt", aspect_ratio="4:3")


def test_capability_lookup_http_error_raises() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(ENDPOINTS_URL).respond(status_code=500)
        with pytest.raises(OpenRouterError):
            _client().resolve_aspect_ratio(1448, 1072)


def test_capability_lookup_malformed_response_raises() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(ENDPOINTS_URL).respond(json={"id": MODEL})  # missing "endpoints"
        with pytest.raises(OpenRouterError):
            _client().resolve_aspect_ratio(1448, 1072)


@pytest.mark.parametrize(
    "width,height,expected",
    [
        (1448, 1072, "4:3"),  # Kindle Voyage
        (1920, 1080, "16:9"),  # wide
        (1000, 1000, "1:1"),  # square
    ],
)
def test_nearest_aspect_ratio(width: int, height: int, expected: str) -> None:
    assert nearest_aspect_ratio(width, height, ASPECT_RATIOS) == expected
