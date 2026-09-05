/**
 * Friendly copy for each failure the backend can return.
 *
 * The 422 case is the one that matters most. An unreadable photograph is not a
 * verdict about the package and not the user's mistake — it is a limitation of
 * the image — so it gets encouraging, actionable copy rather than an error
 * shout, and never any language implying the product failed.
 */
const PRESENTATION = Object.freeze({
  label_unreadable: {
    tone: 'warn',
    title: "Couldn't read this label clearly",
    body:
      'Try retaking the photo with better lighting — hold the pack steady, fill the frame with the ' +
      'declaration panel, and tilt it slightly to keep glare off the wrapper.',
  },
  undecodable_image: {
    tone: 'warn',
    title: 'That image could not be opened',
    body: 'The file may be corrupt or incomplete. Capture the photograph again.',
  },
  image_processing_failed: {
    tone: 'warn',
    title: 'That image could not be processed',
    body: 'It may be truncated. Capture the photograph again.',
  },
  empty_upload: {
    tone: 'warn',
    title: 'The file was empty',
    body: 'Nothing was captured. Try again.',
  },
  upload_too_large: {
    tone: 'warn',
    title: 'That photo is too large',
    body: 'Retake it at a lower resolution, or pick a smaller file. The limit is 20 MB.',
  },
  unsupported_media_type: {
    tone: 'warn',
    title: 'That file type is not supported',
    body: 'Use a JPEG, PNG, WEBP, BMP or TIFF image.',
  },
  invalid_file: {
    tone: 'warn',
    title: 'That file cannot be used',
    body: null,
  },
  vision_service_unavailable: {
    tone: 'bad',
    title: 'The inspection service is unavailable',
    body:
      'Your photo has not been assessed and no finding has been recorded. This is a problem on the ' +
      'server, not with the package.',
  },
  network_error: {
    tone: 'bad',
    title: 'Could not reach the inspection service',
    body: null,
  },
  internal_error: {
    tone: 'bad',
    title: 'Something went wrong on the server',
    body: 'Your photo has not been assessed. Please try again.',
  },
})

const FALLBACK = { tone: 'bad', title: 'Something went wrong', body: null }

/**
 * Render a scan failure in plain language.
 *
 * @param {object} props
 * @param {import('../api/client.js').ApiError} props.error
 * @param {() => void} props.onDismiss
 * @returns {JSX.Element}
 */
export default function ScanErrorNotice({ error, onDismiss }) {
  const preset = PRESENTATION[error.code] || FALLBACK
  const quality = error.imageQuality

  return (
    <section className={`notice notice--${preset.tone}`} role="alert">
      <div className="notice__head">
        <h2 className="notice__title">{preset.title}</h2>
        <button
          type="button"
          className="notice__close"
          onClick={onDismiss}
          aria-label="Dismiss this message"
        >
          ×
        </button>
      </div>

      <p className="notice__body">{preset.body || error.message}</p>

      {/* The remedy is written by the backend for the officer in the field and
          is more specific than anything generic here — surface it verbatim. */}
      {error.remedy && <p className="notice__remedy">{error.remedy}</p>}

      {quality?.quality_notes?.length > 0 && (
        <div className="notice__measured">
          <p className="notice__measured-label">What the image check measured</p>
          <ul>
            {quality.quality_notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      {preset.body && error.message && preset.body !== error.message && (
        <details className="notice__details">
          <summary>Technical detail</summary>
          <p>{error.message}</p>
        </details>
      )}
    </section>
  )
}
