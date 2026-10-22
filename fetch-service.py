import os, re, json, socket, ipaddress, logging, secrets, base64, tempfile, shutil, subprocess, time, urllib.request, urllib.parse
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import yt_dlp
import importlib.util

TOKEN = os.environ.get("FETCH_TOKEN", "")
# Auth is fail-closed: with no FETCH_TOKEN the service refuses to serve,
# unless local development explicitly opts in with FETCH_ALLOW_NO_TOKEN=1.
ALLOW_NO_TOKEN = os.environ.get("FETCH_ALLOW_NO_TOKEN", "") == "1"

# Optional Netscape cookies for extractors that hit login/bot walls
# (notably YouTube from datacenter IPs). Provide via YT_COOKIES_B64
# (base64 of a cookies.txt) or YT_COOKIES (raw text). Materialized once
# at startup into a 0600 temp file; contents are never logged.
def _cookies_path():
    raw = os.environ.get("YT_COOKIES", "")
    b64 = os.environ.get("YT_COOKIES_B64", "")
    if b64:
        try:
            raw = base64.b64decode(b64, validate=True).decode("utf-8", "replace")
        except Exception:
            log.error("YT_COOKIES_B64 is not valid base64; cookies disabled")
            return None
    if not raw.strip():
        return None
    fd, path = tempfile.mkstemp(prefix="yt-cookies-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw)
    os.chmod(path, 0o600)
    entries = sum(1 for ln in raw.splitlines()
                  if ln.strip() and (not ln.startswith("#") or ln.startswith("#HttpOnly_")))
    log.info("extractor cookies configured (%d entries)", entries)
    return path


log = logging.getLogger("fetch-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

COOKIES_PATH = _cookies_path()


# ---------------------------------------------------------------------------
# Deno JS runtime for yt-dlp. Current yt-dlp needs a real JS runtime to
# extract YouTube's full format set (without one, formats silently go
# missing / SABR responses degrade). Wire the detected binary into yt-dlp
# via its "js_runtimes" param; when none is found, yt-dlp still gets its
# own default lookup and we log the degradation honestly at startup.
# Detection order: $DENO_BIN, PATH, then the conventional install spots
# (including ./.deno/bin next to this file, where the Render build step
# installs it — see deploy notes).
def _find_deno():
    env = (os.environ.get("DENO_BIN") or "").strip()
    if env:
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        log.warning("DENO_BIN=%s is not an executable file; ignoring it", env)
    found = shutil.which("deno")
    if found:
        return found
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, ".deno", "bin", "deno"),
                 os.path.expanduser("~/.deno/bin/deno")):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


DENO_PATH = _find_deno()
if DENO_PATH:
    log.info("yt-dlp JS runtime: deno at %s", DENO_PATH)
else:
    log.warning("no Deno JS runtime found; yt-dlp extraction runs degraded "
                "(YouTube formats may be missing). Install deno or set DENO_BIN.")


# ---------------------------------------------------------------------------
# gallery-dl fallback for photo/album posts — the class of content yt-dlp
# is weakest at (Instagram photos/albums, X media, Reddit galleries...).
# It runs in --dump-json mode, which only *lists* media URLs and never
# downloads bytes. Detection mirrors _find_deno ($GALLERY_DL_BIN, PATH).
def _find_gallery_dl():
    env = (os.environ.get("GALLERY_DL_BIN") or "").strip()
    if env:
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        log.warning("GALLERY_DL_BIN=%s is not an executable file; ignoring it", env)
    return shutil.which("gallery-dl")


GALLERY_DL = _find_gallery_dl()
if GALLERY_DL:
    log.info("gallery-dl fallback available at %s", GALLERY_DL)
else:
    log.info("gallery-dl not found; photo/album fallback disabled")

# Hosts gallery-dl serves well (suffix-matched on the registrable host).
GALLERY_HOSTS = ("instagram.com", "twitter.com", "x.com", "reddit.com",
                 "tumblr.com", "flickr.com", "imgur.com", "deviantart.com",
                 "pinterest.com")
GDL_MAX_LINKS = 12
# Time discipline (D-07 posture): the worker gives this service a 30s
# per-attempt window. Only start the fallback when the yt-dlp phase was
# fast, and cap the subprocess so the total stays inside that window.
GDL_TIMEOUT = 13
GDL_LATEST_START = 15


# ---------------------------------------------------------------------------
# bgutil PO Token provider — lets YouTube extraction work WITHOUT account
# cookies. The pip plugin (bgutil-ytdlp-pot-provider) asks a local HTTP
# server for BotGuard PO tokens; in the Docker deployment that server runs
# in the same container (see services/docker/). Wiring verified against
# the provider's v2.0.2 docs/source:
#   * plugin registers provider "bgutil:http"; its DEFAULT base URL is
#     http://127.0.0.1:4416, so strictly no yt-dlp config is needed — we
#     still pass base_url explicitly (extractor arg key
#     "youtubepot-bgutilhttp", value list) so the wiring is visible,
#     overridable via POT_PROVIDER_URL, and unit-testable.
#   * the plugin pings {base_url}/ping itself and degrades gracefully
#     (no token; extraction continues) when the server is down — so a
#     dead provider can never fake success here either: YouTube then
#     answers with its bot wall, mapped to needs_login below.
#   * PO tokens mitigate, not abolish, IP-based walls (upstream FAQ);
#     YT_COOKIES_* therefore stays honored as an optional fallback.
POT_PROVIDER_URL = (os.environ.get("POT_PROVIDER_URL") or "http://127.0.0.1:4416").rstrip("/")


def _pot_plugin_installed():
    try:
        return importlib.util.find_spec(
            "yt_dlp_plugins.extractor.getpot_bgutil_http") is not None
    except Exception:
        return False


POT_PLUGIN = _pot_plugin_installed()

# Cheap liveness gate: one GET /ping (1s timeout), cached for
# POT_PING_TTL seconds, so /fetch pays at most one probe per window.
# Any error simply reads as "down".
_pot_cache = {"up": None, "at": 0.0}
POT_PING_TTL = 15.0
POT_PING_TIMEOUT = 1.0


def pot_provider_up():
    now = time.monotonic()
    if _pot_cache["up"] is not None and now - _pot_cache["at"] < POT_PING_TTL:
        return _pot_cache["up"]
    up = False
    try:
        with urllib.request.urlopen(POT_PROVIDER_URL + "/ping",
                                    timeout=POT_PING_TIMEOUT) as r:
            up = r.status == 200
    except Exception:
        up = False
    _pot_cache.update(up=up, at=now)
    return up


if POT_PLUGIN:
    log.info("bgutil PO token plugin installed; provider URL %s", POT_PROVIDER_URL)
    if pot_provider_up():
        log.info("bgutil PO provider server reachable at startup")
    else:
        log.warning("bgutil PO provider server NOT reachable at startup; "
                    "YouTube tokens unavailable until it answers /ping "
                    "(re-checked per request, cached %ss)", POT_PING_TTL)
else:
    log.info("bgutil PO token plugin not installed; YouTube relies on "
             "cookies/default clients only")


def _is_youtube_url(url):
    if url.startswith("ytsearch"):
        return True
    try:
        h = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return (h == "youtu.be" or h == "youtube.com"
            or h.endswith(".youtube.com"))


def _gallery_host(host):
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    if h.startswith("pinterest."):
        return True
    return any(h == d or h.endswith("." + d) for d in GALLERY_HOSTS)


_VID_EXT = {"mp4", "mov", "webm", "mkv", "m4v"}
_AUD_EXT = {"mp3", "m4a", "ogg", "opus", "wav", "flac"}


def _gdl_kind(meta, url):
    ext = str(meta.get("extension") or "").lower()
    if not ext:
        ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lstrip(".").lower()
    if ext in _VID_EXT:
        return "video"
    if ext in _AUD_EXT:
        return "audio"
    return "image"  # gallery-dl is image-first; unknown exts land here


def gallery_extract(url):
    """gallery-dl --dump-json -> {"title","thumb","links"} or None.

    None means the fallback produced nothing usable (unsupported URL,
    login wall, timeout, unparseable output) — the caller must then
    surface yt-dlp's own verdict, never invent media.
    """
    if not GALLERY_DL:
        return None
    try:
        proc = subprocess.run([GALLERY_DL, "--dump-json", url],
                              capture_output=True, text=True, timeout=GDL_TIMEOUT)
    except Exception as e:
        log.warning("gallery-dl run failed (%s)", type(e).__name__)
        return None
    out = (proc.stdout or "").strip()
    if not out:
        log.info("gallery-dl empty output rc=%s", proc.returncode)
        return None
    try:
        data = json.loads(out)
    except Exception:
        log.warning("gallery-dl output unparseable")
        return None
    links, title, thumb = [], "", ""
    counts = {"image": 0, "video": 0, "audio": 0}
    word = {"image": "عکس", "video": "ویدئو", "audio": "فایل صوتی"}
    for msg in data if isinstance(data, list) else []:
        if not isinstance(msg, list) or len(msg) < 2:
            continue
        if msg[0] == -1 and isinstance(msg[1], dict):
            # extractor-level failure, e.g. a login wall: log it, keep going
            log.info("gallery-dl error %s: %s", msg[1].get("error"),
                     str(msg[1].get("message") or "")[:160])
            continue
        if not title and isinstance(msg[-1], dict):
            for key in ("title", "description", "content", "caption"):
                if isinstance(msg[-1].get(key), str) and msg[-1][key].strip():
                    title = msg[-1][key].strip()[:200]
                    break
        if msg[0] != 3 or len(msg) < 3 or not isinstance(msg[1], str):
            continue  # only Message.Url entries are direct media
        meta = msg[2] if isinstance(msg[2], dict) else {}
        kind = _gdl_kind(meta, msg[1])
        counts[kind] += 1
        links.append({"label": "%s %d" % (word[kind], counts[kind]),
                      "url": msg[1], "kind": kind})
        if kind == "image" and not thumb:
            thumb = msg[1]
        if len(links) >= GDL_MAX_LINKS:
            break
    if not links:
        return None
    return {"title": title, "thumb": thumb, "links": links}


app = FastAPI()

class Req(BaseModel):
    url: str


def _host_blocked(host):
    """True for loopback / private / link-local / reserved targets.

    Covers literal IPs, localhost-style names, and hostnames that resolve
    only to non-public addresses. Residual risk (documented): a public
    name that rebinds between this check and yt-dlp's own lookup (TOCTOU)
    is not fully mitigated without an egress proxy.
    """
    if not host:
        return True
    h = host.strip().lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost") or h.endswith(".local") or h.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return not ip.is_global
    except ValueError:
        pass
    if re.fullmatch(r"\d+", h):  # single-number host (decimal IP trick)
        return True
    try:
        infos = socket.getaddrinfo(h, None, proto=socket.IPPROTO_TCP)
        ips = {i[4][0] for i in infos}
        if ips and all(_ip_dangerous(x) for x in ips):
            return True
    except Exception:
        pass  # unresolvable: yt-dlp will fail on its own; not an SSRF pass
    return False


_RFC1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def _ip_dangerous(x):
    """Loopback / link-local / RFC1918 / unique-local only.

    Deliberately NOT 'not is_global': fake-IP DNS pools (198.18.0.0/15,
    used by common proxy tools) and CGNAT answers must not block every
    legitimate hostname in proxied environments.
    """
    try:
        ip = ipaddress.ip_address(x)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return True
    if any(ip in n for n in _RFC1918):
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_private and not ip.is_global:
        return True
    return False


def validate_url(url):
    """-> public host string, or None when the URL must be rejected."""
    if not url or len(url) > 2048:
        return None
    if not re.match(r"^https?://", url, re.I):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return None
    if parts.username or parts.password:
        return None
    host = parts.hostname or ""
    if _host_blocked(host):
        return None
    return host

def pick_formats(info):
    fmts = info.get("formats") or []
    links = []
    seen_h = set()
    # single-file (progressive) formats first: video+audio in one file
    singles = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none") and f.get("url")]
    singles.sort(key=lambda f: (f.get("height") or 0), reverse=True)
    for f in singles:
        h = f.get("height") or 0
        if h in seen_h: continue
        seen_h.add(h)
        links.append({"label": (str(h) + "p") if h else "ویدئو", "url": f["url"], "kind": "video"})
        if len(links) >= 3: break
    if not links:
        # No progressive (audio+video) format: YouTube serves most videos
        # adaptive-only. Offer the best video-only stream, honestly labeled
        # as silent, instead of degrading to an audio-only result.
        vids = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none") and f.get("url")]
        vids.sort(key=lambda f: (f.get("height") or 0), reverse=True)
        if vids:
            h = vids[0].get("height") or 0
            links.append({"label": ((str(h) + "p") if h else "ویدئو") + " (بدون صدا)", "url": vids[0]["url"], "kind": "video"})
        elif info.get("url"):
            links.append({"label": "ویدئو", "url": info["url"], "kind": "video"})
    # best audio-only
    auds = [f for f in fmts if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none") and f.get("url")]
    if auds:
        auds.sort(key=lambda f: (f.get("abr") or 0), reverse=True)
        links.append({"label": "فقط صدا", "url": auds[0]["url"], "kind": "audio"})
    return links

def extract(url):
    ea = {"youtube": {"player_client": ["android", "ios", "tv", "web"]}}
    if _is_youtube_url(url) and POT_PLUGIN:
        if pot_provider_up():
            # bgutil:http provider config (verified key/arg for v2.0.2);
            # values are lists, per yt-dlp extractor_args convention.
            ea["youtubepot-bgutilhttp"] = {"base_url": [POT_PROVIDER_URL]}
        else:
            log.warning("youtube fetch with PO provider down and no token; "
                        "bot wall (needs_login) is the likely outcome")
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 20, "retries": 2,
            "extractor_args": ea}
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    if DENO_PATH:
        opts["js_runtimes"] = {"deno": {"path": DENO_PATH}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def _elapsed(started):
    return time.monotonic() - started

@app.get("/health")
def health():
    return {"ok": True, "deno": DENO_PATH is not None, "gallery_dl": GALLERY_DL is not None,
            "po_provider": bool(POT_PLUGIN and pot_provider_up())}

@app.post("/fetch")
def fetch(req: Req, x_fetch_token: str = Header(default="")):
    if not TOKEN and not ALLOW_NO_TOKEN:
        log.error("FETCH_TOKEN is not set; refusing to serve (fail-closed)")
        raise HTTPException(status_code=503, detail="service misconfigured")
    if TOKEN and not secrets.compare_digest(x_fetch_token or "", TOKEN):
        raise HTTPException(status_code=403, detail="bad token")
    url = (req.url or "").strip()
    host = validate_url(url)
    if host is None:
        return {"ok": False, "code": "bad_url"}
    # Spotify: no direct audio; match by title via YouTube search
    if host == "open.spotify.com":
        try:
            oe = json.loads(urllib.request.urlopen(
                urllib.request.Request("https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe=""),
                                       headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read().decode())
            title = oe.get("title") or ""
            if not title:
                return {"ok": False, "code": "no_media"}
            info = extract("ytsearch1:" + title)
            entries = info.get("entries") or []
            if not entries:
                return {"ok": False, "code": "no_media"}
            info = entries[0]
            return {"ok": True, "title": title, "thumb": info.get("thumbnail") or oe.get("thumbnail_url") or "",
                    "links": pick_formats(info)}
        except Exception:
            return {"ok": False, "code": "fetch_failed"}
    started = time.monotonic()

    def _fail(code):
        """Failure exit for the generic path. On gallery-oriented hosts,
        give gallery-dl one chance before returning yt-dlp's verdict.
        A fallback failure never masks that verdict: the original code is
        returned, flagged with fallback_error — no fake media, ever."""
        if (GALLERY_DL and _gallery_host(host)
                and _elapsed(started) <= GDL_LATEST_START):
            g = gallery_extract(url)
            if g:
                log.info("fetch host=%s engine=gallery-dl links=%d", host, len(g["links"]))
                return {"ok": True, "title": g["title"], "thumb": g["thumb"],
                        "links": g["links"], "engine": "gallery-dl"}
            log.info("fetch host=%s code=%s gallery_dl=failed", host, code)
            return {"ok": False, "code": code, "fallback_error": "gallery_failed"}
        return {"ok": False, "code": code}

    try:
        info = extract(url)
    except Exception as e:
        msg = str(e).lower()
        # login-class: yt-dlp's PO-token/bot wall reads "Sign in to confirm
        # you're not a bot" — it names neither "login" nor "cookies", so it
        # is matched explicitly, else it would masquerade as fetch_failed.
        if ("login" in msg or "private" in msg or "cookies" in msg
                or "sign in" in msg or "not a bot" in msg):
            return _fail("needs_login")
        return _fail("fetch_failed")
    if not info:
        return _fail("fetch_failed")
    if info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if not entries:
            return _fail("no_media")
        info = entries[0]
    links = pick_formats(info)
    if not links:
        log.info("fetch host=%s code=no_media", host)
        return _fail("no_media")
    log.info("fetch host=%s code=ok links=%d", host, len(links))
    return {"ok": True, "title": info.get("title") or "", "thumb": info.get("thumbnail") or "", "links": links}
