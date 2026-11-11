"""Trim Portainer entities in Home Assistant, selectively.

Goal:
- Containers matching a name prefix (default: ix_* for TrueNAS/HexOS apps):
  disable all Portainer entities except whitelisted kinds (--keep).
- Everything else: left untouched.

Only entities from the Portainer integration are ever modified, and only
those whose entity ID starts with the prefix. Passing --prefix "" (empty)
targets ALL Portainer entities.

Configuration precedence: CLI flag > environment variable / .env file > default.

Execute runs write a JSON recovery manifest into the manifests/ folder before
changing entities. Pass it to --undo to restore states that still match the
run's expected result. The folder is created on demand.

One-off maintenance script. Requires: pip install -r requirements.txt

Usage:
  # see what entity name patterns exist for ix-* before deciding:
  python main.py --dry-run --list-suffixes

  # preview what would be disabled (disables nothing):
  python main.py --dry-run

  # keep e.g. OOM sensors and health sensors on prefixed containers:
  python main.py --dry-run --keep out_of_memory,health

  # preview with containers grouped by identical sensor sets:
  python main.py --dry-run --group --keep out_of_memory

  # diagnose zero-match problems (dump platform names + sample ids):
  python main.py --list-platforms

  # use a different container prefix:
  python main.py --dry-run --prefix app_

  # target ALL portainer entities regardless of prefix:
  python main.py --dry-run --prefix ""

  # actually disable (junk) and enable (kept-but-disabled) entities
  # (writes an undo manifest):
    python main.py --enable-kept --keep out_of_memory
    # confirm at the prompt, or add --yes for non-interactive use

    # revert changes using the manifest filename printed by the run:
    python main.py --undo manifests/undo-2026-11-11-063000-123456.json

Env vars (also read from a .env file in the project folder — see .env.example):
  HA_WS_URL   e.g. wss://ha.example.org/api/websocket
  HA_TOKEN    long-lived access token (Profile -> Security -> Long-lived access tokens)
  HA_PREFIX   optional, same as --prefix (empty is only possible via the CLI flag)
  HA_KEEP     optional, comma-separated, same as --keep
"""

import argparse
import asyncio
import datetime
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
import websockets
from websockets.exceptions import WebSocketException

load_dotenv()  # reads .env from the project folder if present

HA_WS_URL = os.environ.get("HA_WS_URL", "ws://homeassistant.local:8123/api/websocket")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
# Container names use dashes (ix-adguard-home) but entity ids use underscores
# (binary_sensor.ix_adguard_home_...). Match on the underscore form.
DEFAULT_PREFIX = "ix_"
DEFAULT_KEEP: list[str] = []  # entity_id substrings to keep; empty = disable all
CONNECT_TIMEOUT_S = 10   # seconds to wait for the WebSocket connection
RESPONSE_TIMEOUT_S = 30  # seconds to wait for a WebSocket response
PROGRESS_EVERY = 200     # print progress during execute every N updates
MANIFESTS_DIR = Path("manifests")  # undo manifests are written here (gitignored)


def parse_keep(raw: str | None) -> list[str]:
    """Parse a comma-separated keep list from CLI/env; empty -> []."""
    return [k.strip().lower() for k in (raw or "").split(",") if k.strip()]


def select_portainer_entities(entities, prefix, keep, enable_kept):
    """Select targeted, kept, disable, and re-enable candidates."""
    portainer = [e for e in entities if e.get("platform") == "portainer"]
    targeted = [
        e for e in portainer
        if not prefix or e["entity_id"].split(".", 1)[1].startswith(prefix)
    ]
    is_kept = lambda e: any(k in e["entity_id"].lower() for k in keep)
    targets = [
        e for e in targeted
        if not is_kept(e) and not e.get("disabled_by")
    ]
    kept_disabled = [
        e for e in targeted
        if is_kept(e) and e.get("disabled_by")
    ] if enable_kept else []
    kept = [e for e in targeted if is_kept(e)]
    return portainer, targeted, targets, kept_disabled, kept


def slugify(name: str) -> str:
    """Mirror HA entity-id slugification: lowercase, non-alnum -> underscore."""
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def split_entity(entity_id: str, device_slug: str | None,
                 prefix: str) -> tuple[str, str]:
    """Split an entity id into (container, kind).

    Preferred: strip the owning device's slugified name from the object id,
    which is exact (handles container names with or without digits).
    Fallback: regex heuristic if the device lookup didn't help.
    """
    dom, tail = entity_id.split(".", 1)
    if device_slug and tail.startswith(device_slug + "_"):
        return device_slug, tail[len(device_slug) + 1:]
    body = tail[len(prefix):] if prefix and tail.startswith(prefix) else tail
    m = re.match(r"^([a-z0-9_]+?)_(\d+)_(.+)$", body)
    if m:
        return (prefix + m.group(1)) if prefix else m.group(1), m.group(3)
    return body, body


async def recv_matching(ws, msg_id):
    """Receive messages until one with our id arrives; return it."""
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == msg_id:
            return msg


async def send(ws, msg_id, payload):
    payload["id"] = msg_id
    await ws.send(json.dumps(payload))
    try:
        return await asyncio.wait_for(
            recv_matching(ws, msg_id), timeout=RESPONSE_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"No response for Home Assistant request {msg_id} within "
            f"{RESPONSE_TIMEOUT_S}s.") from exc


async def recv_json_with_timeout(ws, waiting_for):
    try:
        raw_message = await asyncio.wait_for(
            ws.recv(), timeout=RESPONSE_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"Timed out waiting for {waiting_for} from Home Assistant after "
            f"{RESPONSE_TIMEOUT_S}s.") from exc
    return json.loads(raw_message)


def check(resp, action):
    """Fail loudly with HA's actual error message if a call failed."""
    if resp is None:
        raise SystemExit(f"{action}: no response received.")
    if not resp.get("success", True) or ("result" not in resp and "event" not in resp):
        err = resp.get("error", resp)
        raise SystemExit(f"{action} failed. HA said: {json.dumps(err)[:500]}")


def annotate(entities, dev_slug_by_id, prefix):
    """Attach _cname (container) and _kind (suffix) to each entity dict."""
    for e in entities:
        slug = dev_slug_by_id.get(e.get("device_id"))
        cname, kind = split_entity(e["entity_id"], slug, prefix)
        e["_cname"] = cname
        e["_kind"] = f"{e['entity_id'].split('.', 1)[0]}.{kind}"


def print_shape_grouped(entities, verb):
    """Bucket containers by their identical entity-kind signature.

    Each unique signature (set of domain.kind) is printed once,
    followed by all container names sharing it. Small signatures first.
    """
    per_container = defaultdict(set)
    per_container_count = Counter()
    disabled_flags = defaultdict(set)
    for e in entities:
        per_container[e["_cname"]].add(e["_kind"])
        per_container_count[e["_cname"]] += 1
        if e.get("disabled_by"):
            disabled_flags[e["_cname"]].add(e["disabled_by"])
    by_signature = defaultdict(list)
    for cname, kinds in per_container.items():
        by_signature[frozenset(kinds)].append(cname)
    for sig in sorted(by_signature, key=lambda s: (len(s), sorted(s))):
        names = sorted(by_signature[sig])
        total = sum(per_container_count[n] for n in names)
        flagged = [f"{n} (disabled_by: {', '.join(sorted(disabled_flags[n]))})"
                   for n in names if n in disabled_flags]
        print(f"\n  {verb} {len(names)} container(s), {total} entities — "
              f"kinds: {', '.join(sorted(sig))}")
        for n in names:
            print(f"      {n}")
        for f in flagged:
            print(f"      {f}")


async def apply_updates(ws, msg_id_start, ops):
    """Send a batch of entity-registry updates.

    ops: list of dicts with keys entity_id and disabled_by (the NEW value).
    Returns (succeeded, failed) as lists of entity ids. Prints progress.
    """
    ok, failed = [], []
    msg_id = msg_id_start
    for i, op in enumerate(ops, 1):
        resp = await send(ws, msg_id, {
            "type": "config/entity_registry/update",
            "entity_id": op["entity_id"],
            "disabled_by": op["disabled_by"],
        })
        if resp.get("success"):
            ok.append(op["entity_id"])
        else:
            failed.append(op["entity_id"])
            print(f"FAILED: {op['entity_id']} {resp.get('error')}")
        if i % PROGRESS_EVERY == 0:
            print(f"  ... {i}/{len(ops)} processed", flush=True)
        msg_id += 1
    return ok, failed


def select_undo_ops(manifest_ops, current_disabled_by):
    """Classify entities as restorable, missing, already restored, or changed."""
    restore, missing, already_restored, changed = [], [], [], []
    for op in reversed(manifest_ops):
        entity_id = op["entity_id"]
        if entity_id not in current_disabled_by:
            missing.append(entity_id)
        elif current_disabled_by[entity_id] == op["was_disabled_by"]:
            already_restored.append(entity_id)
        elif current_disabled_by[entity_id] != op["new_disabled_by"]:
            changed.append(entity_id)
        else:
            restore.append({
                "entity_id": entity_id,
                "disabled_by": op["was_disabled_by"],
            })
    return restore, missing, already_restored, changed


def find_latest_undo_manifest(manifests_dir):
    """Return the newest manifest not marked as completely undone."""
    if not manifests_dir.exists():
        return None

    candidates = []
    for path in manifests_dir.glob("undo-*.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Cannot inspect undo manifest {path}: {exc}")
        if not isinstance(manifest, dict) or not isinstance(
                manifest.get("ops"), list):
            raise SystemExit(f"Invalid undo manifest {path}: expected an object "
                             "with an ops list.")
        if manifest.get("undo_status") == "completed" or not manifest["ops"]:
            continue

        try:
            when = datetime.datetime.fromisoformat(manifest["when"]).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            when = 0
        candidates.append((when, path.stat().st_mtime, path.name, path))

    return max(candidates)[-1] if candidates else None


def confirm_mutations(assume_yes, description):
    """Require explicit confirmation before changing Home Assistant."""
    if assume_yes:
        return
    try:
        answer = input(f"{description} Type 'yes' to continue: ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        raise SystemExit("Cancelled. Enter y or yes, or pass --yes to confirm "
                         "non-interactively.")


def maybe_delete_undo_manifest(manifest_path, assume_yes, delete_requested):
    """Offer cleanup after a clean undo; never delete implicitly with --yes."""
    if delete_requested:
        should_delete = True
    elif assume_yes:
        print("Recovery manifest retained; pass --delete-manifest to remove it "
              "non-interactively.")
        return False
    else:
        try:
            answer = input("Undo completed successfully. Delete its recovery "
                           "manifest? [y/N]: ")
        except EOFError:
            answer = ""
        should_delete = answer.strip().lower() in {"y", "yes"}

    if not should_delete:
        print(f"Recovery manifest retained: {manifest_path}")
        return False
    try:
        manifest_path.unlink()
    except OSError as exc:
        print(f"Could not delete recovery manifest {manifest_path}: {exc}")
        return False
    print(f"Deleted recovery manifest: {manifest_path}")
    return True


def write_manifest(path, manifest):
    """Atomically persist a manifest so an interrupted run remains recoverable."""
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary_path.replace(path)


async def apply_journaled_updates(ws, msg_id_start, req_ops, manifest,
                                 manifest_path):
    """Apply updates and persist whether the batch completed or was interrupted."""
    try:
        ok, failed = await apply_updates(ws, msg_id_start, req_ops)
    except (TimeoutError, OSError, WebSocketException) as exc:
        manifest["status"] = "interrupted"
        manifest["error"] = str(exc)
        write_manifest(manifest_path, manifest)
        raise SystemExit(f"Could not finish registry updates: {exc}. "
                         f"Recovery manifest: {manifest_path}; use --undo "
                         "to restore updates that were applied.")

    manifest["status"] = "completed"
    manifest["succeeded"] = ok
    manifest["failed"] = failed
    write_manifest(manifest_path, manifest)
    return ok, failed


async def authenticate(ws, token):
    challenge = await recv_json_with_timeout(ws, "authentication challenge")
    if not isinstance(challenge, dict) or challenge.get("type") != "auth_required":
        raise SystemExit(f"Unexpected Home Assistant authentication challenge: "
                         f"{challenge}")
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    response = await recv_json_with_timeout(ws, "authentication response")
    if not isinstance(response, dict) or response.get("type") != "auth_ok":
        raise SystemExit(f"Auth failed: {response}")


async def connect_and_auth():
    """Connect to HA and authenticate; returns the websocket."""
    try:
        ws = await websockets.connect(HA_WS_URL, max_size=2**23,
                                      open_timeout=CONNECT_TIMEOUT_S)
    except OSError as exc:
        raise SystemExit(
            f"Could not connect to {HA_WS_URL} within {CONNECT_TIMEOUT_S}s: "
            f"{exc}. Check HA_WS_URL, the port, and that HA is reachable.")
    await authenticate(ws, HA_TOKEN)
    return ws


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default=None,
                        help="entity-id prefix identifying managed containers "
                             f"(env HA_PREFIX, default {DEFAULT_PREFIX}; use "
                             "the UNDERSCORE form, e.g. ix_ not ix-; pass an "
                             'empty string "" to target ALL portainer '
                             "entities)")
    parser.add_argument("--keep", default=None,
                        help="comma-separated entity_id substrings to keep; "
                             "matching entities are NOT disabled (env HA_KEEP, "
                             "default none — all targeted entities are "
                             "disabled)")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview what would be disabled/enabled; "
                             "change nothing")
    parser.add_argument("--yes", action="store_true",
                        help="confirm registry changes without prompting")
    parser.add_argument("--group", action="store_true",
                        help="group dry-run output: containers with identical "
                             "sensor sets are listed together (disable, keep "
                             "and enable lists all shown)")
    parser.add_argument("--enable-kept", action="store_true",
                        help="also ENABLE kept entities that are currently "
                             "disabled in the registry (sets disabled_by=None)")
    parser.add_argument("--list-suffixes", action="store_true",
                        help="show what entity kinds exist for prefixed "
                             "containers; run this FIRST to refine --keep")
    parser.add_argument("--list-platforms", action="store_true",
                        help="dump platform names and sample entity ids from "
                             "the registry (diagnoses zero-match problems)")
    parser.add_argument("--undo", nargs="?", const="__latest__",
                        metavar="MANIFEST",
                        help="restore a manifest; omit MANIFEST to use the "
                             "newest unfinished recovery manifest")
    parser.add_argument("--delete-manifest", action="store_true",
                        help="delete the manifest after a clean undo "
                             "(for non-interactive use with --undo)")
    args = parser.parse_args()

    if args.undo is not None and any([
            args.dry_run, args.enable_kept, args.list_suffixes,
            args.list_platforms]):
        raise SystemExit("--undo cannot be combined with other modes.")
    if args.delete_manifest and args.undo is None:
        raise SystemExit("--delete-manifest requires --undo.")

    undo_manifest_path = None
    if args.undo is not None:
        if args.undo == "__latest__":
            undo_manifest_path = find_latest_undo_manifest(MANIFESTS_DIR)
            if undo_manifest_path is None:
                raise SystemExit(f"No unfinished recovery manifests found in "
                                 f"{MANIFESTS_DIR}.")
        else:
            undo_manifest_path = Path(args.undo)

    # Precedence: CLI flag > environment/.env > built-in default.
    # NOTE: args.prefix is checked against None (not falsy) so that an
    # explicit empty string --prefix "" is honored (= all portainer entities);
    # an empty HA_PREFIX in .env still falls back to the default.
    prefix = (args.prefix if args.prefix is not None
              else os.environ.get("HA_PREFIX") or DEFAULT_PREFIX).lower()
    keep = parse_keep(args.keep if args.keep is not None
                      else os.environ.get("HA_KEEP"))
    scope_label = f"'{prefix}'" if prefix else "ALL"

    if not HA_TOKEN:
        raise SystemExit("Set HA_TOKEN (env var or .env file; see "
                         ".env.example). Optionally also HA_WS_URL, HA_PREFIX, "
                         "HA_KEEP.")

    async with await connect_and_auth() as ws:
        # ---- undo mode: restore only entities unchanged since this run ----
        if args.undo is not None:
            manifest_path = undo_manifest_path
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SystemExit(f"Cannot read manifest {manifest_path}: {exc}")
            if not isinstance(manifest, dict):
                raise SystemExit(f"Invalid manifest {manifest_path}: "
                                 "expected a JSON object.")
            manifest_ops = manifest.get("ops", [])
            if not isinstance(manifest_ops, list):
                raise SystemExit(f"Invalid manifest {manifest_path}: "
                                 "'ops' must be a list.")
            try:
                for op in manifest_ops:
                    if not isinstance(op, dict) or not all(
                            key in op for key in (
                                "entity_id", "was_disabled_by", "new_disabled_by")):
                        raise ValueError("each op needs entity_id, "
                                         "was_disabled_by and new_disabled_by")
                    if not isinstance(op["entity_id"], str) or any(
                            value is not None and not isinstance(value, str)
                            for value in (op["was_disabled_by"],
                                          op["new_disabled_by"])):
                        raise ValueError("entity_id must be a string and "
                                         "disabled_by values must be strings or null")
            except ValueError as exc:
                raise SystemExit(f"Invalid manifest {manifest_path}: {exc}")

            resp = await send(ws, 1, {"type": "config/entity_registry/list"})
            check(resp, "config/entity_registry/list")
            current_disabled_by = {
                entity["entity_id"]: entity.get("disabled_by")
                for entity in resp["result"]
            }
            ops, missing, already_restored, changed = select_undo_ops(
                manifest_ops, current_disabled_by)
            print(f"Undoing {len(ops)} of {len(manifest_ops)} change(s) from "
                  f"{manifest.get('when', '?')} ({manifest_path}).")
            if missing:
                print(f"Skipped {len(missing)} entity(s) no longer in the registry.")
            if already_restored:
                print(f"Already restored: {len(already_restored)} entity(s).")
            if changed:
                print(f"Skipped {len(changed)} entity(s) whose state changed "
                      "since this manifest was created.")
            if ops:
                confirm_mutations(args.yes, f"Restore {len(ops)} entity state(s)?")
            manifest["undo_status"] = "in_progress"
            manifest["undo_last_attempt_at"] = datetime.datetime.now().isoformat(
                timespec="seconds")
            write_manifest(manifest_path, manifest)
            try:
                ok, failed = await apply_updates(ws, 2, ops) if ops else ([], [])
            except (TimeoutError, OSError, WebSocketException) as exc:
                manifest["undo_status"] = "partial"
                manifest["undo_error"] = str(exc)
                write_manifest(manifest_path, manifest)
                raise SystemExit(f"Undo was interrupted: {exc}. Recovery "
                                 f"manifest remains at {manifest_path}; retry "
                                 "with --undo.")

            undo_complete = not changed and not failed
            manifest["undo_status"] = (
                "completed" if undo_complete else "partial")
            manifest.pop("undo_error", None)
            if undo_complete:
                manifest["undo_completed_at"] = datetime.datetime.now().isoformat(
                    timespec="seconds")
            else:
                manifest.pop("undo_completed_at", None)
            write_manifest(manifest_path, manifest)
            print(f"Restored {len(ok)}/{len(ops)} entities"
                  + (f", {len(failed)} FAILED (see above)." if failed else "."))
            if undo_complete and not missing:
                maybe_delete_undo_manifest(
                    manifest_path, args.yes, args.delete_manifest)
            elif missing:
                print("Recovery manifest retained because some entities no "
                      "longer exist.")
            else:
                print("Recovery manifest retained; resolve the remaining "
                      "failures or changed entities and retry the undo.")
            if ok:
                print("Changes usually appear immediately. If entities still "
                    "look stale, reload the UI or "
                    "restart Home Assistant.")
            return

        # ---- normal modes --------------------------------------------------
        resp = await send(ws, 1, {"type": "config/entity_registry/list"})
        check(resp, "config/entity_registry/list")
        entities = resp["result"]
        print(f"Registry loaded: {len(entities)} entities total.")

        if args.list_platforms:
            platforms = Counter(e.get("platform") for e in entities)
            print("Platforms seen in registry:")
            for plat, n in platforms.most_common(40):
                print(f"  {n:5d}  {plat}")
            ix_sample = ([e["entity_id"] for e in entities
                          if e.get("platform") == "portainer"][:10]
                         if not prefix else
                         [e["entity_id"] for e in entities
                          if prefix in e.get("entity_id", "")][:10])
            print(f"\nSample entity ids containing '{prefix}':")
            for eid in ix_sample:
                print(" ", eid)
            return

        resp = await send(ws, 2, {"type": "config/device_registry/list"})
        check(resp, "config/device_registry/list")
        devices = resp["result"]
        dev_slug_by_id = {}
        for d in devices:
            name = d.get("name_by_user") or d.get("name")
            if name:
                dev_slug_by_id[d["id"]] = slugify(name)

        portainer, targeted, targets, kept_disabled, kept = (
            select_portainer_entities(entities, prefix, keep, args.enable_kept))
        annotate(portainer, dev_slug_by_id, prefix)

        if not portainer:
            print("WARNING: no entities with platform == 'portainer' found. "
                  "Re-run with --list-platforms to see actual platform names.")
        if portainer and not targeted:
            print(f"WARNING: portainer entities found but none start with "
                  f"'{prefix}'. Sample portainer entity ids:")
            for e in [x["entity_id"] for x in portainer][:10]:
                print(" ", e)

        if args.list_suffixes:
            counts = Counter(e["_kind"] for e in targeted)
            print(f"{len(targeted)} {scope_label} portainer entities; "
                  "kind counts:")
            for kind, n in counts.most_common():
                print(f"  {n:5d}  {kind}")
            unparsed = [e for e in targeted
                        if e["_kind"] == e["entity_id"].split(".", 1)[1]]
            if unparsed:
                print(f"\n{len(unparsed)} entities could not be split into "
                      "container + kind (device name mismatch?). Samples:")
                for e in unparsed[:10]:
                    slug = dev_slug_by_id.get(e.get("device_id"))
                    print(f"  {e['entity_id']}  (device slug: {slug})")
            return

        keep_desc = f" ({', '.join(keep)})" if keep else ""
        print(f"{scope_label} portainer entities: {len(targeted)} total, "
              f"{len(kept)} kept{keep_desc}, "
              f"{len(targets)} to disable"
              + (f", {len(kept_disabled)} kept-but-disabled to enable"
                 if args.enable_kept else "") + ".")
        if not prefix:
            print("Empty prefix: ALL portainer entities are in scope, "
                  "including non-managed containers and stacks.")
        if not keep:
            print("No keep list given (via --keep or HA_KEEP): ALL in-scope "
                  "entities will be disabled. Pass keep substrings to "
                  "preserve entity types (e.g. --keep out_of_memory).")

        if args.dry_run:
            by_id = lambda e: e["entity_id"]
            if args.group:
                print("\n--- WOULD DISABLE (grouped by identical sensor set) ---")
                print_shape_grouped(targets, "disable")
                if kept:
                    print("\n--- KEPT (grouped by identical sensor set) ---")
                    print_shape_grouped(kept, "keep")
                if args.enable_kept:
                    print("\n--- WOULD ENABLE (kept-but-disabled) ---")
                    print_shape_grouped(kept_disabled, "enable")
            else:
                for e in sorted(targets, key=by_id)[:30]:
                    print("  would disable:", e["entity_id"])
                if len(targets) > 30:
                    print(f"  ... and {len(targets) - 30} more")
                if kept:
                    print("\nKept (sample):")
                    for e in sorted(kept, key=by_id)[:10]:
                        state = "DISABLED" if e.get("disabled_by") else "enabled"
                        print(f"  keeping ({state}):", e["entity_id"])
                    if any(e.get("disabled_by") for e in kept):
                        print("\nNote: some kept entities are disabled. "
                              "Re-run with --enable-kept to turn them on.")
                if args.enable_kept:
                    print("\nKept-but-disabled to be ENABLED:")
                    for e in sorted(kept_disabled, key=by_id)[:20]:
                        print(f"  would enable ({e.get('disabled_by')}):",
                              e["entity_id"])
                    if len(kept_disabled) > 20:
                        print(f"  ... and {len(kept_disabled) - 20} more")
            return

        # ---- execute -------------------------------------------------------
        # Persist an undo journal before sending mutations. If the process or
        # connection stops partway through, --undo can inspect live state.
        ops = (
            [{"entity_id": e["entity_id"],
              "was_disabled_by": e.get("disabled_by"),
              "new_disabled_by": "user"}
             for e in targets]
            + [{"entity_id": e["entity_id"],
                "was_disabled_by": e.get("disabled_by"),
                "new_disabled_by": None}
               for e in kept_disabled]
        )
        req_ops = [{"entity_id": op["entity_id"],
                    "disabled_by": op["new_disabled_by"]} for op in ops]
        MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
        manifest_path = MANIFESTS_DIR / f"undo-{stamp}.json"
        manifest = {
            "when": datetime.datetime.now().isoformat(timespec="seconds"),
            "prefix": prefix,
            "keep": keep,
            "status": "in_progress",
            "ops": ops,
        }
        if req_ops:
            confirm_mutations(args.yes, f"Apply {len(req_ops)} entity update(s)?")
        write_manifest(manifest_path, manifest)
        ok, failed = await apply_journaled_updates(
            ws, 10, req_ops, manifest, manifest_path)

        print(f"Done: {len(ok)} of {len(req_ops)} entity updates succeeded"
              + (f", {len(failed)} FAILED (listed above)." if failed else "."))
        print(f"Undo manifest written: {manifest_path} "
              f"(revert with: python main.py --undo {manifest_path})")
        print("Changes usually appear immediately. If entities still look "
              "stale, reload the UI or restart Home Assistant.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except TimeoutError as exc:
        raise SystemExit(str(exc))
