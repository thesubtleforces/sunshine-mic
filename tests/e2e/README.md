# Client-microphone end-to-end tests

Black-box tests for the client-microphone path of this fork: RTSP `SETUP streamid=mic` -> UDP Opus
datagrams on `base+12` -> PipeWire `Audio/Source` node `moonlight-mic` (+ default-source handling).
The harness runs a **real `sunshine` binary** against a **private PipeWire daemon** and drives it with a
synthetic Moonlight client written in plain Python (HTTPS `/launch` with a client certificate, one-shot
RTSP connections formatted like moonlight-common-c, an ENet control attach, real Opus datagrams).
Assertions are made on observable state: PipeWire nodes and `default` metadata, recorded audio
(`pw-record`), UDP port ownership and HTTP/RTSP status codes. Log substrings are only used where Sunshine
offers no other observable (the "ambiguous" line, a sync point for the ENet attach, a crash/`Fatal` audit);
they are named constants at the top of `mic_e2e.py`.

## Requirements

* Linux with unprivileged user namespaces (`unshare -rnm` must work) and Python 3.9+ (stdlib only).
* CLIs: `unshare`, `ip`, `openssl`, `pipewire`, `pw-cli`, `pw-dump`, `pw-link`, `pw-metadata`, `pw-record`;
  `libopus.so.0` (used through `ctypes`).
* A built `sunshine` binary from this fork (`--sunshine <path>`). The harness runs a private copy of it, so a
  concurrent rebuild of `build/sunshine` cannot change a run; the summary prints the sha256 of what was tested.

## Isolation model

The harness re-executes itself in `unshare --user --map-root-user --net --mount`; nothing it starts can
reach the live desktop session, its PipeWire, its Sunshine or the network:

* private network namespace (only `lo`, no LAN, mDNS or UPnP exposure; fixed private ports, no conflicts);
* every child gets a scrubbed environment: private `HOME`, `XDG_*`, `PIPEWIRE_RUNTIME_DIR`, no
  `DISPLAY`/`WAYLAND_DISPLAY` from the session, `DBUS_*_BUS_ADDRESS=unix:path=/nonexistent`;
* private PipeWire daemon with a `default` metadata object and no session manager (WirePlumber is never started);
* `/dev/uinput`, `/dev/uhid` and `/dev/dri/card*` are bind-mounted over with `/dev/null` (the run aborts if
  that fails), so Sunshine cannot create virtual input devices on the host; `/run/user/<uid>`,
  `~/.config/sunshine`, `~/.config/pipewire`, `~/.config/voxtype` and `~/.local/state/wireplumber` are hidden behind
  empty tmpfs mounts that only exist in this namespace; `/tmp` is a private tmpfs;
* the "display" Sunshine needs for its encoder probe is a small fake X11 server inside the harness
  (`capture = x11`, `encoder = software`); nothing is ever captured and no real display is contacted;
* Sunshine runs with `stream_audio = disabled`, `upnp`/`system_tray` disabled, its own config, state, credentials
  and log under the temp dir `~/.cache/sunshine-mic/e2e.XXXXXX` (mode 700, removed on success unless `--keep`);
* every scenario also asserts that no second `moonlight-mic` node ever coexisted with the first, that Sunshine
  never crashed and logged no `Fatal` line;
* all processes run under `nice -n 15`, are tracked by PID and terminated by PID (never `pkill`/`pgrep -f`),
  and die with the harness (`PR_SET_PDEATHSIG`).

The summary ends with an isolation audit (namespace interfaces, neutralised devices, hidden paths, host input
device list before/after, fake X server statistics).

## Running

```
python3 tests/e2e/mic_e2e.py --sunshine ~/.cache/sunshine-mic/e2e/bin/sunshine            # everything
python3 tests/e2e/mic_e2e.py --sunshine <bin> --scenario baseline --scenario port-blocked -v
python3 tests/e2e/mic_e2e.py --list
```

`--keep` keeps the temp dir (config, logs, recordings) and prints its path; `-v` echoes the harness log.
One `PASS`/`FAIL` line is printed per scenario (with `[existing]` or `[new]` behaviour); the exit status is
non-zero if any selected scenario fails. For failed scenarios the Sunshine log, PipeWire log, harness log,
WAV recordings and a final PipeWire snapshot are copied to `~/.cache/sunshine-mic/e2e-logs/<timestamp>/<scenario>/`.
The full suite takes about 3.5 minutes (each `/cancel` waits for the initial-ping timeout, see below).

## Scenarios

`existing` = behaviour already implemented; `new` = behaviour being implemented, may fail on an older binary.

| scenario | covers | asserts |
|---|---|---|
| `baseline` | existing | SETUP mic 200 + `server_port=base+12`; node `moonlight-mic` (Audio/Source) appears; default key = `{"name":"moonlight-mic"}`; UDP port held; 440 Hz tone recorded with RMS > 2000; `/cancel` -> node gone, key absent, port free |
| `previous-default` | existing | a pre-set default source is restored byte-for-byte after teardown |
| `mic-disabled` | existing | `stream_mic = disabled`: SETUP mic still 200, no node, no socket, key untouched, session fine |
| `default-disabled` | existing | `mic_default_source = disabled`: node + audio, key never written (metadata monitor) |
| `toggle-restart` | existing | stop, then resume from a new source port with the counter restarted at 0: audio flows again |
| `toggle-restart-lost-first` | new | counter restarts at 1 (packet 0 lost) after ~335 packets: audio resumes within 5 packets; control phase proves the datagrams arrive |
| `reconnect` | existing | `/cancel`, second launch with mic works; the node is created once per session and never twice at the same instant (event-driven `pw-dump -m`) |
| `reconnect-concurrent` | existing | second launch+mic while the first is alive, same cert: documents `/launch` (refused) vs `/resume` (accepted); one node |
| `no-packets` | existing | SETUP + ANNOUNCE, no datagrams: node exists and is silent, no crash, clean teardown, key restored |
| `port-blocked` | existing | UDP `base+12` already bound: no node, key untouched, session continues; after release a new session works |
| `fresh-launch` | existing | mic session, `/cancel`, launch without SETUP mic: no node, no registration |
| `sticky-setup` | new | one raised launch: attempt 1 does SETUP mic but never ANNOUNCEs, attempt 2 (no mic) must not register |
| `killed-host` | existing | SIGKILL mid-session leaves the stale key; a fresh Sunshine's session restores to *absent* |
| `user-replaced-default` | new | default replaced by the user during the session survives the teardown |
| `user-cleared-default` | new | default cleared by the user during the session stays absent after teardown |
| `pending-launch` | existing | informational: a second `/resume` while the first launch session is still pending is discarded until the ENet control attach (the scope of `setup_mic`) |
| `foreign-source-ip` | existing | datagrams from 127.0.0.2 are ignored, from the RTSP client's IP accepted (extra) |
| `announce-attributes` | existing | the 10 attributes without a default suffice for ANNOUNCE; each missing one gives 400 (extra) |
| `ambiguous-same-ip` | new | two sessions, same IP, different certs: silence + log mentions `ambiguous`; audio resumes after one leaves |
| `same-client-supersede` | new | same cert twice: newer session owns the mic, also after the older one leaves |

## Sunshine behaviours the harness relies on (found by reading the source and by running it)

* **Display**: `/launch` (and start-up) run `video::probe_encoders()`; without a working encoder/display it
  answers 503. Hence the fake X server; the video/audio threads only wait for the client's UDP ping and never capture.
* **State file** (`file_state`, boost JSON): `{"root": {"uniqueid": "...", "named_devices": [{"name","cert","uuid","enabled"}]}}`
  with PEM client certificates; the TLS verifier needs an *exact* certificate match. HTTPS is `base-5`, HTTP `base`.
* **Launch/resume**: `/launch` needs `rikey`, `rikeyid`, `localAudioPlayMode`, `appid`. With `appid > 0` the app (an
  app with an empty `cmd` is a "desktop") counts as running until `/cancel`; a second `/launch` returns XML
  `status_code=400 "An app is already running on this host"`, `/resume` works and starts a second concurrent session.
  `/cancel` stops *all* sessions; a single session is ended by an ENet disconnect (app keeps running).
* **RTSP**: one TCP connection per request; the request buffer is 2048 bytes. `launch_session_t::setup_mic` lives
  in the raised launch session, so it is shared by every RTSP attempt of that launch. The raised launch session
  is cleared when the ENet control peer attaches (or after `ping_timeout`); the harness attaches control like a client.
* **ANNOUNCE**: only `x-nv-audio.surround.{numChannels,channelMask,AudioQuality}`, `x-nv-video[0].{packetSize,
  clientViewportHt,clientViewportWd,maxFPS,videoEncoderSlicesPerFrame,maxNumReferenceFrames}` and
  `x-nv-vqos[0].bw.maximumBitrateKbps` have no default; every attribute line must end with CR/LF.
* **Timeouts**: sessions without a video/audio ping end after `ping_timeout`; `/cancel` blocks until the
  session threads are joined, i.e. until that initial-ping wait is over, and `session::join` has a 10 s watchdog
  that kills Sunshine with SIGTRAP ("Fatal: Hang detected! Session failed to terminate in 10 seconds").
  The harness therefore uses `ping_timeout = 8000` (reproduced: with 15000 a `/cancel` on a session that never
  pinged crashes Sunshine after 10 s; do not use a large value). The microphone is released at the *start*
  of the join, so the node disappears about 0.1 s after `/cancel`, long before the HTTP reply.
* **Several sessions**: `/cancel` joins them one after the other; the node outlives `/cancel` until the last
  session is unregistered.

## Known limitations

* No video/audio is streamed or encrypted: RTSP is plaintext (`corever` 0), control is `useReliableUdp=1`;
  `SS_ENC_MIC` is not exercised. Every session is therefore bounded by `ping_timeout` (8 s).
* PipeWire has no session manager: the harness plays its role for the recorder (`PortConfig` via
  `pw-cli set-param`, link via `pw-link`). WirePlumber's own default-device policy is not exercised.
* One loopback network: "same IP" means 127.0.0.1 (127.0.0.2 only for the negative test); NAT is not modelled.
* The ambiguity scenarios end one session with an ENet DISCONNECT because `/cancel` ends all of them.
* Timing-based checks (recording alignment, node appearance) use generous margins; a heavily loaded host can
  stretch them. Linux/PipeWire only.
