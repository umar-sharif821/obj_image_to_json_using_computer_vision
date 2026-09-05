import { VERDICTS } from '../lib/format.js'

/**
 * Large colour-coded verdict banner.
 *
 * @param {object} props
 * @param {object} props.report The ComplianceReport.
 * @returns {JSX.Element}
 */
export default function VerdictBadge({ report }) {
  const verdict = VERDICTS[report.status] || VERDICTS.indeterminate
  const chargeable = report.violations.filter((v) => v.severity !== 'advisory').length
  const unassessed = report.violations.length - chargeable

  return (
    <section className={`verdict verdict--${verdict.tone}`} aria-labelledby="verdict-heading">
      <p className="verdict__eyebrow">Assessment result</p>
      <h2 id="verdict-heading" className="verdict__label">
        {verdict.label}
      </h2>
      <p className="verdict__gloss">{verdict.gloss}</p>

      <dl className="verdict__stats">
        <div>
          <dt>Contraventions</dt>
          <dd>{chargeable}</dd>
        </div>
        <div>
          <dt>Mandatory fields absent</dt>
          <dd>{report.critical_violation_count ?? 0}</dd>
        </div>
        <div>
          <dt>Not assessed</dt>
          <dd>{unassessed}</dd>
        </div>
        <div>
          <dt>Score</dt>
          <dd>{Number(report.compliance_score ?? 0).toFixed(0)}/100</dd>
        </div>
      </dl>

      {report.summary && <p className="verdict__summary">{report.summary}</p>}
    </section>
  )
}
