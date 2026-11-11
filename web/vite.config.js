import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const backend = process.env.API_PROXY || 'http://127.0.0.1:8787'

export default defineConfig({
  plugins: [react()],
  base: './', // GitHub Pages serves the app from a sub-path
  server: { host: '127.0.0.1', proxy: { '/api': backend } },
  preview: { host: '127.0.0.1', proxy: { '/api': backend } },
})
