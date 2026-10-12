# Client microphone streaming (sunshine-mic)

This fork adds a client-to-host microphone stream. A client that sends its microphone appears on the host as a
PipeWire audio source named **Moonlight Mic**, so dictation tools, voice chat and recorders can use it like a real
microphone.

> **Read [Security model and limits](#security-model-and-limits) before enabling this.** The microphone stream is
> not authenticated and not encrypted, and what it carries is played into the host as if it were a local
> microphone. Use it with **one** known client over Tailscale/WireGuard.

## Compatible clients

Stock Moonlight clients do not send a microphone. These do, using the wire format that Sunshine-Foundation
introduced:

| Client | Platform |
|---|---|
| VoidLink (App Store lists "microphone redirection" with Sunshine-Foundation hosts) | iPhone / iPad |
| qiin2333 `moonlight-qt` (AUR: `moonlight-qt-qiin2333`) | Linux, Windows, macOS |
| qiin2333 `moonlight-android` | Android |

Compatibility of this host with VoidLink is **not yet proven**; it is the acceptance gate for a real trial. Turn the
client's microphone option on **before** connecting; the client negotiates the stream when the session starts.

## What the host does

1. The client's RTSP `SETUP streamid=mic` is answered with `200` and `server_port=<base port + 12>` (UDP 48001 with
   the default base port). The reply is `200` even when `stream_mic` is disabled, because some clients abort the
   whole connection otherwise. Every RTSP `DESCRIBE` clears the "microphone was set up" flag of the launch state, so
   an abandoned handshake cannot leak into the next one.
2. When the stream session starts, the host binds that UDP port first. Only if the bind succeeded does it create
   the PipeWire source `moonlight-mic` (S16, 48 kHz, mono) and, if `mic_default_source` is enabled, try to make it
   the default audio source (see [Default source](#default-source)).
3. Datagrams are decoded with Opus and played into the source (see [Ownership](#ownership) and
   [Sequence handling](#sequence-handling) for which datagrams are accepted).
4. When the last session ends, the socket is closed, the source is removed and the default source is restored if it
   is still ours to restore.

## Security model and limits

- **No authentication, no encryption.** Microphone datagrams are plain Opus over UDP. The host does not advertise
  `SS_ENC_MIC`, and the encryption negotiated for the main stream (video, audio, control) **does not cover the
  microphone stream**: it is a separate UDP flow with no key exchange. Anyone who can observe the path can listen
  to it; anyone who can send packets that the host accepts can inject audio.
- **Routing is by source IP address only.** The wire format carries no session or client identity (the SSRC is a
  constant). The host accepts datagrams only from the address that opened the session's RTSP connection. Clients
  behind one NAT address cannot be told apart. On a shared LAN segment a host can forge the source address; inside
  a tailnet WireGuard ties source addresses to peers, which is why this fork is meant for Tailscale.
- **Injected audio becomes dictated text.** Whatever reaches the virtual microphone is indistinguishable from
  speech. If a dictation tool types its output (for example Voxtype with `output.mode = "type"`), injected audio
  becomes keystrokes in the focused window — including a terminal. Dictate into a scratch text editor, not a
  terminal, while the microphone is connected.
- **Do not widen the firewall.** Allow the microphone port for exactly the one client you use (see
  [Deployment](#deployment-one-client-over-tailscale)); never for whole private ranges.
- **Main-stream approval is not microphone approval.** A client that is paired and allowed to stream can send
  microphone audio. If you need to stop that, set `stream_mic = disabled` (the stream session itself still works).

### Ownership

There is one virtual microphone, so one session owns it at a time:

| Situation | Result |
|---|---|
| One mic session | It owns the microphone; datagrams from its address are accepted. |
| The same paired client (same client certificate) and the same address registers again (reconnect or resume) | The new session replaces the old one immediately; the old one's late teardown is harmless. |
| Sessions of **different** clients, or of unknown identity, on the **same** address | **Fail closed:** no microphone audio from that address is accepted while both exist (a warning is logged). The audio resumes when one of them ends. |
| A newer session from a **different** address | It takes the microphone over; the older session resumes when the newer one ends. |

Unavoidable limitation: a datagram cannot be attributed to a session inside a shared address, so the fork can only
block, not disambiguate. The client identity used for "same client" is the TLS client certificate that Sunshine
already authenticates for `/launch` and `/resume`.

### Sequence handling

The client restarts its 16-bit packet counter at 0 whenever the user toggles its microphone, and the first packet
after a toggle can be lost. Per datagram the host:

- accepts the next packet and conceals up to five lost frames (packet-loss concealment);
- drops duplicates and packets slightly behind the newest one (reordering, 16 packets);
- treats an explicit packet 0 behind the newest as a restart;
- recognises a restart that lost its first packets from **three consecutive** far-behind packets (about 60 ms), or
  from 25 increasing far-behind packets when loss is heavy (about half a second);
- never lets a single stray old packet, repeated packets, interleaved old packets or descending numbers restart
  anything.

### Default source

`mic_default_source` changes the PipeWire `default.configured.audio.source` key (the one `wpctl set-default`
writes) so applications that record from "the default input" use the remote microphone:

- nothing is written unless PipeWire answered a synchronisation barrier after the `default` metadata object was
  bound, so the current value is known; if it is not known, or the metadata object does not exist, the default is
  left alone and a warning is logged;
- the previous value is remembered as a configured value or as confirmed absence; a value that already names
  `moonlight-mic` (left behind by a killed session) counts as absence;
- the key is observed for the whole session; at teardown it is restored **only while it still names
  `moonlight-mic`**. A source the user picked in the meantime, or an explicit clearing, is left as it is;
- every write and flush is checked and success is only logged when confirmed. If PipeWire does not answer, the
  default is left as it is (a warning shows how to clear it).

For a first trial set `mic_default_source = disabled`: the virtual device still exists and can be selected
explicitly, but nothing else in the desktop changes.

## Options

| Option | Default | Description |
|---|---|---|
| `stream_mic` | `enabled` | Accept the client's microphone stream. |
| `mic_default_source` | `enabled` | Make "Moonlight Mic" the default audio source while a client microphone is connected. |

Both are Linux-only and are available in the web UI under **Audio/Video**.

## Deployment: one client over Tailscale

1. Pair and verify the client with stock behaviour first. Keep `stream_mic = disabled` until you want to test.
2. Find the client's Tailscale address (`tailscale status`) — it is a `100.x.y.z` address; use it as
   `<client-tailscale-ip>` below. Do not use LAN ranges.
3. Allow UDP `base port + 12` (48001 by default) **only from that address, on `tailscale0`**:

   ```bash
   sudo ufw allow in on tailscale0 from <client-tailscale-ip> to any port 48001 proto udp comment 'sunshine-mic'
   ```

4. **Check what that rule actually enforces before relying on it.** Tailscale installs a `ts-input` chain at the
   top of `INPUT` that normally contains `-i tailscale0 -j ACCEPT`; in that case a UFW rule on `tailscale0` does
   not filter tailnet traffic at all. Inspect the real order:

   ```bash
   sudo iptables -S INPUT | head -20
   sudo iptables -S ts-input
   tailscale debug prefs | grep -i netfilter
   ```

   If `ts-input` accepts everything from `tailscale0`, restrict access with a tailnet ACL/grant that only lets the
   client reach this host's port, or with an nftables rule at an earlier hook priority. Until you have checked, do
   not describe the setup as "single-client enforced". The fork itself ignores datagrams from any address other
   than the owning session's, so the firewall layer is defence in depth.
5. Dictate into a scratch text editor, not a terminal.

To remove the rule: `sudo ufw delete allow in on tailscale0 from <client-tailscale-ip> to any port 48001 proto udp`.

## Wire format

Little-endian, one Opus packet per datagram (at most 1400 bytes in total):

```
u8 flags (0x00) | u8 type (0x61) | u16 sequence | u32 timestamp (ms) | u32 ssrc (0x12345678) | Opus payload
```

A 13-byte variant with a 16-bit type of `0x5504` is also accepted. Any valid Opus frame length is accepted.

## Other limitations

- **Linux/PipeWire only.** Windows and macOS builds accept the stream setup but have no virtual device, so the
  audio is dropped with a warning.
- **One microphone at a time**, no mixing.
- **No UPnP mapping and no SDP advertisement** of the microphone stream; clients learn the port from the RTSP
  `SETUP` reply.
- `SUNSHINE_BUILD_PIPEWIRE_MIC` requires PipeWire to be found at configure time (it rides on the KWin/portal
  capture options, which are on by default).
- The default source is switched when a microphone session **starts**, not when the first audio packet arrives.
  Clients that set the stream up without sending audio still change the default; use `mic_default_source =
  disabled` if that matters.

## Verification

- Unit tests: `build/tests/test_sunshine --gtest_filter='Mic*'` — packet parser, sequence tracker, ownership
  registry (over loopback with real Opus), the default-source policy, and the PipeWire glue against a **private
  PipeWire daemon** (own runtime directory, own `default` metadata object, no session manager; the user's session
  is never touched).
- Isolated end-to-end harness: `tests/e2e/mic_e2e.py` runs the real `sunshine` binary in a private user, network
  and mount namespace with a private PipeWire daemon and a synthetic Moonlight client (HTTPS launch, RTSP
  `SETUP`/`ANNOUNCE`, ENet control attach, UDP Opus, recording from the virtual source). See `tests/e2e/README.md`.
- Not covered by automation: a real VoidLink/iPad session (the acceptance gate), Windows/macOS builds, NAT,
  encrypted main streams.

### Evidence (2026-10-12)

| Check | Result |
|---|---|
| `test_sunshine --gtest_filter='Mic*:ConfigConsistencyTest.*:LocaleConsistencyTest.*'` | 88 tests in 10 suites, all pass |
| PipeWire glue against a private daemon (`MicSinkPipeWireTest`) | switch and restore a previous source; delete the key when none was configured; keep a source the user chose mid-session; keep an explicit clearing; ignore a stale own node; leave the default alone when switching is disabled; work without a `default` metadata object; bounded teardown when the daemon dies |
| `tests/e2e/mic_e2e.py` against the packaged `sunshine` executable | 20/20 scenarios: `baseline`, `previous-default`, `mic-disabled`, `default-disabled`, `toggle-restart`, `toggle-restart-lost-first`, `reconnect`, `reconnect-concurrent`, `no-packets`, `port-blocked`, `fresh-launch`, `sticky-setup`, `killed-host`, `user-replaced-default`, `user-cleared-default`, `foreign-source-ip`, `announce-attributes`, `pending-launch`, `ambiguous-same-ip`, `same-client-supersede` |
| Package lint (`appstreamcli` 1.2.0, `appstream-util` 0.8.4, `desktop-file-validate` 0.28) | metainfo and desktop files validate |

Findings that the harness reproduced before the fixes, and now guards against: a restart that loses packet 0 after
hundreds of packets stayed silent for as long as the old counter had run; a user's mid-session default source was
overwritten at teardown; a microphone SETUP from an abandoned RTSP attempt leaked into the next attempt of the same
launch; two clients on one address were both accepted.

## Development

- Manual PipeWire check (briefly makes "Moonlight Mic" the default source of the **running session** and plays a
  440 Hz tone for ten seconds): `test_sunshine --gtest_also_run_disabled_tests --gtest_filter='MicSinkManualTest.*'`.
- Code: `src/mic_packet.h` (wire parser), `src/mic_stream.{h,cpp}` (socket, decoding, sessions, sequence
  tracking, ownership), `src/platform/linux/mic_source.{h,cpp}` (PipeWire source and default-source policy), plus
  small hooks in `src/rtsp.cpp`, `src/stream.cpp` and `src/config.cpp`.

The parser and the PipeWire stream setup are adapted from Nova (https://github.com/F-e-n-y-x/nova-host, GPL-3.0).
