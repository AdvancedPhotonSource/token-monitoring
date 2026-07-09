# token-monitoring (INACTIVE)

THIS REPO IS INACTIVE AND NOT CONNECTED TO ANY DASHBOARD. 

Anthropic-Messages-API proxy that forwards to Argonne's Argo LLM gateway,
records per-user token usage in SQLite, and exposes a dashboard on
`arecibo:9014`.

Built as a stopgap so VS Code plugin users (who don't get the token
counts that Claude Code CLI users see) can see their own consumption
until the Argo team's native dashboard ships. Once that lands, this
service can be shut down or kept running for the historical view — the
DB is standalone and portable.

## What it does

1. **Proxies `/v1/messages`** (Anthropic Messages API shape) from VS Code
   plugins to Argo. Streaming and non-streaming both supported; bytes
   pass through unchanged.
2. **Counts tokens per request** by parsing the `usage` field in the
   response (buffered for non-streaming; scanned live from
   `message_start` / `message_delta` SSE events for streaming).
3. **Records one row per request** in SQLite, grouped by SHA-256 of the
   caller's API key.
4. **Serves a dashboard** on the same port (PAM-gated with ANL domain
   credentials, same auth stack as SSHd to arecibo).

## Pointing a VS Code plugin at it

Whatever your plugin calls the Anthropic base URL setting, set it to:

```
ANTHROPIC_BASE_URL=https://arecibo.xray.aps.anl.gov:9014
ANTHROPIC_API_KEY=<your ANL username>
```

Then use the plugin normally — everything flows through the proxy and
lands in the dashboard.

Confirmed-compatible plugins: any plugin that speaks the Anthropic
Messages API and lets you configure a custom base URL. Plugins that only
speak the OpenAI Chat Completions format won't work directly (that's a
separate adapter that isn't in scope for v1).

## Dashboard

Open `https://arecibo.xray.aps.anl.gov:9014/` in a browser (any device
on the ANL network). Sign in with your ANL domain credentials.

Tabs:
- **Overview** — tokens/day, top users, top models, current-month totals.
- **Requests** — paginated, filterable log of every request.
- **User** — drill-down on any user by clicking their name.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

Run against a local mock upstream:

```bash
TM_PAM_ENABLED=0 \
TM_UPSTREAM_URL=https://apps.inside.anl.gov/argoapi \
.venv/bin/token-monitoring serve --host 127.0.0.1 --port 9014
```

## Deploy

See [`deploy/README.md`](deploy/README.md) for the arecibo runbook.

## Repo layout

```
src/token_monitoring/
  api.py         FastAPI app wiring
  proxy.py       /v1/* forward + SSE tee + token accounting
  dashboard.py   /api/* JSON endpoints for the SPA
  db.py          SQLite storage: requests + key_labels
  auth_pam.py    PAM sign-in (vendored from Califone; env-prefix TM_)
  cli.py         serve, init-db, label-key, hash-key
  config.py      env-var readers
web/index.html   single-file Tailwind + Alpine + Chart.js dashboard
deploy/          serve_with_restart.sh + runbook
tests/           pytest coverage for db, proxy (streaming + not), auth
```

## Related services on arecibo

| Service    | Port | What it does                                       |
|------------|------|----------------------------------------------------|
| CAI        | 9001 | Django internal website                            |
| JMD        | 9002 | OCI job-management dashboard                       |
| XEOL       | 9003 | Beamline XEOL Studio                               |
| **token-monitoring** | **9014** | **this service**                                   |
| Califone   | 9008 | PBS + GPU node dashboard                           |
| argo_relay | 7000 | JMD's stdlib Argo forward-proxy (byte-forwarder)   |
