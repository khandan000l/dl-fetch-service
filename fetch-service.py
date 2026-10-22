import os, re, json, socket, ipaddress, logging, secrets, base64, tempfile, urllib.request, urllib.parse
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import yt_dlp

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
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 20, "retries": 2,
            "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv", "web"]}}}
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

@app.get("/health")
def health():
    return {"ok": True}

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
    try:
        info = extract(url)
    except Exception as e:
        msg = str(e)
        if "login" in msg.lower() or "private" in msg.lower() or "cookies" in msg.lower():
            return {"ok": False, "code": "needs_login"}
        return {"ok": False, "code": "fetch_failed"}
    if not info:
        return {"ok": False, "code": "fetch_failed"}
    if info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if not entries:
            return {"ok": False, "code": "no_media"}
        info = entries[0]
    links = pick_formats(info)
    if not links:
        log.info("fetch host=%s code=no_media", host)
        return {"ok": False, "code": "no_media"}
    log.info("fetch host=%s code=ok links=%d", host, len(links))
    return {"ok": True, "title": info.get("title") or "", "thumb": info.get("thumbnail") or "", "links": links}
