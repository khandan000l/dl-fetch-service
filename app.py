import os, re, json, urllib.request, urllib.parse
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import yt_dlp

TOKEN = os.environ.get("FETCH_TOKEN", "")

app = FastAPI()

class Req(BaseModel):
    url: str

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
    if not links and info.get("url"):
        links.append({"label": "ویدئو", "url": info["url"], "kind": "video"})
    # best audio-only
    auds = [f for f in fmts if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none") and f.get("url")]
    if auds:
        auds.sort(key=lambda f: (f.get("abr") or 0), reverse=True)
        links.append({"label": "فقط صدا", "url": auds[0]["url"], "kind": "audio"})
    return links

def extract(url):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 20, "retries": 2}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/fetch")
def fetch(req: Req, x_fetch_token: str = Header(default="")):
    if TOKEN and x_fetch_token != TOKEN:
        raise HTTPException(status_code=403, detail="bad token")
    url = (req.url or "").strip()
    if not url:
        return {"ok": False, "code": "bad_url"}
    # Spotify: no direct audio; match by title via YouTube search
    if "open.spotify.com" in url:
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
        return {"ok": False, "code": "no_media"}
    return {"ok": True, "title": info.get("title") or "", "thumb": info.get("thumbnail") or "", "links": links}
