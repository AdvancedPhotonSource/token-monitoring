# Deployment (arecibo)

Sibling of `Califone_Dashboard` / `JMD_project` / `XEOL_studio` / `CAI` on
`arecibo.xray.aps.anl.gov`. Runs as user `haskels` under `screen`, TLS
terminated by uvicorn using the shared InCommon cert at
`~/.jmd/tls/{key,cert}.pem`.

## First-time install

```bash
# On arecibo, as haskels
mkdir -p /home/beams/HASKELS/token_monitoring
cd /home/beams/HASKELS/token_monitoring
git clone https://github.com/AdvancedPhotonSource/token-monitoring.git .
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# Data + auth SQLite files. Committed .gitignore excludes *.sqlite.
mkdir -p data
```

Create `~/.token_monitoring/env` (NEVER committed):

```bash
TM_PAM_ENABLED=1
TM_DB_PATH=/home/beams/HASKELS/token_monitoring/data/usage.sqlite
TM_SESSION_DB_PATH=/home/beams/HASKELS/token_monitoring/data/sessions.sqlite
TM_UPSTREAM_URL=https://apps.inside.anl.gov/argoapi
```

Start:

```bash
screen -dmS token-monitoring bash deploy/serve_with_restart.sh
```

Verify:

```bash
curl -k https://arecibo.xray.aps.anl.gov:9004/health
```

## Staged smoke on port 9005

For any first deploy or risky change, launch on the unused 9005 first so
9004 keeps serving:

```bash
PORT=9005 screen -dmS token-monitoring-stg bash deploy/serve_with_restart.sh
curl -k https://arecibo.xray.aps.anl.gov:9005/health
# Point one VS Code plugin at :9005, drive one chat, confirm the row lands.
screen -X -S token-monitoring-stg quit
```

## Update flow

Per the standing "gitlab-first" rule — commit + push first, deploy second:

```bash
# On your workstation
git commit -am "..."
git push origin main

# On arecibo
cd /home/beams/HASKELS/token_monitoring
git pull
.venv/bin/pip install -e .  # only if pyproject changed
screen -X -S token-monitoring quit
screen -dmS token-monitoring bash deploy/serve_with_restart.sh
```

## Ports

| Service    | Port | Notes                                        |
|------------|------|----------------------------------------------|
| CAI        | 9001 | Django, genesis_proxy TLS                    |
| JMD        | 9002 | uvicorn + PAM                                |
| XEOL       | 9003 | uvicorn                                      |
| **token-monitoring** | **9004** | **this service**                             |
| Califone   | 9008 | uvicorn + PAM                                |
| argo_relay | 7000 | JMD's stdlib Argo forward-proxy. **DO NOT clobber.** |

## Labeling user hashes

The dashboard shows the first 8 hex chars of a user_hash by default. To
show a friendly name (e.g. an ANL username):

```bash
.venv/bin/token-monitoring label-key haskels haskels
```

That computes SHA-256(`haskels`) and inserts a label row. You can also
pass a hash directly instead of the plaintext.
