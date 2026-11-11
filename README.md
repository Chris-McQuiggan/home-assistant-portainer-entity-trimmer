# Home Assistant Portainer Entity Trimmer

[![Tests](https://github.com/Chris-McQuiggan/home-assistant-portainer-entity-trimmer/actions/workflows/tests.yml/badge.svg?branch=master)](https://github.com/Chris-McQuiggan/home-assistant-portainer-entity-trimmer/actions/workflows/tests.yml)

A command-line tool to disable Home Assistant entities created by the Portainer
integration. By default, it targets entities whose IDs begin with `ix_`, as
used by TrueNAS and HexOS apps. Other integrations and non-matching Portainer
entities are left alone.

Start with a dry run, review the targets, and then execute. Mutating runs prompt
for confirmation and write a recovery manifest before sending updates. Undo
checks current entity state and skips entities that are missing or have changed
since the manifest was written.

> [!WARNING]
> This tool changes Home Assistant's entity registry. Back up Home Assistant
> and review a dry run before executing. Use a long-lived token only from a
> trusted machine, and never commit your `.env` file.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Quick start](#quick-start)
- [Undo and recovery](#undo-and-recovery)
- [Targeting options](#targeting-options)
- [Command reference](#command-reference)
- [Tests](#tests)
- [Behavior and limitations](#behavior-and-limitations)
- [License](#license)

## Requirements

- **Home Assistant**: any recent version supporting the WebSocket commands
  `config/entity_registry/list`, `config/device_registry/list` and
  `config/entity_registry/update`. Tested against HAOS Core 2026.11.x.
  (Older releases than roughly mid-2024 may lack
  `config/entity_registry/list` and fail with `unknown_command`.)
- **Python 3.10+** (uses `X | None` type syntax).
- Python packages: `websockets`, `python-dotenv` (see `requirements.txt`).
- A **long-lived access token** for a Home Assistant user authorized to update
  the entity registry. Create one from your profile under **Security → Long-lived
  access tokens**; copy it when created because it is shown only once.

## Installation

All commands assume you are already inside the cloned project folder. Create
an isolated virtual environment, activate it, and install dependencies:

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks the activation script, run:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first, then
retry the Activate line.

**Linux / macOS:**

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
pip install -r requirements.txt
```

Deactivate the venv later with `deactivate`. The venv lives in the `.venv`
folder — recreate it anytime by deleting the folder and rerunning the steps.
Keep it active while running the commands below. In a new terminal, activate
it again with `\.venv\Scripts\Activate.ps1` on Windows or
`source .venv/bin/activate` on Linux/macOS.

## Configuration

The script reads settings from environment variables, and optionally from a
`.env` file in the project folder. Precedence: **CLI flag > .env / environment
variable > built-in default**.

Copy the template and fill it in:

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Then edit `.env`:

| Variable                | Purpose                                      | Default                                       |
| ----------------------- | -------------------------------------------- | --------------------------------------------- |
| `HA_WS_URL`             | WebSocket endpoint of your HA instance       | `ws://homeassistant.local:8123/api/websocket` |
| `HA_TOKEN`              | Long-lived access token (required)           | —                                             |
| `HA_PREFIX`             | Entity-ID prefix of managed containers       | `ix_`                                         |
| `HA_KEEP`               | Comma-separated entity-ID substrings to keep | _(none)_                                      |
| `HA_CONNECT_TIMEOUT_S`  | WebSocket connection timeout in seconds      | `10`                                          |
| `HA_RESPONSE_TIMEOUT_S` | Timeout per WebSocket response in seconds    | `30`                                          |

- `HA_WS_URL`: use the same hostname as your Home Assistant browser URL, with
  `/api/websocket` appended. Use `ws://` for HTTP or `wss://` for HTTPS, and
  include the port if HA doesn't use the default one. `ws://` does not encrypt
  the access token or API traffic; use it only on a trusted network. Prefer
  `wss://` when connecting remotely or across an untrusted network.
- `HA_TOKEN` has the permissions of its Home Assistant user. Keep `.env`
  private, never commit it, and revoke the token from your HA profile when it
  is no longer needed.
- `HA_PREFIX`: use the _underscore_ form as it appears in entity IDs
  (`ix_` not `ix-`). An empty `HA_PREFIX=` falls back to the default; the
  "target everything" mode is only reachable via the explicit CLI form
  `--prefix ""`.
- `HA_KEEP`: e.g. `HA_KEEP=out_of_memory,health`. Empty means ALL prefixed
  entities are disabled.
- `HA_CONNECT_TIMEOUT_S` and `HA_RESPONSE_TIMEOUT_S`: optional positive whole
  numbers of seconds. Increase them if your Home Assistant instance responds
  slowly or is reached over a high-latency connection.

`--prefix` and `--keep` override their environment values for a single run.
Timeouts are configured through the environment or `.env` file. You can also
export variables in your shell (`export HA_TOKEN=...` /
`$env:HA_TOKEN = "..."`) instead of using a `.env` file.

## Quick start

After configuring `.env`, inspect the entity kinds and preview the changes.
Dry runs do not modify Home Assistant.

```bash
# Inspect the entity kinds in scope:
python main.py --dry-run --kinds

# Preview what would change, preserving OOM and health entities:
python main.py --dry-run --keep out_of_memory,health

# Diagnose zero-match problems (dump platform names + sample ids):
python main.py --platforms

# Execute the reviewed changes; you'll be asked to confirm:
python main.py --keep out_of_memory,health --enable-kept
```

Add `--yes` to confirm non-interactively. CLI options override values from the
environment or `.env` file.

`--keep` contains case-insensitive substrings matched against entity IDs. If it
is omitted or empty, all enabled entities in scope are candidates for disabling.
`--enable-kept` also enables matching entities that are already disabled.
Every dry run includes a `WOULD ENABLE` section for kept-but-disabled entities.
Its heading makes clear whether `--enable-kept` was requested; without the flag,
the section is informational and no entities will be enabled.

## Undo and recovery

Each execute run writes a JSON recovery manifest to `manifests/` before sending
updates. It includes the previous and intended state for each entity. If a run
is interrupted, the manifest can be used to undo updates that took effect.

```bash
# Undo the newest unfinished run. Repeat to undo runs from newest to oldest.
python main.py --undo

# Or choose a specific manifest:
python main.py --undo manifests/undo-2026-11-11-063000-123456.json
```

Replace the example filename with the recovery manifest printed by your run.

Before restoring, the tool checks the live registry. It skips entities that no
longer exist or whose `disabled_by` value no longer matches the manifest's
intended state. This is a best-effort check; Home Assistant does not make the
check and update atomic. Entities already at their original state count as
already undone, so retries only attempt remaining changes. Changed entities or
failed updates leave the manifest unfinished; bare `--undo` retries that
newest unfinished manifest instead of moving on to an older run. Missing
entities are skipped and do not block the undo chain, but their manifest is
kept.

After a fully successful undo, the tool offers to delete the manifest. Decline
to keep it. `--yes` confirms entity updates but does not delete the manifest;
use `--delete-manifest` with `--undo` to request deletion non-interactively.

## Targeting options

Managed-container prefixes other than `ix_*` work via `--prefix` (or
`HA_PREFIX`) — use the _underscore_ form, as it appears in entity IDs
(`app_` not `app-`):

```bash
python main.py --dry-run --prefix app_
```

To target **all** Portainer entities regardless of prefix:

```bash
python main.py --dry-run --prefix ""
```

Use this broad mode with care. The dry run prints a warning. After executing,
changes should normally appear without a reload or restart. If entities still
look stale, reload the UI or restart Home Assistant.

## Command reference

| Flag                    | Purpose                                                                                                         |
| ----------------------- | --------------------------------------------------------------------------------------------------------------- |
| `-p, --prefix p`        | Entity-ID prefix identifying managed containers (env `HA_PREFIX`, default `ix_`; `""` = all Portainer entities) |
| `-k, --keep a,b,c`      | Comma-separated entity-ID substrings to keep (env `HA_KEEP`, default: none — disable all targeted)              |
| `-n, --dry-run`         | Preview only; change nothing                                                                                    |
| `-y, --yes`             | Confirm registry changes without an interactive prompt                                                          |
| `-g, --by-kind`         | Group dry-run output by identical entity-kind sets                                                              |
| `-e, --enable-kept`     | Execute mode also enables kept-but-disabled entities                                                            |
| `-u, --undo [MANIFEST]` | Restore a manifest; without a path, use the newest unfinished run                                               |
| `--delete-manifest`     | Delete the manifest after a fully successful undo; requires `--undo`                                            |
| `--kinds`               | Show entity-kind counts for targeted containers                                                                 |
| `--platforms`           | Show integration platform names and sample entity IDs for debugging                                             |
| `-h, --help`            | Show all flags with descriptions                                                                                |

## Tests

Install the requirements, then run the test suite from the project directory:

```bash
python -m unittest discover -v
```

The tests use Python's standard-library `unittest` and do not connect to Home
Assistant.

## Behavior and limitations

- **New containers**: future apps on the managed host get fresh, enabled
  entities. Re-run the script after adding apps.
- **Refresh behavior**: registry changes normally appear without a restart,
  though UI counts may lag. If entities still look stale, reload the UI or
  restart Home Assistant.
- **Recovery**: `--undo` restores prior state only for entities whose current
  values still match the manifest. Entities can also be re-enabled in Home
  Assistant under **Settings → Devices & services → Entities**.
- **Entity IDs can shift**: a disabled entity re-enabled via `--undo` (or the
  UI) usually reappears under its original ID, but naming collisions can
  produce `_2` suffixed IDs — verify in Settings → Devices & Entities.
- **Large runs take a while**: updates are sent one at a time over the
  WebSocket API; thousands of entities means minutes. Progress prints every
  200 updates. Connection attempts time out after 10s; missing WebSocket
  responses time out after 30s.
- **Matching detail**: container names use dashes (`ix-adguard-home`) but
  entity IDs use underscores (`ix_adguard_home_...`); the script matches the
  underscore prefix. Container/kind splitting uses the owning device's
  registry name, so unusual container names are handled correctly.
- `--dry-run` output is truncated in flat mode (first 30); `--by-kind` shows all.

## License

See [LICENSE](LICENSE) for licensing terms.
