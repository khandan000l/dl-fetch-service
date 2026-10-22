# Render switch: fetch service → Docker runtime + bgutil PO tokens

Goal: YouTube extraction **without account cookies**. The bgutil PO-token
provider is two parts — a yt-dlp plugin (pip) and a Node HTTP server that
mints BotGuard tokens. The server cannot run on Render's native Python
runtime, so the service moves to a single Docker image:
Python (fetch service) + Deno (yt-dlp JS runtime) + Node (provider,
loopback :4416 only) + the plugin. Cookies (`YT_COOKIES_*`) remain an
optional fallback — with PO tokens live, the owner's pending cookies.txt
is **no longer on the critical path**.

All steps below are dashboard/GitHub actions for the parent. Nothing here
has been deployed yet.

---

## 0. Pre-flight (before touching anything)

- [ ] In Render → `dl-fetch` service → Settings, **note the current
  Build Command and Start Command verbatim** (needed only for rollback).
- [ ] In Render → Environment, confirm the env vars present
  (`FETCH_TOKEN` must exist; `YT_COOKIES_B64`/`YT_COOKIES` if set;
  `FETCH_ALLOW_NO_TOKEN` must **not** be set in production).

## 1. GitHub repo changes — `khandan000l/dl-fetch-service`

Same upload flow as previous deploys (web upload + rename where needed).
Build context is the repo root; after this change it must contain
`Dockerfile`, `entrypoint.sh`, `requirements.txt`, `app.py`.

| Workspace file (source of truth) | Repo file | Action |
|---|---|---|
| `novafly-fixed/services/fetch-service.py` | `app.py` | **replace** (PO wiring + `/health.po_provider`) |
| `novafly-fixed/services/docker/Dockerfile` | `Dockerfile` | **add** (repo root) |
| `novafly-fixed/services/docker/entrypoint.sh` | `entrypoint.sh` | **add** (repo root) |
| `novafly-fixed/services/docker/requirements.txt` | `requirements.txt` | **replace** (adds `uvicorn`, `gallery-dl`, `bgutil-ytdlp-pot-provider==2.0.2`) |
| `novafly-fixed/services/docker/.dockerignore` | `.dockerignore` | **add** (optional but recommended) |

Commit on `main`. (The Dockerfile clones the bgutil server itself at tag
`2.0.2` during the build — no bgutil files go into the repo. The plugin
pin `==2.0.2` and the server tag are a matched pair; bump them together.)

## 2. Render dashboard: switch the runtime

1. Open the `dl-fetch` service → **Settings → Runtime** and switch it
   from **Python 3** to **Docker**.
   - Render detects the `Dockerfile` at the repo root.
   - **Build Command / Start Command no longer apply** under the Docker
     runtime — they are ignored, not migrated. Process startup is owned
     by the image (`ENTRYPOINT` → provider supervisor + uvicorn). Leave
     the optional **Docker Command** field **empty** so the image's
     entrypoint is used.
2. **Environment variables carry over unchanged** across a runtime
   switch — verify in the Environment tab after switching:
   `FETCH_TOKEN` (required; service is fail-closed without it),
   `YT_COOKIES_B64` / `YT_COOKIES` (optional fallback, may stay or be
   removed), `DENO_BIN` / `GALLERY_DL_BIN` / `POT_PROVIDER_URL`
   (do **not** set — image defaults are correct: deno and gallery-dl
   are on PATH, provider is `http://127.0.0.1:4416`).
   `PORT` is injected by Render automatically.
3. (Optional) Settings → Health Check path: `/health`.
4. **Manual Deploy → Deploy latest commit** and watch the logs. The
   first Docker build is slow (Node download + `npm ci` + possible
   node-canvas compile) — several minutes is normal.

## 3. First-deploy verification

Startup logs should show, in order:

```
[bgutil] Started POT server (v2.0.2) on address [::1]:4416, 127.0.0.1:4416
[entrypoint] PO provider is up
yt-dlp JS runtime: deno at /usr/local/bin/deno
gallery-dl fallback available at /usr/local/bin/gallery-dl
bgutil PO token plugin installed; provider URL http://127.0.0.1:4416
bgutil PO provider server reachable at startup
```

Then:

- `GET https://dl-fetch.onrender.com/health` →
  `{"ok": true, "deno": true, "gallery_dl": true, "po_provider": true}`
  (any `false` = that layer failed; the log lines above say which).
- Live smoke through the worker/mini app (not curl — the worker holds
  the token):
  1. a **YouTube** link → media links, **not** `needs_login`;
  2. an **Instagram photo** post → gallery-dl path still works;
  3. any non-YouTube link → unchanged behavior.
- If the first YouTube attempt right after the deploy/idle returns
  `needs_login` once, retry it — see the cold-start caveat below before
  concluding anything is broken.

## 4. Free-tier caveats (plainly)

- **512 MB RAM is shared** by uvicorn/yt-dlp, the Node provider
  (BotGuard minting can spike ~150–250 MB), and Deno subprocesses
  during YouTube extraction. It fits, but many *simultaneous* heavy
  YouTube extractions are the pressure point. If the provider is
  OOM-killed, its supervisor restarts it and the service keeps serving
  other platforms; YouTube degrades to `needs_login` until it's back.
- **Spin-down (~15 min idle) + cold provider:** after idle, the first
  request pays the container cold start, and the first token mint can
  exceed the plugin's 20 s `/get_pot` timeout while BotGuard warms up.
  Expect a possible one-off failure on the very first YouTube fetch
  after idle; a retry seconds later succeeds. Inherent to the free
  tier — not a defect.
- **PO tokens mitigate, not abolish, IP reputation:** upstream is
  explicit that a token does not guarantee bypassing bot checks on a
  flagged datacenter IP. If Render's egress IP is hard-flagged,
  YouTube may still demand login — the service then returns the honest
  `needs_login`, and `YT_COOKIES_B64` (throwaway account) remains the
  fallback lever. No code path fakes media.
- Image is linux/amd64 (Render's platform). Build cache makes later
  deploys fast unless `requirements.txt`/`Dockerfile` change.

## 5. Rollback

1. Render → Settings → Runtime: switch back to **Python 3**.
2. Restore the pre-switch **Build Command** and **Start Command** noted
   in step 0 (build: the previous `pip install -r requirements.txt`
   + Deno-install command; start: the previous `uvicorn app:app …`).
3. In GitHub, restore `app.py` and `requirements.txt` from the commit
   **before** this switch (git history keeps them; the Docker files can
   stay in the repo unused), then Manual Deploy.
4. Verify `/health` answers (it will no longer carry `po_provider`)
   and a YouTube link returns to the pre-switch behavior
   (`needs_login` until cookies are provided).
