import { useCallback, useEffect, useRef, useState } from 'react'
import AppHeader from './components/AppHeader.jsx'
import CapturePanel from './components/CapturePanel.jsx'
import LoadingOverlay from './components/LoadingOverlay.jsx'
import ScanErrorNotice from './components/ScanErrorNotice.jsx'
import ResultScreen from './components/ResultScreen.jsx'
import { ApiError, scanLabel, validateImageFile } from './api/client.js'

/** Screens the app can be on. */
const VIEW = Object.freeze({ CAPTURE: 'capture', RESULT: 'result' })

/**
 * Root application component.
 *
 * Holds the whole flow in one place because it is genuinely one flow: pick an
 * image, submit it, read the verdict. A router would add a dependency and a
 * bundle for two screens that never need to be linked to directly.
 *
 * @returns {JSX.Element}
 */
export default function App() {
  const [view, setView] = useState(VIEW.CAPTURE)
  const [file, setFile] = useState(null)
  const [previewUrl, setPreviewUrl] = useState(null)
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)
  const [isScanning, setIsScanning] = useState(false)

  // Held in a ref so the cancel button can reach the in-flight request without
  // the controller becoming render state.
  const abortRef = useRef(null)

  // Object URLs are a manual allocation; releasing them on replace and on
  // unmount keeps a long session from accumulating image blobs in memory.
  useEffect(() => {
    if (!file) {
      setPreviewUrl(null)
      return undefined
    }
    const url = URL.createObjectURL(file)
    setPreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  useEffect(() => () => abortRef.current?.abort(), [])

  /**
   * Accept a file chosen from the camera or the gallery.
   *
   * @param {File|null} selected
   */
  const handleSelect = useCallback((selected) => {
    setError(null)
    if (!selected) {
      setFile(null)
      return
    }
    const problem = validateImageFile(selected)
    if (problem) {
      setFile(null)
      setError(new ApiError(problem, { code: 'invalid_file' }))
      return
    }
    setFile(selected)
  }, [])

  /** Submit the selected image for inspection. */
  const handleScan = useCallback(async () => {
    if (!file || isScanning) return

    const controller = new AbortController()
    abortRef.current = controller
    setIsScanning(true)
    setError(null)

    try {
      const result = await scanLabel(file, { signal: controller.signal })
      setReport(result)
      setView(VIEW.RESULT)
      window.scrollTo({ top: 0, behavior: 'auto' })
    } catch (caught) {
      if (caught?.name === 'AbortError') return
      setError(
        caught instanceof ApiError
          ? caught
          : new ApiError('Something went wrong while scanning this label.', {
              code: 'unexpected_error',
            }),
      )
    } finally {
      setIsScanning(false)
      abortRef.current = null
    }
  }, [file, isScanning])

  /** Abandon an in-flight scan. */
  const handleCancel = useCallback(() => {
    abortRef.current?.abort()
    setIsScanning(false)
  }, [])

  /** Clear everything and return to the capture screen. */
  const handleReset = useCallback(() => {
    abortRef.current?.abort()
    setFile(null)
    setReport(null)
    setError(null)
    setIsScanning(false)
    setView(VIEW.CAPTURE)
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [])

  return (
    <div className="app">
      <AppHeader onHome={view === VIEW.RESULT ? handleReset : null} />

      <main className="main" id="main">
        {view === VIEW.CAPTURE ? (
          <>
            {error && <ScanErrorNotice error={error} onDismiss={() => setError(null)} />}
            <CapturePanel
              file={file}
              previewUrl={previewUrl}
              isScanning={isScanning}
              onSelect={handleSelect}
              onScan={handleScan}
              onClear={() => handleSelect(null)}
            />
          </>
        ) : (
          <ResultScreen report={report} previewUrl={previewUrl} onScanAnother={handleReset} />
        )}
      </main>

      <footer className="footer">
        <p>
          System-generated preliminary result — subject to manual verification by
          the inspecting officer.
        </p>
      </footer>

      {isScanning && <LoadingOverlay onCancel={handleCancel} />}
    </div>
  )
}
