---
name: site-crawler
description: >-
  Crawl a website and download every HTML page and JavaScript source file it
  serves, skipping CSS, images, fonts, and other media. Use whenever the user
  wants to mirror, archive, scrape, clone, or bulk-download a site's pages and/or
  its JS — e.g. "爬取/镜像这个站点", "抓取所有页面和JS", "克隆网站源码", "mirror this
  site", "grab every page and script", "pull the site's JavaScript". Handles
  same-domain crawling, static or headless-browser (SPA/JS-rendered, lazy-loaded
  JS) fetching, robots.txt, rate limiting, subdomains, path scoping, auth
  cookies/headers, and proxy control. Reach for it even when the user doesn't say
  "crawler" but clearly wants a site's pages or scripts saved to disk in bulk.
---

# site-crawler

Download all of a website's **HTML pages** and **JavaScript source** to local
disk, mirroring the URL structure. Everything else — CSS, images, fonts, media,
archives — is skipped on purpose, because the goal is reading/analyzing markup
and code, not archiving assets.

The engine is `scripts/crawl.py`. Static mode uses **only the Python standard
library** (nothing to install). Headless-browser mode is opt-in and needs
Playwright.

## Quick start

For a normal multi-page site, this is all you need:

```bash
python3 scripts/crawl.py https://example.com
```

It crawls same-domain and saves everything into a **`站点/` folder inside the
current project directory** (the agent's working directory) — each crawled host
gets its own subfolder within `站点/` — plus a `manifest.json`. Watch the live
log; each line is a saved page or script.

**Output location is fixed: `<project dir>/站点/`.** Resolve the script path
relative to this SKILL.md (it lives in `scripts/crawl.py` next to this file), but
run it from the project directory and do **not** pass `-o` — the default already
targets the `站点/` folder there. Only pass `-o` if the user explicitly asks to
save somewhere else.

## Framework detection (runs first)

Before crawling, the tool fetches the start page once and **fingerprints the
site's stack** — framework (React, Vue, Angular, Svelte, Next/Nuxt/Gatsby…),
libraries (jQuery, Bootstrap…), bundler (webpack, Vite), and CMS/platform
(WordPress, Shopify, Wix…) — from the HTML, response headers, `<meta generator>`
and script URLs. It prints a `Stack :` line and records it in `manifest.json`.
If the start page fingerprints as a **client-rendered framework**, the tool says
so and suggests `--render`; pass `--auto-render` to let it switch to render mode
automatically in that case.

## Choosing the mode

**Start with static** (the default). It's fast, dependency-free, and correct for
traditional server-rendered sites — it downloads each page's HTML and every
`<script src>` it references.

**Switch to `--render` when the site is a SPA / JS-rendered app** (React, Vue,
Angular, Next.js hydration, etc.). Static fetching only sees the initial HTML
shell, so on those sites you'd capture an almost-empty `<div id="root">` and miss
both the real content and the dynamically-loaded chunks. Two signals tell you to
switch:

- The run's summary prints a note that several pages "looked like empty JS-app
  shells." That's the built-in SPA detector.
- The user says the site is React/Vue/Angular/Next/an "app", or you already know
  it renders client-side.

```bash
python3 scripts/crawl.py https://app.example.com --render
```

`--render` drives headless Chromium: it waits for the network to settle, then
**auto-scrolls each page** so lazy-loaded JS (IntersectionObserver imports,
infinite scroll, on-demand chunks) actually fires, saves the *rendered* DOM,
follows links found in the live page, and captures **every** JavaScript response
the browser loaded — code-split chunks and dynamically injected scripts static
parsing can't see. Auto-scroll is on by default (`--no-autoscroll` to disable,
`--scroll-passes N` to cap steps on infinite-scroll pages). It's slower and needs
a one-time setup:

```bash
pip install playwright && python3 -m playwright install chromium
```

If that `pip install` is blocked by a "externally managed environment" error on
macOS, use a venv instead:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install playwright && playwright install chromium
```

## What gets saved, and what doesn't

- **Saved:** documents served as HTML (`text/html`/XHTML) and JavaScript
  (`.js`/`.mjs`/`.cjs`, or any `javascript` content-type). Inline `<script>` code
  already lives inside the saved HTML; add `--include-inline-js` to *also* split
  each inline block into its own `.js` file.
- **Skipped:** CSS, images (png/jpg/gif/svg/webp/…), fonts, audio/video, PDFs,
  archives, wasm, source maps. These are filtered by extension *before* fetching,
  so no bandwidth is wasted on them.

The decision is content-type first, extension second, so a page at a clean URL
like `/about` (no `.html`) is still recognized and saved as HTML.

## Scope — how far the crawl reaches

By default the crawl stays on the **exact start host** (same-domain). Widen or
narrow it:

| Goal | Flag |
|---|---|
| Also crawl subdomains (`blog.`, `docs.`, …) | `--include-subdomains` |
| Restrict to one section of the site | `--path-prefix /docs/` |
| Only download JS from the site's own hosts (not CDNs) | `--js-scope same-domain` |

Note the deliberate asymmetry: **page crawling** is same-domain, but by default
**JavaScript** is pulled from wherever it's referenced (`--js-scope all`),
including CDNs — because a site's real application code is often bundled onto a
CDN and you'd want it for analysis. Use `--js-scope same-domain` to suppress
third-party scripts (analytics, ads, widgets).

## Common options

```
-o, --output DIR          where to save (default ./站点/ in the project dir)
--max-pages N             stop after N HTML pages (default 500; raise for big sites)
--max-depth N             max link depth from the start URL (default: unlimited)
--delay SECONDS           politeness pause between requests (default 0.3)
--timeout SECONDS         per-request timeout (default 20)
--cookie "k=v; k2=v2"     send a Cookie header (for logged-in areas)
--header "Name: Value"    extra request header (repeatable, e.g. auth tokens)
--user-agent "..."        custom UA string
--ignore-robots           do NOT obey robots.txt (default: obey it)
--insecure                skip TLS verification (self-signed certs)
--proxy URL               route all requests through this proxy (default: direct)
--use-env-proxy           honor $HTTP(S)_PROXY / $NO_PROXY (default: ignore them)
--no-autoscroll           render mode: don't scroll to trigger lazy-loaded JS
--scroll-passes N         render mode: max scroll steps per page (default 20)
--auto-render             switch to render mode if the site fingerprints as a SPA
```

The default `--max-pages 500` is a safety valve so a crawl can't run away. If the
summary says URLs are "still queued," the site is bigger than the cap — rerun
with a higher `--max-pages` (and mention the number to the user).

## Output layout

```
站点/                                  # created in the current project directory
├── manifest.json                     # every resource: url, kind, file, bytes, depth
├── example.com/
│   ├── index.html
│   ├── about/index.html
│   ├── pricing.html
│   └── static/app.bundle.js
└── cdn.jsdelivr.net/                 # cross-origin JS (with --js-scope all)
    └── npm/chart.js/dist/chart.min.js
```

Everything lands inside `站点/`. Within it each host becomes a folder and the URL
path becomes the sub-path. A trailing-slash
or extensionless page URL is saved as `…/index.html`; query strings are appended
to the filename so distinct pages don't collide. `manifest.json` is the machine-
readable index of the whole crawl — read it to answer "what did we get?"

## Proxies & agent/sandbox environments

By default the crawler connects **directly to the target and ignores any
`HTTP(S)_PROXY` / `NO_PROXY` environment variables.** This matters in agent and
sandbox environments (like Claude Code on the web), where `HTTPS_PROXY` is set
to the *agent's own* outbound proxy — meant for the agent reaching approved
services, not for crawling arbitrary sites. If the crawler inherited it, every
request to the target would tunnel through that proxy and typically come back
`407`/`403`, failing the whole crawl. So the default is direct, and the run
header prints which path it took (`Proxy : direct (env ignored)`).

- **Need to go through a proxy** (e.g. the sandbox's proxy is the *only* egress,
  or you're behind a corporate proxy): pass it explicitly —
  `--proxy http://host:3128`, or reuse the environment's value with
  `--proxy "$HTTPS_PROXY"`. This applies to static fetches, JS, robots.txt, and
  `--render` (Playwright) alike.
- **Want the old "honor the environment" behavior**: `--use-env-proxy`.

## Being a good citizen (and staying legal)

Only crawl sites the user owns or is authorized to crawl. The tool defaults to
the polite path — it **obeys robots.txt**, rate-limits with `--delay`, and sends
an honest User-Agent. If the user asks to ignore robots or hammer the site, it's
their call to make on infrastructure they're allowed to test; surface the tradeoff
rather than silently changing defaults. If a crawl returns 403/401 everywhere,
the site likely needs auth (`--cookie`/`--header`) or is blocking the UA —
don't try to evade bot-protection, just tell the user what's happening.

## Troubleshooting

- **Pages are nearly empty / content missing** → it's a SPA. Re-run with
  `--render`.
- **Some JS still missing under `--render`** → it loads on interaction beyond a
  scroll (click/hover/route change). Auto-scroll catches scroll/intersection
  chunks; raise `--scroll-passes` for very long pages, and make sure the routes
  that load those chunks are reachable as links so the crawler visits them.
- **`Render mode needs Playwright`** → run the pip/playwright install above.
- **Lots of `HTTP 403`/`401`** → needs login. Pass the session cookie via
  `--cookie "sessionid=…"` (get it from the browser dev tools), or an auth token
  via `--header "Authorization: Bearer …"`.
- **Crawl stops early** → hit `--max-pages`; raise it.
- **`CERTIFICATE_VERIFY_FAILED`** → add `--insecure` (only for sites you trust).
- **Too much third-party JS** → add `--js-scope same-domain`.
- **Only want one section** → add `--path-prefix /path/`.
- **Everything returns `407`, or traffic goes through an unexpected proxy** →
  you're in a proxied environment. The crawler now goes **direct by default**;
  only pass `--proxy` if the target genuinely needs one. If direct egress is
  blocked and the sandbox proxy is your only way out, use
  `--proxy "$HTTPS_PROXY"`.

## Extending

The crawler is a single readable file (`scripts/crawl.py`) with a small core:
a BFS frontier, scope checks, a stdlib HTML link/script extractor, and two fetch
backends (static via `urllib`, rendered via Playwright). If a task needs
something new — capturing WebSocket payloads, following JS `import` graphs,
concurrent workers — extend that file rather than writing a one-off scraper.
