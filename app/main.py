"""
FastAPI application for the Legal Metrology compliance platform (SIH26034).

Request pipeline for a scan:

    multipart upload
        -> content-type and size validation          (415 / 413)
        -> SHA-256 digest                            (chain of custody)
        -> compute_image_quality_metrics(original)   (400 on undecodable)
        -> preprocess_for_ocr(original)              (400 on undecodable)
        -> extract_declarations_from_image(...)      (422 unreadable, 503 outage)
        -> evaluate_compliance(...)                  (deterministic verdict)
        -> ComplianceReport as JSON                  (200)

Two ordering decisions in that pipeline are deliberate:

* **Quality is measured on the original, not the processed frame.** The glare
  pipeline inpaints specular highlights; measuring glare after that step would
  report a clean image every time and defeat the gate. The metrics carried into
  the report therefore describe the bytes the officer actually submitted.
* **The route pre-processes, then calls the extractor with ``preprocess=False``.**
  :func:`~app.vision_parser.extract_declarations_from_image` runs the OpenCV
  pipeline itself by default; running it here as well would rescale, inpaint and
  sharpen the frame twice, softening exactly the fine print the system exists to
  read. Pre-processing in the route additionally surfaces a corrupt upload as a
  400 before any billable vision call is made.

No database is wired up: the rules engine is stateless and pure, and nothing in
the current request path needs persistence. The seam for it is
:func:`scan_package` -- a session dependency injected there would let the
returned ``report_id`` be resolved later, which is what Phase 4 will need if
notices are to be re-issued from storage rather than regenerated on the fly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Final, Optional

import cv2
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from app import __version__
from app.rules_engine import RULES_ENGINE_VERSION, evaluate_compliance
from app.schemas import ComplianceReport, EvaluationContext, ImageQualityMetrics
from app.vision_parser import (
    MAX_UPLOAD_BYTES,
    VISION_MODEL_ID,
    UnsupportedImageError,
    VisionServiceError,
    compute_image_quality_metrics,
    extract_declarations_from_image,
    preprocess_for_ocr,
)

logger = logging.getLogger(__name__)

API_PREFIX: Final[str] = "/api/v1"

#: Image types the vision pipeline can decode and the API will accept.
ACCEPTED_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp", "image/tiff"}
)

#: Header carrying the correlation identifier for one inspection request.
REQUEST_ID_HEADER: Final[str] = "X-Request-ID"


# --------------------------------------------------------------------------- #
# Error envelope                                                               #
# --------------------------------------------------------------------------- #


class ErrorResponse(BaseModel):
    """
    Structured error body returned by every failing endpoint.

    ``code`` is a stable machine-readable token so client applications can
    branch on the failure without parsing prose, and ``remedy`` tells the
    officer in the field what to do about it -- most failures here are
    recoverable by re-photographing the package.
    """

    code: str = Field(description="Stable machine-readable error token.")
    detail: str = Field(description="Human-readable explanation of the failure.")
    remedy: Optional[str] = Field(
        default=None, description="Suggested corrective action for the caller."
    )
    image_quality: Optional[ImageQualityMetrics] = Field(
        default=None,
        description=(
            "Objective measurements of the submitted image, where the failure "
            "was caused by image quality."
        ),
    )


def _error(
    status_code: int,
    code: str,
    detail: str,
    *,
    remedy: Optional[str] = None,
    image_quality: Optional[ImageQualityMetrics] = None,
) -> HTTPException:
    """Build an ``HTTPException`` whose body is an :class:`ErrorResponse`."""
    payload = ErrorResponse(
        code=code, detail=detail, remedy=remedy, image_quality=image_quality
    )
    return HTTPException(status_code=status_code, detail=payload.model_dump(mode="json"))


# --------------------------------------------------------------------------- #
# Application                                                                  #
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """
    Record the versions this instance is serving, for the audit trail.

    A report issued today may be contested a year from now; the log must show
    which vision model and which rules-engine revision produced it.
    """
    logger.info(
        "Legal Metrology Compliance Scanner v%s started "
        "(vision model=%s, rules engine=%s).",
        __version__,
        VISION_MODEL_ID,
        RULES_ENGINE_VERSION,
    )
    yield
    logger.info("Legal Metrology Compliance Scanner shutting down.")


app = FastAPI(
    lifespan=lifespan,
    title="Legal Metrology Compliance Scanner",
    version=__version__,
    summary=(
        "Scans packaged commodity labels and evaluates the mandatory declarations "
        "against the Legal Metrology (Packaged Commodities) Rules, 2011."
    ),
    description=(
        "Built for Smart India Hackathon problem statement SIH26034 (Ministry of "
        "Consumer Affairs, Food and Public Distribution).\n\n"
        "Extraction is performed by a vision model under a strict transcription "
        "contract; adjudication is performed by a deterministic rules engine. An "
        "image that cannot be read reliably yields HTTP 422 and no verdict -- the "
        "system never records a finding against a package it could not examine."
    ),
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=f"{API_PREFIX}/redoc",
    openapi_url=f"{API_PREFIX}/openapi.json",
)

# Open CORS policy for the hackathon build. Note that ``allow_credentials`` is
# left False deliberately: browsers reject a wildcard origin combined with
# credentialed requests, so enabling it here would silently break every call.
# Restrict ``allow_origins`` to the deployed console origins before release.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[REQUEST_ID_HEADER],
)


@app.middleware("http")
async def attach_request_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """
    Attach a correlation identifier to every request and response.

    Each scan may end in a statutory notice, so the log line for a request must
    be locatable months later from the response the field officer received.
    """
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Render ``HTTPException`` detail as an :class:`ErrorResponse` body."""
    detail: Any = exc.detail
    if not isinstance(detail, dict):
        detail = ErrorResponse(code="error", detail=str(detail)).model_dump(mode="json")
    return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Convert an unanticipated failure into a 500 without leaking internals.

    The traceback goes to the log with the request identifier; the caller gets
    that identifier and nothing else.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled error while serving %s [%s]", request.url.path, request_id)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            code="internal_error",
            detail=(
                "The request could not be completed due to an internal error. "
                f"Quote reference {request_id} when reporting this."
            ),
        ).model_dump(mode="json"),
    )


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """
    Send the bare host to the interactive API documentation.

    Every route on this service is namespaced under ``/api/v1``, so the URL
    uvicorn prints on startup -- the one an operator actually clicks -- would
    otherwise answer 404 and look like a broken deployment.

    Returns:
        A temporary redirect to the OpenAPI documentation.
    """
    return RedirectResponse(url=f"{API_PREFIX}/docs")


@app.get(
    f"{API_PREFIX}/health",
    tags=["operations"],
    summary="Liveness probe",
)
async def health() -> dict[str, str]:
    """
    Report that the process is up and serving.

    Deliberately does not touch the vision service: this is a liveness probe for
    the load balancer, and an upstream outage must not cause healthy instances
    to be recycled.

    Returns:
        ``{"status": "ok"}``.
    """
    return {"status": "ok"}


@app.post(
    f"{API_PREFIX}/scan",
    response_model=ComplianceReport,
    status_code=status.HTTP_200_OK,
    tags=["inspection"],
    summary="Scan a packaged commodity label and adjudicate it",
    responses={
        400: {"model": ErrorResponse, "description": "Corrupt or undecodable image."},
        413: {"model": ErrorResponse, "description": "Upload exceeds the size limit."},
        415: {"model": ErrorResponse, "description": "Unsupported content type."},
        422: {
            "model": ErrorResponse,
            "description": (
                "The label could not be read reliably. No verdict is issued -- this "
                "is an evidentiary failure, not a finding against the package."
            ),
        },
        503: {"model": ErrorResponse, "description": "Vision service unavailable."},
    },
)
async def scan_package(
    request: Request,
    image: Annotated[
        UploadFile,
        File(description="Photograph of the package's principal display panel."),
    ],
    context: Annotated[
        Optional[str],
        Form(
            description=(
                "Optional JSON object matching the EvaluationContext schema: "
                "commodity category, perishability, import status, physical "
                "measurements enabling the Rule 7 character-size checks, and the "
                "inspecting officer's identifiers. Omitted fields fall back to "
                "conservative defaults."
            )
        ),
    ] = None,
) -> ComplianceReport:
    """
    Scan a packaged commodity label and evaluate it against the 2011 Rules.

    Args:
        request: The inbound request, carrying the correlation identifier.
        image: Multipart file upload of the package photograph.
        context: Optional JSON-encoded :class:`~app.schemas.EvaluationContext`.
            Without it the engine assumes the strictest path (a general
            commodity, no other statute displacing these Rules) and reports the
            Rule 7 size checks as not assessed, since those need physical
            measurements that no uncalibrated photograph can supply.

    Returns:
        A :class:`~app.schemas.ComplianceReport` bound by SHA-256 to the exact
        bytes examined.

    Raises:
        HTTPException:
            * 400 -- the upload is empty or cannot be decoded as an image.
            * 413 -- the upload exceeds the configured size limit.
            * 415 -- the content type is not a supported image type.
            * 422 -- the ``context`` JSON is invalid, or the label could not be
              read reliably. In the latter case no verdict is issued.
            * 503 -- the vision service could not be reached.
    """
    started = time.perf_counter()
    request_id = getattr(request.state, "request_id", "unknown")

    evaluation_context = _parse_context(context)

    if image.content_type and image.content_type.lower() not in ACCEPTED_CONTENT_TYPES:
        raise _error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            f"Content type '{image.content_type}' is not a supported image type.",
            remedy=f"Upload one of: {', '.join(sorted(ACCEPTED_CONTENT_TYPES))}.",
        )

    raw_bytes = await image.read()
    if not raw_bytes:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "empty_upload",
            "The uploaded file is empty.",
            remedy="Re-capture the photograph and upload again.",
        )
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "upload_too_large",
            f"The upload is {len(raw_bytes) / (1024 * 1024):.1f} MB, which exceeds "
            f"the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
            remedy="Reduce the capture resolution or re-encode the photograph.",
        )

    image_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    logger.info(
        "Scan received [%s]: filename=%s bytes=%d sha256=%s",
        request_id,
        image.filename,
        len(raw_bytes),
        image_sha256,
    )

    metrics, processed_bytes = await _prepare_image(raw_bytes)

    if not metrics.is_usable:
        # Refuse here rather than downstream: the objective measurements are
        # already conclusive, and sending a frame we have measured as unreadable
        # to the vision service would spend a billable call to reach the same
        # answer. The extractor applies this gate too -- this is the cheap path,
        # not the only one.
        logger.info(
            "Image failed the quality gate [%s]: %s",
            request_id,
            "; ".join(metrics.quality_notes),
        )
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "label_unreadable",
            "The submitted image does not meet the minimum quality required to "
            "read the declarations, so no finding of compliance or contravention "
            "has been recorded.",
            remedy=_capture_advice(metrics),
            image_quality=metrics,
        )

    try:
        extracted = await extract_declarations_from_image(processed_bytes, preprocess=False)
    except VisionServiceError as exc:
        # An outage on our side must never be recorded as a finding about a
        # package, so it surfaces as 503 rather than an INDETERMINATE report.
        logger.error("Vision service unavailable [%s]: %s", request_id, exc)
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "vision_service_unavailable",
            # VisionServiceError messages are authored in this codebase and
            # describe configuration or upstream state, never internals, so the
            # text is safe to hand back to the caller -- and a service that is
            # merely unconfigured should say so rather than look like an outage.
            f"The label extraction service is currently unavailable: {exc} "
            "No inspection was performed.",
            remedy="Retry shortly. The submitted image has not been assessed.",
        ) from exc

    if extracted is None:
        logger.info("Label unreadable [%s]: sha256=%s", request_id, image_sha256)
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "label_unreadable",
            "The declarations on this package could not be read reliably, so no "
            "finding of compliance or contravention has been recorded. This is an "
            "evidentiary limitation of the submitted image, not a determination "
            "about the package.",
            remedy=_capture_advice(metrics),
            image_quality=metrics,
        )

    report = evaluate_compliance(
        extracted,
        image_sha256=image_sha256,
        context=evaluation_context,
        image_filename=image.filename,
        image_quality=metrics,
        vision_model_id=VISION_MODEL_ID,
        processing_time_ms=(time.perf_counter() - started) * 1000.0,
    )

    logger.info(
        "Scan adjudicated [%s]: report=%s status=%s violations=%d score=%.1f",
        request_id,
        report.report_id,
        report.status.value,
        report.violation_count,
        report.compliance_score,
    )
    return report


# --------------------------------------------------------------------------- #
# Route helpers                                                                #
# --------------------------------------------------------------------------- #


def _parse_context(raw: Optional[str]) -> Optional[EvaluationContext]:
    """
    Parse the optional JSON evaluation context supplied alongside the image.

    Args:
        raw: JSON text, or ``None`` when the caller supplied no context.

    Returns:
        The parsed context, or ``None``.

    Raises:
        HTTPException: 422 if the JSON is malformed or fails validation. The
            context governs which provisions bite, so a silently discarded
            malformed context could produce a materially wrong verdict.
    """
    if raw is None or not raw.strip():
        return None
    try:
        return EvaluationContext.model_validate_json(raw)
    except ValidationError as exc:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_context",
            f"The 'context' field is not a valid EvaluationContext: {exc.error_count()} "
            "validation error(s).",
            remedy="Correct the context JSON, or omit it to use conservative defaults.",
        ) from exc


async def _prepare_image(raw_bytes: bytes) -> tuple[ImageQualityMetrics, bytes]:
    """
    Measure and pre-process the uploaded image off the event loop.

    Both operations are CPU-bound OpenCV work; running them in a worker thread
    keeps one large upload from stalling every other in-flight inspection.

    Args:
        raw_bytes: The original uploaded bytes.

    Returns:
        The quality metrics of the *original* image, and the pre-processed
        frame to send to the vision model.

    Raises:
        HTTPException: 400 if the payload cannot be decoded or processed.
    """
    try:
        metrics = await asyncio.to_thread(compute_image_quality_metrics, raw_bytes)
        processed = await asyncio.to_thread(preprocess_for_ocr, raw_bytes)
    except UnsupportedImageError as exc:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "undecodable_image",
            str(exc),
            remedy="Upload an uncorrupted JPEG, PNG, WEBP, BMP or TIFF image.",
        ) from exc
    except cv2.error as exc:
        logger.exception("OpenCV failed while preparing the uploaded image.")
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "image_processing_failed",
            "The uploaded image could not be processed. It may be truncated or corrupt.",
            remedy="Re-capture the photograph and upload again.",
        ) from exc

    return metrics, processed


def _capture_advice(metrics: ImageQualityMetrics) -> str:
    """
    Turn objective image measurements into instructions for the field officer.

    Generic advice ("take a better photo") is useless at the point of capture;
    naming the specific defect the measurements found is not.
    """
    if metrics.quality_notes:
        return " ".join(metrics.quality_notes) + (
            " Re-capture the declaration panel accordingly."
        )
    return (
        "Re-capture the declaration panel square to the camera, filling the frame, "
        "with diffuse lighting and the glare off the wrapper."
    )
