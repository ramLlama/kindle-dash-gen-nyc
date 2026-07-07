"""Tests for the Kindle image post-processing (fit + grayscale + quantize)."""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from kindle_dash_gen.render.postprocess import post_process


def _gradient_png(width: int, height: int, vmax: int = 128) -> bytes:
    """An RGB horizontal gray gradient (0..vmax, never pure white) as PNG bytes."""
    row = bytes(c for x in range(width) for c in (round(vmax * x / (width - 1)),) * 3)
    img = Image.frombytes("RGB", (width, height), row * height)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _open(png: bytes) -> Image.Image:
    return Image.open(BytesIO(png))


def test_resize_produces_exact_dimensions_and_grayscale() -> None:
    out = _open(
        post_process(
            _gradient_png(64, 48),
            width=32,
            height=48,
            gray_levels=16,
            method="resize",
            rotate=False,
        )
    )
    assert out.size == (32, 48)
    assert out.mode == "L"


def test_crop_covers_without_introducing_white_bars() -> None:
    # Landscape source into a portrait target: crop scales to cover and trims the excess,
    # so no white padding is introduced (the gradient tops out well below 255).
    out = _open(
        post_process(
            _gradient_png(64, 48), width=32, height=48, gray_levels=16, method="crop", rotate=False
        )
    )
    assert out.size == (32, 48)
    assert out.getextrema()[1] < 255


def test_pad_adds_white_bars() -> None:
    # Same landscape-into-portrait fit, but pad preserves the whole image and fills the
    # leftover strip with white e-ink background.
    out = _open(
        post_process(
            _gradient_png(64, 48), width=32, height=48, gray_levels=16, method="pad", rotate=False
        )
    )
    assert out.size == (32, 48)
    assert out.getpixel((0, 0)) == 255  # top bar
    assert out.getextrema()[1] == 255


def test_gray_levels_snaps_to_evenly_spaced_palette() -> None:
    # A full 0..255 gradient must quantize to exactly the evenly-spaced palette spanning both
    # endpoints — locks the LUT's values, not just how many there are.
    out = _open(
        post_process(
            _gradient_png(256, 4, vmax=255),
            width=256,
            height=4,
            gray_levels=16,
            method="resize",
            rotate=False,
        )
    )
    values = {value for _, value in out.getcolors(maxcolors=999_999)}
    assert values == {round(i * 255 / 15) for i in range(16)}  # {0, 17, 34, ..., 255}


def test_gray_levels_below_two_raises() -> None:
    with pytest.raises(ValueError):
        post_process(
            _gradient_png(64, 48), width=32, height=48, gray_levels=1, method="resize", rotate=False
        )


def test_rotate_swaps_dimensions_90_degrees() -> None:
    # A rotated portrait target comes out landscape: the (width, height) panel is turned on its
    # side for a physically rotated device.
    out = _open(
        post_process(
            _gradient_png(64, 48), width=32, height=48, gray_levels=16, method="resize", rotate=True
        )
    )
    assert out.size == (48, 32)
    assert out.mode == "L"
