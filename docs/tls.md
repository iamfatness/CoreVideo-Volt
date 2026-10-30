# TLS: the supported reverse-proxy recipe

Volt serves plain HTTP and will not grow a TLS stack of its own ([foundation](specs/foundation.md) non-goals). The
operator token is a bearer token, so on any network you do not own end to end it must cross a TLS hop. The supported
way is a reverse proxy on the same machine that terminates TLS and talks to Volt over loopback. This page gives the
recipe for Caddy (primary: it fetches and renews certificates by itself) and nginx, and records what was run
against each on 2026-10-30.

## The shape

```
browser / client  --https-->  proxy (443)  --http-->  volt serve on 127.0.0.1:8765
```

1. Run Volt on loopback with a token: `VOLT_HOST=127.0.0.1` (the default) and `VOLT_TOKEN` set to 16 or more
   characters. Nothing else can then reach Volt except through the proxy. Open only the proxy's port in the host
   firewall.
2. The proxy owns the certificate, HTTP to HTTPS redirect, HSTS and the body cap.
3. Clients use `https://volt.example.com`. Nothing in Volt needs to know its public name.

**Always set `VOLT_TOKEN` behind a proxy.** With no token Volt answers only when the `Host` header is a loopback
name (`localhost`, `127.0.0.1`, `[::1]`), as a DNS-rebinding guard. A proxy forwards the public `Host`
(`volt.example.com`), so a token-less Volt answers every page except `/health` with `403 bad_host`
(verified, below). The fix is the token, not rewriting `Host` to a loopback name: that would turn the guard off and
leave the service open to anyone who can reach the proxy.

## What Volt does and does not need from the proxy

Read from `control/volt/serve.py` and confirmed by the runs below.

| Question | Answer |
|---|---|
| Streaming / SSE / WebSocket? | None. Every response is a plain request/response with a `Content-Length`; the longest is a media proxy download. No upgrade headers, no `text/event-stream`. Buffering can stay on for API calls; it is turned off below for media so a download starts immediately and does not spool a temp file. |
| Request body cap | `MAX_BODY` is 64 KiB (65536 bytes). Volt answers `413` from the `Content-Length` before it reads the body. The proxy cap below is the same number, as a first line of defence. |
| Chunked request bodies | Volt refuses them (`411 length_required`). Browsers and normal HTTP clients send `Content-Length` for JSON bodies. **nginx** buffers the body and adds `Content-Length` itself, so a chunked client still works. **Caddy** passes the chunked body through, so a client that sends one gets `411` (verified; `request_buffers` did not change it). |
| Range requests | Volt serves one byte range (`206`, `Content-Range`, `Accept-Ranges: bytes`) on `/api/assets/{id}/proxy` and `/media`. Both proxies pass `Range` and the `206` through unchanged. Do not enable proxy caching or response compression for these routes. |
| Media tickets | `GET /api/media-ticket` returns a relative URL (`/api/assets/{id}/proxy?ticket=...`) signed over the asset id, kind and expiry. It contains no host, so it is valid at any public name. A `<video src>` can use it without an `Authorization` header (verified through both proxies). |
| Client address | Volt logs `self.client_address`, which behind a proxy is `127.0.0.1` for every request. The `client=` field in the access log and in the `401/403` warnings is therefore useless for tracing a caller; use the proxy's own access log for that. |
| Does Volt read `X-Forwarded-For`, `X-Forwarded-Proto`, `Forwarded`, `X-Real-IP`? | No. Not for auth, not for logging, not for building URLs. Leave it that way: do not add trust in these headers unless Volt is changed to verify the peer is the proxy. Anything that can reach port 8765 directly could forge them, which is why step 1 binds to loopback. |
| `Origin` / CORS | Volt sets no CORS headers and does no origin check. Serve the desk and the API from the same origin (the proxy above does). |
| Ticket in the access log | With `VOLT_ACCESS_LOG=1` the request line is logged, including `?ticket=...`. The ticket expires (120 seconds by default, at most 3600) and is scoped to one asset and kind, but treat that log, and the proxy's, as sensitive. |

## Caddy (primary)

Caddy 2 gets and renews a public certificate on its own when the name resolves to this host and ports 80 and 443
are reachable. Create `/etc/caddy/Caddyfile` (or `Caddyfile` next to the binary on Windows):

```caddyfile
volt.example.com {
	# HTTPS is automatic and HTTP redirects to it. HSTS: start with a short max-age, raise it when sure.
	header Strict-Transport-Security "max-age=31536000"

	# Same 64 KiB cap as Volt's MAX_BODY.
	request_body {
		max_size 64KiB
	}

	reverse_proxy 127.0.0.1:8765 {
		# Host is passed through unchanged by default. flush_interval -1 sends bytes as they arrive,
		# so a media download starts at once.
		flush_interval -1
		transport http {
			dial_timeout 5s
			response_header_timeout 60s
		}
	}
}
```

Notes:

- `reverse_proxy` adds `X-Forwarded-For`, `-Proto` and `-Host` and a `Via` header. Volt ignores them.
- There is no read/write timeout on the body of a response by default, so a slow media download is not cut off.
  The `response_header_timeout` bounds how long Volt may take to start answering.
- Do not add `encode` for `/api/assets/*/proxy`: compressing video wastes CPU and conflicts with `Range`.
- For a host with no public name (a LAN, a studio), use `tls internal` in the site block. Caddy then issues from its
  own local CA; install that CA's root (`caddy trust`, or copy
  `pki/authorities/local/root.crt` from Caddy's data directory) on each client. `auto_https` behaviour and
  `skip_install_trust` are in the Caddy docs.
- Run Caddy as a service (systemd unit shipped with the Caddy package, or `caddy run` under your service manager).
  Volt's own service files do not start it.

## nginx

```nginx
server {
    listen 80;
    server_name volt.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;                      # nginx 1.25.1 or newer; on older versions put http2 on the listen line
    server_name volt.example.com;

    ssl_certificate     /etc/letsencrypt/live/volt.example.com/fullchain.pem;   # certbot, or your own
    ssl_certificate_key /etc/letsencrypt/live/volt.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    add_header Strict-Transport-Security "max-age=31536000" always;

    client_max_body_size 64k;          # Volt's MAX_BODY; oversize gets nginx's own 413
    proxy_request_buffering on;        # default; also turns a chunked request into one with Content-Length

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";

        proxy_buffering off;           # media downloads stream; no temp files, Range passes through untouched
        proxy_connect_timeout 5s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;        # idle gap between reads, not total time; a long download is fine
    }
}
```

Notes:

- nginx would send `Host: 127.0.0.1:8765` by default; `proxy_set_header Host $host` keeps the public name, which is
  harmless with a token (see above) and keeps redirects and logs coherent.
- Do not set `proxy_cache`, `gzip on` or `slice` for this location.
- Certificates: certbot (`certbot --nginx -d volt.example.com`) or your organisation's CA. For a LAN with a private CA,
  install that CA on the clients.
- `proxy_read_timeout 60s` is fine because Volt has no long-lived responses. Raise it only if you add slow routes.

## Volt settings that go with it

```
VOLT_HOST=127.0.0.1
VOLT_PORT=8765
VOLT_TOKEN=<16+ characters, generated; not reused anywhere else>
VOLT_ACCESS_LOG=1        # optional; client= will read 127.0.0.1, see above
```

Put them in the settings file (`deploy/systemd/volt.env.example`, `deploy/windows/volt.env.example`) and see
[operations](operations.md) for the rest. The clients (`curl`, the desk, scripts) send
`Authorization: Bearer <token>` over `https://`.

## What was run (2026-10-30)

This was run on the Windows dev box, with both proxies as unprivileged processes listening on loopback high ports
(8443 and 8444) and an internal/self-signed certificate for `localhost`. Nothing was installed system-wide and no
firewall or trust store was changed; the clients passed `--cacert`.

| Component | Version |
|---|---|
| Volt | 0.1.0 at `dd88b59`, `python -m volt.serve --host 127.0.0.1 --port 8765`, `VOLT_TOKEN` set, `VOLT_ACCESS_LOG=1` |
| Python | 3.12.10 |
| Caddy | v2.11.7 (`tls internal`, `admin off`, the Caddyfile above with `localhost:8443` as the site) |
| nginx | 1.31.8 (Windows build, the config above on `127.0.0.1:8444`, the certificate Caddy issued for `localhost`) |
| curl | 8.21.0 (Schannel; HTTP/1.1 only, no HTTP/2 support in this build) |

Same checks through each proxy, with the result:

| Check | Caddy | nginx |
|---|---|---|
| `GET /health` over TLS | 200, body from Volt, `Strict-Transport-Security` present | 200, HSTS present |
| `GET /api/state` with no token | 401 | 401 |
| `GET /api/state` with bearer token | 200 | 200 |
| `POST /api/shows` with JSON body and `Content-Length` | 200, show created | 200 |
| `POST` with chunked body | 411 (Volt refuses chunked) | 200 (nginx buffered it) |
| `POST` body 70000 bytes (over the cap) | 413 | 413 |
| `GET /api/media-ticket`, then the ticket URL with `Range: bytes=0-9` and no `Authorization` | 206, `Content-Range: bytes 0-9/1048576`, 10 bytes | 206, same |
| `Range: bytes=1048000-` | 206, 576 bytes | 206, 576 bytes |
| Full download of a 1 MiB proxy | 200, SHA-256 identical to the file on disk | 200, identical |
| Ticket URL with a bad ticket | 401 | not run |
| Volt access log | `client=127.0.0.1` for every request | not run |

Without a token, direct to Volt with `Host: volt.example.com`: `/api/state` and `/` are `403 bad_host`, `/health` is 200;
with `Host: localhost` they are 200.

Not verified:

- **Public certificates.** Both proxies ran with an internal certificate. Let's Encrypt issuance (Caddy) and certbot
  (nginx) were not run; the directives are standard but untested here.
- **HTTP/2 and HTTP/3.** The curl used has no HTTP/2 support. Both proxies advertise it by default; a browser
  talking HTTP/2 to the proxy still reaches Volt over HTTP/1.1, so the Volt side is the same, but an HTTP/2 client
  was not exercised.
- **Linux.** Both ran on Windows. The configs contain no platform-specific directives except certificate paths.
- **The desk in a browser** over TLS (the page and its API calls are the same requests as above, by curl).
- **HTTP to HTTPS redirect** and **HSTS preload**: not exercised.
- **Long soak**, many concurrent media downloads, and slow-client behaviour.

## Code changes

None were needed. The checks above show the loopback `Host` guard, the bearer gate, signed media tickets,
`Range` and the body cap all behave correctly behind both proxies provided `VOLT_TOKEN` is set. The one real
limitation is the access log's client address (always the proxy); a trusted-proxy option that reads
`X-Forwarded-For` from `127.0.0.1` only could be added later and is deliberately not part of this change.

