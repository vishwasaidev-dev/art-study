"""Art Study — a deep-zoom study viewer for public-domain artworks.

Search an artist across the Met, Art Institute of Chicago, Cleveland and
Harvard, open any work in an OpenSeadragon viewer and zoom to the brush-
stroke. The server proxies every museum image request so Cloudflare, referer
and CORS never reach the browser.

Run locally with run_local.py (http://127.0.0.1:8730/), or deploy anywhere
that runs `uvicorn app:app` — set HARVARD_API_KEY as an env var to enable the
Harvard source.
"""
from __future__ import annotations

import base64
import os
import time
import json
import re
import threading
from pathlib import Path

import requests
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

try:
    import truststore                       # local AV TLS fix; harmless no-op elsewhere
    truststore.inject_into_ssl()
except Exception:
    pass

BASE = Path(__file__).resolve().parent
PROJECT = BASE.parent
PORT = int(os.environ.get("PORT", 8730))

_UA = "art-study/1.0 (personal study tool; vkarhade@gmail.com)"
BROWSERISH = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
              "Referer": "https://www.artic.edu/"}

_local = threading.local()


def _tls_session() -> requests.Session:
    """A requests.Session per thread (Session isn't thread-safe); pooled, with
    retries - the local AV's TLS interception drops handshakes to the museum
    CDNs under concurrent load (SSLEOFError)."""
    s = getattr(_local, "s", None)
    if s is None:
        from urllib3.util.retry import Retry
        s = requests.Session()
        s.headers["User-Agent"] = _UA
        retry = Retry(total=3, connect=3, read=2, backoff_factor=0.6,
                      status_forcelist=[500, 502, 503, 504],   # 429 handled in _fetch
                      allowed_methods=["GET"], raise_on_status=False)
        s.mount("https://", requests.adapters.HTTPAdapter(
            pool_connections=20, pool_maxsize=40, max_retries=retry))
        _local.s = s
    return s


S = _tls_session()   # module-thread session, for import-time / simple use


def _harvard_key() -> str | None:
    env = os.environ.get("HARVARD_API_KEY") or os.environ.get("Harvard_API_Key")
    if env:
        return env.strip()
    for p in (PROJECT / "latentbridge" / ".env", PROJECT / ".env", BASE / ".env"):
        if p.exists():
            m = re.search(r"[Hh]arvard[_ ]?API[_ ]?[Kk]ey\s*=\s*(\S+)",
                          p.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return m.group(1)
    return None


HARVARD_KEY = _harvard_key()


# --------------------------------------------------------------- ref encoding
def _enc(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _dec(ref: str) -> str:
    return base64.urlsafe_b64decode(ref + "=" * (-len(ref) % 4)).decode()


def _self(request: Request) -> str:
    # behind Heroku/other proxies the dyno sees http but the browser is on
    # https - a http:// tilesource URL is then blocked as mixed content
    host = request.headers.get("host", f"127.0.0.1:{PORT}")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    return f"{proto.split(',')[0].strip()}://{host}"


def _name_match(query: str, candidate: str) -> bool:
    """Loose artist match: the surname plus at least one more query token must
    appear in the candidate (which may carry middle names, dates, nationality
    - "Rembrandt Harmensz. van Rijn (Dutch, 1606-1669)")."""
    if not query:
        return True
    qt = [t for t in re.split(r"[^\w]+", query.lower()) if len(t) > 1]
    if not qt:
        return True
    c = candidate.lower()
    if qt[-1] not in c:                       # surname missing
        return False
    return sum(t in c for t in qt) >= min(2, len(qt))


# --------------------------------------------------------------- search
def _aic(artist: str, q: str, n: int):
    term = (artist or q or "").strip()
    r = _tls_session().get("https://api.artic.edu/api/v1/artworks/search", params={
        "q": term, "limit": n,
        "fields": "id,title,artist_title,date_display,image_id,is_public_domain,dimensions"},
        timeout=25)
    cfg = r.json().get("config", {})
    iiif = cfg.get("iiif_url", "https://www.artic.edu/iiif/2")
    out = []
    for a in r.json().get("data", []):
        if not a.get("is_public_domain") or not a.get("image_id"):
            continue
        if not _name_match(artist, a.get("artist_title") or ""):
            continue
        out.append({
            "title": a.get("title") or "Untitled", "artist": a.get("artist_title") or "",
            "date": a.get("date_display") or "", "museum": "Art Institute of Chicago",
            "source": "aic", "id": str(a["id"]), "key": f"aic:{a['id']}",
            "dims": a.get("dimensions") or "",
            "iiif": f"{iiif}/{a['image_id']}",
            "thumb": f"{iiif}/{a['image_id']}/full/400,/0/default.jpg",
            "page": f"https://www.artic.edu/artworks/{a['id']}"})
    return out


def _harvard(artist: str, q: str, n: int):
    if not HARVARD_KEY:
        return []
    p = {"apikey": HARVARD_KEY, "hasimage": 1, "imagepermissionlevel": 0,
         "size": n, "sort": "rank",
         "fields": "id,objectnumber,title,people,dated,images,copyright,accesslevel,url,dimensions"}
    if artist:
        p["person"] = artist
    else:
        p["keyword"] = q
    out = []
    for a in _tls_session().get("https://api.harvardartmuseums.org/object", params=p, timeout=25).json().get("records", []):
        if a.get("accesslevel") != 1 or a.get("copyright"):
            continue
        imgs = [i for i in (a.get("images") or []) if i.get("baseimageurl")]
        if not imgs:
            continue
        # prefer a full-resolution base (a plain nrs.harvard.edu/urn-3 id) over
        # the size-capped "_dynmc" derivative; else the biggest by pixels
        full = [i for i in imgs if "_dynmc" not in (i.get("baseimageurl") or "")]
        img = max(full or imgs, key=lambda i: (i.get("width") or 0) * (i.get("height") or 0))
        base = img["baseimageurl"]
        ppl = ", ".join(x.get("name", "") for x in (a.get("people") or []) if x.get("role") in (None, "Artist", "Primary"))
        if not _name_match(artist, ppl):
            continue
        out.append({
            "title": a.get("title") or "Untitled", "artist": ppl,
            "date": a.get("dated") or "", "museum": "Harvard Art Museums",
            "source": "harvard", "id": str(a["id"]), "key": f"harvard:{a['id']}",
            "dims": a.get("dimensions") or "",
            "iiif": base, "thumb": base + "/full/400,/0/default.jpg",
            "page": a.get("url") or ""})
    return out


def _met(artist: str, q: str, n: int):
    term = (artist or q or "").strip()
    r = _tls_session().get("https://collectionapi.metmuseum.org/public/collection/v1/search",
              params={"q": term, "hasImages": "true", "isPublicDomain": "true"}, timeout=15)
    ids = (r.json().get("objectIDs") or [])[:35]
    out = []
    for oid in ids:
        if len(out) >= n:
            break
        try:
            o = _tls_session().get(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}", timeout=8).json()
        except Exception:
            continue
        if not o.get("primaryImage") or not o.get("isPublicDomain"):
            continue
        if not _name_match(artist, o.get("artistDisplayName") or ""):
            continue
        out.append({
            "title": o.get("title") or "Untitled", "artist": o.get("artistDisplayName") or "",
            "date": o.get("objectDate") or "", "museum": "The Metropolitan Museum of Art",
            "source": "met", "id": str(oid), "key": f"met:{oid}",
            "dims": o.get("dimensions") or "",
            "image": o["primaryImage"], "thumb": o.get("primaryImageSmall") or o["primaryImage"],
            "page": o.get("objectURL") or ""})
    return out


def _cleveland(artist: str, q: str, n: int):
    term = (artist or q or "").strip()
    r = _tls_session().get("https://openaccess-api.clevelandart.org/api/artworks/", params={
        "q": term, "cc0": "1", "has_image": "1", "limit": n,
        "fields": "id,accession_number,title,creators,creation_date,images,measurements"}, timeout=25)
    out = []
    for a in r.json().get("data", []):
        im = a.get("images") or {}
        if not im.get("print") and not im.get("web"):
            continue
        creators = ", ".join(c.get("description", "") for c in (a.get("creators") or []))
        if not _name_match(artist, creators):
            continue
        # `print` is a ~3400px JPEG - instant; `full` is a 12000px .tif that has
        # to be downloaded whole and transcoded, too heavy for the live viewer.
        big = (im.get("print") or im.get("web"))["url"]
        out.append({
            "title": a.get("title") or "Untitled", "artist": creators,
            "date": a.get("creation_date") or "", "museum": "The Cleveland Museum of Art",
            "source": "cleveland", "id": str(a["id"]), "key": f"cleveland:{a['id']}",
            "dims": a.get("measurements") or "",
            "image": big, "thumb": (im.get("web") or im.get("print"))["url"],
            "page": f"https://www.clevelandart.org/art/{a.get('id')}"})
    return out


SOURCES = {"aic": _aic, "harvard": _harvard, "met": _met, "cleveland": _cleveland}


async def api_search(request: Request) -> JSONResponse:
    artist = request.query_params.get("artist", "").strip()
    q = request.query_params.get("q", "").strip()
    which = request.query_params.get("source", "all")
    per = int(request.query_params.get("limit", 40))
    if not artist and not q:
        return JSONResponse({"ok": False, "error": "give an artist or a search term"}, status_code=400)

    picks = list(SOURCES) if which == "all" else [w for w in which.split(",") if w in SOURCES]
    results, errors = [], {}

    def run_one(key):
        return key, SOURCES[key](artist, q, per)

    def gather_all():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=len(picks) or 1) as ex:
            futs = {ex.submit(run_one, k): k for k in picks}
            for fut in as_completed(futs, timeout=40):
                k = futs[fut]
                try:
                    _, rows = fut.result()
                    results.extend(rows)
                except Exception as exc:              # noqa: BLE001
                    errors[k] = re.sub(r"apikey=[^&\s]+", "apikey=***", str(exc))[:200]

    try:
        await run_in_threadpool(gather_all)
    except Exception as exc:                          # noqa: BLE001
        errors["_"] = str(exc)[:200]

    _prio = {"aic": 0, "cleveland": 1, "met": 2, "harvard": 3}
    results.sort(key=lambda r: _prio.get(r.get("source"), 9))
    _wire(results, _self(request), _is_local(request))
    return JSONResponse({"ok": True, "count": len(results), "results": results, "errors": errors})


def _is_local(request: Request) -> bool:
    h = request.headers.get("host", "")
    return h.startswith(("127.0.0.1", "localhost", "0.0.0.0"))


# On a datacenter host (Heroku) AIC's Cloudflare 403s image assets and Harvard
# 429s a shared IP - but a real browser on a home connection loads both fine.
# So when deployed, hand the browser direct museum URLs for AIC/Harvard tiles
# (AIC's info.json still goes through the proxy, keeping its real @id so tiles
# resolve straight to www.artic.edu). Cleveland/Met never send CORS and their
# plain CDNs don't block datacenters, so those stay fully proxied. Running
# locally, AIC's Cloudflare dislikes a localhost referer, so proxy everything.
def _wire(results, self, local):
    for r in results:
        src, iiif, thumb = r.get("source"), r.get("iiif"), r.get("thumb")
        if iiif and src == "harvard" and not local:
            r["tilesource"] = iiif.rstrip("/") + "/info.json"
            r["thumb"] = iiif.rstrip("/") + "/full/400,/0/default.jpg"
        elif iiif and src == "aic" and not local:
            r["tilesource"] = f"{self}/iiif/{_enc(iiif)}/info.json?keepid=1"
            r["thumb"] = iiif.rstrip("/") + "/full/400,/0/default.jpg"
        elif iiif:
            r["tilesource"] = f"{self}/iiif/{_enc(iiif)}/info.json"
            if thumb:
                r["thumb"] = f"{self}/img/{_enc(thumb)}"
        elif "image" in r:
            r["tilesource"] = {"type": "image", "url": f"{self}/img/{_enc(r['image'])}"}
            if thumb:
                r["thumb"] = f"{self}/img/{_enc(thumb)}"


# --------------------------------------------------------------- image proxy
# Harvard's image host rate-limits a shared server IP hard (429). Throttle our
# own requests to it: at most a few concurrent, spaced out.
_HOST_SEM = {"nrs.harvard.edu": threading.Semaphore(3)}
_HOST_MIN_GAP = {"nrs.harvard.edu": 0.12}
_HOST_LAST = {}
_HOST_LOCK = threading.Lock()


def _throttle(url: str):
    for host, gap in _HOST_MIN_GAP.items():
        if host in url:
            with _HOST_LOCK:
                wait = gap - (time.monotonic() - _HOST_LAST.get(host, 0))
                if wait > 0:
                    time.sleep(wait)
                _HOST_LAST[host] = time.monotonic()


def _fetch(url: str) -> requests.Response:
    hdr = BROWSERISH if ("artic.edu" in url) else {}
    sem = next((s for h, s in _HOST_SEM.items() if h in url), None)
    if sem:
        sem.acquire()
    try:
        last = None
        for attempt in range(4):
            try:
                _throttle(url)
                r = _tls_session().get(url, headers=hdr, timeout=45)
                if r.status_code == 429:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                return r
            except requests.exceptions.RequestException as e:   # SSLEOF etc.
                last = e
                time.sleep(0.7 * (attempt + 1))
        if last:
            raise last
        return r
    finally:
        if sem:
            sem.release()


_CACHE: dict[str, tuple[bytes, str]] = {}
_INFO_CACHE: dict[str, dict] = {}
_CACHE_MAX = 600


def _cache_put(key: str, body: bytes, ct: str):
    if len(_CACHE) > _CACHE_MAX:
        for k in list(_CACHE)[:150]:
            _CACHE.pop(k, None)
    _CACHE[key] = (body, ct)


async def iiif_info(request: Request) -> Response:
    ref = request.path_params["ref"]
    try:
        upstream = _dec(ref)
    except Exception:
        return PlainTextResponse("bad ref", status_code=400)
    info = _INFO_CACHE.get(upstream)
    if info is None:
        r = await run_in_threadpool(_fetch, upstream.rstrip("/") + "/info.json")
        if r.status_code != 200:
            return PlainTextResponse(f"upstream {r.status_code}", status_code=502)
        info = r.json()
        if len(_INFO_CACHE) < 2000:
            _INFO_CACHE[upstream] = info
    info = dict(info)
    if request.query_params.get("keepid") != "1":
        info["@id"] = f"{_self(request)}/iiif/{ref}"      # tiles route back through the proxy
        if "id" in info:
            info["id"] = info["@id"]
    return JSONResponse(info, headers={"Access-Control-Allow-Origin": "*"})


def _to_jpeg(raw: bytes, max_side: int = 6000) -> bytes:
    """Browsers can't paint TIFF (Cleveland's `full` is a 12000px .tif) - decode
    with Pillow and hand back a large JPEG."""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im = im.convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


async def _proxy_bytes(request: Request, upstream: str) -> Response:
    hit = _CACHE.get(upstream)
    if hit:
        body, ct = hit
    else:
        r = await run_in_threadpool(_fetch, upstream)
        if r.status_code != 200:
            return PlainTextResponse(f"upstream {r.status_code}", status_code=502)
        body = r.content
        ct = r.headers.get("content-type", "image/jpeg")
        if "tif" in ct or upstream.lower().endswith((".tif", ".tiff")):
            try:
                body = await run_in_threadpool(_to_jpeg, body)
                ct = "image/jpeg"
            except Exception:
                return PlainTextResponse("could not decode upstream image", status_code=502)
        _cache_put(upstream, body, ct)
    return Response(body, media_type=ct,
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"})


async def iiif_tile(request: Request) -> Response:
    ref, tail = request.path_params["ref"], request.path_params["tail"]
    try:
        upstream = _dec(ref).rstrip("/") + "/" + tail
    except Exception:
        return PlainTextResponse("bad ref", status_code=400)
    return await _proxy_bytes(request, upstream)


async def img_proxy(request: Request) -> Response:
    try:
        upstream = _dec(request.path_params["ref"])
    except Exception:
        return PlainTextResponse("bad ref", status_code=400)
    return await _proxy_bytes(request, upstream)


# --------------------------------------------------------------- full record
def _first(*vals):
    for v in vals:
        if v:
            return v
    return ""


def _detail_aic(oid: str) -> dict:
    f = ("title,artist_display,date_display,medium_display,dimensions,credit_line,"
         "inscriptions,provenance_text,publication_history,exhibition_history,"
         "description,short_description,classification_title,place_of_origin,"
         "style_title,department_title,main_reference_number,id")
    a = _tls_session().get(f"https://api.artic.edu/api/v1/artworks/{oid}",
                           params={"fields": f}, timeout=20).json().get("data", {})
    ad = (a.get("artist_display") or "").split("\n")
    artist = ad[0].strip()
    bio = " ".join(x.strip() for x in ad[1:]).strip()
    return {
        "title": a.get("title"), "artist": artist, "artist_bio": bio, "date": a.get("date_display"),
        "medium": a.get("medium_display"), "dims": a.get("dimensions"),
        "credit": a.get("credit_line"), "accession": a.get("main_reference_number"),
        "classification": a.get("classification_title"), "culture": a.get("place_of_origin"),
        "period": a.get("style_title"), "department": a.get("department_title"),
        "inscriptions": a.get("inscriptions"),
        "description": _html_to_text(_first(a.get("description"), a.get("short_description"))),
        "provenance": _html_to_text(a.get("provenance_text")),
        "exhibitions": _html_to_text(a.get("exhibition_history")),
        "literature": _html_to_text(a.get("publication_history")),
        "museum": "Art Institute of Chicago", "city": "Chicago",
        "page": f"https://www.artic.edu/artworks/{oid}"}


def _detail_met(oid: str) -> dict:
    o = _tls_session().get(
        f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}", timeout=15).json()
    return {
        "title": o.get("title"), "artist": _first(o.get("artistDisplayName")),
        "artist_bio": o.get("artistDisplayBio"), "date": o.get("objectDate"),
        "medium": o.get("medium"), "dims": o.get("dimensions"), "credit": o.get("creditLine"),
        "accession": o.get("accessionNumber"), "classification": o.get("classification"),
        "culture": o.get("culture"), "period": _first(o.get("period"), o.get("dynasty")),
        "department": o.get("department"), "gallery": o.get("GalleryNumber"),
        "description": "", "museum": "The Metropolitan Museum of Art", "city": "New York",
        "page": o.get("objectURL")}


def _detail_cleveland(oid: str) -> dict:
    a = _tls_session().get(
        f"https://openaccess-api.clevelandart.org/api/artworks/{oid}", timeout=20).json().get("data", {})
    prov = "; ".join(p.get("description", "") for p in (a.get("provenance") or []))
    return {
        "title": a.get("title"),
        "artist": ", ".join(c.get("description", "") for c in (a.get("creators") or [])),
        "date": a.get("creation_date"), "medium": a.get("technique"),
        "dims": a.get("measurements"), "credit": a.get("creditline"),
        "accession": a.get("accession_number"), "classification": a.get("type"),
        "culture": ", ".join(a.get("culture") or []), "department": a.get("department"),
        "inscriptions": a.get("inscriptions") and "; ".join(
            i.get("inscription", "") for i in a["inscriptions"]),
        "description": _html_to_text(_first(a.get("wall_description"), a.get("digital_description"),
                                            a.get("description"))),
        "fun_fact": a.get("did_you_know"), "provenance": prov,
        "museum": "The Cleveland Museum of Art", "city": "Cleveland",
        "page": f"https://www.clevelandart.org/art/{oid}"}


def _detail_harvard(oid: str) -> dict:
    if not HARVARD_KEY:
        return {}
    f = ("title,people,dated,medium,technique,dimensions,creditline,description,labeltext,"
         "provenance,culture,period,classification,department,division,accessionyear,"
         "accessionmethod,contextualtext,objectnumber,url,contact")
    a = _tls_session().get(f"https://api.harvardartmuseums.org/object/{oid}",
                           params={"apikey": HARVARD_KEY, "fields": f}, timeout=20).json()
    ctx = " ".join(c.get("text", "") for c in (a.get("contextualtext") or []))
    return {
        "title": a.get("title"),
        "artist": ", ".join(p.get("displayname", p.get("name", "")) for p in (a.get("people") or [])
                            if p.get("role") in (None, "Artist", "Primary")),
        "date": a.get("dated"), "medium": _first(a.get("medium"), a.get("technique")),
        "dims": a.get("dimensions"), "credit": a.get("creditline"),
        "accession": a.get("objectnumber"), "classification": a.get("classification"),
        "culture": a.get("culture"), "period": a.get("period"), "department": a.get("department"),
        "description": _html_to_text(_first(a.get("description"), a.get("labeltext"))),
        "context": _html_to_text(ctx), "provenance": _html_to_text(a.get("provenance")),
        "museum": "Harvard Art Museums", "city": "Cambridge, MA",
        "page": a.get("url")}


_DETAIL = {"aic": _detail_aic, "met": _detail_met, "cleveland": _detail_cleveland,
           "harvard": _detail_harvard}


def _html_to_text(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+\n", "\n", s).strip()


def _citation(d: dict) -> str:
    # Chicago-ish: Artist, "Title," date, medium, dimensions (Museum, City,
    # accession no.), URL.
    clean = lambda s: re.sub(r"\s+", " ", str(s)).strip()
    bits = []
    if d.get("artist"):
        bits.append(clean(d["artist"]).rstrip(". "))
    if d.get("title"):
        bits.append(f'“{clean(d["title"]).rstrip(". ")},”')
    if d.get("date"):
        bits.append(f'{d["date"]}.')
    tail = []
    if d.get("medium"):
        tail.append(d["medium"].rstrip(". "))
    if d.get("dims"):
        tail.append(d["dims"].rstrip(". "))
    paren = ", ".join(x for x in [d.get("museum"), d.get("city"),
                                  f"accession no. {d['accession']}" if d.get("accession") else ""] if x)
    line = " ".join(bits)
    if tail:
        line += " " + ", ".join(tail) + "."
    if paren:
        line += f" ({paren})."
    if d.get("page"):
        line += f" {d['page']}."
    return line.strip()


async def api_detail(request: Request) -> JSONResponse:
    src = request.query_params.get("source", "")
    oid = request.query_params.get("id", "")
    fn = _DETAIL.get(src)
    if not fn or not oid:
        return JSONResponse({"ok": False, "error": "source + id required"}, status_code=400)
    try:
        d = await run_in_threadpool(fn, oid)
    except Exception as exc:                              # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=502)
    d = {k: v for k, v in d.items() if v}
    d["citation"] = _citation(d)
    return JSONResponse({"ok": True, "detail": d})


async def api_versions(request: Request) -> JSONResponse:
    """Other impressions / versions of the same work across all collections."""
    title = request.query_params.get("title", "").strip()
    ex_key = request.query_params.get("exclude", "")
    if len(title) < 4:
        return JSONResponse({"ok": True, "results": []})
    core = re.split(r"[,(]| from | also known", title)[0].strip()

    def run():
        from concurrent.futures import ThreadPoolExecutor
        rows = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            for got in ex.map(lambda f: _safe(f, "", core, 6), SOURCES.values()):
                rows += got
        return rows

    rows = await run_in_threadpool(run)
    seen, out = set(), []
    for r in rows:
        if r["key"] == ex_key or r["key"] in seen:
            continue
        if core.lower()[:12] not in (r["title"] or "").lower():
            continue
        seen.add(r["key"])
        out.append(r)
    _wire(out, _self(request), _is_local(request))
    return JSONResponse({"ok": True, "results": out[:12]})


def _safe(fn, artist, q, n):
    try:
        return fn(artist, q, n)
    except Exception:
        return []


# --------------------------------------------------------------- study session
# On an ephemeral host (Heroku) this file resets when the dyno cycles - the
# client also mirrors the session to localStorage, so nothing is really lost.
SESSION_FILE = Path(os.environ.get("SESSION_FILE") or (BASE / "study_session.json"))
_SESSION_LOCK = threading.Lock()


def _load_session() -> dict:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"works": {}, "sets": {}}


async def api_session_get(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "session": _load_session()})


async def api_session_put(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "json body required"}, status_code=400)
    if not isinstance(body, dict) or "works" not in body:
        return JSONResponse({"ok": False, "error": "bad shape"}, status_code=400)
    try:
        with _SESSION_LOCK:
            SESSION_FILE.write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return JSONResponse({"ok": True, "persisted": False})   # client keeps its localStorage copy
    return JSONResponse({"ok": True, "persisted": True})


async def index(request: Request) -> Response:
    page = BASE / "web" / "index.html"
    if not page.exists():
        return PlainTextResponse("art-study UI missing", status_code=500)
    return HTMLResponse(page.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "app": "art-study", "harvard": bool(HARVARD_KEY)})


def build_app() -> Starlette:
    return Starlette(routes=[
        Route("/", index),
        Route("/health", health),
        Route("/api/search", api_search),
        Route("/api/detail", api_detail),
        Route("/api/versions", api_versions),
        Route("/api/session", api_session_get, methods=["GET"]),
        Route("/api/session", api_session_put, methods=["PUT", "POST"]),
        Route("/iiif/{ref}/info.json", iiif_info),
        Route("/iiif/{ref}/{tail:path}", iiif_tile),
        Route("/img/{ref}", img_proxy),
        Mount("/static", StaticFiles(directory=str(BASE / "web"))),
    ])


app = build_app()
