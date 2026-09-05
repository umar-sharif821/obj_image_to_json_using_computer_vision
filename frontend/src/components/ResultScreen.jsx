import { useState } from 'react'
import VerdictBadge from './VerdictBadge.jsx'
import DeclarationsCard from './DeclarationsCard.jsx'
import ViolationsList from './ViolationsList.jsx'
import { downloadReportPdf } from '../api/client.js'
import { formatTimestamp } from '../lib/format.js'

/**
 * Results screen: verdict, declarations, findings and the PDF download.
 *
 * @param {object} props
 * @param {object} props.report The ComplianceReport.
 * @param {string|null} props.previewUrl The image that was scanned.
 * @param {() => void} props.onScanAnother
 * @returns {JSX.Element}
 */
export default function ResultScreen({ report, previewUrl, onScanAnother }) {
  const [isDownloading, setIsDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState(null)

  const handleDownload = async () => {
    setIsDownloading(true)
    setDownloadError(null)
    try {
      await downloadReportPdf(report)
    } catch (caught) {
      setDownloadError(caught?.message || 'The PDF could not be generated.')
    } finally {
      setIsDownloading(false)
    }
  }

  return (
    <div className="result">
      <VerdictBadge report={report} />

      {previewUrl && (
        <figure className="result__thumb">
          <img src={previewUrl} alt="The label that was scanned" />
        </figure>
      )}

      <DeclarationsCard extracted={report.extracted_data} />
      <ViolationsList violations={report.violations} />

      <section className="card card--meta" aria-labelledby="provenance-heading">
        <h3 id="provenance-heading" className="card__heading">
          Report details
        </h3>
        <dl className="meta">
          <div>
            <dt>Assessed</dt>
            <dd>{formatTimestamp(report.generated_at)}</dd>
          </div>
          <div>
            <dt>Statute</dt>
            <dd>{report.statute_reference}</dd>
          </div>
          <div>
            <dt>Rules engine</dt>
            <dd>v{report.rules_engine_version || '—'}</dd>
          </div>
          <div>
            <dt>Image digest</dt>
            {/* Truncated for the phone; the full digest is in the PDF, which is
                what actually ties a notice to the photograph examined. */}
            <dd className="meta__mono">{(report.image_sha256 || '').slice(0, 16)}…</dd>
          </div>
        </dl>
      </section>

      {downloadError && (
        <p className="result__download-error" role="alert">
          {downloadError}
        </p>
      )}

      <div className="actions actions--stack result__actions">
        <button
          type="button"
          className="btn btn--primary"
          onClick={handleDownload}
          disabled={isDownloading}
        >
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
            <path
              d="M12 4v10m0 0 4-4m-4 4-4-4M5 18h14"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          {isDownloading ? 'Preparing PDF…' : 'Download Inspection PDF'}
        </button>

        <button type="button" className="btn btn--secondary" onClick={onScanAnother}>
          Scan another label
        </button>
      </div>

      <p className="result__disclaimer">
        Preliminary result produced by automated reading of a photograph. It is subject to
        manual verification and does not by itself constitute a notice under the Legal
        Metrology Act, 2009.
      </p>
    </div>
  )
}
