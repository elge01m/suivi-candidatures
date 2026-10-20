// Separate ports and proxy: the E2E browser can never reach the application backend.
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  cacheDir: process.env.E2E_TEMP ? `${process.env.E2E_TEMP}/vite-cache` : 'node_modules/.vite-e2e',
  server: {
    host: '127.0.0.1', port: 15173, strictPort: true,
    proxy: { '/api': { target: 'http://127.0.0.1:18000', changeOrigin: true } },
  },
})
