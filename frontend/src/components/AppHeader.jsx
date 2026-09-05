/**
 * Application header: emblem, title and a one-line tagline.
 *
 * The tagline is not decoration. A judge picking up the phone should understand
 * what the tool does before anyone narrates it, so the line states the input,
 * the action and the statute in one sentence.
 *
 * @param {object} props
 * @param {(() => void)|null} [props.onHome] Shown as a back control when set.
 * @returns {JSX.Element}
 */
export default function AppHeader({ onHome = null }) {
  return (
    <header className="header">
      <div className="header__bar">
        {onHome && (
          <button type="button" className="header__back" onClick={onHome} aria-label="New scan">
            <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
              <path
                d="M15 5 8 12l7 7"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        )}

        <div className="header__emblem" aria-hidden="true">
          <svg viewBox="0 0 32 32" width="26" height="26">
            {/* A balance scale — the Legal Metrology department's own subject. */}
            <path
              d="M16 5v20M9 25h14M16 8l-8 3M16 8l8 3M8 11l-3.5 7h7L8 11zM24 11l-3.5 7h7L24 11z"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>

        <div className="header__titles">
          <h1 className="header__title">Legal Metrology Label Scanner</h1>
          <p className="header__tagline">
            Photograph a packaged product — check its mandatory declarations
            against the Packaged Commodities Rules, 2011.
          </p>
        </div>
      </div>
      <div className="header__accent" aria-hidden="true" />
    </header>
  )
}
