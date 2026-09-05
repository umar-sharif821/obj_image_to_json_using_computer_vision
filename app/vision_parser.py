"""
Vision extraction pipeline for the Legal Metrology compliance platform.

The pipeline is deliberately two-staged:

    raw upload bytes
        -> OpenCV pre-processing (deterministic, auditable, CPU-bound)
        -> Vision LLM structured extraction (Claude, JSON-Schema constrained)
        -> ExtractedLabelData | None

**The contract of this module is that it never guesses.** Packaged-commodity
enforcement turns on what is *printed* on the wrapper, so a hallucinated MRP or
an inferred "inclusive of all taxes" phrase would manufacture evidence. Three
mechanisms enforce that:

1. The prompt forbids inference and requires ``null`` for anything not visibly
   printed, with the schema making ``null`` a first-class legal value.
2. The model self-reports legibility (``is_label_legible``,
   ``unreadable_fields``, ``overall_confidence``), and this module gates on it.
3. OpenCV independently measures blur and glare, so an unreadable image is
   detectable without trusting the model's own self-assessment.

Two distinct failure modes are signalled differently, and the distinction is
legally material:

* **Unreadable label** -> returns ``None``. The caller issues an
  ``INDETERMINATE`` report; no adverse finding is recorded.
* **Infrastructure failure** (auth, network, upstream outage) -> raises
  :class:`VisionServiceError`. Returning ``None`` here would silently convert a
  server outage into an evidentiary finding about a package, so it is raised
  and surfaced by the API as a 503 instead.

An *empty but legible* label is neither of these: it returns a fully populated
``ExtractedLabelData`` whose declarations are ``None``, which is precisely the
contravention the rules engine exists to catch.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
from functools import lru_cache
from typing import Final, Optional

import anthropic
import cv2
import numpy as np
from pydantic import ValidationError

from app.schemas import ExtractedLabelData, ImageQualityMetrics

__all__ = [
    "VISION_MODEL_ID",
    "VisionServiceError",
    "UnsupportedImageError",
    "preprocess_for_ocr",
    "compute_image_quality_metrics",
    "extract_declarations_from_image",
]

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #

VISION_MODEL_ID: Final[str] = "claude-opus-5"
MAX_OUTPUT_TOKENS: Final[int] = 16_000
REQUEST_TIMEOUT_SECONDS: Final[float] = 120.0

#: Hard ceiling on an accepted upload, before decoding (20 MB).
MAX_UPLOAD_BYTES: Final[int] = 20 * 1024 * 1024

#: Longest edge fed to the vision model. Above ~1568 px the API downsamples
#: anyway, so sending more costs tokens without adding legible detail.
MAX_EDGE_PIXELS: Final[int] = 1568
#: Below this, small crops are upscaled so fine print survives re-encoding.
MIN_EDGE_PIXELS: Final[int] = 640

#: Specular highlight = very bright, nearly colourless, *and* clearly brighter
#: than the package's own substrate tone. Plastic film and laminated pouches
#: glare white; printed ink does not -- but neither does a plain white wrapper,
#: which is why the excess-over-substrate condition is not optional. Without it,
#: an ordinary white label scores as ~90% glare. See :func:`_glare_mask`.
GLARE_VALUE_THRESHOLD: Final[int] = 243
GLARE_SATURATION_THRESHOLD: Final[int] = 34
#: Margin a highlight must carry above the package's own substrate tone (the
#: median brightness of the frame) before it counts as glare.
GLARE_EXCESS_OVER_SUBSTRATE: Final[int] = 25
#: Beyond this share of glared pixels, inpainting would invent texture over
#: real text, so the frame is left untouched and flagged instead.
MAX_INPAINTABLE_GLARE_RATIO: Final[float] = 0.35

#: Quality floors below which an image is not fit for adjudication. The upper
#: brightness bound is deliberately generous: white wrappers and white paper
#: labels are the norm in retail packaging and legitimately average well above
#: 235. A washed-out frame is caught by the contrast floor instead, which is the
#: sounder signal -- an overexposed image loses tonal separation between ink and
#: substrate, whatever its mean.
MIN_BLUR_VARIANCE: Final[float] = 60.0
MIN_MEAN_BRIGHTNESS: Final[float] = 40.0
MAX_MEAN_BRIGHTNESS: Final[float] = 252.0
MIN_CONTRAST_STD: Final[float] = 12.0

#: JPEG quality for the re-encoded frame; high enough to preserve 6 pt print.
JPEG_QUALITY: Final[int] = 92
OUTPUT_MEDIA_TYPE: Final[str] = "image/jpeg"

#: Extractions at or below this confidence are treated as unreadable.
MIN_ACCEPTABLE_CONFIDENCE: Final[float] = 0.35

#: Transient-failure retry policy (on top of the SDK's own retries).
MAX_ATTEMPTS: Final[int] = 3
RETRY_BASE_DELAY_SECONDS: Final[float] = 1.0


# --------------------------------------------------------------------------- #
# Exceptions                                                                   #
# --------------------------------------------------------------------------- #


class VisionServiceError(RuntimeError):
    """
    The extraction could not be attempted or completed for infrastructure
    reasons (authentication, connectivity, upstream error).

    Distinct from an unreadable label, which returns ``None``.
    """


class UnsupportedImageError(ValueError):
    """The uploaded bytes are empty, oversized, or not a decodable image."""


# --------------------------------------------------------------------------- #
# Prompting                                                                    #
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT: Final[str] = """\
You are a forensic label-reading system operating on behalf of the Legal \
Metrology enforcement wing, Ministry of Consumer Affairs, Government of India. \
You transcribe the mandatory declarations printed on pre-packaged commodities \
so they can be tested against the Legal Metrology (Packaged Commodities) \
Rules, 2011.

Your output is evidence. It may be quoted verbatim in a statutory notice issued \
to a manufacturer. You therefore operate under strict transcription discipline:

1. TRANSCRIBE, NEVER INFER. Record only characters you can actually see in the \
   image. Never complete a partially visible number, never reconstruct an \
   address from brand knowledge, and never supply a value because a package of \
   this kind usually carries one.

2. ABSENT IS NOT THE SAME AS UNREADABLE.
   - A declaration that is genuinely not printed on the package -> set the \
     field to null and do NOT list it in `unreadable_fields`. A missing \
     declaration is a lawful finding and must be reported as missing.
   - A declaration that appears to be present but cannot be read (glare, blur, \
     occlusion, cropping, curvature, low resolution) -> set the field to null, \
     add it to `unreadable_fields`, and note the obstruction in \
     `extraction_notes`.
   Confusing these two is the single most serious error you can make: the first \
   supports enforcement action, the second forbids it.

3. VERBATIM RAW TEXT. Every declaration carries a `raw_text` field. Reproduce \
   the text exactly as printed, preserving casing, punctuation, spacing and \
   abbreviations. Do not correct spelling, do not expand abbreviations, and do \
   not standardise units inside `raw_text`.

4. UNITS ARE REGULATED TEXT. Put the unit exactly as printed in \
   `unit_as_printed` ('gms', 'Gm', 'ML', 'Ltr' are all common). Map it to \
   `si_unit` separately, and use `non_standard` when the printed abbreviation \
   is readable but is not a lawful SI symbol. Never silently rewrite 'gms' to \
   'g'.

5. THE TAX PHRASE IS A SEPARATE DECLARATION. Set \
   `is_inclusive_of_all_taxes` to true only when wording to that effect is \
   actually printed (e.g. 'Inclusive of all taxes', 'Incl. of all taxes', \
   'MRP incl. of all taxes'), and copy that wording into \
   `tax_declaration_text`. The presence of a price alone is never sufficient.

6. DATES. Many packages print only a month and a year ('MFD 03/2024', \
   'PKD: MAR 2024'). In that case set `month` and `year` and leave `day` null. \
   Never invent a day. Resolve month names to numbers, expand two-digit years, \
   and copy the caption into `date_label_as_printed`. Relative shelf life \
   ('Best before 9 months from packaging') goes in `relative_period_text` and \
   `relative_period_months`, leaving the calendar fields null.

7. CONTACT DETAILS. Copy telephone numbers and e-mail addresses exactly, digit \
   for digit and character for character. If a number is partly obscured, omit \
   it entirely and record the field as unreadable; a wrong digit in a notice is \
   worse than no number at all.

8. CALIBRATED CONFIDENCE. `confidence` on each declaration and \
   `overall_confidence` must reflect genuine reading certainty. Glare, blur, \
   curvature of a pouch, and tiny print all lower it. Do not report high \
   confidence to appear decisive.

9. SCOPE CHECK. If the image is not a pre-packaged commodity label at all, set \
   `is_packaged_commodity_label` to false, set `is_label_legible` \
   appropriately, and leave every declaration null.

10. WHOLE-PANEL LEGIBILITY. Set `is_label_legible` to false when the \
   declaration panel as a whole cannot be reliably read, even if a few large \
   words (such as the brand) remain visible.

Fill `raw_text_dump` with every character you can read from the declaration \
panel, in reading order, as an evidentiary transcript.
"""

_USER_INSTRUCTION: Final[str] = (
    "This photograph shows a pre-packaged commodity offered for retail sale in "
    "India. It has been pre-processed to suppress specular glare and normalise "
    "contrast; residual artefacts from that processing must not be read as "
    "printed text.\n\n"
    "Transcribe every mandatory declaration under Rule 6 of the Legal Metrology "
    "(Packaged Commodities) Rules, 2011 into the required structure: name and "
    "complete address of the manufacturer, packer or importer; the common or "
    "generic name of the commodity; the net quantity with its unit exactly as "
    "printed; the month and year of manufacture, pre-packing or import; the "
    "retail sale price together with any 'inclusive of all taxes' wording; and "
    "the consumer care name, address, telephone number and e-mail address.\n\n"
    "Return null for any declaration you cannot see. Do not infer, complete or "
    "reconstruct any value."
)


# --------------------------------------------------------------------------- #
# OpenCV pre-processing                                                        #
# --------------------------------------------------------------------------- #


def _decode(image_bytes: bytes) -> np.ndarray:
    """
    Decode upload bytes into a 3-channel BGR array.

    Raises:
        UnsupportedImageError: if the payload is empty, oversized, or is not a
            format OpenCV can decode.
    """
    if not image_bytes:
        raise UnsupportedImageError("Empty image payload.")
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise UnsupportedImageError(
            f"Image exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit."
        )

    buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise UnsupportedImageError("Payload could not be decoded as an image.")
    return image


def _rescale(image: np.ndarray) -> np.ndarray:
    """
    Fit the frame to the vision model's useful resolution band.

    Downscaling uses INTER_AREA (correct for decimation, avoids the aliasing
    that turns fine print into noise); upscaling small crops uses INTER_CUBIC
    so 6 pt declarations survive JPEG re-encoding.
    """
    height, width = image.shape[:2]
    longest, shortest = max(height, width), min(height, width)

    if longest > MAX_EDGE_PIXELS:
        scale = MAX_EDGE_PIXELS / longest
        interpolation = cv2.INTER_AREA
    elif shortest < MIN_EDGE_PIXELS:
        scale = min(MIN_EDGE_PIXELS / shortest, MAX_EDGE_PIXELS / longest)
        interpolation = cv2.INTER_CUBIC
    else:
        return image

    new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    return cv2.resize(image, new_size, interpolation=interpolation)


def _glare_mask(image: np.ndarray) -> np.ndarray:
    """
    Build a binary mask of specular highlights.

    Three conditions must hold together, and the third is what makes this
    usable on real packaging. Glare is very bright and nearly colourless, but
    so is a plain white wrapper, a white paper label or a milk carton -- on
    brightness and saturation alone an entire white declaration panel scores as
    glare, which would both reject good photographs and hand the caller a
    nonsensical "glare covers 94% of the frame".

    A specular highlight is distinguished by being blown out *relative to the
    package's own tone*. The median brightness of the frame is a robust
    estimate of that substrate tone -- robust because text, highlights and
    shadows are all minorities of the pixel population -- and glare is what
    sits a clear margin above it.

    Referencing the median rather than a local neighbourhood is deliberate.
    Neighbourhood operators (a morphological top-hat, or subtracting a blurred
    copy) have an intrinsic scale: they detect a highlight smaller than their
    kernel and go blind to one larger than it, and glare streaks across a pouch
    are routinely larger than any kernel worth paying for. The median reference
    has no scale, so it behaves the same on a pinpoint highlight and on a band
    across half the panel.

    The consequence worth naming: on a substrate that is *already* near-white,
    the median sits so high that nothing clears the margin and no glare is
    reported. That is the honest answer -- white-on-white glare is not
    separable by tone, and where it actually destroys text the blur and
    contrast metrics catch the frame instead.

    The mask is then opened to discard the thin bright halo that borders dark
    text, and closed and dilated so a highlight is repaired as one region
    rather than as scattered pinholes.
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    saturation, value = hsv[:, :, 1], hsv[:, :, 2]

    substrate_tone = float(np.median(value))
    threshold = max(float(GLARE_VALUE_THRESHOLD), substrate_tone + GLARE_EXCESS_OVER_SUBSTRATE)

    mask = (
        (value >= threshold) & (saturation <= GLARE_SATURATION_THRESHOLD)
    ).astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return cv2.dilate(mask, kernel, iterations=1)


def _reduce_glare(image: np.ndarray) -> np.ndarray:
    """
    Repair specular highlights by inpainting from surrounding label texture.

    Left alone when there is no glare, and equally when there is too much: past
    ``MAX_INPAINTABLE_GLARE_RATIO`` the inpainter has no reliable neighbourhood
    to sample from and would fabricate plausible-looking texture where text
    used to be. That is precisely the failure this system must not commit, so
    a heavily glared frame is passed through unmodified and allowed to fail the
    quality gate honestly.
    """
    mask = _glare_mask(image)
    ratio = float(np.count_nonzero(mask)) / mask.size

    if ratio == 0.0 or ratio > MAX_INPAINTABLE_GLARE_RATIO:
        return image
    return cv2.inpaint(image, mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)


def _normalise_contrast(image: np.ndarray) -> np.ndarray:
    """
    Apply CLAHE to the luminance channel in LAB space.

    Working on L alone lifts local contrast between ink and substrate without
    shifting hue, so a red 'MRP' block stays red. Contrast-limited (rather than
    global) equalisation is what recovers text from the shadowed side of a
    curved pouch while leaving the well-lit side unclipped.
    """
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lightness, a_channel, b_channel = cv2.split(lab)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    lightness = clahe.apply(lightness)

    return cv2.cvtColor(cv2.merge((lightness, a_channel, b_channel)), cv2.COLOR_LAB2BGR)


def _sharpen(image: np.ndarray) -> np.ndarray:
    """
    Edge-preserving denoise followed by an unsharp mask.

    The bilateral filter removes sensor and compression noise while keeping
    glyph edges intact; the unsharp mask then restores the stroke definition
    that rescaling and inpainting soften.
    """
    denoised = cv2.bilateralFilter(image, d=5, sigmaColor=50, sigmaSpace=50)
    blurred = cv2.GaussianBlur(denoised, (0, 0), sigmaX=3.0)
    return cv2.addWeighted(denoised, 1.5, blurred, -0.5, 0)


def _encode(image: np.ndarray) -> bytes:
    """Re-encode the processed frame as JPEG for transport to the vision API."""
    success, buffer = cv2.imencode(
        ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
    )
    if not success:
        raise UnsupportedImageError("Processed frame could not be encoded as JPEG.")
    return buffer.tobytes()


def preprocess_for_ocr(image_bytes: bytes) -> bytes:
    """
    Prepare a packaged-commodity photograph for reliable text extraction.

    The stages run in a deliberate order: rescale first so every subsequent
    kernel operates at a predictable spatial scale; suppress specular glare
    before contrast normalisation, so CLAHE is not driven by blown-out
    highlights; then denoise and sharpen last, once the tonal range is settled.

    Every stage is deterministic and re-runnable, which matters because the
    processed frame is the image the model actually read and may need to be
    reproduced during adjudication.

    Args:
        image_bytes: Raw bytes of the uploaded photograph (JPEG, PNG, WEBP, ...).

    Returns:
        JPEG-encoded bytes of the processed frame.

    Raises:
        UnsupportedImageError: If the payload is empty, oversized or undecodable.
    """
    image = _decode(image_bytes)
    image = _rescale(image)
    image = _reduce_glare(image)
    image = _normalise_contrast(image)
    image = _sharpen(image)
    return _encode(image)


def compute_image_quality_metrics(image_bytes: bytes) -> ImageQualityMetrics:
    """
    Measure the submitted image objectively, independently of the LLM.

    These measurements are what allows an "unreadable" outcome to be defended:
    they are deterministic, re-computable from the archived bytes, and owe
    nothing to the model's own opinion of its reading.

    Args:
        image_bytes: Raw bytes of the uploaded photograph.

    Returns:
        Blur, glare, brightness and contrast measurements plus a usability
        verdict with the reasons behind it.

    Raises:
        UnsupportedImageError: If the payload cannot be decoded.
    """
    image = _rescale(_decode(image_bytes))
    grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    blur_variance = float(cv2.Laplacian(grayscale, cv2.CV_64F).var())
    mask = _glare_mask(image)
    glare_ratio = float(np.count_nonzero(mask)) / mask.size
    mean_brightness = float(grayscale.mean())
    contrast_std = float(grayscale.std())

    notes: list[str] = []
    if blur_variance < MIN_BLUR_VARIANCE:
        notes.append(
            f"Image is out of focus (Laplacian variance {blur_variance:.1f} "
            f"< {MIN_BLUR_VARIANCE:.0f})."
        )
    if glare_ratio > MAX_INPAINTABLE_GLARE_RATIO:
        notes.append(
            f"Specular glare covers {glare_ratio:.0%} of the frame; the "
            "declaration panel cannot be recovered."
        )
    if mean_brightness < MIN_MEAN_BRIGHTNESS:
        notes.append(f"Image is underexposed (mean intensity {mean_brightness:.0f}).")
    elif mean_brightness > MAX_MEAN_BRIGHTNESS:
        notes.append(f"Image is overexposed (mean intensity {mean_brightness:.0f}).")
    if contrast_std < MIN_CONTRAST_STD:
        notes.append(
            f"Image lacks tonal separation between the print and the substrate "
            f"(contrast {contrast_std:.1f} < {MIN_CONTRAST_STD:.0f}); the "
            "declarations cannot be resolved."
        )

    height, width = image.shape[:2]
    return ImageQualityMetrics(
        width=width,
        height=height,
        blur_variance=blur_variance,
        glare_ratio=glare_ratio,
        mean_brightness=mean_brightness,
        contrast_std=contrast_std,
        is_usable=not notes,
        quality_notes=notes,
    )


# --------------------------------------------------------------------------- #
# Vision extraction                                                            #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _client() -> anthropic.AsyncAnthropic:
    """
    Lazily construct the shared async client.

    Cached because the client owns a connection pool that should be reused
    across requests; constructed lazily so importing this module never requires
    credentials (test collection, schema generation, Alembic runs).

    Credentials resolve from the environment: ``ANTHROPIC_API_KEY``,
    ``ANTHROPIC_AUTH_TOKEN``, or an ``ant auth login`` profile.
    """
    return anthropic.AsyncAnthropic(timeout=REQUEST_TIMEOUT_SECONDS)


def _is_reliable(data: ExtractedLabelData) -> tuple[bool, str]:
    """
    Decide whether an extraction may be relied on for adjudication.

    Note what is deliberately *not* a rejection: a legible label whose
    declarations are all null. That is a fully valid reading of a
    non-conforming package and must reach the rules engine intact.

    Returns:
        ``(True, "")`` when the extraction is usable, otherwise
        ``(False, reason)`` with the reason for logging and audit.
    """
    if not data.is_packaged_commodity_label:
        return False, "Image is not a pre-packaged commodity label."
    if not data.is_label_legible:
        return False, "Model reported the declaration panel as illegible."
    if data.overall_confidence <= MIN_ACCEPTABLE_CONFIDENCE:
        return False, (
            f"Extraction confidence {data.overall_confidence:.2f} is at or below "
            f"the {MIN_ACCEPTABLE_CONFIDENCE:.2f} threshold."
        )
    return True, ""


async def _request_extraction(payload_b64: str) -> Optional[ExtractedLabelData]:
    """
    Issue one structured-output request and return the parsed model.

    ``messages.parse`` constrains generation to the ``ExtractedLabelData`` JSON
    Schema and validates the response, so a syntactically valid but
    schema-violating reply cannot reach the rules engine.

    Returns:
        The parsed extraction, or ``None`` if the model declined the request or
        returned nothing parseable.
    """
    response = await _client().messages.parse(
        model=VISION_MODEL_ID,
        max_tokens=MAX_OUTPUT_TOKENS,
        # Adaptive thinking; effort defaults to "high", which is what this
        # task warrants -- reading 6 pt print off a curved foil pouch.
        thinking={"type": "adaptive"},
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": OUTPUT_MEDIA_TYPE,
                            "data": payload_b64,
                        },
                    },
                    {"type": "text", "text": _USER_INSTRUCTION},
                ],
            }
        ],
        output_format=ExtractedLabelData,
    )

    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        logger.warning(
            "Vision extraction declined by safety classifier (category=%s, request_id=%s).",
            category,
            response._request_id,
        )
        return None

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Vision extraction truncated at max_tokens (request_id=%s); "
            "structured output is incomplete.",
            response._request_id,
        )
        return None

    return response.parsed_output


async def extract_declarations_from_image(
    image_bytes: bytes,
    *,
    preprocess: bool = True,
) -> Optional[ExtractedLabelData]:
    """
    Extract the mandatory declarations from a packaged-commodity photograph.

    Args:
        image_bytes: Raw bytes of the uploaded photograph.
        preprocess: Run the OpenCV glare/contrast pipeline first. Disable only
            for images that have already been processed, or when reproducing a
            historical extraction from an archived processed frame.

    Returns:
        A validated :class:`~app.schemas.ExtractedLabelData` when the label was
        read reliably, or ``None`` when it was not -- because the image is
        undecodable, fails the objective quality gate, is not a commodity
        label, or was reported illegible or low-confidence by the model.

        ``None`` never means "compliant" or "non-compliant"; the caller must
        record an ``INDETERMINATE`` verdict. A legible label carrying no
        declarations returns a populated model with null fields, which *is* a
        finding.

    Raises:
        VisionServiceError: The vision service could not be reached or refused
            the credentials. Deliberately raised rather than folded into
            ``None`` so an outage is never recorded as a finding about a
            package.
    """
    # OpenCV is CPU-bound and releases the GIL only inside its kernels; off the
    # event loop it goes, so one large upload cannot stall the API's other
    # in-flight requests.
    try:
        metrics = await asyncio.to_thread(compute_image_quality_metrics, image_bytes)
    except UnsupportedImageError as exc:
        logger.info("Rejected upload: %s", exc)
        return None

    if not metrics.is_usable:
        logger.info(
            "Image failed the objective quality gate; extraction not attempted: %s",
            "; ".join(metrics.quality_notes),
        )
        return None

    try:
        payload = (
            await asyncio.to_thread(preprocess_for_ocr, image_bytes)
            if preprocess
            else image_bytes
        )
    except UnsupportedImageError as exc:
        logger.info("Pre-processing failed: %s", exc)
        return None
    except cv2.error:
        # A pipeline fault must not cost us the inspection: the model can still
        # read an unprocessed frame that already cleared the quality gate.
        logger.exception("OpenCV pipeline failed; falling back to the original frame.")
        payload = image_bytes

    payload_b64 = base64.standard_b64encode(payload).decode("ascii")

    data: Optional[ExtractedLabelData] = None
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            data = await _request_extraction(payload_b64)
            break

        except ValidationError:
            # The model produced output that does not satisfy the schema. Not
            # retryable in any useful way, and unsafe to salvage partially.
            logger.exception("Vision output failed schema validation; discarding.")
            return None

        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise VisionServiceError(
                "Vision service rejected the configured credentials."
            ) from exc

        except TypeError as exc:
            # With no credential resolvable at all, the SDK raises a bare
            # TypeError while building the request headers rather than an
            # AuthenticationError. Left alone it surfaces as an opaque 500, so
            # it is translated here into the same configuration failure the
            # rejected-credential path reports.
            if "authentication" not in str(exc).lower():
                raise
            raise VisionServiceError(
                "No Anthropic credential is configured. Set the ANTHROPIC_API_KEY "
                "environment variable (or run `ant auth login`) and restart the "
                "service."
            ) from exc

        except anthropic.BadRequestError as exc:
            # Malformed request or an image the API cannot accept. Retrying an
            # identical payload cannot help.
            logger.error("Vision request rejected as invalid: %s", exc)
            return None

        except (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
        ) as exc:
            last_error = exc
        except anthropic.APIStatusError as exc:
            if exc.status_code < 500:
                raise VisionServiceError(
                    f"Vision service returned {exc.status_code}: {exc.message}"
                ) from exc
            last_error = exc

        if attempt == MAX_ATTEMPTS:
            raise VisionServiceError(
                f"Vision service unavailable after {MAX_ATTEMPTS} attempts."
            ) from last_error

        # Full jitter, so a burst of concurrent inspections does not retry in
        # lockstep and re-saturate the upstream.
        delay = RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
        logger.warning(
            "Transient vision failure (attempt %d/%d): %s. Retrying in %.1fs.",
            attempt,
            MAX_ATTEMPTS,
            last_error,
            delay,
        )
        await asyncio.sleep(random.uniform(0, delay))

    if data is None:
        return None

    reliable, reason = _is_reliable(data)
    if not reliable:
        logger.info("Extraction discarded as unreliable: %s", reason)
        return None

    if data.unreadable_fields:
        logger.info(
            "Extraction accepted with %d unreadable declaration(s): %s",
            len(data.unreadable_fields),
            ", ".join(field.value for field in data.unreadable_fields),
        )

    return data
