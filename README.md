# vuse

**Sync your Vuse Ultra puff history locally, without the cloud.**

A single-file Python daemon that connects to a Vuse Ultra e-cigarette over Bluetooth Low Energy, streams the puff history straight out of the device, and stores it in local SQLite. No BAT account, no HTTP calls, no data ever leaves your machine.

Built because I wanted personal analytics on my own vaping without surrendering it to [myVuse's](https://www.myvuse.com/) cloud. The protocol was reverse-engineered from the official Android app.

## Why you might want this

- You want to own your usage data end-to-end — not rent it.
- You want to correlate puff patterns with something else (Garmin heart rate, sleep, caffeine log).
- You want a long historical record instead of the rolling window the app shows.
- You distrust cloud services that can change the ToS or shut down.

## Hardware support

| Model | Status |
|---|---|
| Vuse Ultra (SKU `SMABRZ`, protocol gen `0x0109`, Nordic nRF52) | ✅ Tested |
| Other Vuse / BAT products | ❓ Untested — may work if they share the same GATT service UUIDs |
| Non-BAT vapes | ❌ Out of scope |

Runs on **macOS 12+** with **Python 3.11+**. Linux and Windows haven't been tested; `bleak` supports both in theory, but macOS-specific behaviour (CoreBluetooth synthesized peripheral UUIDs, TCC permission flow) is documented in [`TROUBLESHOOTING.md`](./TROUBLESHOOTING.md).

## Quickstart

```bash
git clone https://github.com/havocked/vuse
cd vuse
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python vuse.py watch
# First run prompts for a puff, discovers your device, and saves the UUID.
```

Full install (PATH shim, first-connect pairing gesture, troubleshooting) → [**INSTALL.md**](./INSTALL.md).

## How it works

```
        ┌─────────────────────────── your Mac ────────────────────────────┐
        │                                                                 │
  BLE   │  passive           connect when       stream puff records       │
 ──────→│  scanner  ──advert─→  target is    ──notify──→  SQLite          │
Ultra   │                    in range + puff                              │
8032    │                                                                 │
        │  watchdog (rebuilds scanner after 90s of advert silence)        │
        └─────────────────────────────────────────────────────────────────┘
                                                     ↓
                                            ~/.vuse/state.db
```

- **Passive scan forever**: the daemon listens for the device's BLE advert (the Ultra's radio sleeps until you puff).
- **Connect on advert, hold the link**: battery reads every 30 s double as a keepalive.
- **Drain-on-subscribe**: subscribing to the puffs characteristic causes the device to stream its full unsynced buffer (`StartOfFile → records → EndOfFile`), then clear it. See [HARDWARE.md](./HARDWARE.md) for the protocol details.
- **Watchdog auto-heals** common macOS Core Bluetooth stalls (a BT toggle invalidates the scanner silently; we rebuild it after 90 s of advert silence).

## Commands

```
vuse watch       run the daemon (foreground)
vuse calibrate   discover your device and save its UUID
vuse status      last-known state — watcher, BLE link, battery, puff count
vuse puffs       list stored puffs (--hours, --limit, --json)
vuse export      dump DB to CSV
vuse doctor      diagnose common Bluetooth stuck states
```

## Docs

- [INSTALL.md](./INSTALL.md) — fresh setup, per-host pairing-mode gesture
- [OPERATION.md](./OPERATION.md) — day-to-day flow, what to expect
- [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) — silent connect hangs, zombie pairings, doctor output
- [HARDWARE.md](./HARDWARE.md) — device reference, BLE characteristic map, puff record schema

## Limitations & disclaimer

This project reverse-engineers a closed commercial product and may violate BAT's terms of service for the myVuse app. It is offered "as is" with no warranty; using it on your own device is your decision. Nicotine is a regulated substance in most jurisdictions — this tool doesn't endorse, encourage, or normalize vaping, it just lets you own the data if you already do it.

Not affiliated with or endorsed by British American Tobacco, Imperial Tobacco, or any Vuse brand holder.

## License

[MIT](./LICENSE) — © 2026 Nataniel Martin
