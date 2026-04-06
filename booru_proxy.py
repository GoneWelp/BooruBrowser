"""
Booru image proxy — run this once, keep it open while using the tool.
Requires Python 3 (no extra installs needed).

HOW TO RUN:
  Windows:  python booru_proxy.py
  Mac/Linux: python3 booru_proxy.py

Keep the window open while using the browser tool. Stop with Ctrl+C.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen, Request
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.error import URLError, HTTPError
import json, os, sys, threading, time

PORT = 8765

# ── Credentials ──────────────────────────────────────────────────────────────
DB_LOGIN = ""
DB_API_KEY = ""

GB_USER_ID = ""
GB_API_KEY = ""

E621_LOGIN = ""
E621_API_KEY = ""

def _load_credentials():
    global DB_LOGIN, DB_API_KEY, GB_USER_ID, GB_API_KEY, E621_LOGIN, E621_API_KEY
    if len(sys.argv) == 3:
        DB_LOGIN, DB_API_KEY = sys.argv[1], sys.argv[2]
        print(f"[auth] Credentials from command line (login={DB_LOGIN!r})")
        return
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for search in [script_dir, os.getcwd()]:
        p = os.path.join(search, "auth.json")
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
                DB_LOGIN = d.get("login", "")
                DB_API_KEY = d.get("api_key", "")
                GB_USER_ID = d.get("gb_user_id", "")
                GB_API_KEY = d.get("gb_api_key", "")
                E621_LOGIN = d.get("e621_login", "")
                E621_API_KEY = d.get("e621_api_key", "")
                if DB_LOGIN and DB_API_KEY:
                    print(f"[auth] Danbooru credentials loaded (login={DB_LOGIN!r})")
                if GB_USER_ID and GB_API_KEY:
                    print(f"[auth] Gelbooru credentials loaded (user_id={GB_USER_ID!r})")
                if E621_LOGIN and E621_API_KEY:
                    print(f"[auth] e621 credentials loaded (login={E621_LOGIN!r})")
                return
            except Exception as e:
                print(f"[auth] WARNING: could not read {p}: {e}")

def _db_auth_params():
    if DB_LOGIN and DB_API_KEY: return {"login": DB_LOGIN, "api_key": DB_API_KEY}
    return {}

def _gb_auth_params():
    if GB_USER_ID and GB_API_KEY: return {"api_key": GB_API_KEY, "user_id": GB_USER_ID}
    return {}

def _e621_auth_params():
    if E621_LOGIN and E621_API_KEY: return {"login": E621_LOGIN, "api_key": E621_API_KEY}
    return {}

def _test_credentials():
    if not (DB_LOGIN and DB_API_KEY): return
    url = "https://danbooru.donmai.us/profile.json?" + urlencode(_db_auth_params())
    print("[auth] Testing credentials...")
    try:
        d = json.loads(fetch_api(url)[0])
        print(f"[auth] OK — logged in as {d.get('name')!r}\n")
    except Exception as e:
        print(f"[auth] !! {e}\n")

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def fetch_api(url):
    req = Request(url)
    req.add_header("User-Agent", "BooruArtistBrowser/1.0 (Local Reference Tool)")
    with urlopen(req, timeout=15) as r:
        return r.read(), dict(r.headers)

# ── Caches ────────────────────────────────────────────────────────────────────
_IMAGE_CACHE = {}
_META_CACHE  = {}
_HOVER_CACHE = {}
_cache_lock  = threading.Lock()
MAX_CACHE = 2000

_DISK_CACHE_DIR = None

def _disk_cache_init():
    global _DISK_CACHE_DIR
    _DISK_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tag_cache")
    os.makedirs(_DISK_CACHE_DIR, exist_ok=True)

def _disk_cache_path(source, letter, tag_type="artist"):
    prefix = "char_" if tag_type == "character" else ""
    return os.path.join(_DISK_CACHE_DIR, f"{source}_{prefix}{letter.upper()}.json")

def _disk_cache_read(source, letter, tag_type="artist"):
    p = _disk_cache_path(source, letter, tag_type)
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict) and obj.get("complete") and isinstance(obj.get("data"), list):
                return obj["data"]
            print(f"[cache] stale/incomplete cache for {source}/{letter} — deleting")
            try: os.remove(p)
            except Exception: pass
    except Exception:
        pass
    return None

def _disk_cache_write(source, letter, data, tag_type="artist"):
    p = _disk_cache_path(source, letter, tag_type)
    tmp = p + ".tmp"
    try:
        envelope = {"complete": True, "count": len(data), "data": data}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(envelope, f)
        os.replace(tmp, p)
        print(f"[cache] saved {source}/{letter} ({len(data)} {tag_type}s)")
    except Exception as e:
        print(f"[cache] write error: {e}")
        try: os.remove(tmp)
        except Exception: pass

def _cache_set(d, key, val):
    with _cache_lock:
        if len(d) >= MAX_CACHE: d.pop(next(iter(d)))
        d[key] = val

def _cache_get(d, key):
    with _cache_lock: return d.get(key)

# ── Danbooru ──────────────────────────────────────────────────────────────────
def _db_fetch_posts(artist, limit=1):
    for tags, is_safe in [(artist + " rating:general", True), (artist + " rating:sensitive", False), (artist, False)]:
        p = {"tags": tags, "limit": str(limit), "only": "id,preview_file_url,large_file_url,file_url,rating,media_asset,tag_string_copyright"}
        p.update(_db_auth_params())
        try:
            raw, _ = fetch_api("https://danbooru.donmai.us/posts.json?" + urlencode(p))
            posts = json.loads(raw)
            if posts: return posts, is_safe
        except Exception as e: print(f"[db] error for {tags!r}: {e}")
    return [], False

def _db_post_urls(post):
    ma = post.get("media_asset") or {}
    by_type = {v.get("type"): v.get("url") for v in (ma.get("variants") or []) if v.get("url")}
    urls = []
    for want in ("sample", "720x720", "360x360", "180x180"):
        if want in by_type: urls.append(by_type[want])
    for key in ("preview_file_url", "large_file_url", "file_url"):
        u = post.get(key)
        if u and u not in urls: urls.append(u)
    return urls

# ── Gelbooru ──────────────────────────────────────────────────────────────────
_GB_LOCK = threading.Lock()
_last_gb_req = 0.0

def _wait_gb_rate_limit():
    global _last_gb_req
    with _GB_LOCK:
        now = time.time()
        if now - _last_gb_req < 0.33: time.sleep(0.33 - (now - _last_gb_req))
        _last_gb_req = time.time()

def _gb_fetch_posts(artist, limit=1):
    try:
        _wait_gb_rate_limit()
        p = {"tags": artist, "limit": limit, "pid": 0}
        p.update(_gb_auth_params())
        raw, _ = fetch_api("https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&" + urlencode(p))
        data = json.loads(raw)
        posts = data.get("post", data) if isinstance(data, dict) else data
        if isinstance(posts, list) and posts:
            rating = str(posts[0].get("rating", "")).lower()
            return posts, rating in ("general", "safe", "g", "s")
    except Exception as e: print(f"[gb] error for {artist!r}: {e}")
    return [], False

def _gb_post_urls(post):
    urls = []
    for key in ("preview_url", "sample_url", "file_url"):
        u = post.get(key)
        if u and u not in urls: urls.append(u)
    return urls

# ── e621 ──────────────────────────────────────────────────────────────────────
_E621_LOCK = threading.Lock()
_last_e621_req = 0.0

def _wait_e621_rate_limit():
    global _last_e621_req
    with _E621_LOCK:
        now = time.time()
        if now - _last_e621_req < 1.1: time.sleep(1.1 - (now - _last_e621_req))
        _last_e621_req = time.time()

def _e621_fetch_posts(artist, limit=1):
    for tags, is_safe in [(artist + " rating:s", True), (artist + " -rating:s", False), (artist, False)]:
        try:
            _wait_e621_rate_limit()
            p = {"tags": tags, "limit": str(limit)}
            p.update(_e621_auth_params())
            raw, _ = fetch_api("https://e621.net/posts.json?" + urlencode(p))
            data = json.loads(raw)
            posts = data.get("posts", [])
            if posts: return posts, is_safe
        except Exception as e: print(f"[e621] error for {tags!r}: {e}")
    return [], False

def _e621_post_urls(post):
    f, s, p = post.get("file", {}), post.get("sample", {}), post.get("preview", {})
    urls = []
    for u in (p.get("url"), s.get("url"), f.get("url")):
        if u and u not in urls: urls.append(u)
    return urls

# ── API Router ────────────────────────────────────────────────────────────────
def _get_api_funcs(source):
    if source == "gelbooru": return _gb_fetch_posts, _gb_post_urls
    if source == "e621":     return _e621_fetch_posts, _e621_post_urls
    return _db_fetch_posts, _db_post_urls

# ── Image fetching ────────────────────────────────────────────────────────────
def try_urls(candidates):
    for u in candidates:
        try:
            req = Request(u)
            req.add_header("User-Agent", "BooruArtistBrowser/1.0 (Local Reference Tool)")
            with urlopen(req, timeout=15) as r:
                data = r.read()
                print(f"[img] ok {len(data)//1024}KB -- {u[:80]}")
                return data, dict(r.headers)
        except Exception as e:
            print(f"[img] err {type(e).__name__}: {e} -- {u[:80]}")
    return None, {}

# ── Request handler ───────────────────────────────────────────────────────────
class ProxyHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        if path == "/ping":
            self._respond(200, b"ok", "text/plain"); return
        if path == "/cache-save":
            self._handle_cache_save(params); return

        if path == "/" or path == "/index.html":
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "booru_browser.html")
            if os.path.exists(html_path):
                with open(html_path, "rb") as f: data = f.read()
                self._respond(200, data, "text/html; charset=utf-8")
            else:
                self._respond(404, b"booru_browser.html not found", "text/plain")
            return

        if path == "/characters":
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "booru_characters.html")
            if os.path.exists(html_path):
                with open(html_path, "rb") as f: data = f.read()
                self._respond(200, data, "text/html; charset=utf-8")
            else:
                self._respond(404, b"booru_characters.html not found", "text/plain")
            return

        if path == "/thumb-meta":  self._thumb_meta(params); return
        if path == "/thumb":       self._thumb(params); return
        if path == "/hover":       self._hover(params); return
        if path == "/tags":        self._tags(params); return

        if path == "/proxy":
            target = params.get("url", [None])[0]
            if not target: self._respond(400, b"missing url", "text/plain"); return
            allowed = ("donmai.us", "gelbooru.com", "img2.gelbooru.com", "img3.gelbooru.com", "img4.gelbooru.com", "e621.net", "static1.e621.net")
            host = urlparse(target).netloc
            if not any(host == d or host.endswith("." + d) for d in allowed):
                self._respond(403, b"forbidden", "text/plain"); return
            data, hdrs = try_urls([target])
            if data: self._send_image(data, hdrs.get("Content-Type","image/jpeg"), False)
            else: self._respond(502, b"upstream error", "text/plain")
            return

        self._respond(404, b"not found", "text/plain")

    def _handle_cache_save(self, params):
        source = params.get("source", ["danbooru"])[0]
        letter = params.get("letter", [None])[0]
        tag_type = params.get("type", ["artist"])[0]
        if not letter: self._respond(400, b"missing letter", "text/plain"); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, list): raise ValueError("expected list")
            _disk_cache_write(source, letter, data, tag_type)
            self._respond(200, json.dumps({"saved": len(data)}).encode(), "application/json")
        except Exception as e:
            self._respond(500, str(e).encode(), "text/plain")

    def do_POST(self):
        if urlparse(self.path).path == "/cache-save":
            self._handle_cache_save(parse_qs(urlparse(self.path).query))
        else: self._respond(404, b"not found", "text/plain")

    def _thumb_meta(self, params):
        artist = params.get("artist", [None])[0]
        source = params.get("source", ["danbooru"])[0]
        if not artist: self._respond(400, b"missing artist", "text/plain"); return

        key = (artist, source)
        cached = _cache_get(_META_CACHE, key)
        if cached: self._json(cached); return

        fetch_func, url_func = _get_api_funcs(source)
        posts, is_safe = fetch_func(artist, 1)

        if not posts: self._respond(404, b'{"error":"no post found"}', "application/json"); return
        urls = url_func(posts[0])
        if not urls: self._respond(404, b'{"error":"no urls"}', "application/json"); return

        copyrights = ""
        if source == "danbooru":
            copyrights = posts[0].get("tag_string_copyright", "").strip().replace(" ", ", ")
        elif source == "e621":
            copyrights = ", ".join(posts[0].get("tags", {}).get("copyright", []))

        meta = {"safe": is_safe, "url": urls[0], "copyright": copyrights}
        _cache_set(_META_CACHE, key, meta)
        self._json(meta)

    def _thumb(self, params):
        artist = params.get("artist", [None])[0]
        source = params.get("source", ["danbooru"])[0]
        if not artist: self._respond(400, b"missing artist", "text/plain"); return

        key = (artist, source)
        cached = _cache_get(_IMAGE_CACHE, key)
        if cached:
            img, ct, safe = cached
            self._send_image(img, ct, safe); return

        meta = _cache_get(_META_CACHE, key)
        if meta and "url" in meta:
            candidates, is_safe = [meta["url"]], meta.get("safe", False)
        else:
            fetch_func, url_func = _get_api_funcs(source)
            posts, is_safe = fetch_func(artist, 1)
            if not posts: self._respond(404, b'{"error":"no post"}', "application/json"); return
            candidates = url_func(posts[0])

        if not candidates: self._respond(404, b'{"error":"no urls"}', "application/json"); return
        print(f"[thumb/{source}] {artist!r:36s} — {len(candidates)} candidates")

        img, hdrs = try_urls(candidates)
        if img is None: self._respond(502, b'{"error":"fetch failed"}', "application/json"); return

        ct = hdrs.get("Content-Type", "image/jpeg")
        _cache_set(_IMAGE_CACHE, key, (img, ct, is_safe))
        self._send_image(img, ct, is_safe)

    def _hover(self, params):
        artist = params.get("artist", [None])[0]
        source = params.get("source", ["danbooru"])[0]
        if not artist: self._respond(400, b"missing artist", "text/plain"); return

        key = (artist, source)
        cached = _cache_get(_HOVER_CACHE, key)
        if cached: self._json(cached); return

        fetch_func, url_func = _get_api_funcs(source)
        posts, _ = fetch_func(artist, 4)

        result = [urls[0] for post in posts if (urls := url_func(post))]
        _cache_set(_HOVER_CACHE, key, result)
        self._json(result)

    def _tags(self, params):
        letter = params.get("letter", [None])[0]
        source = params.get("source", ["danbooru"])[0]
        page   = params.get("page", ["1"])[0]
        tag_type = params.get("type", ["artist"])[0]
        is_char = tag_type == "character"

        if not letter: self._respond(400, b"missing letter", "text/plain"); return

        if page == "1":
            cached = _disk_cache_read(source, letter, tag_type)
            if cached is not None:
                print(f"[cache] hit: {source}/{letter} ({len(cached)} {tag_type}s)")
                self._json({"results": cached, "has_more": False}); return

        # Специальный перехватчик для кнопки #
        if letter == "#":
            self._json({"results": [], "has_more": False}); return

        try:
            if source == "gelbooru":
                GELBOORU_PAGES_PER_BATCH = 15
                pid_start = (int(page) - 1) * GELBOORU_PAGES_PER_BATCH
                results = []
                last_full = False
                for pid in range(pid_start, pid_start + GELBOORU_PAGES_PER_BATCH):
                    _wait_gb_rate_limit()
                    cat_type = "4" if is_char else "1"
                    gb_p = {"name_pattern": letter.lower() + "%", "type": cat_type, "limit": "100", "pid": str(pid), "orderby": "count"}
                    gb_p.update(_gb_auth_params())
                    raw, _ = fetch_api("https://gelbooru.com/index.php?page=dapi&s=tag&q=index&json=1&" + urlencode(gb_p))
                    data = json.loads(raw)
                    tags = data.get("tag", data) if isinstance(data, dict) else data
                    if not isinstance(tags, list) or not tags:
                        last_full = False; break
                    artist_tags = [t for t in tags if str(t.get("type","")) == cat_type and t.get("name")]
                    results.extend({"name": t["name"], "count": int(t.get("count", 0))} for t in artist_tags)
                    print(f"[gb/tags] pid={pid} fetched={len(tags)} {tag_type}s={len(artist_tags)}")
                    last_full = len(tags) == 100
                    if not last_full: break
                results.sort(key=lambda x: -x["count"])
                has_more = last_full

            elif source == "e621":
                _wait_e621_rate_limit()
                cat_type = "4" if is_char else "1"
                p = {"search[category]": cat_type, "search[name_matches]": letter.lower() + "*", "limit": "320", "page": page, "search[order]": "count"}
                p.update(_e621_auth_params())
                raw, _ = fetch_api("https://e621.net/tags.json?" + urlencode(p))
                data = json.loads(raw)
                results = [{"name": t["name"], "count": t["post_count"]} for t in data]
                has_more = len(results) == 320

            else: # Это и есть блок Danbooru
                cat_type = "4" if is_char else "1"
                # Вот здесь добавлена правильная сортировка "search[order]": "count"
                p = {"search[category]": cat_type, "search[name_matches]": letter.lower() + "*", "limit": "1000", "page": page, "search[order]": "count"}
                p.update(_db_auth_params())
                raw, _ = fetch_api("https://danbooru.donmai.us/tags.json?" + urlencode(p))
                data = json.loads(raw)
                results = [{"name": t["name"], "count": t["post_count"]} for t in data]
                has_more = len(results) == 1000

            self._json({"results": results, "has_more": has_more})
        except Exception as e:
            print(f"[tags] error: {e}")
            self._respond(500, json.dumps({"error": str(e)}).encode(), "application/json")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _send_image(self, data, ct, safe):
        self.send_response(200); self._cors()
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Safe", "1" if safe else "0")
        self.end_headers(); self.wfile.write(data)

    def _json(self, obj):
        payload = json.dumps(obj).encode()
        self._respond(200, payload, "application/json")

    def _respond(self, code, body, ct):
        self.send_response(code); self._cors()
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        if ct == "application/json": self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Safe")

    def log_message(self, fmt, *args): pass

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)): return
        super().handle_error(request, client_address)

if __name__ == "__main__":
    _disk_cache_init()
    _load_credentials()
    _test_credentials()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ProxyHandler)
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); local_ip = s.getsockname()[0]; s.close()
    except Exception: local_ip = "YOUR_PC_IP"
    print(f"Booru proxy running!\n  On this PC:  http://localhost:{PORT}\n  On phone:    http://{local_ip}:{PORT}\nPress Ctrl+C to stop.\n")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nProxy stopped."); sys.exit(0)