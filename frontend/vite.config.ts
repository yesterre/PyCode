import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { '/api': { target: `http://127.0.0.1:${process.env.PYCODE_E2E_BACKEND_PORT ?? '8000'}`, changeOrigin: true } } },
  test: { environment: 'jsdom', setupFiles: './src/test/setup.ts', css: true,
    exclude: ['e2e/**', 'node_modules/**'] },
})
