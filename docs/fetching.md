# Fetching — DeepSeek API rung + container-isolated crawling

Rung 0 (host side, when `DEEPSEEK_API_KEY` is set): DeepSeek's official Fetch
API (`POST https://api.deepseek.com/v1/tools/fetch`) fetches server-side and
returns clean Markdown in ~1s — no container, and the exit IP is DeepSeek's
(useful when the host's routing can't reach the page). Thin content, HTTP
403/404, or timeout falls through to the container ladder silently in `auto`
mode. `--tier deepseek` runs rung 0 alone; `--no-deepseek` disables it.

Rungs 1-3 run in the `crawl-agent` container. The host never loads
scrapling or a browser: the host script (`scripts/crawl_fetch.py`, stdlib only)
just assembles a `crawl-env` call relaying `scripts/crawl_fetch_container.py`.

## Interface (any harness, any model)

```sh
python3 <skill>/scripts/agg_search.py "<query>" --top-n 10        # multi-engine recall + RRF + Jina
python3 <skill>/scripts/crawl_fetch.py --url <url...> [--tier auto|deepseek|static|dynamic|stealth] \
  [--no-deepseek] [--timeout 45] [--out json|md] [--text-limit 8000] [--proxy URL] [--persist] [--headful]
```

`crawl_fetch.py` exit codes: `0` = ≥1 URL ok (JSON lines on stdout);
`1` = all failed; `2` = launcher/image missing; `3` = blocked on every URL.
Each URL yields exactly one JSON line (`tier_used` says which rung served it).

## Tiers

| Tier | Engine | When it wins |
|---|---|---|
| `deepseek` | DeepSeek Fetch API (server-side, no container) | most public pages: docs, news, blogs; unreachable-by-host geo blocks |
| `static` | `Fetcher` (curl_cffi TLS impersonation, no browser) | news, docs, blogs, APIs |
| `dynamic` | `DynamicFetcher` (playwright chromium, JS render) | SPA, lazy-load, waiting selectors |
| `stealth` | `StealthyFetcher` (patchright chromium, Cloudflare solving) | `Just a moment` / cf-challenge / captcha |

`auto` climbs the ladder (deepseek first when keyed); a tier's result is rejected
(escalate) when any of:
HTTP != 200; body contains a block marker (`just a moment`, `cf-challenge`,
`attention required`, `verify you are human`, `enable javascript and cookies`,
`access denied`, `captcha`, `人机验证`, `请开启javascript`); or the page is a
JS shell — extracted text < 300 chars while HTML > 3000 bytes. Rung 0 uses the
equivalent thin-content test (squashed Markdown < 300 chars). If the last tier
still fails that test the JSON carries `blocked: true` and the process exits 3.

## Identity and network facts (read before promising anything)

- Container browsers are fresh profiles; the host's `~/.mozilla`, `~/.config`,
  credentials, and cookies are never visible inside.
- Default is ephemeral: no profile persists between runs. `--persist` keeps
  identity in `~/.cache/crawl-agent/home`.
- Docker bridge is NAT: the container's egress follows the host's routing, so
  **the exit IP equals the host's normal exit** (on this machine clash runs in
  TUN mode, so that already is the VPN exit — verify with
  `--url "https://api.ipify.org/?format=json"`).
- `CRAWL_PROXY=http://host:port` routes container traffic through an explicit
  proxy. Bridge containers cannot reach the host's loopback, so a
  loopback-bound proxy (this host: clash mixed-port `127.0.0.1:7897`) needs
  `--host-net`, which shares the host network namespace:
  `CRAWL_PROXY=http://127.0.0.1:7897 crawl_fetch.py --host-net --url …`
  (verified working). `--host-net` slightly weakens network isolation (the
  container can reach host loopback services); only use it when proxying.
  Without `CRAWL_PROXY` the launcher passes **no** proxy variables at all —
  the container goes direct (inheriting the host's shell `HTTPS_PROXY` would
  break both curl_cffi and chromium, since that proxy is loopback-bound).
- Chromium needs `--no-sandbox` inside this container (rootfs read-only +
  non-root + `--cap-drop=ALL`): acceptable, blast radius is one disposable run.

## crawl-env cheatsheet

```sh
crawl-env -e "<python code>"          # one shot
crawl-env <script.py> [args]          # script from current dir
crawl-env --ro-mount <abs> -- <cmd>   # mount host path read-only at same path
crawl-env --persist ...               # keep browser profile
crawl-env --headful ...               # headful chromium under xvfb-run
crawl-env --host-net ...              # share host network (loopback-only proxy)
crawl-env --mcp                       # scrapling-mcp over stdio (register in a
                                      # harness manually; nothing auto-registers)
crawl-env --shell                     # interactive bash
```

## Build / rebuild (the container is disposable by design)

```sh
docker build -t crawl-agent:latest ~/workspace/crawl-agent
docker rmi crawl-agent:latest   # nukes it; rebuild to restore
```

Dockerfile source: `~/workspace/crawl-agent/Dockerfile`
(`ubuntu:24.04` + venv `scrapling[all]` + playwright & patchright chromium).
Image lives in `Docker Root Dir` (`/mnt/docker-data`, data disk — not the
system disk). If the image is gone or docker is down, `crawl_fetch.py`
exits 2 with the rebuild hint; it never falls back to host crawling.

## Troubleshooting

- `docker: command not found` → install/start docker first.
- `no such image` → rebuild (command above).
- All tiers `blocked: true` → try `--proxy`, `--headful`, or `--tier stealth`;
  login-walled pages need `--persist` + manual cookie seeding (out of scope).
- `--headful` starts `Xvfb :99` itself (not `xvfb-run`, whose cleanup hangs
  when exec'd as the container main process) and sets `DISPLAY=:99`.
- `quotes.toscrape.com/js/` is the deterministic ladder test: `--tier static`
  returns `blocked: true` (exit 3); `--no-deepseek` (or no key) returns quotes
  via `dynamic`; with a key `auto` usually serves it from rung 0 in ~1s.
