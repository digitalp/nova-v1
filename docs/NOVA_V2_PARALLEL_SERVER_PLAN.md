# Nova V2 Parallel Server Plan

This plan runs Nova V2 as a separate app on the same server without affecting the current Nova instance on `/opt/avatar-server` and port `8001`.

## Deployment Shape

- Current Nova remains:
  - app root: `/opt/avatar-server`
  - service: `avatar-backend`
  - port: `8001`
- Nova V2 runs in parallel:
  - app root: `/opt/nova-v2`
  - service: `nova-v2`
  - port: `8011`

## Isolation Rules

Nova V2 must not share mutable runtime state with current Nova unless explicitly intended.

- separate `.env`
- separate `config/`
- separate `data/metrics.db`
- separate `logs/`
- separate systemd unit
- separate HA REST commands such as `nova_v2_announce`, `nova_v2_chat`, and `nova_v2_visual_event`

Shared components are acceptable only where intentional:

- Ollama server
- Home Assistant instance
- base source tree used to scaffold the new app

## Required Code Support

To support parallel deployment, Nova now needs runtime-configurable app roots instead of assuming `/opt/avatar-server`.

The first build step is:

- `avatar_backend/runtime_paths.py`
- dynamic `NOVA_APP_ROOT`
- dynamic `NOVA_ENV_FILE`
- metrics DB path derived from app root
- admin and main paths derived from app root

## Server Build Steps

1. Scaffold the new app root

Use:

```bash
sudo /opt/avatar-server/scripts/scaffold_parallel_app.sh /opt/nova-v2
```

2. Create the virtualenv for the new app

```bash
cd /opt/nova-v2
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

3. Fill in `/opt/nova-v2/.env`

Minimum settings:

- `API_KEY`
- `HA_URL`
- `HA_TOKEN`
- `PORT=8011`
- `PUBLIC_URL`
- model-provider settings

4. Enable and start the new service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nova-v2
```

5. Add parallel Home Assistant REST commands

Do not point existing automations at V2 yet. Add new commands alongside the current ones:

- `nova_v2_announce`
- `nova_v2_chat`
- `nova_v2_visual_event`
- `nova_v2_doorbell`
- `nova_v2_motion_outdoor`
- `nova_v2_motion_driveway`

6. Test with isolated HA automations

Start with a few dedicated V2-only automations before any cutover:

- package delivery flow
- outdoor motion visual flow
- driveway vehicle watch

7. Cut over only after validation

Switch one automation family at a time from current Nova to Nova V2.

## Suggested Port and URL Plan

- current Nova: `http://192.168.0.249:8001`
- Nova V2: `http://192.168.0.249:8011`

If reverse proxying later:

- current Nova: `/nova`
- Nova V2: `/nova-v2`

## Initial Build Output

This repo now includes:

- runtime app-root support
- a scaffold script for a parallel app root
- this deployment plan doc

That is enough to start a safe side-by-side V2 deployment on this server.
