import { useEffect, useState } from 'react'

/**
 * Status messages shown in sequence while a scan is in flight.
 *
 * Vision extraction routinely takes several seconds. A single frozen spinner
 * for that long reads as a hung app, so the copy advances through the actual
 * stages of the pipeline — which is both honest and reassuring.
 */
const STAGES = Object.freeze([
  { at: 0, text: 'Uploading the photograph…' },
  { at: 1800, text: 'Reducing glare and normalising contrast…' },
  { at: 4200, text: 'Reading the declarations on the label…' },
  { at: 9000, text: 'Checking against the Packaged Commodities Rules, 2011…' },
  { at: 15000, text: 'Almost there — finishing the assessment…' },
])

/**
 * Full-screen blocking overlay shown during a scan.
 *
 * @param {object} props
 * @param {() => void} props.onCancel
 * @returns {JSX.Element}
 */
export default function LoadingOverlay({ onCancel }) {
  const [stage, setStage] = useState(0)

  useEffect(() => {
    const timers = STAGES.map((entry, index) =>
      window.setTimeout(() => setStage(index), entry.at),
    )
    return () => timers.forEach(window.clearTimeout)
  }, [])

  return (
    <div className="overlay" role="alertdialog" aria-live="polite" aria-busy="true">
      <div className="overlay__card">
        <div className="spinner" aria-hidden="true" />
        <p className="overlay__status">{STAGES[stage].text}</p>
        <p className="overlay__sub">This usually takes a few seconds.</p>
        <button type="button" className="btn btn--ghost btn--small" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  )
}
