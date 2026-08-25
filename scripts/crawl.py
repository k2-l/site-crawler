#!/usr/bin/env python3
"""
site-crawler — download every HTML page and JavaScript source file from a site.

Only HTML documents and JS files are saved. CSS, images, fonts, audio/video and
other binaries are deliberately skipped. By default the crawl stays on the start
URL's host (same-domain). Fetching is done with plain HTTP (Python stdlib only);
pass --render to drive a headless browser (Playwright) so JS-rendered / SPA pages
and dynamically loaded scripts are captured too.

By default the crawler connects DIRECTLY to the target and ignores any
HTTP(S)_PROXY environment variables (so it won't silently tunnel through an
agent/sandbox proxy meant for other traffic). Use --proxy URL to route through
a proxy, or --use-env-proxy to honor the environment's proxy variables.

Static mode needs NO third-party packages. Render mode needs:
    pip install playwright && python -m playwright install chromium

Examples
--------
    python crawl.py https://example.com
    python crawl.py https://example.com -o ./dump --max-pages 2000
    python crawl.py https://app.example.com --render          # SPA / dynamic
    python crawl.py https://example.com --include-subdomains
    python crawl.py https://example.com --path-prefix /docs/
    python crawl.py https://example.com --cookie "session=abc; theme=dark"
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import socket
import ssl
import sys
import time
import zlib
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse, unquote
from urllib.request import (Request, build_opener,
                            ProxyHandler, HTTPSHandler)

# --- what counts as what -----------------------------------------------------

# Extensions we never follow or save (styles, images, fonts, media, archives...).
SKIP_EXTS = {
    ".css", ".scss", ".less", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".tif", ".tiff", ".avif", ".apng",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".ogg",
    ".flac", ".m4a", ".m4v", ".wmv",
    ".zip", ".gz", ".tar", ".tgz", ".rar", ".7z", ".bz2",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".dmg", ".exe", ".apk", ".bin", ".wasm",
}
JS_EXTS = {".js", ".mjs", ".cjs"}


def ext_of(url):
    """Lowercased file extension of a URL's path, or '' if none."""
    last = urlparse(url).path.rsplit("/", 1)[-1]
    return ("." + last.rsplit(".", 1)[-1].lower()) if "." in last else ""


def is_js(url, content_type=""):
    ct = content_type.lower()
    return "javascript" in ct or "ecmascript" in ct or ext_of(url) in JS_EXTS


def is_html(content_type=""):
    ct = content_type.lower()
    return "text/html" in ct or "application/xhtml" in ct or "text/xml" in ct


def normalize(url):
    """Strip the fragment and give a bare host the root path.

    This is what makes two links 'the same page', so http://h and http://h/
    (and #anchors on one page) aren't fetched and saved twice.
    """
    url = urldefrag(url.strip())[0]
    p = urlparse(url)
    if p.scheme in ("http", "https") and p.path == "":
        url = urlunparse(p._replace(path="/"))
    return url


def clean_link(base, href):
    """Resolve href against base, drop fragments, keep only http(s). None if unusable."""
    if not href:
        return None
    href = href.strip()
    low = href.lower()
    if low.startswith(("javascript:", "mailto:", "tel:", "data:", "blob:", "#")):
        return None
    absolute = normalize(urljoin(base, href))
    return absolute if urlparse(absolute).scheme in ("http", "https") else None


def sanitize(component):
    """Make one path/query component safe to use as a filename."""
    component = re.sub(r"[^A-Za-z0-9._-]", "_", unquote(component))
    return component[:100] or "_"


# --- stdlib HTML parsing (no bs4 needed) -------------------------------------

class LinkExtractor(HTMLParser):
    """Pull <a href>, <script src>, and inline <script> bodies out of HTML.

    Also tallies visible text length so the caller can spot an unrendered SPA
    shell (near-empty body + script tags) and suggest --render.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.a_hrefs, self.script_srcs, self.inline_scripts = [], [], []
        self._in_script = self._script_had_src = False
        self._cur = []
        self._skip_depth = 0        # inside <script>/<style>: don't count as text
        self.text_len = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and d.get("href"):
            self.a_hrefs.append(d["href"])
        elif tag == "script":
            self._in_script = True
            if d.get("src"):
                self.script_srcs.append(d["src"])
                self._script_had_src = True
            else:
                self._script_had_src, self._cur = False, []
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_startendtag(self, tag, attrs):  # e.g. <script src=... />
        self.handle_starttag(tag, attrs)
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
            self._in_script = False

    def handle_endtag(self, tag):
        if tag == "script":
            if self._in_script and not self._script_had_src:
                self.inline_scripts.append("".join(self._cur))
            self._in_script = self._script_had_src = False
            self._cur = []
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_script and not self._script_had_src:
            self._cur.append(data)
        elif self._skip_depth == 0:
            self.text_len += len(data.strip())


# --- one fetched resource ----------------------------------------------------

class Fetched:
    __slots__ = ("url", "status", "content_type", "body", "charset", "error")

    def __init__(self, url, status=None, content_type="", body=b"",
                 charset=None, error=None):
        self.url = url
        self.status = status
        self.content_type = content_type
        self.body = body
        self.charset = charset
        self.error = error


# --- the crawler ------------------------------------------------------------

class Crawler:
    def __init__(self, args):
        self.start = normalize(args.url)
        sp = urlparse(self.start)
        if sp.scheme not in ("http", "https") or not sp.hostname:
            sys.exit(f"Not a valid http(s) URL: {args.url!r}")
        self.base_host = sp.hostname.lower()
        self.output_dir = args.output or self._default_output()

        self.render = args.render
        self.max_pages = args.max_pages
        self.max_depth = args.max_depth
        self.delay = args.delay
        self.timeout = args.timeout
        self.insecure = args.insecure
        self.include_subdomains = args.include_subdomains
        self.path_prefix = args.path_prefix
        self.js_scope = args.js_scope           # "all" | "same-domain"
        self.respect_robots = not args.ignore_robots
        self.include_inline_js = args.include_inline_js
        self.user_agent = args.user_agent

        self.headers = {"User-Agent": self.user_agent,
                        "Accept": "text/html,application/xhtml+xml,*/*"}
        for h in args.header or []:
            if ":" in h:
                k, v = h.split(":", 1)
                self.headers[k.strip()] = v.strip()
        if args.cookie:
            self.headers["Cookie"] = args.cookie

        # -- proxy policy -----------------------------------------------------
        # Default: connect directly and IGNORE env proxy vars (so a crawl never
        # silently tunnels through an agent/sandbox proxy). --proxy sets one
        # explicitly; --use-env-proxy restores the "honor $HTTP(S)_PROXY" behavior.
        if args.proxy:
            self.proxy_mode = "explicit"
            self.proxy_url = args.proxy
            proxies = {"http": args.proxy, "https": args.proxy}
        elif args.use_env_proxy:
            self.proxy_mode = "env"
            self.proxy_url = None
            proxies = None                      # None -> ProxyHandler() reads env
        else:
            self.proxy_mode = "direct"
            self.proxy_url = None
            proxies = {}                        # empty dict -> disable all proxies

        handlers = []
        if proxies is not None:
            handlers.append(ProxyHandler(proxies))
        else:
            handlers.append(ProxyHandler())     # no-arg = read env
        if self.insecure:
            handlers.append(HTTPSHandler(context=ssl._create_unverified_context()))
        self._opener = build_opener(*handlers)

        self.visited = set()                    # normalized URLs we've processed
        self.robots = {}                        # (scheme, netloc) -> RobotFileParser|True
        self.manifest = []                      # per-resource records
        self.pages = self.js_files = 0
        self.skipped_robots = self.errors = self.spa_suspect = 0

        socket.setdefaulttimeout(self.timeout)

    def _default_output(self):
        # Always save into a "站点" folder in the agent's current project
        # directory (cwd); each crawled host becomes a subfolder inside it.
        return os.path.abspath("站点")

    # -- scope decisions ------------------------------------------------------

    def _host_ok(self, host):
        host = (host or "").lower()
        if host == self.base_host:
            return True
        return self.include_subdomains and host.endswith("." + self.base_host)

    def page_in_scope(self, url):
        p = urlparse(url)
        if not self._host_ok(p.hostname):
            return False
        if self.path_prefix and not p.path.startswith(self.path_prefix):
            return False
        return ext_of(url) not in SKIP_EXTS

    def js_in_scope(self, url):
        if self.js_scope == "all":
            return True
        return self._host_ok(urlparse(url).hostname)

    def robots_ok(self, url):
        if not self.respect_robots:
            return True
        p = urlparse(url)
        key = (p.scheme, p.netloc)
        rp = self.robots.get(key)
        if rp is None:
            # Fetch robots.txt through our own opener so it follows the same
            # proxy policy as every other request (RobotFileParser.read() would
            # bypass it and hit the env proxy directly).
            robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
            r = self.fetch_static(robots_url)
            if r.body and not r.error:
                rp = robotparser.RobotFileParser()
                try:
                    text = r.body.decode(r.charset or "utf-8", errors="replace")
                    rp.parse(text.splitlines())
                except Exception:
                    rp = True                   # unparsable robots.txt -> allow
            else:
                rp = True                       # unreachable robots.txt -> allow
            self.robots[key] = rp
        return True if rp is True else rp.can_fetch(self.user_agent, url)

    # -- saving ---------------------------------------------------------------

    def path_for(self, url, kind):
        p = urlparse(url)
        host = (p.hostname or "unknown") + (f"_{p.port}" if p.port else "")
        path = p.path
        if path in ("", "/") or path.endswith("/"):
            path += "index"
        segs = [sanitize(s) for s in path.split("/") if s not in ("", ".", "..")]
        if not segs:
            segs = ["index"]
        if p.query:
            segs[-1] += "__" + sanitize(p.query)
        name = segs[-1]
        low = name.lower()
        if kind == "html" and not low.endswith((".html", ".htm")):
            name += ".html"
        elif kind == "js" and not low.endswith((".js", ".mjs", ".cjs")):
            name += ".js"
        if len(name) > 150:                     # keep under filesystem limits
            stem, _, ext = name.rpartition(".")
            name = f"{stem[:130]}_{hashlib.md5(name.encode()).hexdigest()[:8]}.{ext}"
        segs[-1] = name
        return os.path.join(self.output_dir, host, *segs)

    def save(self, url, body, kind, source_url, depth):
        dest = self.path_for(url, kind)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(body)
        rel = os.path.relpath(dest, self.output_dir)
        self.manifest.append({
            "url": url, "kind": kind, "file": rel, "bytes": len(body),
            "depth": depth, "via": source_url,
        })
        if kind == "html":
            self.pages += 1
        else:
            self.js_files += 1
        tag = "HTML" if kind == "html" else "JS  "
        print(f"[{self.pages + self.js_files:4}] {tag} d{depth} {url}  ->  {rel}")
        return dest

    # -- fetching -------------------------------------------------------------

    def fetch_static(self, url, _redirects=0):
        req = Request(url, headers=self.headers)
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except HTTPError as e:
            # urllib follows 301/302/303/307 itself; 308 and stragglers land here.
            if e.code in (301, 302, 303, 307, 308) and _redirects < 6:
                loc = e.headers.get("Location")
                if loc:
                    return self.fetch_static(urljoin(url, loc), _redirects + 1)
            return Fetched(e.geturl() or url, status=e.code, error=f"HTTP {e.code}")
        except (URLError, socket.timeout, ssl.SSLError, ConnectionError) as e:
            return Fetched(url, error=str(e))
        except Exception as e:
            return Fetched(url, error=str(e))
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        try:
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
        return Fetched(resp.geturl(), status=getattr(resp, "status", 200),
                       content_type=resp.headers.get_content_type(),
                       body=raw, charset=resp.headers.get_content_charset())

    # -- per-page handling ----------------------------------------------------

    def handle_html(self, final_url, body, charset, depth, queue):
        self.save(final_url, body, "html", final_url, depth)
        text = body.decode(charset or "utf-8", errors="replace")
        parser = LinkExtractor()
        try:
            parser.feed(text)
        except Exception:
            pass

        if parser.text_len < 50 and parser.script_srcs and not self.render:
            self.spa_suspect += 1

        if self.include_inline_js:
            stem = os.path.splitext(self.path_for(final_url, "html"))[0]
            for i, code in enumerate(parser.inline_scripts):
                if code.strip():
                    dest = f"{stem}.inline{i}.js"
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(code.encode("utf-8", "replace"))

        # queue in-scope page links
        if self.max_depth is None or depth < self.max_depth:
            for href in parser.a_hrefs:
                link = clean_link(final_url, href)
                if link and link not in self.visited and self.page_in_scope(link):
                    self.visited.add(link)
                    queue.append((link, depth + 1))

        # download referenced JS (static mode only; render captures it live)
        if not self.render:
            for src in parser.script_srcs:
                js_url = clean_link(final_url, src)
                if js_url and js_url not in self.visited and self.js_in_scope(js_url):
                    self.visited.add(js_url)
                    self.download_js(js_url, final_url, depth + 1)

    def download_js(self, url, source_url, depth):
        if self.respect_robots and not self.robots_ok(url):
            self.skipped_robots += 1
            return
        r = self.fetch_static(url)
        self.visited.add(normalize(r.url))
        if r.error or not r.body:
            self.errors += 1 if r.error else 0
            return
        self.save(r.url, r.body, "js", source_url, depth)
        if self.delay:
            time.sleep(self.delay)

    # -- render (Playwright) --------------------------------------------------

    def _setup_render(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sys.exit("Render mode needs Playwright:\n"
                     "    pip install playwright && python -m playwright install chromium")
        self._pw = sync_playwright().start()
        launch_kwargs = {"headless": True}
        # Match the static proxy policy so render mode doesn't leak through the
        # env proxy either. "direct://" forces a direct connection even if
        # Chromium would otherwise pick up $HTTP(S)_PROXY.
        if self.proxy_mode == "direct":
            launch_kwargs["proxy"] = {"server": "direct://"}
        elif self.proxy_mode == "explicit":
            launch_kwargs["proxy"] = {"server": self.proxy_url}
        # env mode: leave proxy unset -> Playwright/Chromium default behavior
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._ctx = self._browser.new_context(
            user_agent=self.user_agent,
            ignore_https_errors=self.insecure,
            extra_http_headers={k: v for k, v in self.headers.items()
                                if k.lower() not in ("user-agent", "cookie")},
        )
        if self.headers.get("Cookie"):
            cookies = []
            for pair in self.headers["Cookie"].split(";"):
                if "=" in pair:
                    n, v = pair.split("=", 1)
                    cookies.append({"name": n.strip(), "value": v.strip(),
                                    "domain": self.base_host, "path": "/"})
            if cookies:
                self._ctx.add_cookies(cookies)

    def _teardown_render(self):
        for closer in ("_ctx", "_browser"):
            try:
                getattr(self, closer).close()
            except Exception:
                pass
        try:
            self._pw.stop()
        except Exception:
            pass

    def render_page(self, url, depth, queue):
        page = self._ctx.new_page()

        def on_response(resp):
            try:
                u = normalize(resp.url)
                if (is_js(u, resp.headers.get("content-type", ""))
                        and self.js_in_scope(u) and u not in self.visited):
                    self.visited.add(u)
                    self.save(u, resp.body(), "js", url, depth + 1)
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
        except Exception:
            try:
                page.wait_for_load_state("load", timeout=self.timeout * 1000)
            except Exception:
                pass
        try:
            html = page.content()
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        except Exception as e:
            page.close()
            self.errors += 1
            print(f"  ! render failed: {url}  ({e})")
            return
        page.close()

        self.save(url, html.encode("utf-8", "replace"), "html", url, depth)
        if self.max_depth is None or depth < self.max_depth:
            for href in hrefs:
                link = clean_link(url, href)
                if link and link not in self.visited and self.page_in_scope(link):
                    self.visited.add(link)
                    queue.append((link, depth + 1))

    # -- main loop ------------------------------------------------------------

    def run(self):
        os.makedirs(self.output_dir, exist_ok=True)
        print(f"Start : {self.start}")
        print(f"Scope : {self.base_host}"
              + (" +subdomains" if self.include_subdomains else "")
              + (f"  prefix={self.path_prefix}" if self.path_prefix else ""))
        print(f"Mode  : {'headless browser (render)' if self.render else 'static'}"
              f"  |  JS scope: {self.js_scope}"
              f"  |  robots: {'respect' if self.respect_robots else 'ignore'}")
        proxy_desc = {"direct": "direct (env ignored)",
                      "env": "environment",
                      "explicit": self.proxy_url}[self.proxy_mode]
        print(f"Proxy : {proxy_desc}")
        print(f"Output: {self.output_dir}\n")

        if self.render:
            self._setup_render()
        try:
            queue = deque([(self.start, 0)])
            self.visited.add(self.start)
            while queue and self.pages < self.max_pages:
                url, depth = queue.popleft()
                if not self.robots_ok(url):
                    self.skipped_robots += 1
                    continue
                if self.render:
                    self.render_page(url, depth, queue)
                else:
                    r = self.fetch_static(url)
                    self.visited.add(normalize(r.url))   # dedupe redirect targets
                    if r.error:
                        self.errors += 1
                        print(f"  ! {r.error}: {url}")
                    elif is_js(r.url, r.content_type):
                        self.save(r.url, r.body, "js", url, depth)
                    elif is_html(r.content_type) or r.content_type == "":
                        self.handle_html(r.url, r.body, r.charset, depth, queue)
                    # else: some other content type slipped in -> ignore
                if self.delay:
                    time.sleep(self.delay)
        finally:
            if self.render:
                self._teardown_render()

        self._write_manifest()
        self._summary(len(queue))

    def _write_manifest(self):
        meta = {
            "start_url": self.start,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "render" if self.render else "static",
            "base_host": self.base_host,
            "include_subdomains": self.include_subdomains,
            "path_prefix": self.path_prefix,
            "js_scope": self.js_scope,
            "pages": self.pages,
            "js_files": self.js_files,
            "resources": self.manifest,
        }
        with open(os.path.join(self.output_dir, "manifest.json"), "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

    def _summary(self, remaining):
        print("\n" + "-" * 60)
        print(f"HTML pages saved : {self.pages}")
        print(f"JS files saved   : {self.js_files}")
        if self.skipped_robots:
            print(f"Skipped (robots) : {self.skipped_robots}  "
                  f"(use --ignore-robots to fetch anyway)")
        if self.errors:
            print(f"Fetch errors     : {self.errors}")
        if remaining and self.pages >= self.max_pages:
            print(f"Stopped at --max-pages={self.max_pages}; "
                  f"{remaining} URLs still queued. Raise --max-pages to go further.")
        if self.spa_suspect >= 3:
            print(f"\nNote: {self.spa_suspect} pages looked like empty JS-app shells "
                  f"(little HTML, heavy scripts).\n"
                  f"      This site is probably a SPA — re-run with --render to "
                  f"capture rendered content.")
        print(f"Manifest         : {os.path.join(self.output_dir, 'manifest.json')}")
        print("-" * 60)


def build_parser():
    p = argparse.ArgumentParser(
        description="Download all HTML pages and JS source from a website "
                    "(skips CSS, images, fonts, media).")
    p.add_argument("url", help="start URL, e.g. https://example.com")
    p.add_argument("-o", "--output", help="output directory "
                   "(default: ./站点 in the current project directory)")
    p.add_argument("--render", action="store_true",
                   help="use a headless browser (Playwright) for JS-rendered/SPA sites")
    p.add_argument("--max-pages", type=int, default=500,
                   help="stop after this many HTML pages (default: 500)")
    p.add_argument("--max-depth", type=int, default=None,
                   help="max link depth from the start URL (default: unlimited)")
    p.add_argument("--delay", type=float, default=0.3,
                   help="seconds to wait between requests (default: 0.3)")
    p.add_argument("--timeout", type=float, default=20,
                   help="per-request timeout in seconds (default: 20)")
    p.add_argument("--include-subdomains", action="store_true",
                   help="also crawl subdomains of the start host")
    p.add_argument("--path-prefix", default=None,
                   help="only crawl pages whose path starts with this, e.g. /docs/")
    p.add_argument("--js-scope", choices=("all", "same-domain"), default="all",
                   help="which referenced JS to download: 'all' incl. CDNs "
                        "(default), or 'same-domain'")
    p.add_argument("--cookie", help='Cookie header, e.g. "session=abc; k=v"')
    p.add_argument("--header", action="append",
                   help='extra request header "Name: Value" (repeatable)')
    p.add_argument("--user-agent",
                   default="Mozilla/5.0 (compatible; site-crawler/1.0)",
                   help="User-Agent string")
    p.add_argument("--ignore-robots", action="store_true",
                   help="do not fetch/obey robots.txt (default: obey)")
    p.add_argument("--include-inline-js", action="store_true",
                   help="also extract inline <script> blocks to separate .js files")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS certificate verification")
    p.add_argument("--proxy", default=None,
                   help="route all requests through this proxy, e.g. "
                        'http://host:3128 or "$HTTPS_PROXY" '
                        "(default: connect directly)")
    p.add_argument("--use-env-proxy", action="store_true",
                   help="honor the HTTP(S)_PROXY / NO_PROXY environment "
                        "variables (default: ignore them and go direct)")
    return p


def main():
    args = build_parser().parse_args()
    try:
        Crawler(args).run()
    except KeyboardInterrupt:
        print("\nInterrupted — partial results are on disk.")
        sys.exit(130)


if __name__ == "__main__":
    main()
