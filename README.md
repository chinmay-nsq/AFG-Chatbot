# AFG Chatbot — EC2 Deployment

Prototype deployment of the AFG Aberdeen Qatar chatbot (`afg_new.py`) to an AWS EC2
instance, using Docker Compose for the Flask app + Postgres, and nginx as the
reverse proxy. No domain is used — the app is reached via the EC2 public IP.

## Stack

- **App**: Flask + gunicorn (`afg_new.py`), containerized via `Dockerfile`
- **DB**: Postgres 16, containerized via `docker-compose.yml` (`afg_postgres` service)
- **Proxy**: nginx (`nginx_afg.conf`) terminating on port 80, forwarding to the app
  container on `127.0.0.1:5050`
- **Seed data**: `init_db()` runs at module import time (not gated behind
  `if __name__ == "__main__"`), so it fires under gunicorn too. DDL uses
  `CREATE TABLE IF NOT EXISTS` and seed inserts use `ON CONFLICT DO NOTHING` —
  safe to run on every container start/restart.

## 1. Launch the EC2 instance

- Ubuntu 22.04/24.04 LTS, **t2.micro** (free-tier eligible). Note: this is a
  cost tradeoff, not a capacity recommendation — 1GB RAM is tight for
  Postgres + gunicorn workers running together. Watch for OOM kills under load
  (`dmesg` / `docker compose logs`); a swapfile (step 2a) helps but isn't a
  substitute for more RAM if the app gets real traffic.
- Security group inbound rules:
  - `22` (SSH) — restrict to your IP
  - `80` (HTTP) — `0.0.0.0/0`
  - `443` (HTTPS) — not needed for now (see [HTTPS / voice input](#https--voice-input-deferred) below)
  - **Do not open `5050` or `5432`** — compose binds both to `127.0.0.1` only on
    the host, so they're not reachable externally even if you forget this rule,
    but keep the security group tight regardless.

### 1a. (Recommended on t2.micro) Add a swapfile

1GB RAM with no swap risks the OOM killer taking out Postgres or gunicorn
under any memory pressure (e.g. `docker compose build`). A small swapfile is
cheap insurance:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Install Docker + nginx on the instance

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin nginx
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # log out/in after this to use docker without sudo
```

## 3. Ship the code

Copy this `AFG` folder to the instance (excluding `venv/`, which is Windows-only
and not needed in the container). Use `scp -r`, `rsync`, or `git` if the repo is
pushed somewhere.

## 4. Create `.env` on the server

Base it on `.env.example`:

```
OPENAI_API_KEY=...
DEEPGRAM_API_KEY=...
CARTESIA_API_KEY=...
PG_HOST=afg_postgres
PG_PORT=5432
PG_DB=afg_school
PG_USER=postgres
PG_PASSWORD=<strong-password>
```

Do not copy a local dev `.env` as-is — recreate it on the server with real
production values.

## 5. Build and start

```bash
cd /path/to/AFG
docker compose up -d --build
docker compose logs -f afg_chatbot   # confirm "Database initialized" appears
```

`restart: unless-stopped` in `docker-compose.yml` means both containers survive
reboots automatically, as long as Docker itself starts on boot (handled by
`systemctl enable docker` above).

## 6. Configure nginx

Copy `nginx_afg.conf` to `/etc/nginx/sites-available/afg`, symlink it, and set
`server_name` to match any host (since there's no domain):

```bash
sudo cp nginx_afg.conf /etc/nginx/sites-available/afg
sudo ln -s /etc/nginx/sites-available/afg /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default   # avoid the stock nginx page competing on port 80
```

In `/etc/nginx/sites-available/afg`, change:

```nginx
server_name your-domain-or-ec2-ip;
```

to:

```nginx
server_name _;
```

(`_` matches any Host header — fine with no domain.)

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 7. Verify

Visit `http://<ec2-public-ip>/` — the chat UI should load. Test with a query
like "roll number 1 attendance" to confirm it hits the seeded dummy DB.

## HTTPS via CloudFront + ACM

**Current decision: put CloudFront in front of the EC2 instance**, using a
free AWS Certificate Manager (ACM) cert. This gives a trusted
`https://xxxxxxxxxxxxxx.cloudfront.net` URL with no domain needed, so mic
access (`getUserMedia`) works for real users — CloudFront terminates TLS at
the edge and forwards to EC2 over HTTP internally.

Why CloudFront over the other options: cheaper than an ALB at low/prototype
traffic (mostly pay-per-request instead of an hourly minimum), and no domain
needed (unlike Route 53 + Let's Encrypt). The tradeoff is that CloudFront is a
caching CDN by design, so `/chat-stream` (SSE) and `/tts` (chunked audio) must
be explicitly configured to bypass caching and buffering, or streaming breaks.

### 1. Request an ACM certificate

ACM certs used by CloudFront **must be requested in `us-east-1`**, regardless
of which region your EC2 instance is in.

- Console: **Certificate Manager** (us-east-1) → Request a public certificate
- Since there's no domain, you can't use ACM's normal DNS-validated cert for a
  custom domain name. Instead, skip requesting your own cert entirely —
  CloudFront's *default* `*.cloudfront.net` certificate (already issued and
  trusted by all browsers) covers this automatically. **No ACM step is
  actually required** unless you later attach a custom domain to the
  distribution.

### 2. Create the CloudFront distribution

- **Origin domain**: your EC2 public IP or public DNS (e.g.
  `ec2-x-x-x-x.compute-1.amazonaws.com`)
- **Origin protocol policy**: HTTP only (port 80) — CloudFront-to-EC2 traffic
  stays internal to AWS; the public-facing leg (browser-to-CloudFront) is what
  gets HTTPS
- **Viewer protocol policy**: Redirect HTTP to HTTPS
- Leave **Alternate domain names (CNAMEs)** and the custom certificate field
  empty — this is what makes CloudFront use its free default cert and give
  you the `*.cloudfront.net` URL.

### 3. Cache behaviors — critical for streaming

By default, CloudFront caches GET responses and buffers content before
forwarding, which breaks SSE and chunked audio. Add two extra **behaviors**
(Distribution → Behaviors → Create behavior) before the default `*` catch-all:

**Path pattern `/chat-stream`:**
- Cache policy: **CachingDisabled** (managed policy)
- Origin request policy: **AllViewerExceptHostHeader** (managed policy) — forwards
  all headers/cookies/query strings needed for the POST body and SSE
  negotiation
- Allowed HTTP methods: GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE (POST is
  required — `/chat-stream` is a POST endpoint)

**Path pattern `/tts`:**
- Same settings as above (CachingDisabled, AllViewerExceptHostHeader, all
  methods including POST)

**Default behavior (`*`)** can keep standard caching for the static HTML/JS
UI response from `/`.

### 4. Origin response timeout

CloudFront's origin response timeout defaults to 30s (max 60s configurable
under **Origin → Response timeout**). If a full chat/TTS response can take
longer than that under load, raise it to 60s. This is separate from — and in
addition to — nginx's existing `proxy_read_timeout 300s`, which still applies
to the EC2-internal leg.

### 5. nginx changes

None required for CloudFront's default cert path — nginx keeps listening on
plain port 80 exactly as already configured; CloudFront is what's adding
HTTPS on the public side. Update `server_name` in `nginx_afg.conf` to the
distribution's origin domain (or leave it as `_` to match any Host header,
same as the current HTTP-only setup).

### 6. Verify

After the distribution deploys (~5-15 min the first time), visit
`https://xxxxxxxxxxxxxx.cloudfront.net/` — confirm:
- The chat UI loads over HTTPS with a valid padlock (no cert warning)
- Text chat streams incrementally (not all-at-once) — confirms
  `/chat-stream`'s cache behavior is correctly bypassing buffering
- The mic button requests permission and voice input works — confirms secure
  context is satisfied

### Cost note

CloudFront is pay-per-request/data-transfer with a perpetual free tier (1TB
data transfer + 10M requests/month, free tier terms may vary) — for a
low-traffic prototype this should cost close to $0/month, much cheaper than
the ALB's ~$16-20/month hourly minimum. Watch data transfer if TTS audio
volume grows, since audio streaming is more bytes than text chat.
