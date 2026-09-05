import { SEVERITY_LABELS, partitionViolations } from '../lib/format.js'

/**
 * The findings: contraventions first, unassessed provisions separately.
 *
 * The two lists are never merged. An `advisory` records a provision the engine
 * could not evaluate — showing it beside real contraventions would inflate the
 * apparent case against the manufacturer, which is exactly what the backend
 * takes care to avoid.
 *
 * @param {object} props
 * @param {object[]} props.violations The report's `violations` array.
 * @returns {JSX.Element}
 */
export default function ViolationsList({ violations = [] }) {
  const { contraventions, unassessed } = partitionViolations(violations)

  return (
    <>
      <section className="card" aria-labelledby="findings-heading">
        <h3 id="findings-heading" className="card__heading">
          Rules contravened
          {contraventions.length > 0 && (
            <span className="card__count">{contraventions.length}</span>
          )}
        </h3>

        {contraventions.length === 0 ? (
          <p className="card__clear">
            <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
              <path
                d="m5 12.5 4.5 4.5L19 7.5"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            No contravention was found in the declarations that could be assessed.
          </p>
        ) : (
          <ol className="violations">
            {contraventions.map((violation) => (
              <li className="violation" key={violation.violation_id}>
                <div className="violation__head">
                  <span className="violation__rule">{violation.rule_reference}</span>
                  <span className={`chip chip--${violation.severity}`}>
                    {SEVERITY_LABELS[violation.severity] || violation.severity}
                  </span>
                </div>

                <h4 className="violation__title">{violation.title}</h4>
                <p className="violation__description">{violation.description}</p>

                {violation.observed_value && (
                  <p className="violation__observed">
                    <span className="violation__field-label">On the package</span>
                    <q>{violation.observed_value}</q>
                  </p>
                )}

                <p className="violation__expected">
                  <span className="violation__field-label">Required</span>
                  {violation.expected_requirement}
                </p>

                {violation.penalty_reference && (
                  <p className="violation__penalty">{violation.penalty_reference}</p>
                )}
              </li>
            ))}
          </ol>
        )}
      </section>

      {unassessed.length > 0 && (
        <section className="card card--muted" aria-labelledby="unassessed-heading">
          <h3 id="unassessed-heading" className="card__heading">
            Not assessed
            <span className="card__count card__count--muted">{unassessed.length}</span>
          </h3>
          <p className="card__sub">
            These provisions could not be evaluated from what was available. They are
            <strong> not </strong>
            findings against the package.
          </p>
          <ul className="unassessed">
            {unassessed.map((item) => (
              <li key={item.violation_id}>
                <span className="unassessed__rule">{item.rule_reference}</span>
                <span className="unassessed__reason">{item.description}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  )
}
