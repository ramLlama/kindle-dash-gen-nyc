"""Massage a generated dashboard image into a Kindle-ready frame.

The Kindle Voyage displays exactly ``width``×``height`` grayscale pixels drawn from a fixed set
of hardware gray levels (16 on the Voyage). A freshly generated image is close to the target
aspect but not exact and carries full color depth, so it is fitted to the panel and quantized
down to the device's gray palette.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps

from ..config import PostProcessMethod


def post_process(
    png: bytes,
    *,
    width: int,
    height: int,
    gray_levels: int,
    method: PostProcessMethod,
    rotate: bool,
) -> bytes:
    """Fit, grayscale, and quantize ``png`` into a Kindle-ready PNG.

    Steps, in order: grayscale → fit to ``(width, height)`` via ``method`` → quantize to
    ``gray_levels`` evenly-spaced grays → optionally rotate 90° (for a physically rotated
    device). Returns PNG bytes.
    """
    img = Image.open(BytesIO(png)).convert("L")
    img = _fit(img, width, height, method)
    img = img.point(_quantize_lut(gray_levels))
    if rotate:
        img = img.transpose(Image.Transpose.ROTATE_90)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _fit(img: Image.Image, width: int, height: int, method: PostProcessMethod) -> Image.Image:
    """Resize ``img`` to exactly ``(width, height)`` according to ``method``."""
    size = (width, height)
    if method == "resize":
        return img.resize(size, Image.Resampling.LANCZOS)  # stretch to fill, ignoring aspect
    if method == "crop":
        return ImageOps.fit(
            img, size, Image.Resampling.LANCZOS, centering=(0.5, 0.5)
        )  # cover + center-crop
    if method == "pad":
        # Fit within the frame; fill the leftover strip with white to match the e-ink background.
        return ImageOps.pad(img, size, Image.Resampling.LANCZOS, color=255, centering=(0.5, 0.5))
    raise ValueError(f"unknown post-process method: {method!r}")


def _quantize_lut(levels: int) -> list[int]:
    """A 256-entry lookup table mapping each gray value to the nearest of ``levels`` evenly-spaced
    grays spanning 0–255 (models the device's fixed hardware gray levels)."""
    if levels < 2:
        raise ValueError(f"gray_levels must be >= 2, got {levels}")
    step = 255 / (levels - 1)
    return [round(round(v / step) * step) for v in range(256)]
