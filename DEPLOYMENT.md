# Deployment

Two services: **backend on Render**, **frontend on Vercel**, **AI inference via Groq**.

## 1. Push to GitHub

Keep `backend/.env` out of Git (`.gitignore` already excludes it).

## 2. Backend — Render

Create a **Web Service** from the GitHub repo. The included `render.yaml` works as a Blueprint, or configure manually:

| Setting | Value |
|---|---|
| Runtime | Python |
| Root directory | `backend` |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check | `/health` |

### Environment variables (set in Render)

| Variable | Value | Notes |
|---|---|---|
| `GROQ_API_KEY` | your key | **Required.** Server-side only. |
| `GROQ_VISION_MODEL` | `qwen/qwen3.8-27b` | Or any Groq vision model. |
| `GROQ_TEXT_MODEL` | `qwen/qwen3.8-27b` | Or any Groq text model. |
| `FRONTEND_ORIGIN` | `https://your-app.vercel.app` | Comma-separated if multiple. |
| `REVIEWER_TOKEN` | a secret string | Needed for manual-review actions. |
| `GROQ_MAX_RETRIES` | `3` | |
| `RATE_LIMIT_PER_10_MIN` | `20` | Per-IP. Raise for demos. |

The free Render tier's filesystem is ephemeral: the claim database resets on each deploy. For persistence, attach a Render disk and set `DATA_DIR=/var/data`.

### Verify

```bash
curl https://YOUR-SERVICE.onrender.com/health
# → {"status":"ok", "groq_configured":true, ...}
```

## 3. Frontend — Vercel

Import the repo into Vercel. Set **Root Directory** to `frontend`. Vercel detects Next.js automatically.

### Environment variable

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://YOUR-SERVICE.onrender.com` |

### Disable Deployment Protection

In the Vercel project: **Settings → Deployment Protection → turn off Vercel Authentication** for the production deployment. Without this, evaluators see a "You Need Access" wall.

### Verify

Open the Vercel URL in an incognito/private browser window. You should see the claim desk, a green "API online" dot, and sample claims.

## 4. CORS

The backend allows `*.vercel.app` by default (via `allow_origin_regex`). If you use a custom domain, add it to `FRONTEND_ORIGIN`.

## 5. Updating

Push to `main`. Render and Vercel both auto-deploy from the default branch.
