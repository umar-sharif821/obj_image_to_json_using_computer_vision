/**
 * Client for the Legal Metrology compliance API.
 *
 * Every failure the backend can produce arrives as the same JSON envelope --
 * `{ code, detail, remedy, image_quality }` -- so this module normalises all of
 * them into one `ApiError` carrying that shape. The UI can then branch on
 * `error.code` instead of parsing prose or guessing from a status number.
 */

/**
 * Base URL of the FastAPI backend, without a trailing slash.
 *
 * Set `VITE_API_BASE_URL` in `.env.development` for local work and in the host's
 * environment settings for the deployed demo. The fallback points at a local
 * uvicorn so a fresh clone runs with no configuration at all.
 */
export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'
).replace(/\/+$/, '')

/**
 * Largest upload the backend will accept (20 MB, matching `MAX_UPLOAD_BYTES`).
 * Checked client-side so a phone photo over the limit fails instantly instead
 * of after a long upload on venue wifi.
 */
export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024

/** Image types the backend can decode, matching `ACCEPTED_CONTENT_TYPES`. */
export const ACCEPTED_CONTENT_TYPES = Object.freeze([
  'image/jpeg',
  'image/jpg',
  'image/png',
  'image/webp',
  'image/bmp',
  'image/tiff',
])

/** An error carrying the backend's structured failure envelope. */
export class ApiError extends Error {
  /**
   * @param {string} message Human-readable detail.
   * @param {object} options
   * @param {string} [options.code] Stable machine-readable token.
   * @param {number} [options.status] HTTP status, 0 for a transport failure.
   * @param {string} [options.remedy] Suggested corrective action.
   * @param {object} [options.imageQuality] Measurements, on a quality failure.
   */
  constructor(message, { code = 'error', status = 0, remedy = null, imageQuality = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.remedy = remedy
    this.imageQuality = imageQuality
  }
}

/**
 * Turn a non-OK response into an `ApiError`.
 *
 * @param {Response} response
 * @returns {Promise<ApiError>}
 */
async function toApiError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // A proxy or crash can return HTML or nothing at all; fall through to the
    // status-only message rather than masking the real failure with a parse error.
  }

  if (body && typeof body === 'object' && typeof body.detail === 'string') {
    return new ApiError(body.detail, {
      code: body.code || 'error',
      status: response.status,
      remedy: body.remedy ?? null,
      imageQuality: body.image_quality ?? null,
    })
  }

  return new ApiError(`The server responded with HTTP ${response.status}.`, {
    code: 'unexpected_response',
    status: response.status,
  })
}

/**
 * Validate a file before it is uploaded.
 *
 * @param {File} file
 * @returns {string|null} An error message, or null if the file is acceptable.
 */
export function validateImageFile(file) {
  if (!file) return 'Choose or capture a photograph first.'
  if (file.size === 0) return 'That file is empty. Try capturing the photograph again.'
  if (file.size > MAX_UPLOAD_BYTES) {
    const mb = (file.size / (1024 * 1024)).toFixed(1)
    return `That image is ${mb} MB. The limit is 20 MB — retake it at a lower resolution.`
  }
  // Some Android pickers report an empty type; accept those and let the backend
  // decide rather than blocking a capture that would have worked.
  if (file.type && !ACCEPTED_CONTENT_TYPES.includes(file.type.toLowerCase())) {
    return `${file.type} is not a supported image type. Use JPEG, PNG, WEBP, BMP or TIFF.`
  }
  return null
}

/**
 * Submit a label photograph for inspection.
 *
 * @param {File} file The captured or selected image.
 * @param {object} [options]
 * @param {AbortSignal} [options.signal] Lets the UI cancel an in-flight scan.
 * @param {object} [options.context] Optional EvaluationContext; serialised to
 *   the `context` form field the backend expects.
 * @returns {Promise<object>} The ComplianceReport.
 * @throws {ApiError}
 */
export async function scanLabel(file, { signal, context } = {}) {
  const form = new FormData()
  // The field name must be "image" -- it is the backend parameter name.
  form.append('image', file, file.name || 'label.jpg')
  if (context) form.append('context', JSON.stringify(context))

  let response
  try {
    response = await fetch(`${API_BASE_URL}/api/v1/scan`, {
      method: 'POST',
      body: form,
      signal,
    })
  } catch (cause) {
    if (cause?.name === 'AbortError') throw cause
    throw new ApiError(
      `Could not reach the inspection service at ${API_BASE_URL}.`,
      {
        code: 'network_error',
        remedy:
          'Check that the backend is running and that VITE_API_BASE_URL points at it. ' +
          'On a phone, localhost means the phone itself — use the computer’s LAN address.',
      },
    )
  }

  if (!response.ok) throw await toApiError(response)
  return response.json()
}

/**
 * Render a report as a PDF and hand it to the browser as a download.
 *
 * The report is posted back rather than referenced by id: the backend keeps no
 * report store, so the client holds the only copy of what it was given.
 *
 * @param {object} report The ComplianceReport received from {@link scanLabel}.
 * @returns {Promise<string>} The filename the browser was given.
 * @throws {ApiError}
 */
export async function downloadReportPdf(report) {
  let response
  try {
    response = await fetch(`${API_BASE_URL}/api/v1/report/pdf`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(report),
    })
  } catch {
    throw new ApiError('Could not reach the inspection service to build the PDF.', {
      code: 'network_error',
      remedy: 'Check your connection and try again.',
    })
  }

  if (!response.ok) throw await toApiError(response)

  const blob = await response.blob()
  const filename = filenameFromDisposition(response.headers.get('Content-Disposition'))
    || `inspection-report-${(report.report_id || 'report').slice(0, 8)}.pdf`

  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // Revoking immediately can cancel the download in some browsers; a short
  // delay is the pragmatic fix and leaks nothing meaningful.
  setTimeout(() => URL.revokeObjectURL(url), 30_000)

  return filename
}

/**
 * Pull the filename out of a Content-Disposition header.
 *
 * @param {string|null} header
 * @returns {string|null}
 */
function filenameFromDisposition(header) {
  if (!header) return null
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header)
  return match ? decodeURIComponent(match[1]) : null
}

/**
 * Check that the backend is reachable.
 *
 * @param {object} [options]
 * @param {AbortSignal} [options.signal]
 * @returns {Promise<boolean>}
 */
export async function checkHealth({ signal } = {}) {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/health`, { signal })
    if (!response.ok) return false
    const body = await response.json()
    return body?.status === 'ok'
  } catch {
    return false
  }
}
