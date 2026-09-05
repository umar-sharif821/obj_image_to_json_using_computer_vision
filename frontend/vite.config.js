import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Vite configuration.
 *
 * `base: '/'` targets a root deployment, which is what Vercel and Netlify give
 * you by default. If you ever host this under a sub-path (GitHub Pages project
 * sites, for instance), change it to that path or the hashed asset URLs will
 * 404.
 */
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: 'dist',
    // Keeps the demo honest: a warning here means the bundle grew past what a
    // phone on venue wifi will fetch comfortably.
    chunkSizeWarningLimit: 400,
  },
  server: {
    // `host: true` binds every interface, so a phone on the same wifi can open
    // the dev server by the laptop's LAN address. Without it Vite listens on
    // localhost only and the phone gets a connection refused.
    host: true,
    port: 5173,
  },
  preview: {
    host: true,
    port: 4173,
  },
})
