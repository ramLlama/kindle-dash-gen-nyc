"""Client for the OpenRouter Unified Image API.

Discovers per-model request parameters at runtime (via the model's live
``/images/models/{id}/endpoints`` capability listing) instead of hardcoding them, since
different image models support different aspect ratios, resolutions, etc.
"""

from __future__ import annotations

import base64
from functools import cached_property

import niquests

API = "https://openrouter.ai/api/v1"


class OpenRouterError(RuntimeError):
    """Raised when an OpenRouter request fails or returns something unexpected."""


class OpenRouterClient:
    """Client for OpenRouter's Unified Image API, scoped to one model."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        session: niquests.Session | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._session = session or niquests.Session()
        # Capability lookup and aspect resolution need no auth; only generate() does.
        if api_key is not None:
            self._session.headers["Authorization"] = f"Bearer {api_key}"

    @cached_property
    def supported_parameters(self) -> dict:
        """The model's supported image-generation parameters, merged across endpoints.

        A parameter is present if any endpoint lists it; for enum parameters, the ``values``
        are unioned across endpoints (order preserved, deduped).
        """
        url = f"{API}/images/models/{self._model}/endpoints"
        try:
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            endpoints = payload["endpoints"]
        except niquests.exceptions.RequestException as exc:
            raise OpenRouterError(f"failed to fetch model capabilities: {url}") from exc
        except (KeyError, TypeError) as exc:
            raise OpenRouterError("unexpected OpenRouter endpoints response") from exc
        return _merge_supported_parameters(endpoints)

    def resolve_aspect_ratio(self, width: int, height: int, override: str | None = None) -> str:
        """The aspect ratio to request: ``override`` if the model supports it, else nearest."""
        supported = self._enum_values("aspect_ratio")
        if override is not None:
            if override not in supported:
                raise OpenRouterError(
                    f"aspect_ratio {override!r} not supported by {self._model}; "
                    f"valid values: {supported}"
                )
            return override
        return nearest_aspect_ratio(width, height, supported)

    def generate(self, prompt: str, *, aspect_ratio: str, resolution: str | None = None) -> bytes:
        """Generate an image from ``prompt`` and return the raw (e.g. PNG) bytes."""
        if self._api_key is None:
            raise OpenRouterError("generate() requires an api_key")
        body = {"model": self._model, "prompt": prompt, "aspect_ratio": aspect_ratio}
        if resolution is not None:
            # _enum_values fails fast if the model has no resolution parameter at all, rather
            # than silently dropping a value the user explicitly asked for.
            valid = self._enum_values("resolution")
            if resolution not in valid:
                raise OpenRouterError(
                    f"resolution {resolution!r} not supported by {self._model}; "
                    f"valid values: {valid}"
                )
            body["resolution"] = resolution
        try:
            resp = self._session.post(f"{API}/images", json=body, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
            data = payload["data"]
            if len(data) == 0:
                raise OpenRouterError("OpenRouter returned no image data")
            b64 = data[0]["b64_json"]
        except niquests.exceptions.RequestException as exc:
            raise OpenRouterError("OpenRouter image generation request failed") from exc
        except (KeyError, TypeError, IndexError) as exc:
            raise OpenRouterError("unexpected OpenRouter images response") from exc
        try:
            return base64.b64decode(b64, validate=True)
        except ValueError as exc:  # binascii.Error (bad base64/padding) subclasses ValueError
            raise OpenRouterError("OpenRouter returned invalid base64 image data") from exc

    def _enum_values(self, param: str) -> list[str]:
        spec = self.supported_parameters.get(param)
        if spec is None or spec.get("type") != "enum":
            raise OpenRouterError(f"{self._model} does not support parameter {param!r}")
        return spec["values"]


def nearest_aspect_ratio(width: int, height: int, supported: list[str]) -> str:
    """The entry in ``supported`` (each ``"w:h"``) whose ratio is closest to ``width/height``."""
    target = width / height

    def _ratio(entry: str) -> float:
        w, h = entry.split(":")
        return float(w) / float(h)

    return min(supported, key=lambda entry: abs(_ratio(entry) - target))


def _merge_supported_parameters(endpoints: list[dict]) -> dict:
    """Merge ``supported_parameters`` across endpoints: union enum values across endpoints."""
    merged: dict = {}
    for endpoint in endpoints:
        for name, spec in endpoint.get("supported_parameters", {}).items():
            if name not in merged:
                # Copy so unioning below never mutates the source JSON payload's values list.
                merged[name] = dict(spec)
                if "values" in spec:
                    merged[name]["values"] = list(spec["values"])
                continue
            if spec.get("type") == "enum" and merged[name].get("type") == "enum":
                existing = merged[name]["values"]
                for value in spec.get("values", []):
                    if value not in existing:
                        existing.append(value)
    return merged
