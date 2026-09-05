import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './styles.css'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

/*
 * Register the service worker that makes the app installable.
 *
 * Only in a production build: during development Vite serves modules that must
 * never be cached, and a stale worker there produces confusing "my edit didn't
 * apply" bugs. Registration is also deferred to the load event so it never
 * competes with the first paint on a phone.
 */
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((error) => {
      // An unavailable worker costs offline caching and nothing else, so a
      // failure here must never take the app down with it.
      console.warn('Service worker registration failed:', error)
    })
  })
}
