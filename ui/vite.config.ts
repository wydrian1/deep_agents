/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The production build is written into the Python package, which serves it at "/".
// In development (`npm run dev`) the API is proxied to a locally running service.
const api = process.env.DEEP_AGENTS_API || 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  build: { outDir: '../src/deep_agents_app/static/app', emptyOutDir: true, sourcemap: false },
  server: { proxy: { '/api': api, '/auth': api, '/healthz': api } },
  test: { environment: 'jsdom', include: ['src/**/*.test.ts', 'src/**/*.test.tsx'] },
});
