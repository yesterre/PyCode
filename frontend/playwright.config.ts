import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../', import.meta.url))
const frontend = fileURLToPath(new URL('./', import.meta.url))
const python = process.platform === 'win32' ? '.\\.venv\\Scripts\\python.exe' : './.venv/bin/python'
const backendPort = process.env.PYCODE_E2E_BACKEND_PORT ?? '18000'
const frontendPort = process.env.PYCODE_E2E_FRONTEND_PORT ?? '15173'

export default defineConfig({
  testDir: './e2e',
  timeout: 60000,
  use: {baseURL: `http://127.0.0.1:${frontendPort}`, browserName: 'chromium', headless: true},
  webServer: [
    {command: `${python} -m uvicorn phase8_browser_server:app --app-dir tests --host 127.0.0.1 --port ${backendPort}`,
      cwd: root, url: `http://127.0.0.1:${backendPort}/health`, reuseExistingServer: false, timeout: 30000},
    {command: `npm run dev -- --port ${frontendPort} --strictPort`, env: {PYCODE_E2E_BACKEND_PORT: backendPort},
      cwd: frontend, url: `http://127.0.0.1:${frontendPort}`, reuseExistingServer: false, timeout: 30000},
  ],
})
