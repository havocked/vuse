# Operation

A mental model for how `vuse watch` behaves day-to-day, plus what's required for reliable syncing.

## The happy path

1. Your chosen **sync host** (Mac mini, always-on MacBook, whatever) runs `vuse watch` in the background.
2. You go about your day. Every puff increments the Ultra's internal counter; the device keeps the records in its own flash buffer.
3. You come into BLE range of the sync host (roughly same room, same floor — depends on walls) and take a puff.
4. The Ultra wakes its radio, broadcasts an advert, the daemon sees it within ~1 s, connects in 2–5 s, and subscribes to the puffs characteristic.
5. The device streams `StartOfFile → N records → EndOfFile`. The daemon inserts each record into `~/.vuse/state.db` and acknowledges the stream. The device then **drops those records** from its buffer.
6. The link is held open (~30 s keepalive reads) until the Mac moves out of range or the device goes back to sleep.
7. Query later: `vuse puffs --hours 24 --json`, `vuse export --csv ~/puffs.csv`, or hit the SQLite DB directly.

## Required conditions for sync

| Requirement | Why |
|---|---|
| Sync host is on, `vuse watch` running | The scanner has to be listening. |
| Device is within ~5–10 m of the host | BLE range. Walls, microwaves, and other 2.4 GHz clutter all affect it. |
| You take at least one puff while in range | The Ultra's radio sleeps between draws. No puff = no advert = no sync. |
| Phone's myVuse app is not also syncing | First subscriber wins. If myVuse drains the buffer before we do, we get nothing. Keep the phone unpaired from the device (or myVuse force-quit) if you want this tool to be the source of truth. |
| Device hasn't rebooted since the last sync | A full battery depletion or firmware reboot wipes the un-synced buffer. |

## Buffer behaviour

- **Puff IDs are monotonic across reboots** — the device never re-uses an ID, so you can detect gaps but you never see duplicates.
- **Short gaps preserve everything** — the device buffers puffs in flash; days away from the sync host are usually fine.
- **Reboot wipes un-synced puffs** — if the battery fully discharges or the firmware crashes, the buffer is gone.
- **"Drain-on-subscribe"** — subscribing to the puffs characteristic is the trigger. Only one central wins each sync.

## Multi-host sync

Each Mac has its **own synthesized peripheral UUID** for the Ultra (macOS derives it from the host's BT address + the device's resolving key). That's fine:

- Run `vuse calibrate` once per host → each gets its own `~/.vuse/config.toml`.
- Each host builds its own independent `state.db` of the puffs it happened to sync.
- If you care about a consolidated view, `scp`/`rsync` one DB to the other and write a merge query (puffs are keyed on `(device_mac, puff_id)`, a UNIQUE constraint).

The trade-off: two syncing hosts race. Whoever gets the BLE link first drains. Usually you want exactly **one** always-on sync host and let the rest be read-only.

## Monitoring

```bash
tail -f ~/.vuse/watch.log       # live daemon log
vuse status                     # last-known state (instant, reads DB only)
vuse doctor                     # diagnose common stuck states
vuse puffs --hours 12           # last 12 hours of recorded puffs
vuse export --csv ~/puffs.csv   # dump everything
```

A healthy `vuse status` looks like:

```
watcher:        running pid=12345
BLE connected:  yes (57s)
last advert:    59s ago
device:         Ultra XXXX  sku=SMABRZ  mac=dc:5b:32:xx:xx:xx
battery:        54%  (26s old)
puffs in DB:    210
last puff:      #288  (50s ago)
```

If the watcher is running but `BLE connected: no` and `last advert:` is more than a few minutes old, you're probably out of range or the device is out of battery. If none of that is true → [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).
