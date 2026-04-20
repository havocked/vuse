#!/usr/bin/env python3
"""Single-file Vuse Ultra BLE client.

The whole tool: passive scan, connect, stream puff records to SQLite,
query the DB later. No daemon/IPC/sockets/pidfiles — `vuse watch` is
just a foreground process; CLI readers hit SQLite directly (WAL mode).

Commands
--------
  vuse watch      run in foreground: scan → connect → stream puffs → DB
  vuse calibrate  discover target device and save its UUID to config.toml
  vuse analyze    daily-glance summary (today vs 7-day baseline + hour pattern)
  vuse puffs      list stored puffs
  vuse status     last-known daemon state (DB read)
  vuse export     dump DB to CSV
  vuse doctor     diagnose common BT issues (zombie pair, BT off, etc.)

Backing store: ~/.vuse/state.db
Config:        ~/.vuse/config.toml  (per-host peripheral UUID)
Log:           ~/.vuse/vuse.log  (watch-mode only)

Requires Python 3.11+ (for `tomllib`; bleak's pyobjc dep also dropped 3.9).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice


# ── constants ────────────────────────────────────────────────────────

STATE_DIR   = Path.home() / ".vuse"
DB_PATH     = STATE_DIR / "state.db"
LOG_PATH    = STATE_DIR / "vuse.log"
CONFIG_PATH = STATE_DIR / "config.toml"

# Watcher tuning
STALL_SECS           = 90              # watchdog: rebuild scanner after this much advert silence
CONNECT_TIMEOUT_S    = 20              # bleak connect wrapper timeout (macOS CoreBluetooth can hang)
KEEPALIVE_S          = 30              # battery read interval while connected
DISCOVERY_TIMEOUT_S  = 30              # how long `calibrate` / first-run scan waits for a Vuse advert
UNIX_EPOCH_THRESHOLD = 1_000_000_000   # distinguishes unix-ts (seconds since 1970) from boot-relative ts

# GATT characteristic map for the Vuse Ultra (BAT SmartBox, protocol 0x0109).
# Full UUID map extracted from the myVuse APK; we only use four.
UUID = {
    "deviceInfo":  "6cd6c8b5-e378-0109-010a-1b9740683449",
    "time":        "6cd6c8b5-e378-0109-020a-1b9740683449",
    "battery":     "6cd6c8b5-e378-0109-030a-1b9740683449",
    "puffs":       "6cd6c8b5-e378-0109-060a-1b9740683449",
}

# The Ultra advertises its model as "Ultra XXXX" in kCBAdvDataLocalName. We key discovery off this.
ADVERT_NAME_PREFIX = "Ultra"

log = logging.getLogger("vuse")


# ── decoders ─────────────────────────────────────────────────────────

def be_u32(b: bytes) -> int: return int.from_bytes(b, "big")
def be_u16(b: bytes) -> int: return int.from_bytes(b, "big")


def decode_device_info(b: bytes) -> dict:
    return {
        "sku":  b[7:13].rstrip(b"\x00").decode("ascii", "replace"),
        "mac":  ":".join(f"{x:02x}" for x in b[13:19]),
        "name": b[19:35].rstrip(b"\x00").decode("ascii", "replace"),
    }


def decode_time(b: bytes) -> dict:
    ts = be_u32(b[0:4])
    return {"ts": ts, "tz_offset_min": be_u16(b[4:6])}


def encode_time(ts_unix: int, tz_offset_min: int) -> bytes:
    return ts_unix.to_bytes(4, "big") + (tz_offset_min & 0xFFFF).to_bytes(2, "big")


def decode_puff(b: bytes) -> dict:
    """Return a record dict for every frame on the puffs char."""
    first = be_u32(b[0:4]) if len(b) >= 4 else None
    if first == 0xFFFFFFFE:
        return {"kind": "StartOfFile", "count": be_u32(b[4:8]) if len(b) >= 8 else None}
    if first == 0xFFFFFFFD:
        return {"kind": "EndOfFile"}
    if len(b) == 29:
        ts = be_u32(b[5:9])
        return {
            "kind":        "PuffRecord",
            "puff_id":     be_u32(b[0:4]),
            "ts":          ts,                # either unix (if > 1e9) or boot-rel
            "duration_ms": b[9] * 100,
            "pod_uid":     b[11:16].hex(),
            "liquid":      b[16:21].hex(),
            "raw":         b.hex(),
        }
    return {"kind": "?", "raw": b.hex(), "len": len(b)}


# ── SQLite ───────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS puffs (
    device_mac  TEXT    NOT NULL,
    puff_id     INTEGER NOT NULL,
    ts          INTEGER,
    ts_absolute INTEGER,
    duration_ms INTEGER NOT NULL,
    pod_uid     TEXT,
    liquid      TEXT,
    raw         BLOB,
    inserted_at INTEGER DEFAULT (strftime('%s','now')),
    PRIMARY KEY (device_mac, puff_id)
);

CREATE INDEX IF NOT EXISTS puffs_ts_abs_idx ON puffs(ts_absolute);

CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
"""


def db_connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=5.0)
    c.executescript(SCHEMA)
    return c


def state_set(c: sqlite3.Connection, key: str, value) -> None:
    c.execute(
        "INSERT INTO state(key, value, updated_at) VALUES(?, ?, strftime('%s','now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, None if value is None else str(value)),
    )


def state_get_all(c: sqlite3.Connection) -> dict:
    return {k: v for k, v in c.execute("SELECT key, value FROM state")}


def insert_puff(c: sqlite3.Connection, device_mac: str, rec: dict,
                boot_utc: Optional[int]) -> int:
    ts = rec["ts"]
    ts_abs = ts if ts and ts > UNIX_EPOCH_THRESHOLD else (boot_utc + ts if boot_utc and ts is not None else None)
    cur = c.execute(
        """INSERT OR IGNORE INTO puffs
           (device_mac, puff_id, ts, ts_absolute, duration_ms, pod_uid, liquid, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (device_mac, rec["puff_id"], ts, ts_abs, rec["duration_ms"],
         rec.get("pod_uid"), rec.get("liquid"),
         bytes.fromhex(rec["raw"]) if "raw" in rec else None),
    )
    return cur.rowcount


# ── target resolution: CLI > env > config file > interactive discovery ──

def _read_config() -> dict:
    """Load ~/.vuse/config.toml, return empty dict if missing."""
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def _write_config(cfg: dict) -> None:
    """Write a shallow two-level dict as TOML. We don't need a full serializer —
    config is a single `[device]` section with string values."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        f.write("# vuse config — per-host peripheral UUID, managed by `vuse calibrate`.\n")
        for section, values in cfg.items():
            f.write(f"\n[{section}]\n")
            for k, v in values.items():
                if isinstance(v, str):
                    f.write(f'{k} = "{v}"\n')
                else:
                    f.write(f"{k} = {v}\n")


async def discover_target(timeout_s: int = DISCOVERY_TIMEOUT_S) -> Optional[str]:
    """Scan for adverts, return the Core Bluetooth UUID of a matching Ultra.

    Matches any device whose advertised name starts with ADVERT_NAME_PREFIX
    (case-insensitive). If multiple are seen, picks the highest-RSSI one.
    Returns None if nothing was seen before the deadline.
    """
    seen: dict[str, tuple[str, int]] = {}  # addr -> (name, rssi)

    def on_adv(dev: BLEDevice, adv) -> None:
        name = dev.name or ""
        if not name.upper().startswith(ADVERT_NAME_PREFIX.upper()):
            return
        if dev.address not in seen:
            print(f"  advert: {dev.address}  name={name}  rssi={adv.rssi}")
        seen[dev.address] = (name, adv.rssi)

    scanner = BleakScanner(detection_callback=on_adv)
    await scanner.start()
    print(f"scanning for up to {timeout_s}s — take a puff near this Mac to wake the Ultra...")
    deadline = time.time() + timeout_s
    # First, wait for any advert at all (or the full timeout).
    while time.time() < deadline and not seen:
        await asyncio.sleep(0.5)
    # Linger a few more seconds to pick up additional candidates / better RSSI samples.
    linger = min(5.0, max(0.0, deadline - time.time()))
    if linger:
        await asyncio.sleep(linger)
    await scanner.stop()

    if not seen:
        return None
    if len(seen) > 1:
        print(f"\nmultiple candidates detected:")
        for addr, (name, rssi) in sorted(seen.items(), key=lambda x: x[1][1], reverse=True):
            print(f"  {addr}  {name}  rssi={rssi}")
        print("(picking the strongest signal)")
    best = max(seen.items(), key=lambda x: x[1][1])
    return best[0]


def resolve_target(args: argparse.Namespace) -> str:
    """Resolve peripheral UUID from CLI > env > config file > interactive discovery.

    Persists a newly-discovered UUID to ~/.vuse/config.toml so subsequent runs
    are non-interactive.
    """
    if getattr(args, "target", None):
        return args.target.upper()
    env = os.environ.get("VUSE_TARGET_UUID")
    if env:
        return env.upper()
    cfg = _read_config()
    target = cfg.get("device", {}).get("target_uuid")
    if target:
        return target.upper()

    print("no target UUID configured; running discovery...")
    print("(if this is the first time syncing this Mac with the Vuse, press")
    print(" the switch-view button on the device 5 times to enter pairing mode.)\n")
    uuid = asyncio.run(discover_target())
    if not uuid:
        raise SystemExit(
            "No Vuse Ultra detected within the discovery window.\n"
            "  • Bring the device closer and take a puff to wake its radio\n"
            "  • On first connect from a new Mac: press switch-view 5 times (pairing mode)\n"
            "  • Then: vuse calibrate"
        )
    _write_config({"device": {"target_uuid": uuid}})
    print(f"\nsaved target UUID to {CONFIG_PATH}")
    return uuid


# ── watch loop (single-process daemon in foreground) ─────────────────

class Watcher:
    """Holds the BLE link, streams puffs into SQLite."""

    def __init__(self, db: sqlite3.Connection, target: str):
        self.db = db
        self.target = target.upper()
        self._client: Optional[BleakClient] = None
        self._connect_task: Optional[asyncio.Task] = None
        self._scanner: Optional[BleakScanner] = None
        # Event is created lazily inside run() — binding it here to a loop
        # that may not yet exist causes "attached to a different loop" on 3.9.
        self._stop_evt: Optional[asyncio.Event] = None
        self._device_mac: Optional[str] = None

    # ── passive scanner ──
    def _on_adv(self, dev: BLEDevice, adv) -> None:
        if dev.address.upper() != self.target:
            return
        now = int(time.time())
        state_set(self.db, "last_advert_ts", now)
        state_set(self.db, "last_rssi", adv.rssi)
        if self._client is None and (self._connect_task is None or self._connect_task.done()):
            self._connect_task = asyncio.create_task(self._connect_and_hold(dev))

    async def start_scanner(self) -> None:
        try:
            self._scanner = BleakScanner(detection_callback=self._on_adv)
            await self._scanner.start()
            state_set(self.db, "scanner_started_at", int(time.time()))
            log.info("passive scanner started, target=%s", self.target)
        except Exception as e:
            log.warning("scanner start failed: %s", e)
            self._scanner = None
            state_set(self.db, "last_error", f"scanner: {type(e).__name__}: {e}")

    async def stop_scanner(self) -> None:
        if self._scanner:
            try: await self._scanner.stop()
            except Exception: pass
            self._scanner = None

    async def watchdog(self) -> None:
        """Detect silent scanner stall (e.g. after Mac BT toggle) and rebuild it.

        macOS CoreBluetooth invalidates the scanner's manager when the BT
        radio is toggled off. BleakScanner doesn't raise — it just stops
        emitting events. Heuristic: if we aren't connected AND haven't seen
        any advert in >STALL_SECS, try rebuilding the scanner. We also auto-heal
        if the scanner failed to start initially.
        """
        while not (self._stop_evt and self._stop_evt.is_set()):
            await asyncio.sleep(15)
            try:
                if self._client is not None:
                    continue
                # No scanner at all → try to bring one up
                if self._scanner is None:
                    log.info("watchdog: scanner missing, starting")
                    await self.start_scanner()
                    continue
                st = state_get_all(self.db)
                last = int(st.get("last_advert_ts") or 0)
                stall = int(time.time()) - last if last else 9999
                if stall > STALL_SECS:
                    log.warning("watchdog: %ds without adverts, rebuilding scanner", stall)
                    await self.stop_scanner()
                    await asyncio.sleep(2)
                    await self.start_scanner()
                    state_set(self.db, "watchdog_rebuilds",
                              int(st.get("watchdog_rebuilds") or 0) + 1)
            except Exception as e:
                log.warning("watchdog loop error: %s", e)

    # ── connect + hold ──
    async def _connect_and_hold(self, dev: BLEDevice) -> None:
        log.info("advert seen, connecting to %s", dev.address)
        try:
            client = BleakClient(dev, timeout=30.0,
                                 disconnected_callback=self._on_disconnect)
            try:
                await asyncio.wait_for(client.__aenter__(), timeout=CONNECT_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("connect hung after %ds; giving up, waiting for next advert", CONNECT_TIMEOUT_S)
                try: await client.__aexit__(None, None, None)
                except Exception: pass
                return

            self._client = client
            now = int(time.time())
            state_set(self.db, "connected_since", now)
            state_set(self.db, "mtu", client.mtu_size)
            state_set(self.db, "connection_count",
                      int(state_get_all(self.db).get("connection_count") or 0) + 1)

            # Initial snapshot
            info = decode_device_info(bytes(await client.read_gatt_char(UUID["deviceInfo"])))
            self._device_mac = info["mac"]
            state_set(self.db, "device_mac",  info["mac"])
            state_set(self.db, "device_sku",  info["sku"])
            state_set(self.db, "device_name", info["name"])

            await self._read_battery()
            await self._set_time_if_needed()

            await client.start_notify(UUID["puffs"], self._on_puff)
            log.info("connected; subscribed to puffs; holding link")
            state_set(self.db, "last_error", "")

            # Keepalive: battery read every KEEPALIVE_S
            while self._client is client:
                await asyncio.sleep(KEEPALIVE_S)
                try:
                    await self._read_battery()
                except Exception as e:
                    log.warning("keepalive failed: %s", e)
                    break
        except Exception as e:
            log.error("connect/hold error: %s", e, exc_info=False)
            state_set(self.db, "last_error", f"{type(e).__name__}: {e}")
        finally:
            if self._client is not None:
                try: await self._client.__aexit__(None, None, None)
                except Exception: pass
            self._client = None
            state_set(self.db, "connected_since", None)
            log.info("connection closed; returning to passive scan")

    def _on_disconnect(self, _client) -> None:
        log.info("peer disconnected")
        self._client = None
        state_set(self.db, "connected_since", None)

    async def _read_battery(self) -> None:
        assert self._client is not None
        b = bytes(await self._client.read_gatt_char(UUID["battery"]))
        state_set(self.db, "last_battery_level", b[0])
        state_set(self.db, "last_battery_at", int(time.time()))

    async def _set_time_if_needed(self) -> None:
        assert self._client is not None
        try:
            t = decode_time(bytes(await self._client.read_gatt_char(UUID["time"])))
            now = int(time.time())
            if t["ts"] < UNIX_EPOCH_THRESHOLD or abs(t["ts"] - now) > 30:
                offset = dt.datetime.now().astimezone().utcoffset()
                tz_min = int(offset.total_seconds() / 60) if offset else 0
                await self._client.write_gatt_char(
                    UUID["time"], encode_time(now, tz_min), response=True)
                log.info("device clock synced to %d tz=%dmin", now, tz_min)
                state_set(self.db, "last_clock_sync", now)
        except Exception as e:
            log.warning("time sync failed: %s", e)

    def _on_puff(self, _sender, data: bytearray) -> None:
        rec = decode_puff(bytes(data))
        kind = rec.get("kind")
        if kind == "PuffRecord":
            boot_utc = None
            if rec.get("ts") is not None and rec["ts"] < UNIX_EPOCH_THRESHOLD:
                boot_utc = int(time.time()) - rec["ts"]
            n = insert_puff(self.db, self._device_mac or "unknown", rec, boot_utc)
            state_set(self.db, "last_puff_id", rec["puff_id"])
            state_set(self.db, "last_puff_at", int(time.time()))
            state_set(self.db, "puffs_captured",
                      int(state_get_all(self.db).get("puffs_captured") or 0) + n)
            log.info("puff #%d  dur=%dms  %s",
                     rec["puff_id"], rec["duration_ms"], "inserted" if n else "dup")
        elif kind == "StartOfFile":
            log.info("stream: StartOfFile count=%s", rec.get("count"))
            state_set(self.db, "last_stream_count", rec.get("count"))
        elif kind == "EndOfFile":
            log.info("stream: EndOfFile")

    # ── lifecycle ──
    async def run(self) -> None:
        self._stop_evt = asyncio.Event()
        await self.start_scanner()
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try: loop.add_signal_handler(s, self._stop_evt.set)
            except NotImplementedError: pass
        watchdog_task = asyncio.create_task(self.watchdog())
        try:
            await self._stop_evt.wait()
        finally:
            watchdog_task.cancel()
            try: await watchdog_task
            except asyncio.CancelledError: pass
            await self.stop_scanner()
            if self._client:
                try: await self._client.__aexit__(None, None, None)
                except Exception: pass


# ── doctor: diagnose common stuck states ─────────────────────────────

def doctor() -> int:
    print("=== vuse doctor ===\n")
    ok = True

    # 1. BT radio
    try:
        power = subprocess.run(["blueutil", "--power"], capture_output=True, text=True, timeout=5).stdout.strip()
        print(f"Mac Bluetooth: {'on' if power == '1' else 'OFF'}")
        if power != "1":
            print("  ↳ Turn Bluetooth on in the menu bar.")
            ok = False
    except FileNotFoundError:
        print("Mac Bluetooth: (blueutil not installed)")

    # 2. Zombie pairing
    try:
        paired = subprocess.run(["blueutil", "--paired"], capture_output=True, text=True, timeout=5).stdout
        zombie = [line for line in paired.splitlines() if "Ultra" in line or "vuse" in line.lower()]
        if zombie:
            print(f"Mac has Vuse paired: {len(zombie)} entry(ies)")
            for line in zombie:
                print(f"  {line.strip()}")
            print("  ↳ Forget the device in System Settings → Bluetooth.")
            print("    (macOS re-syncs the pairing from your iPhone over iCloud — "
                  "pair once on one or the other, not both.)")
            ok = False
        else:
            print("Mac has Vuse paired: no (good)")
    except FileNotFoundError:
        print("blueutil not installed; can't check pairing state")

    # 3. Watcher alive
    pid = watcher_pid()
    if pid:
        print(f"vuse watch: running (pid {pid})")
    else:
        print("vuse watch: not running  ↳ start it: `vuse watch`")
        ok = False

    # 4. DB sanity
    if DB_PATH.exists():
        c = db_connect()
        n = c.execute("SELECT COUNT(*) FROM puffs").fetchone()[0]
        st = state_get_all(c)
        print(f"DB: {DB_PATH}   puffs stored: {n}")
        if st.get("last_advert_ts"):
            age = int(time.time()) - int(st["last_advert_ts"])
            print(f"last advert: {age}s ago")
        if st.get("connected_since"):
            age = int(time.time()) - int(st["connected_since"])
            print(f"connected for {age}s")
    else:
        print(f"DB: not yet created at {DB_PATH}")

    print("\n" + ("all checks passed." if ok else "see notes above."))
    return 0 if ok else 1


def watcher_pid() -> Optional[int]:
    """Return PID of running `vuse watch`, or None."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "vuse.py watch"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        pids = [int(p) for p in out.splitlines() if p.isdigit() and int(p) != os.getpid()]
        return pids[0] if pids else None
    except Exception:
        return None


# ── CLI dispatch ─────────────────────────────────────────────────────

def _local(ts: Optional[int]) -> str:
    if ts is None:
        return "(unknown)"
    return dt.datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def cmd_watch(args: argparse.Namespace) -> int:
    # Single-instance check
    existing = watcher_pid()
    if existing:
        print(f"vuse watch already running (pid {existing})", file=sys.stderr)
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = resolve_target(args)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_PATH, mode="a"),
        ],
    )
    log.info("vuse watch starting, pid=%d, target=%s", os.getpid(), target)
    db = db_connect()
    state_set(db, "watcher_pid", os.getpid())
    state_set(db, "watcher_started_at", int(time.time()))
    w = Watcher(db, target=target)
    try:
        asyncio.run(w.run())
    except KeyboardInterrupt:
        pass
    finally:
        state_set(db, "watcher_pid", None)
        log.info("vuse watch stopped")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Print a daily-glance summary: today vs rolling 7-day baseline, hour
    pattern, last puff, current pod. Read-only — no BLE, no writes."""
    c = db_connect()
    now_ts = int(time.time())
    today_midnight = dt.datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0)
    today_start = int(today_midnight.timestamp())
    yesterday_start = today_start - 86400
    week_start = today_start - 7 * 86400

    # Today: puffs between local midnight and now
    today_rows = c.execute(
        "SELECT puff_id, ts_absolute, duration_ms FROM puffs "
        "WHERE ts_absolute >= ? ORDER BY puff_id",
        (today_start,),
    ).fetchall()
    today_count = len(today_rows)
    today_duration_s = sum(r[2] for r in today_rows) / 1000.0

    # Yesterday (previous calendar day)
    yday_count = c.execute(
        "SELECT COUNT(*) FROM puffs WHERE ts_absolute >= ? AND ts_absolute < ?",
        (yesterday_start, today_start),
    ).fetchone()[0]

    # Baseline: last 7 calendar days BEFORE today. Denominator is 7 days
    # (fixed), so vacation/skipped days correctly drag the avg down.
    baseline_sum = c.execute(
        "SELECT COUNT(*) FROM puffs WHERE ts_absolute >= ? AND ts_absolute < ?",
        (week_start, today_start),
    ).fetchone()[0]
    baseline_days_active = c.execute(
        "SELECT COUNT(DISTINCT strftime('%Y-%m-%d', ts_absolute, 'unixepoch', 'localtime')) "
        "FROM puffs WHERE ts_absolute >= ? AND ts_absolute < ?",
        (week_start, today_start),
    ).fetchone()[0]

    # Today's hour histogram (local time)
    today_by_hour = c.execute(
        "SELECT CAST(strftime('%H', ts_absolute, 'unixepoch', 'localtime') AS INT) AS h, "
        "       COUNT(*) FROM puffs WHERE ts_absolute >= ? GROUP BY h",
        (today_start,),
    ).fetchall()
    hours = [0] * 24
    for h, n in today_by_hour:
        hours[h] = n

    # Most recent puff (across all time)
    last = c.execute(
        "SELECT puff_id, ts_absolute, duration_ms, pod_uid FROM puffs "
        "WHERE ts_absolute IS NOT NULL ORDER BY ts_absolute DESC LIMIT 1",
    ).fetchone()

    # Current pod (pod of the most recent puff) + how much it's been used
    current_pod_puffs = 0
    pod_opened_ts = None
    if last and last[3]:
        current_pod_puffs, pod_opened_ts = c.execute(
            "SELECT COUNT(*), MIN(ts_absolute) FROM puffs WHERE pod_uid = ?",
            (last[3],),
        ).fetchone()

    # ── formatting helpers ──
    def fmt_hms(seconds: float) -> str:
        s = int(round(seconds))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:   return f"{h}h{m:02d}m{sec:02d}s"
        if m:   return f"{m}m{sec:02d}s"
        return f"{sec}s"

    def fmt_ago(ts: Optional[int]) -> str:
        if ts is None:
            return "?"
        delta = now_ts - int(ts)
        if delta < 60:    return f"{delta}s ago"
        if delta < 3600:  return f"{delta // 60} min ago"
        if delta < 86400: return f"{delta // 3600}h{(delta % 3600) // 60:02d}m ago"
        return f"{delta // 86400}d ago"

    def sparkline(counts: list[int]) -> str:
        """Render 24 hour-counts as a compact bar strip using Unicode blocks."""
        blocks = "▁▂▃▄▅▆▇█"
        mx = max(counts)
        if mx == 0:
            return "·" * 24
        out = []
        for n in counts:
            if n == 0:
                out.append("·")
            else:
                idx = min(len(blocks) - 1, round((n - 1) * (len(blocks) - 1) / max(1, mx - 1)))
                out.append(blocks[idx])
        return "".join(out)

    # ── render ──
    print("=== vuse analyze ===\n")

    # Today
    if today_count == 0:
        print("Today          no puffs yet")
    else:
        id_range = (f"#{today_rows[0][0]} → #{today_rows[-1][0]}"
                    if today_rows[0][0] != today_rows[-1][0]
                    else f"#{today_rows[0][0]}")
        print(f"Today          {today_count} puffs  ·  "
              f"{fmt_hms(today_duration_s)} inhaled  ·  {id_range}")

    # Baseline comparison
    if baseline_days_active < 3:
        need = 3 - baseline_days_active
        noun = "day" if need == 1 else "days"
        print(f"7-day avg      (need {need} more {noun} of history "
              f"for a reliable baseline)")
    else:
        baseline_avg = baseline_sum / 7.0
        delta_pct = ((today_count - baseline_avg) / baseline_avg * 100
                     if baseline_avg > 0 else 0)
        arrow = "↑" if delta_pct > 1 else "↓" if delta_pct < -1 else "="
        word = "above" if delta_pct > 1 else "below" if delta_pct < -1 else "at"
        hint = (f" [{baseline_days_active}/7 active days]"
                if baseline_days_active < 7 else "")
        print(f"7-day avg      {baseline_avg:.1f} puffs/day{hint}  "
              f"(today {arrow} {abs(delta_pct):.0f}% {word} usual)")

    # Yesterday
    print(f"Yesterday      {yday_count} puffs")
    print()

    # Hour pattern
    print(f"Hour pattern   {sparkline(hours)}   00h → 23h")
    print()

    # Last puff
    if last:
        print(f"Last puff      {fmt_ago(last[1])}  (#{last[0]}, {last[2] / 1000:.1f}s)")
    else:
        print("Last puff      never")

    # Current pod
    if last and last[3]:
        opened = (dt.datetime.fromtimestamp(pod_opened_ts).astimezone()
                  .strftime("%Y-%m-%d %H:%M"))
        print(f"Current pod    {last[3]}  ·  opened {opened}  ·  "
              f"{current_pod_puffs} puffs in")

    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Run discovery and (over)write the target UUID in config.toml."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    print("Take a puff near this Mac to wake the Ultra.")
    print("If this is the first time pairing this Mac with the device, press")
    print("the switch-view button 5 times on the Ultra to enter pairing mode first.\n")
    uuid = asyncio.run(discover_target())
    if not uuid:
        print(
            "No Vuse Ultra detected within the discovery window.\n"
            "Move the device closer, puff, and re-run `vuse calibrate`.",
            file=sys.stderr,
        )
        return 1
    _write_config({"device": {"target_uuid": uuid}})
    print(f"\nsaved target UUID {uuid}\n        to {CONFIG_PATH}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    c = db_connect()
    st = state_get_all(c)
    pid = watcher_pid()

    print(f"watcher:        {'running pid=' + str(pid) if pid else 'NOT RUNNING'}")
    if st.get("connected_since"):
        age = int(time.time()) - int(st["connected_since"])
        print(f"BLE connected:  yes ({age}s)")
    else:
        print(f"BLE connected:  no")

    if st.get("last_advert_ts"):
        age = int(time.time()) - int(st["last_advert_ts"])
        print(f"last advert:    {age}s ago")
    else:
        print(f"last advert:    never")

    if st.get("device_name"):
        print(f"device:         {st['device_name']}  sku={st.get('device_sku')}  mac={st.get('device_mac')}")

    if st.get("last_battery_level") is not None:
        age = int(time.time()) - int(st["last_battery_at"])
        print(f"battery:        {st['last_battery_level']}%  ({age}s old)")

    n = c.execute("SELECT COUNT(*) FROM puffs").fetchone()[0]
    print(f"puffs in DB:    {n}")
    if st.get("last_puff_id"):
        age = int(time.time()) - int(st["last_puff_at"]) if st.get("last_puff_at") else None
        print(f"last puff:      #{st['last_puff_id']}  ({age}s ago)" if age else f"last puff:      #{st['last_puff_id']}")

    if st.get("last_error"):
        print(f"last error:     {st['last_error']}")
    return 0


def cmd_puffs(args: argparse.Namespace) -> int:
    c = db_connect()
    since = int(time.time()) - int(args.hours * 3600) if args.hours else None
    q = "SELECT puff_id, ts, ts_absolute, duration_ms, pod_uid FROM puffs"
    params: list = []
    if since is not None:
        q += " WHERE ts_absolute >= ?"
        params.append(since)
    q += " ORDER BY puff_id DESC LIMIT ?"
    params.append(args.limit)
    rows = c.execute(q, params).fetchall()

    if args.json:
        print(json.dumps([
            {"puff_id": r[0], "ts": r[1], "ts_absolute": r[2],
             "duration_ms": r[3], "pod_uid": r[4]}
            for r in rows], indent=2))
        return 0

    if not rows:
        print("(no puffs)")
        return 0
    print(f"{'id':>5}  {'local time':<23}  {'dur':>7}   pod")
    print("-" * 56)
    for r in rows:
        print(f"{r[0]:>5}  {_local(r[2]):<23}  {r[3]/1000:>5.1f}s   {r[4] or ''}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    c = db_connect()
    rows = c.execute(
        "SELECT device_mac, puff_id, ts, ts_absolute, duration_ms, pod_uid, liquid "
        "FROM puffs ORDER BY device_mac, puff_id"
    ).fetchall()
    with open(args.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["device_mac", "puff_id", "ts", "ts_absolute",
                    "ts_local", "duration_ms", "pod_uid", "liquid"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], r[3],
                        _local(r[3]) if r[3] else "", r[4], r[5], r[6]])
    print(f"wrote {len(rows)} rows to {args.csv}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="vuse")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_watch = sub.add_parser("watch", help="foreground: scan, connect, stream puffs to DB")
    p_watch.add_argument(
        "--target", metavar="UUID",
        help="Core Bluetooth peripheral UUID (overrides VUSE_TARGET_UUID and config.toml)",
    )

    sub.add_parser("calibrate",
                   help=f"discover target device and save UUID to {CONFIG_PATH}")
    sub.add_parser("analyze",
                   help="daily-glance summary: today vs 7-day baseline, "
                        "hour pattern, last puff, current pod")
    sub.add_parser("status", help="show last-known state (DB read)")
    sub.add_parser("doctor", help="diagnose common BT stuck states")

    p_puffs = sub.add_parser("puffs", help="list stored puffs")
    p_puffs.add_argument("--limit", type=int, default=50)
    p_puffs.add_argument("--hours", type=float, default=None)
    p_puffs.add_argument("--json", action="store_true")

    p_exp = sub.add_parser("export", help="dump DB to CSV")
    p_exp.add_argument("--csv", required=True)

    args = p.parse_args(argv)
    dispatch = {
        "watch":     cmd_watch,
        "calibrate": cmd_calibrate,
        "analyze":   cmd_analyze,
        "status":    cmd_status,
        "puffs":     cmd_puffs,
        "export":    cmd_export,
        "doctor":    cmd_doctor,
    }
    try:
        return dispatch[args.cmd](args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
