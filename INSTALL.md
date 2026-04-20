# Install

## Requirements

- macOS 12+ (Ventura or newer recommended)
- Python 3.11+ — note that macOS' stock `/usr/bin/python3` is 3.9 on older systems; install a newer Python via [Homebrew](https://brew.sh/) (`brew install python@3.13`) or [pyenv](https://github.com/pyenv/pyenv).
- A Vuse Ultra within BLE range (roughly the same room).
- Bluetooth enabled on the Mac.

## 1. Clone and set up a virtualenv

```bash
git clone https://github.com/havocked/vuse ~/projects/vuse
cd ~/projects/vuse

python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Put `vuse` on your `PATH` (optional but recommended)

```bash
mkdir -p ~/.local/bin
cat > ~/.local/bin/vuse <<EOF
#!/bin/zsh
exec \$HOME/projects/vuse/.venv/bin/python -u \$HOME/projects/vuse/vuse.py "\$@"
EOF
chmod +x ~/.local/bin/vuse
```

Add `~/.local/bin` to your `PATH` if it isn't already:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

## 3. First run — device discovery

`vuse watch` auto-discovers the device the first time. It prints:

```
no target UUID configured; running discovery...
(if this is the first time syncing this Mac with the Vuse, press
 the switch-view button on the device 5 times to enter pairing mode.)

scanning for up to 30s — take a puff near this Mac to wake the Ultra...
```

Do exactly what it asks:

1. **Press the *switch-view* button on the Ultra 5 times in quick succession.** This enters the device's pairing-mode: the firmware will accept a connect from a new central exactly once. Without this, the device silently ignores connect requests from hosts it doesn't recognise. _This is only required on the first connect from each new Mac._
2. **Take one puff** to wake the radio so it advertises.
3. The daemon picks up the advert, matches on the `Ultra XXXX` name, and writes the synthesized Core Bluetooth UUID to `~/.vuse/config.toml`.

```
  advert: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX  name=Ultra 1234  rssi=-58

saved target UUID to /Users/<you>/.vuse/config.toml
```

On subsequent starts, `vuse watch` reads that config and skips discovery.

> **About the per-host UUID**: macOS synthesizes a different 128-bit peripheral UUID for the same physical device on each Mac (from the host's BT address + the device's resolving key). That's why every new Mac is a "new central" from the Ultra's perspective and requires the 5-press pairing-mode gesture to be allowlisted.

## 4. macOS Bluetooth permission

The first time a Python binary issues a BLE call, macOS prompts:

> **"Terminal" would like to use Bluetooth.**

Click **Allow**. If you dismissed it by mistake, enable it manually in **System Settings → Privacy & Security → Bluetooth**. On macOS Tahoe+, this dialog does **not** surface over SSH — you must be at the physical keyboard (or using Screen Sharing) the first time. See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#ble-permission-dialog-never-appears) if the prompt doesn't appear.

## 5. Run detached

Once the foreground run works and the daemon is syncing cleanly:

```bash
nohup vuse watch > ~/.vuse/watch.log 2>&1 &
disown
```

The watcher runs until you log out or the Mac reboots. To auto-start on login, add this guard to your `~/.zshrc`:

```bash
if ! pgrep -f 'vuse.py watch' > /dev/null; then
  nohup vuse watch > ~/.vuse/watch.log 2>&1 &
  disown
fi
```

(We don't use a `launchd` agent — macOS' TCC won't grant Bluetooth to launchd-spawned binaries without a signed app bundle. See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#launchagent-wont-work).)

## 6. Sanity check

```bash
vuse status        # should show the watcher running and the BLE link alive
vuse doctor        # diagnoses common stuck states
vuse puffs --hours 1
```

If anything is off → [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

## Re-calibration

If you ever move to a new Mac, or macOS regenerates the synthesized UUID (rare — happens when the host's BT stack is fully reset):

```bash
vuse calibrate
# Discovers and overwrites ~/.vuse/config.toml with the new UUID.
```
