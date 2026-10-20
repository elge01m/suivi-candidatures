// SSR assertions need TSX compilation, not a browser dependency pre-bundle.
import path from 'node:path'
import os from 'node:os'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

export function createRenderServer(name) {
  return createServer({
    configFile: false, plugins: [react()], appType: 'custom',
    cacheDir: path.join(process.env.E2E_TEMP ?? os.tmpdir(), 'inf4173-render-cache', `${name}-${process.pid}`),
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  })
}
