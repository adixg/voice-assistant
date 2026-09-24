> **Obsolete (the pre-2026-09-16 Wi-Fi firmware).** The Stick now talks BLE to
> the Android companion app, which relays to the cluster gateway on NodePort
> 30800 (see `deploy/kubernetes/README.md`, "Primary device path"). The current
> firmware has no Wi-Fi, so it can't reach this relay. Kept for history only.

# Termux relay: using the M5Stick when it's not on the laptop's network

Lets the Stick reach `bridge_server.py` while it (and your phone, providing
its hotspot) are somewhere else entirely from the laptop — no need for the
laptop to join the hotspot, or even be on the same network at all.

```
Stick --(phone hotspot, plain WebSocket)--> Termux relay --(Tailscale)--> laptop
```

This trades "keep the laptop's Wi-Fi steady" for "keep Termux running on the
phone" — it's not zero-maintenance. Only worth setting up when you actually
need the Stick away from the laptop; for the same-room case, just keep both
on the same Wi-Fi (no relay needed at all).

## 0. Check which Tailscale is actually active on the phone

There are two different ways Tailscale can be running on Android, and it
changes what Termux can do:

- **The regular Tailscale app** — a system-wide VPN. If it's already
  connected (check the Tailscale admin console, or that your phone shows up
  as an online peer in `tailscale status` on the laptop), every app on the
  phone — Termux included — already routes tailnet traffic transparently,
  with zero extra setup. **This is the case these instructions assume.**
- **Tailscale installed as a separate binary inside Termux** — Android only
  lets one app hold the VPN slot at a time, so a second tailscaled instance
  in Termux has to run in `--tun=userspace-networking` mode instead, which
  only routes traffic sent through a local SOCKS5 proxy (`localhost:1055`),
  not normal sockets. If that's your setup, the relay script below won't
  reach the laptop as-is — say so and I'll adapt it to go through that proxy
  (e.g. via `python-socks`).

Test which case you're in — in Termux:

```bash
ping -c 3 100.70.0.38
```

(`100.70.0.38` is this laptop's Tailscale IP — confirm it's still current
with `tailscale status` on the laptop if this doesn't work.)

- **Replies come back** → you're set, skip to step 1.
- **No reply / "Network unreachable"** → you're likely in the second case
  above. Come back and tell me before continuing.

## 1. Install Python + websockets in Termux

```bash
pkg update && pkg upgrade
pkg install python
pip install websockets
```

## 2. Get the relay script onto the phone

Easiest: paste it directly. In Termux:

```bash
cat > ~/termux_relay.py << 'PYEOF'
```

...then paste the full contents of `tools/termux_relay.py` from this repo,
then a line with just `PYEOF` to close the heredoc. (Or, if the phone has
its own way to pull from this machine — Syncthing, `git clone` if the repo's
pushed somewhere reachable, `adb push`, etc. — use whichever's easiest.)

## 3. Run it

```bash
python ~/termux_relay.py --laptop-host main
```

Leave this running. You should see `listening on ws://0.0.0.0:8765/ ...`.
To stop Android from killing it while Termux is backgrounded, grab a wake
lock first (in a Termux session, before or after starting the relay):

```bash
termux-wake-lock
```

This only helps while the Termux app process stays alive — Android can
still kill it eventually, or on app swipe-away. For anything more durable
(auto-restart on boot, survive being swiped from recents), that's
Termux:Boot + a proper service script — ask if you want that set up too.

## 4. Nothing to do on the Stick

The firmware resolves the relay's address itself at connect time
(`WiFi.gatewayIP()` in `connectNetwork()`, `firmware/m5stick_bridge/src/main.cpp`)
— since the phone is always this Wi-Fi's own AP, it's also always the
gateway, so there's no IP to hardcode or reflash, even when Android rotates
the hotspot's subnet on restart. `secrets.h` only needs `WIFI_SSID`/`WIFI_PASS`
for the phone's hotspot.

## 5. Switching which laptop it talks to

That's chosen entirely on the phone, not the Stick — just restart the relay
with a different `--laptop-host` (see the top of this doc / `termux_relay.py`'s
`KNOWN_HOSTS`). No firmware change either way.
