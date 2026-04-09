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
import collections
import json, os, sys, threading, time

PORT = 8765

# ── Credentials ──
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
                if DB_LOGIN and DB_API_KEY: print(f"[auth] Danbooru credentials loaded (login={DB_LOGIN!r})")
                if GB_USER_ID and GB_API_KEY: print(f"[auth] Gelbooru credentials loaded (user_id={GB_USER_ID!r})")
                if E621_LOGIN and E621_API_KEY: print(f"[auth] e621 credentials loaded (login={E621_LOGIN!r})")
                return
            except Exception as e:
                print(f"[auth] WARNING: could not read {p}: {e}")

def _db_auth_params(): return {"login": DB_LOGIN, "api_key": DB_API_KEY} if DB_LOGIN and DB_API_KEY else {}
def _gb_auth_params(): return {"api_key": GB_API_KEY, "user_id": GB_USER_ID} if GB_USER_ID and GB_API_KEY else {}
def _e621_auth_params(): return {"login": E621_LOGIN, "api_key": E621_API_KEY} if E621_LOGIN and E621_API_KEY else {}

def get_user_agent():
    user = E621_LOGIN if E621_LOGIN else "anonymous"
    return f"BooruBrowser/1.0 (by {user} on e621)"

def _test_credentials():
    if not (DB_LOGIN and DB_API_KEY): return
    url = "https://danbooru.donmai.us/profile.json?" + urlencode(_db_auth_params())
    print("[auth] Testing credentials...")
    try:
        d = json.loads(fetch_api(url)[0])
        print(f"[auth] OK — logged in as {d.get('name')!r}\n")
    except Exception as e: print(f"[auth] !! {e}\n")

def fetch_api(url):
    req = Request(url)
    req.add_header("User-Agent", get_user_agent())
    with urlopen(req, timeout=15) as r:
        return r.read(), dict(r.headers)

# ── Caches ──
_IMAGE_CACHE = collections.OrderedDict()
_HOVER_CACHE = collections.OrderedDict()
_META_CACHE  = {}
_META_DIRTY  = False
_cache_lock  = threading.Lock()
MAX_CACHE = 2000

_DISK_CACHE_DIR = None
_META_CACHE_FILE = None

def _meta_saver_loop():
    global _META_DIRTY
    while True:
        time.sleep(3)
        if _META_DIRTY:
            try:
                with _cache_lock:
                    data = dict(_META_CACHE)
                    _META_DIRTY = False
                tmp = _META_CACHE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, _META_CACHE_FILE)
            except Exception as e:
                print(f"[cache] Failed to save meta: {e}")

def _disk_cache_init():
    global _DISK_CACHE_DIR, _META_CACHE_FILE
    _DISK_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tag_cache")
    os.makedirs(_DISK_CACHE_DIR, exist_ok=True)

    _META_CACHE_FILE = os.path.join(_DISK_CACHE_DIR, "meta_cache.json")
    if os.path.exists(_META_CACHE_FILE):
        try:
            with open(_META_CACHE_FILE, "r", encoding="utf-8") as f:
                _META_CACHE.update(json.load(f))
            print(f"[cache] Loaded {len(_META_CACHE)} saved previews from disk.")
        except Exception as e:
            print(f"[cache] Could not load meta cache: {e}")

    threading.Thread(target=_meta_saver_loop, daemon=True).start()

def _disk_cache_path(source, letter, tag_type="artist"):
    prefix = "char_" if tag_type == "character" else ""
    return os.path.join(_DISK_CACHE_DIR, f"{source}_{prefix}{letter.upper()}.json")

def _disk_cache_read(source, letter, tag_type="artist"):
    p = _disk_cache_path(source, letter, tag_type)
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f: obj = json.load(f)
            if isinstance(obj, dict) and obj.get("complete") and isinstance(obj.get("data"), list):
                return obj["data"]
            try: os.remove(p)
            except Exception: pass
    except: pass
    return None

def _disk_cache_write(source, letter, data, tag_type="artist"):
    p = _disk_cache_path(source, letter, tag_type)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"complete": True, "count": len(data), "data": data}, f)
        os.replace(tmp, p)
    except Exception:
        try: os.remove(tmp)
        except: pass

def _cache_set_lru(d, key, val):
    with _cache_lock:
        if key in d: del d[key]
        elif len(d) >= MAX_CACHE: d.popitem(last=False)
        d[key] = val

def _cache_get_lru(d, key):
    with _cache_lock:
        if key in d:
            val = d.pop(key)
            d[key] = val
            return val
        return None

# ── Tag Logic ──
def _is_safe(post, source):
    if source == "danbooru": return str(post.get("rating", "")).lower() in ("g", "s")
    if source == "gelbooru": return str(post.get("rating", "")).lower() in ("general", "safe", "g", "s")
    if source == "e621": return str(post.get("rating", "")).lower() == "s"
    return False

def _has_solo_tags(post, source):
    solo_tags = {"solo", "1girl", "1boy"}
    if source == "danbooru":
        tags = set(post.get("tag_string", "").split())
    elif source == "gelbooru":
        tags = set(post.get("tags", "").split())
    elif source == "e621":
        tags = set()
        for cat in post.get("tags", {}).values():
            if isinstance(cat, list): tags.update(cat)
    else:
        tags = set()
    return bool(tags.intersection(solo_tags))

def _find_best_post(posts, url_func, source):
    valid_posts = []
    for p in posts:
        urls = url_func(p)
        if urls: valid_posts.append((p, urls))
    if not valid_posts: return None, []
    for p, urls in valid_posts:
        if _has_solo_tags(p, source): return p, urls
    return valid_posts[0]

# ── Danbooru ──
def _db_fetch_posts(artist, limit=20):
    try:
        p = {"tags": artist, "limit": str(limit), "only": "id,preview_file_url,large_file_url,file_url,rating,media_asset,tag_string_copyright,tag_string"}
        p.update(_db_auth_params())
        raw, _ = fetch_api("https://danbooru.donmai.us/posts.json?" + urlencode(p))
        return json.loads(raw)
    except Exception as e: print(f"[db] error for {artist!r}: {e}")
    return []

def _db_post_urls(post):
    ma = post.get("media_asset") or {}
    by_type = {v.get("type"): v.get("url") for v in (ma.get("variants") or []) if v.get("url")}
    urls = [by_type[w] for w in ("sample", "720x720", "360x360", "180x180") if w in by_type]
    for key in ("preview_file_url", "large_file_url", "file_url"):
        if (u := post.get(key)) and u not in urls: urls.append(u)
    return urls

# ── Gelbooru ──
_GB_LOCK = threading.Lock()
_last_gb_req = 0.0

def _wait_gb_rate_limit():
    global _last_gb_req
    with _GB_LOCK:
        now = time.time()
        if now - _last_gb_req < 0.2: time.sleep(0.2 - (now - _last_gb_req))
        _last_gb_req = time.time()

def _gb_fetch_posts(artist, limit=20):
    try:
        _wait_gb_rate_limit()
        p = {"tags": artist, "limit": limit, "pid": 0}
        p.update(_gb_auth_params())
        raw, _ = fetch_api("https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&" + urlencode(p))
        data = json.loads(raw)
        posts = data.get("post", data) if isinstance(data, dict) else data
        return posts if isinstance(posts, list) else []
    except Exception as e: print(f"[gb] error for {artist!r}: {e}")
    return []

def _gb_post_urls(post):
    urls = []
    for key in ("preview_url", "sample_url", "file_url"):
        if (u := post.get(key)) and u not in urls: urls.append(u)
    return urls

# ── e621 ──
_E621_LOCK = threading.Lock()
_last_e621_req = 0.0

def _wait_e621_rate_limit():
    global _last_e621_req
    with _E621_LOCK:
        now = time.time()
        if now - _last_e621_req < 0.55: time.sleep(0.55 - (now - _last_e621_req))
        _last_e621_req = time.time()

def _e621_fetch_posts(artist, limit=20):
    try:
        _wait_e621_rate_limit()
        p = {"tags": artist, "limit": str(limit)}
        p.update(_e621_auth_params())
        raw, _ = fetch_api("https://e621.net/posts.json?" + urlencode(p))
        data = json.loads(raw)
        return data.get("posts", [])
    except Exception as e: print(f"[e621] error for {artist!r}: {e}")
    return []

def _e621_post_urls(post):
    f, s, p = post.get("file", {}), post.get("sample", {}), post.get("preview", {})
    urls = []
    for u in (p.get("url"), s.get("url"), f.get("url")):
        if u and u not in urls: urls.append(u)
    return urls

def _get_api_funcs(source):
    if source == "gelbooru": return _gb_fetch_posts, _gb_post_urls
    if source == "e621":     return _e621_fetch_posts, _e621_post_urls
    return _db_fetch_posts, _db_post_urls

# ── Image fetching ──
def try_urls(candidates, source=None):
    for u in candidates:
        try:
            req = Request(u)
            req.add_header("User-Agent", get_user_agent())
            with urlopen(req, timeout=15) as r:
                data = r.read()
                print(f"[img] ok {len(data)//1024}KB -- {u[:80]}")
                return data, dict(r.headers)
        except Exception as e: print(f"[img] err {type(e).__name__}: {e} -- {u[:80]}")
    return None, {}

# ── Request handler ──
class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        if path == "/ping": self._respond(200, b"ok", "text/plain"); return
        if path == "/cache-save": self._handle_cache_save(params); return

        if path == "/" or path == "/index.html":
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "booru_browser.html")
            if os.path.exists(html_path):
                with open(html_path, "rb") as f: data = f.read()
                self._respond(200, data, "text/html; charset=utf-8")
            else: self._respond(404, b"booru_browser.html not found", "text/plain")
            return

        if path == "/thumb-meta":  self._thumb_meta(params); return
        if path == "/thumb":       self._thumb(params); return
        if path == "/hover":       self._hover(params); return
        if path == "/tags":        self._tags(params); return
        if path == "/search":      self._search(params); return

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

    def do_POST(self):
        if urlparse(self.path).path == "/cache-save":
            self._handle_cache_save(parse_qs(urlparse(self.path).query))
        else: self._respond(404, b"not found", "text/plain")

    def _handle_cache_save(self, params):
        source, letter, tag_type = params.get("source", ["danbooru"])[0], params.get("letter", [None])[0], params.get("type", ["artist"])[0]
        if not letter: self._respond(400, b"missing letter", "text/plain"); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            _disk_cache_write(source, letter, data, tag_type)
            self._respond(200, json.dumps({"saved": len(data)}).encode(), "application/json")
        except Exception as e: self._respond(500, str(e).encode(), "text/plain")

    def _search(self, params):
        query, source, tag_type = params.get("q", [""])[0], params.get("source", ["danbooru"])[0], params.get("type", ["artist"])[0]
        if not query: self._respond(400, b"missing q", "text/plain"); return
        try:
            cat_type = "4" if tag_type == "character" else "1"
            if source == "gelbooru":
                _wait_gb_rate_limit()
                p = {"name_pattern": f"%{query}%", "type": cat_type, "limit": "20", "orderby": "count"}
                p.update(_gb_auth_params())
                raw, _ = fetch_api("https://gelbooru.com/index.php?page=dapi&s=tag&q=index&json=1&" + urlencode(p))
                data = json.loads(raw)
                tags = data.get("tag", data) if isinstance(data, dict) else data
                results = [{"name": t["name"], "count": int(t.get("count", 0))} for t in tags if str(t.get("type","")) == cat_type and t.get("name")] if isinstance(tags, list) else []
                results.sort(key=lambda x: -x["count"])
                self._json({"results": results[:20]})
            elif source == "e621":
                _wait_e621_rate_limit()
                p = {"search[category]": cat_type, "search[name_matches]": f"*{query}*", "limit": "20", "search[order]": "count"}
                p.update(_e621_auth_params())
                raw, _ = fetch_api("https://e621.net/tags.json?" + urlencode(p))
                self._json({"results": [{"name": t["name"], "count": t["post_count"]} for t in json.loads(raw)]})
            else:
                p = {"search[category]": cat_type, "search[name_matches]": f"*{query}*", "limit": "20", "search[order]": "count"}
                p.update(_db_auth_params())
                raw, _ = fetch_api("https://danbooru.donmai.us/tags.json?" + urlencode(p))
                self._json({"results": [{"name": t["name"], "count": t["post_count"]} for t in json.loads(raw)]})
        except Exception as e: self._respond(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _thumb_meta(self, params):
        artist, source = params.get("artist", [None])[0], params.get("source", ["danbooru"])[0]
        if not artist: self._respond(400, b"missing artist", "text/plain"); return

        key = f"{source}:{artist}"
        with _cache_lock: cached = _META_CACHE.get(key)
        if cached: self._json(cached); return

        fetch_func, url_func = _get_api_funcs(source)
        posts = fetch_func(artist, 20)

        best_post, urls = _find_best_post(posts, url_func, source)
        if not best_post: self._respond(404, b'{"error":"no post found"}', "application/json"); return

        is_safe = _is_safe(best_post, source)
        copyrights = ""
        if source == "danbooru": copyrights = best_post.get("tag_string_copyright", "").strip().replace(" ", ", ")
        elif source == "e621": copyrights = ", ".join(best_post.get("tags", {}).get("copyright", []))

        meta = {"safe": is_safe, "url": urls[0], "copyright": copyrights}
        with _cache_lock:
            _META_CACHE[key] = meta
            global _META_DIRTY
            _META_DIRTY = True
        self._json(meta)

    def _thumb(self, params):
        artist, source = params.get("artist", [None])[0], params.get("source", ["danbooru"])[0]
        if not artist: self._respond(400, b"missing artist", "text/plain"); return

        img_key = f"{source}:{artist}"
        cached = _cache_get_lru(_IMAGE_CACHE, img_key)
        if cached:
            img, ct, safe = cached
            self._send_image(img, ct, safe); return

        with _cache_lock: meta = _META_CACHE.get(img_key)
        if meta and "url" in meta:
            candidates, is_safe = [meta["url"]], meta.get("safe", False)
        else:
            fetch_func, url_func = _get_api_funcs(source)
            posts = fetch_func(artist, 20)
            best_post, candidates = _find_best_post(posts, url_func, source)
            if not best_post: self._respond(404, b'{"error":"no urls"}', "application/json"); return
            is_safe = _is_safe(best_post, source)

        img, hdrs = try_urls(candidates, source)
        if img is None: self._respond(502, b'{"error":"fetch failed"}', "application/json"); return

        ct = hdrs.get("Content-Type", "image/jpeg")
        _cache_set_lru(_IMAGE_CACHE, img_key, (img, ct, is_safe))
        self._send_image(img, ct, is_safe)

    def _hover(self, params):
        artist, source = params.get("artist", [None])[0], params.get("source", ["danbooru"])[0]
        if not artist: self._respond(400, b"missing artist", "text/plain"); return

        key = f"{source}:{artist}"
        cached = _cache_get_lru(_HOVER_CACHE, key)
        if cached: self._json(cached); return

        fetch_func, url_func = _get_api_funcs(source)
        posts = fetch_func(artist, 20)

        valid_posts = []
        for p in posts:
            if urls := url_func(p): valid_posts.append((p, urls))

        valid_posts.sort(key=lambda x: not _has_solo_tags(x[0], source))
        result = [urls[0] for _, urls in valid_posts[:4]]

        _cache_set_lru(_HOVER_CACHE, key, result)
        self._json(result)

    def _tags(self, params):
        letter, source, page, tag_type = params.get("letter", [None])[0], params.get("source", ["danbooru"])[0], params.get("page", ["1"])[0], params.get("type", ["artist"])[0]
        if not letter: self._respond(400, b"missing letter", "text/plain"); return

        if page == "1" and (cached := _disk_cache_read(source, letter, tag_type)) is not None:
            self._json({"results": cached, "has_more": False}); return
        if letter == "#": self._json({"results": [], "has_more": False}); return

        cat_type = "4" if tag_type == "character" else "1"
        try:
            if source == "gelbooru":
                results = []
                pid_start = (int(page) - 1) * 15
                for pid in range(pid_start, pid_start + 15):
                    _wait_gb_rate_limit()
                    p = {"name_pattern": letter.lower() + "%", "type": cat_type, "limit": "100", "pid": str(pid), "orderby": "count"}
                    p.update(_gb_auth_params())
                    raw, _ = fetch_api("https://gelbooru.com/index.php?page=dapi&s=tag&q=index&json=1&" + urlencode(p))
                    tags = json.loads(raw).get("tag", [])
                    if not isinstance(tags, list) or not tags:
                        has_more = False; break
                    results.extend({"name": t["name"], "count": int(t.get("count", 0))} for t in tags if str(t.get("type","")) == cat_type and t.get("name"))
                    has_more = len(tags) == 100
                    if not has_more: break
                results.sort(key=lambda x: -x["count"])

            elif source == "e621":
                _wait_e621_rate_limit()
                p = {"search[category]": cat_type, "search[name_matches]": letter.lower() + "*", "limit": "320", "page": page, "search[order]": "count"}
                p.update(_e621_auth_params())
                raw, _ = fetch_api("https://e621.net/tags.json?" + urlencode(p))
                results = [{"name": t["name"], "count": t["post_count"]} for t in json.loads(raw)]
                has_more = len(results) == 320

            else:
                p = {"search[category]": cat_type, "search[name_matches]": letter.lower() + "*", "limit": "1000", "page": page, "search[order]": "count"}
                p.update(_db_auth_params())
                raw, _ = fetch_api("https://danbooru.donmai.us/tags.json?" + urlencode(p))
                results = [{"name": t["name"], "count": t["post_count"]} for t in json.loads(raw)]
                has_more = len(results) == 1000

            self._json({"results": results, "has_more": has_more})
        except Exception as e: self._respond(500, json.dumps({"error": str(e)}).encode(), "application/json")

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

    def do_OPTIONS(self): self.send_response(204); self._cors(); self.end_headers()
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
