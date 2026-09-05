import { useRef } from 'react'

/**
 * Capture screen: camera, gallery, preview and the scan trigger.
 *
 * Two separate file inputs rather than one. The `capture="environment"`
 * attribute is what makes a phone open the rear camera directly instead of a
 * file chooser, but on desktop it is ignored and on some Android builds it
 * suppresses the gallery entirely. Keeping a second, plain input guarantees
 * both routes work everywhere, which matters when the demo may run on either.
 *
 * @param {object} props
 * @param {File|null} props.file Currently selected image.
 * @param {string|null} props.previewUrl Object URL for the preview.
 * @param {boolean} props.isScanning Whether a scan is in flight.
 * @param {(file: File|null) => void} props.onSelect
 * @param {() => void} props.onScan
 * @param {() => void} props.onClear
 * @returns {JSX.Element}
 */
export default function CapturePanel({
  file,
  previewUrl,
  isScanning,
  onSelect,
  onScan,
  onClear,
}) {
  const cameraRef = useRef(null)
  const galleryRef = useRef(null)

  /** @param {React.ChangeEvent<HTMLInputElement>} event */
  const handleChange = (event) => {
    const [selected] = event.target.files || []
    onSelect(selected || null)
    // Reset the input so re-picking the same file still fires a change event.
    event.target.value = ''
  }

  const sizeLabel = file ? `${(file.size / (1024 * 1024)).toFixed(1)} MB` : null

  return (
    <section className="panel" aria-labelledby="capture-heading">
      <h2 id="capture-heading" className="panel__heading">
        {file ? 'Confirm the photograph' : 'Capture the label'}
      </h2>

      {!file && (
        <p className="panel__hint">
          Fill the frame with the panel carrying the MRP, net quantity and
          manufacturer details. Avoid glare on plastic wrappers.
        </p>
      )}

      {previewUrl ? (
        <figure className="preview">
          <img className="preview__image" src={previewUrl} alt="The label you captured" />
          <figcaption className="preview__caption">
            <span className="preview__name">{file?.name || 'Captured photo'}</span>
            {sizeLabel && <span className="preview__size">{sizeLabel}</span>}
          </figcaption>
        </figure>
      ) : (
        <div className="dropzone" aria-hidden="true">
          <svg viewBox="0 0 48 48" width="52" height="52">
            <path
              d="M6 16a4 4 0 0 1 4-4h5l3-4h12l3 4h5a4 4 0 0 1 4 4v18a4 4 0 0 1-4 4H10a4 4 0 0 1-4-4z"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinejoin="round"
            />
            <circle cx="24" cy="25" r="7" fill="none" stroke="currentColor" strokeWidth="2.2" />
          </svg>
          <p>No photograph selected yet</p>
        </div>
      )}

      {/*
        Both inputs stay in the DOM and are triggered by the visible buttons, so
        the tap targets can be sized properly instead of inheriting the browser's
        cramped native file-input styling.
      */}
      <input
        ref={cameraRef}
        type="file"
        accept="image/*"
        capture="environment"
        onChange={handleChange}
        className="visually-hidden"
        tabIndex={-1}
        aria-hidden="true"
      />
      <input
        ref={galleryRef}
        type="file"
        accept="image/*"
        onChange={handleChange}
        className="visually-hidden"
        tabIndex={-1}
        aria-hidden="true"
      />

      <div className="actions">
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => cameraRef.current?.click()}
          disabled={isScanning}
        >
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
            <path
              d="M4 8a2 2 0 0 1 2-2h2.5L10 4h4l1.5 2H18a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.9"
              strokeLinejoin="round"
            />
            <circle cx="12" cy="12.5" r="3.4" fill="none" stroke="currentColor" strokeWidth="1.9" />
          </svg>
          {file ? 'Retake photo' : 'Open camera'}
        </button>

        <button
          type="button"
          className="btn btn--secondary"
          onClick={() => galleryRef.current?.click()}
          disabled={isScanning}
        >
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
            <path
              d="M4 6.5A1.5 1.5 0 0 1 5.5 5h13A1.5 1.5 0 0 1 20 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.9"
            />
            <path
              d="m4 15 4.5-4 4 3.5L16 11l4 4"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.9"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          Choose from gallery
        </button>
      </div>

      <div className="actions actions--stack">
        <button
          type="button"
          className="btn btn--scan"
          onClick={onScan}
          disabled={!file || isScanning}
        >
          {isScanning ? 'Scanning…' : 'Scan Label'}
        </button>

        {file && !isScanning && (
          <button type="button" className="btn btn--ghost" onClick={onClear}>
            Remove photo
          </button>
        )}
      </div>

      {!file && (
        <p className="panel__disabled-note">
          The scan button turns on once a photograph is selected.
        </p>
      )}
    </section>
  )
}
