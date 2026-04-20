# Hardware reference

Everything we know about the Vuse Ultra's BLE interface, extracted by reverse-engineering the `com.bat.myvuse.mobile` Android app (jadx-decompiled `BatVocabulary.java`) and verified against a real device.

## Device identity

| Field | Value |
|---|---|
| Product | Vuse Ultra |
| SKU | `SMABRZ` |
| BLE chip | Nordic nRF52 (advertises `0000fe59-…` Secure DFU service) |
| Internal SDK class | `SmartBox` |
| Protocol generation | `0x0109` |
| BD_ADDR format | `dc:5b:32:xx:xx:xx` (public, stable across hosts) |

## Advertising

- Name: `Ultra <4 hex>` (e.g. `Ultra ABCD`)
- Connectable: yes (`kCBAdvDataIsConnectable = 1`)
- **Allowlist-based**: the firmware keeps a list of authorized central identifiers. Unknown centrals get their `CONNECT_IND` silently ignored. Press the switch-view button 5 times to enter pairing mode and allowlist the next unknown central.
- **Wake-on-draw**: the radio sleeps until a puff. No puff = no advert.

## GATT characteristic map

Base service UUID: `6cd6c8b5-e378-0109-*-1b9740683449`. The 4-hex segment varies per characteristic.

The tool reads/writes four of the device's 20+ characteristics. Names are BAT's internal identifiers where known.

| UUID (short) | Name | Access | Used by this tool |
|---|---|---|---|
| `010a` | device info | read | yes — SKU, BD_ADDR, device name |
| `020a` | time | read/write | yes — RTC sync (device resets on reboot) |
| `030a` | battery | read | yes — percentage, also our keepalive pinger |
| `040a` | lock / PIN | write | no |
| `060a` | puffs | indicate | yes — the main event stream |
| `090a` | find-me beacon | read/write | no |
| `0a0a` | LED control | write | no |
| `0b0a` | reset | write-only | no — destructive, don't poke |
| `0d0a` | haptic | write | no |
| `0e0a` | buzzer | write | no |
| `0f0a` | pod info | read | no (pod_uid is embedded in each puff record) |
| `100a` | power save | read | no |
| `110a` | session indication | read | no |
| `120a` | recharge reminders | read | no |
| `130a` | usage reminder config | read | no |
| `010b` | error / event log | read | no |
| `020c` | control bus | write | no |
| `030c` | payload bus | write | no |
| `040c` | payload challenge | write | no — only relevant for the cloud-signed sync that myVuse does |

Also present: Nordic Secure DFU control point at `8ec90004-f315-4f60-9fb8-838830daea50`. Not used.

## Puffs stream framing

After subscribing to `060a`, the device emits a framed record stream:

```
4 bytes       BE u32 = 0xFFFFFFFE  →  StartOfFile, next 4 bytes = count
29 bytes      PuffRecord × count
4 bytes       BE u32 = 0xFFFFFFFD  →  EndOfFile
```

Subscribing triggers the device to stream its entire buffered history once, then drop those records. This is the drain-on-sync behaviour.

### PuffRecord (29 bytes)

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | `puff_id` | BE u32, monotonic across reboots |
| 4 | 1 | `?` | unknown, observed as flags-like |
| 5 | 4 | `ts` | BE u32, either unix seconds or boot-relative seconds (see below) |
| 9 | 1 | `duration` | value × 100 = duration in ms |
| 10 | 1 | `?` | unknown |
| 11 | 5 | `pod_uid` | hex, stable per pod |
| 16 | 5 | `liquid` | hex, seems to identify the flavor/nicotine SKU |
| 21 | 8 | `?` | unknown (possibly temperature, wattage, or telemetry) |

### Timestamp interpretation

The device's clock resets on reboot. `ts` values below `1_000_000_000` (the `UNIX_EPOCH_THRESHOLD` constant in `vuse.py`) are treated as boot-relative seconds; we combine them with the connect-time offset to reconstruct wall-clock time. Values above the threshold are already unix seconds and used as-is.

When we see a boot-relative timestamp, we compute the device's boot UTC and store both `ts` (raw) and `ts_absolute` (reconstructed unix) in the DB.

## SQLite schema

```sql
CREATE TABLE puffs (
    device_mac   TEXT    NOT NULL,
    puff_id      INTEGER NOT NULL,
    ts           INTEGER,            -- raw from device (may be boot-relative)
    ts_absolute  INTEGER,             -- reconstructed unix seconds (NULL if unknown)
    duration_ms  INTEGER NOT NULL,
    pod_uid      TEXT,
    liquid       TEXT,
    raw          BLOB,                -- 29-byte record as received
    inserted_at  INTEGER DEFAULT (strftime('%s','now')),
    PRIMARY KEY (device_mac, puff_id)
);

CREATE INDEX puffs_ts_abs_idx ON puffs(ts_absolute);

CREATE TABLE state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
```

The `state` table is a simple key-value store used by the daemon to publish things like `connected_since`, `last_battery_level`, `last_advert_ts`, `watcher_pid`, etc. CLI readers (`vuse status`, `vuse doctor`) query it instead of talking to the running process.

## Pod detection

The `pod_uid` column changes when you swap pods. A `SELECT DISTINCT pod_uid` on the `puffs` table gives you a per-pod timeline. Useful for analytics like "puffs per pod" or "pods per week":

```sql
SELECT pod_uid,
       COUNT(*) AS puffs,
       MIN(puff_id) AS first,
       MAX(puff_id) AS last,
       datetime(MIN(inserted_at),'unixepoch','localtime') AS first_seen,
       datetime(MAX(inserted_at),'unixepoch','localtime') AS last_seen
FROM puffs
GROUP BY pod_uid
ORDER BY first;
```

Liquid code (`liquid` column) is typically constant within a flavor SKU, so a change in it tells you you've swapped to a different flavor.

## Cloud side (for context — not used by this tool)

The myVuse app talks to two BAT endpoints:

- `imperial-auth-api.wearefuturemaker.ca` — user auth + device PIN vault
- `dg8pseng141qj.cloudfront.net/v1.0` — device inventory + firmware downloads

The device's puff data is uploaded to BAT's servers from the phone, not directly from the device. This tool bypasses both by talking straight to the GATT characteristics. Nothing we read/write over BLE requires a cloud token — auth is only for the myVuse backend, not the device itself.

## Scope caveat

The map above is what we know from one Ultra device + decompiled Android sources. Other BAT/Imperial products (IQOS, Glo, etc.) use different protocols and aren't supported. Other Vuse generations (non-Ultra) may share the base service UUID but differ in their advertising or stream framing.
