# site-crawler

一个 Claude Code / Agent **skill**：爬取指定站点的**所有 HTML 页面和 JavaScript 源码**，
刻意跳过 CSS、图片、字体、音视频等媒体文件——目标是拿到可读的页面结构与代码，而不是归档静态资源。

抓取结果按 `host + 路径` 镜像，保存到当前项目目录下的 **`站点/`** 文件夹，并附带一份 `manifest.json`。

## 特性

- **只存 HTML + JS**，CSS / 图片 / 字体 / 音视频 / PDF / 压缩包等按扩展名在请求前就跳过，不浪费带宽。
- **默认仅同域名**，可 `--include-subdomains` 扩到子域，或 `--path-prefix /docs/` 限定某板块。
- **静态优先，可切换无头浏览器**：默认用纯 HTTP 抓取（适合传统/SSR 站点），SPA/动态站加 `--render` 用 Playwright 渲染后再抓，能拿到运行时加载的 JS。
- **礼貌合规**：默认遵守 `robots.txt`、`--delay` 限速、诚实 UA；支持 `--cookie` / `--header` 抓登录区。
- **静态模式零第三方依赖**（纯 Python 标准库），开箱即用。

## 安装（作为 Claude Code skill）

克隆到你的 skills 目录即可：

```bash
git clone https://github.com/k2-l/site-crawler.git ~/.claude/skills/site-crawler
```

之后在 Claude Code 里自然语言触发，例如「**爬取 example.com 的所有页面和 JS**」「**把这个网站镜像下来**」。

## 直接用（命令行）

```bash
python3 scripts/crawl.py https://example.com
```

结果落在当前目录的 `站点/` 下。常用参数：

| 参数 | 说明 |
|---|---|
| `-o DIR` | 输出目录（默认 `./站点/`）|
| `--render` | 用无头浏览器（Playwright）抓 SPA/动态站 |
| `--max-pages N` | 最多抓 N 个 HTML 页面（默认 500）|
| `--max-depth N` | 最大链接深度（默认不限）|
| `--delay SECONDS` | 请求间隔（默认 0.3）|
| `--include-subdomains` | 同时抓子域名 |
| `--path-prefix /docs/` | 只抓某路径前缀下的页面 |
| `--js-scope same-domain` | 只抓同域 JS（默认 `all`，含 CDN）|
| `--cookie "k=v; k2=v2"` | 携带 Cookie（登录区）|
| `--header "Name: Value"` | 附加请求头（可重复）|
| `--ignore-robots` | 不遵守 robots.txt（默认遵守）|
| `--insecure` | 跳过 TLS 证书校验 |
| `--proxy URL` | 所有请求走该代理（默认直连，忽略环境代理）|
| `--use-env-proxy` | 沿用 `$HTTP(S)_PROXY` / `$NO_PROXY` 环境变量（默认忽略）|

> **代理说明**：爬虫**默认直连目标并忽略环境里的 `HTTPS_PROXY`**。在 agent / 沙箱环境里，
> `HTTPS_PROXY` 指向的是 agent 自己的出站代理（给 agent 访问受信任服务用的，不是用来爬任意站的），
> 若沿用它，抓取目标常会被代理拒绝（407）。需要走代理时用 `--proxy URL`（或 `--proxy "$HTTPS_PROXY"`）；
> 想恢复旧的"读环境代理"行为用 `--use-env-proxy`。

`--render` 模式需要一次性安装 Playwright：

```bash
pip install playwright && python3 -m playwright install chromium
```

## 输出结构

```
站点/
├── manifest.json                 # 每个资源：url / 类型 / 文件 / 字节数 / 深度
├── example.com/
│   ├── index.html
│   ├── about/index.html
│   └── static/app.bundle.js
└── cdn.jsdelivr.net/             # 跨域 JS（--js-scope all 时）
    └── npm/chart.js/dist/chart.min.js
```

## 合规提示

只爬你**拥有或已获授权**的站点。工具默认走礼貌路径（遵守 robots、限速、诚实 UA）。
若全站返回 403/401，通常是需要登录（用 `--cookie`/`--header`）或站点在拦截——
本工具不会去绕过验证码 / 机器人检测。

## 更多

完整的触发说明、模式选择、排错清单见 [`SKILL.md`](SKILL.md)。爬虫核心是单文件
[`scripts/crawl.py`](scripts/crawl.py)：BFS 队列 + 作用域判定 + 标准库 HTML 解析 +
两套抓取后端（`urllib` 静态 / Playwright 渲染），需要扩展直接改它即可。
