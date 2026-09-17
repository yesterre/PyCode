# PyCode Web Frontend

Use Node.js and npm in this directory:

```sh
npm install
npm run dev
```

The Vite dev server uses `http://127.0.0.1:5173`. It proxies `/api` requests, including SSE, to the FastAPI server at `http://127.0.0.1:8000`. Set `VITE_API_BASE_URL` only when the backend is served from a different origin; its value is the origin without `/api/v1`.

Verification commands:

```sh
npm run typecheck
npm test
npm run build
npm run e2e
```

The Playwright command starts a controlled FastAPI test server and the Vite dev server, then runs Chromium against their real HTTP and SSE endpoints. It requires the repository Python virtual environment and installed Playwright Chromium browser. It does not call a paid LLM or require PostgreSQL or Redis.
