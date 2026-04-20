# Troubleshooting

Known failure modes and how to get out of them. Also see [`vuse doctor`](#vuse-doctor) — it catches most of this automatically.

## Scanner sees adverts but every connect silently times out

**Signature**: `vuse watch` log shows `advert seen, connecting to ...` followed 20 s later by `connect hung after 20s; giving up, waiting for next advert`, on repeat. `vuse doctor` says BT is on and nothing's paired. The device is in range and charged.

**Cause**: The Ultra firmware maintains a per-device allowlist keyed by Core Bluetooth peripheral UUID. Unknown centrals' `CONNECT_IND` is silently ignored at the link layer — no error, no HCI response, just nothing. A fresh Mac is an "unknown central" by default.

**Fix**: Put the Ultra into pairing mode by **pressing the switch-view button 5 times in quick succession**. The next connect from an unknown central will succeed and add that host's UUID to the allowlist. Subsequent connects work normally without the gesture.

This typically only happens on the first connect from a new Mac. If it recurs on a host that used to work, the device's allowlist may have been cleared (factory reset, firmware update) — re-do the pairing gesture.

## iCloud Handoff silently re-pairs the Ultra on this Mac

**Signature**: `vuse doctor` reports `Mac has Vuse paired: 1 entry(ies)`. Connect hangs (with or without an error), or works but feels unstable.

**Cause**: If you ever paired the Ultra with your iPhone's `Settings → Bluetooth` (not just myVuse's in-app pairing), iCloud Handoff may mirror that pairing onto every Mac on the same iCloud account. macOS then tries to use stored pairing keys the device doesn't recognise, which breaks our bleak-based flow.

**Fix**:

1. **System Settings → Bluetooth → `Ultra XXXX` (your device's advertised name) → ⓘ → Forget This Device** on the Mac.
2. Menu-bar Bluetooth icon → **Turn Bluetooth Off**, wait ~5 s, **Turn Bluetooth On**. This flushes CoreBluetooth's in-memory key cache — "Forget" alone doesn't always do it.
3. Also Forget the device on the phone's Bluetooth settings (the iCloud mirror re-populates otherwise).
4. Re-run `vuse watch` — connect should succeed.

macOS' TCC sandbox won't let the tool do any of this programmatically (`blueutil --unpair` fails silently); it has to be manual.

## The phone is hogging the Bluetooth link

**Signature**: Connects succeed occasionally, but most puffs never sync. Or the buffer arrives with gaps.

**Cause**: If the myVuse app is running on your phone and the phone is in range, it subscribes to the puffs characteristic before your Mac does. Drain-on-subscribe means the phone gets the data and the device buffer is cleared before we ever connect.

**Fix**: Force-quit myVuse on the phone. If you want a permanent fix, uninstall myVuse entirely, or Forget the device from the phone's Bluetooth settings (the app needs a system-level pairing to work).

## BLE permission dialog never appears

**Signature**: `vuse watch` starts, scanner starts, but no adverts ever come in. `System Settings → Privacy & Security → Bluetooth` doesn't list Terminal (or your shell).

**Cause**: macOS TCC (Transparency, Consent and Control) gates BLE access. The prompt attaches to the parent app that launched Python (Terminal, iTerm). If you first tried `vuse watch` over SSH, the prompt can't surface — TCC blocks the dialog on non-local-console sessions.

**Fix**: Run `vuse watch` once from the physical keyboard or Screen Sharing. Click Allow on the prompt. After that, subsequent SSH runs work fine (permission is granted to Terminal.app, not the session).

If Terminal has a cached denial and the prompt never fires:

```bash
tccutil reset Bluetooth   # resets ALL apps' BT permission — use sparingly
```

## Watcher dies silently after the Mac wakes from sleep

**Signature**: `vuse status` shows `watcher: NOT RUNNING` after the Mac was asleep overnight.

**Cause**: Depending on how `vuse watch` was launched, background processes can be killed when macOS enters deeper sleep states. `nohup ... & disown` usually survives short sleeps but not multi-day closed-lid sleeps.

**Fix**: For always-on syncing, use a Mac that doesn't sleep (Mac mini plugged in). Or add the `~/.zshrc` guard from [INSTALL.md §5](./INSTALL.md#5-run-detached) so the watcher restarts on your next shell session.

## LaunchAgent won't work

If you're thinking "just wrap this in a `~/Library/LaunchAgents/…plist`" — don't. On macOS Tahoe (and at least Ventura+), `launchd` spawns binaries without the TCC context that allows Bluetooth access. `bleak`'s scanner starts but never sees any adverts, and the permission prompt never surfaces. The only way around this is a full signed `.app` bundle with `NSBluetoothAlwaysUsageDescription` in its `Info.plist`, which defeats the single-file-script design.

Use `nohup ... & disown` from a shell, or the `~/.zshrc` guard.

## `BleakClient` internal timeout hangs

We wrap `client.__aenter__()` in an explicit `asyncio.wait_for(CONNECT_TIMEOUT_S)` (default 20 s). If the connect succeeds eventually on the next advert, the watchdog picks it up. If it doesn't, see the "silent timeout" section above.

## Mac Bluetooth stack is completely stuck

Rare, but if nothing helps:

```bash
# 1. Menu bar → Bluetooth off → wait 5 s → Bluetooth on
# 2. Kill the watcher and any stragglers
pkill -9 -f 'vuse.py watch'
# 3. Restart detached
nohup vuse watch > ~/.vuse/watch.log 2>&1 &
disown
```

If the watcher-watchdog isn't kicking in (we expect it to handle this — it rebuilds the scanner after 90 s of silence), there may be a lower-level `bluetoothd` issue. `sudo pkill -9 bluetoothd` will make `launchd` respawn it cleanly; that fixes most daemon-stuck cases.

## `vuse doctor`

Runs through all the common checks and prints a compact diagnosis:

- BT radio on/off (via `blueutil`)
- Is the Ultra paired to this Mac (zombie pairing detection)
- Is `vuse watch` running
- DB sanity + last-advert age

```bash
vuse doctor
# → non-zero exit if anything's off
```

If `blueutil` isn't installed (`brew install blueutil`), the pairing-zombie check is skipped but everything else works.
