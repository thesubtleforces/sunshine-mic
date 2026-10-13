# Client microphone streaming (sunshine-mic)

This fork adds a client-to-host microphone stream. A client that sends its microphone appears on the host as a
PipeWire audio source named **Moonlight Mic**, so dictation tools, voice chat and recorders can use it like a real
microphone.

> **Read [Security model and limits](#security-model-and-limits) before enabling this.** The microphone stream is
> not authenticated and not encrypted, and what it carries is played into the host as if it were a local
> microphone. Use it with **one** known client over Tailscale/WireGuard. Both options of this feature are
> **disabled by default**; turning the microphone on is an explicit opt-in (see [Options](#options)).

## Compatible clients

Stock Moonlight clients do not send a microphone. These do, using the wire format that Sunshine-Foundation
introduced:

| Client | Platform |
|---|---|
| VoidLink (App Store lists "microphone redirection" with Sunshine-Foundation hosts) | iPhone / iPad |
| qiin2333 `moonlight-qt` (AUR: `moonlight-qt-qiin2333`) | Linux, Windows, macOS |
| qiin2333 `moonlight-android` | Android |

VoidLink compatibility was confirmed in **one** live acceptance session on one host/client pair (see
[Live acceptance](#live-acceptance-one-session)); it has not been tested beyond that. Turn the client's microphone
option on **before** connecting; the client negotiates the stream when the session starts.

## What the host does

1. The client's RTSP `SETUP streamid=mic` is answered with `200` and `server_port=<base port + 12>` (UDP 48001 with
   the default base port). The reply is `200` even when `stream_mic` is disabled (the default), because some
   clients abort the whole connection otherwise. Every RTSP `DESCRIBE` clears the "microphone was set up" flag of
   the launch state, so an abandoned handshake cannot leak into the next one.
2. When the stream session starts **and `stream_mic` is enabled**, the host binds that UDP port first. With
   `stream_mic` disabled it binds nothing and creates nothing. Only if the bind succeeded does it create
   the PipeWire source `moonlight-mic` (S16, 48 kHz, mono) and, if `mic_default_source` is enabled, try to make it
   the default audio source (see [Default source](#default-source)).
3. Datagrams are decoded with Opus and played into the source (see [Ownership](#ownership) and
   [Sequence handling](#sequence-handling) for which datagrams are accepted).
4. When the last session ends, the socket is closed, the source is removed and the default source is restored if it
   is still ours to restore.

## Security model and limits

- **Opt-in only.** `stream_mic` and `mic_default_source` are both `disabled` by default. With the options absent
  from `sunshine.conf` the host opens no UDP port and creates no PipeWire source, even when a paired client sends
  the microphone setup (the end-to-end scenario `defaults-absent` checks the port and the source, not the value).
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
- **Tested only behind a restrictive host guard.** The live acceptance ran with a host firewall rule set in place that
  let UDP 48001 through only from the one client's tailnet address on the Tailscale interface and dropped every other
  source and interface, IPv4 and IPv6. Nothing in this fork makes the microphone stream safe on its own; without such
  a restriction the only control is the application's source-address check described above.
- **Do not widen the firewall.** Allow the microphone port for exactly the one client you use (see
  [Deployment](#deployment-one-client-over-tailscale)); never for whole private ranges.
- **Main-stream approval is not microphone approval.** A client that is paired and allowed to stream can send
  microphone audio once `stream_mic` is enabled. If you need to stop that, set `stream_mic = disabled` (the
  default; the stream session itself still works).

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

`mic_default_source` is `disabled` by default: with only `stream_mic = enabled` the virtual device exists and can be
selected explicitly, but nothing else in the desktop changes. Enable it only when applications that record from
"the default input" should pick up the remote microphone.

## Options

| Option | Default | Description |
|---|---|---|
| `stream_mic` | `disabled` | Accept the client's microphone stream. |
| `mic_default_source` | `disabled` | Make "Moonlight Mic" the default audio source while a client microphone is connected. |

Both are Linux-only and are available in the web UI under **Audio/Video**. Enable `stream_mic` only after the network
restriction has been verified (see [Deployment](#deployment-one-client-over-tailscale)); for a first trial leave
`mic_default_source` disabled.

### Defaults changed to disabled

The first validated builds of this fork (package revisions `-1` and `-2`, built from `2d8eb97a` and earlier) defaulted
**both options to `enabled`**. An unauthenticated stream should never be reachable without a deliberate choice, so
both now default to `disabled`:

- a configuration that sets the options explicitly behaves exactly as before, in every build;
- a configuration that leaves them out now opens no microphone port and creates no source. Add `stream_mic = enabled`
  to turn the microphone on;
- `stream_mic = enabled` alone no longer changes the default input. Add `mic_default_source = enabled` for that, or
  select "Moonlight Mic" explicitly.

If you run an older build, set both options to `disabled` in `sunshine.conf` yourself until you have upgraded.

## Deployment: one client over Tailscale

1. Pair and verify the client with stock behaviour first. Leave `stream_mic` disabled (the default) until the
   restriction below is in place.
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
5. Only now opt in: set `stream_mic = enabled` in `sunshine.conf` (or the web UI, **Audio/Video**) and restart
   Sunshine. Leave `mic_default_source` disabled for a first trial and select "Moonlight Mic" in the application.
6. Dictate into a scratch text editor, not a terminal.

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
  With `mic_default_source` enabled, clients that set the stream up without sending audio still change the
  default; it is disabled by default.

## Verification

- Unit tests: `build/tests/test_sunshine --gtest_filter='Mic*'` — packet parser, sequence tracker, ownership
  registry (over loopback with real Opus), the default-source policy, and the PipeWire glue against a **private
  PipeWire daemon** (own runtime directory, own `default` metadata object, no session manager; the user's session
  is never touched).
- Isolated end-to-end harness: `tests/e2e/mic_e2e.py` runs the real `sunshine` binary in a private user, network
  and mount namespace with a private PipeWire daemon and a synthetic Moonlight client (HTTPS launch, RTSP
  `SETUP`/`ANNOUNCE`, ENet control attach, UDP Opus, recording from the virtual source). See `tests/e2e/README.md`.
- Not covered by automation: Windows/macOS builds, NAT, encrypted main streams. A real VoidLink/iPad session was run
  by hand; see [Live acceptance](#live-acceptance-one-session).

### Evidence for the first validated package (2026-10-12, revision `-2`, commit `2d8eb97a`)

| Check | Result |
|---|---|
| `test_sunshine --gtest_filter='Mic*:ConfigConsistencyTest.*:LocaleConsistencyTest.*'` | 88 tests in 10 suites, all pass |
| PipeWire glue against a private daemon (`MicSinkPipeWireTest`) | switch and restore a previous source; delete the key when none was configured; keep a source the user chose mid-session; keep an explicit clearing; ignore a stale own node; leave the default alone when switching is disabled; work without a `default` metadata object; bounded teardown when the daemon dies |
| `tests/e2e/mic_e2e.py` against the packaged `sunshine` executable | 20/20 scenarios: `baseline`, `previous-default`, `mic-disabled`, `default-disabled`, `toggle-restart`, `toggle-restart-lost-first`, `reconnect`, `reconnect-concurrent`, `no-packets`, `port-blocked`, `fresh-launch`, `sticky-setup`, `killed-host`, `user-replaced-default`, `user-cleared-default`, `foreign-source-ip`, `announce-attributes`, `pending-launch`, `ambiguous-same-ip`, `same-client-supersede` |
| Package lint (`appstreamcli` 1.2.0, `appstream-util` 0.8.4, `desktop-file-validate` 0.28) | metainfo and desktop files validate |
| Live acceptance with VoidLink on an iPad | one session, passed; see below |

Findings that the harness reproduced before the fixes, and now guards against: a restart that loses packet 0 after
hundreds of packets stayed silent for as long as the old counter had run; a user's mid-session default source was
overwritten at teardown; a microphone SETUP from an abandoned RTSP attempt leaked into the next attempt of the same
launch; two clients on one address were both accepted.

### Evidence for the defaults change (package revision `-3`, commit `9bde3fb8`)

| Check | Result |
|---|---|
| `test_sunshine --gtest_filter='Mic*:ConfigConsistencyTest.*:LocaleConsistencyTest.*'`, binary built from the commit | 88 tests in 10 suites, all pass |
| `test_sunshine`, every suite except the three that need real audio, HID and GPU devices | 656 tests in 93 suites: 653 pass, 3 skipped (tray and non-Windows UTF helpers), none fail |
| `tests/e2e/mic_e2e.py` against the packaged `-3` executable | 23/23 scenarios, including the new `defaults-absent`, `mic-only-optin` and `cli-optin` |
| the new scenarios against the `-2` executable (negative control) | `defaults-absent` and `mic-only-optin` **fail** (the UDP port is bound, the node is created, the default source is switched); `baseline`, `mic-disabled` and `cli-optin` pass. The scenarios detect the former defaults |
| validators run by the package build (`appstreamcli`, `appstream-util`, `desktop-file-validate`) | validation successful (one pedantic note), `OK`, no findings |
| built web UI | the shipped config page bundle has `stream_mic` and `mic_default_source` defaulting to `disabled`, and both checkboxes use `default: false` |
| identity of the build | `sunshine --version` of the packaged executable prints commit `9bde3fb8a5d4729b98b88999a6a57ca5df9227c0` |

Revision `-3` was built and tested in isolation only (private user, network and mount namespaces, private PipeWire
daemon, synthetic client). It was **not** installed and never carried a real stream. The revision `-2` package
validated and accepted below is unchanged; both are listed with their checksums in `packaging/omarchy/README.md`.

### Live acceptance (one session)

One real session, run by hand after the packaged build below was installed, on one host and one client:

- **Host:** Arch/Omarchy, Hyprland (Wayland), Intel iGPU with VAAPI, running the `sunshine-mic` package built from
  commit `2d8eb97a`; that is also the commit its `--version` output prints. Commits made on this branch after it, up
  to `df0718ab`, changed documentation only. The next commit, `9bde3fb8`, changes the code defaults of both options
  to `disabled` (see [Defaults changed to disabled](#defaults-changed-to-disabled)); it was **not** part of this
  acceptance, and the executable that was accepted is the one built from `2d8eb97a`.
- **Client:** an iPad running VoidLink, over Tailscale (a direct path, about 9 ms round trip, not relayed).
- **Configuration:** the restrictive host guard described above was active. The accepted executable defaulted both
  options to `enabled`, so the session set them explicitly: automatic default-source switching
  (`mic_default_source`) was **off**, the saved Sunshine configuration still has both microphone options disabled,
  and the microphone stream was enabled for the running service by a runtime override (`stream_mic=enabled`,
  `mic_default_source=disabled`). "Moonlight
  Mic" was made the dictation input through a manual source preference, not by this fork's default-source switching.
- **Result:** dictation from the iPad microphone through a local streaming speech recognizer (Parakeet) worked; the
  words appeared while speaking, before the recording was stopped. At the end of the session video and dictation were
  both reported as working by the person running it.
- **Disconnect and reconnect:** a read-only watcher saw UDP 48001 and the "Moonlight Mic" source disappear on
  disconnect, the effective default input fall back to the physical microphone, and both come back, with Moonlight Mic
  as the default input, on reconnect. That default was the manual preference above: **automatic switching was not
  exercised live**; it is covered only by the private-PipeWire unit tests and the isolated end-to-end harness.
- **Video (informational; this fork does not touch the video path):** 2752x2064 at 120 fps, HEVC encoded with Intel
  VAAPI; no host encoder settings were changed. After client-side changes the *negotiated* video bitrate rose from
  about 7.3 Mbps to about 71.0 Mbps, which is higher than the 40 Mbps that had been suggested; the setting the client
  actually applied is not recorded here. Audio was negotiated at 512 kbps stereo in total. These are negotiated
  session values, not measurements of network throughput.

**Open caveat.** During the trial there was an episode of microphone silence. Zero microphone datagrams were arriving at
the host (the guard's packet counters did not move), and a full restart of the client app followed by a reconnect
restored the input. The root cause is **not established**: it was not shown whether the client, the network path or the
receiver was responsible, and this document does not claim a bug on either side.

**Not covered by this acceptance:** automatic default-source switching in a live session, more than one client, hosts
other than this one (the Windows and macOS builds have no microphone sink), and any run without the restrictive host
guard.

## Development

- Manual PipeWire check (briefly makes "Moonlight Mic" the default source of the **running session** and plays a
  440 Hz tone for ten seconds): `test_sunshine --gtest_also_run_disabled_tests --gtest_filter='MicSinkManualTest.*'`.
- Code: `src/mic_packet.h` (wire parser), `src/mic_stream.{h,cpp}` (socket, decoding, sessions, sequence
  tracking, ownership), `src/platform/linux/mic_source.{h,cpp}` (PipeWire source and default-source policy), plus
  small hooks in `src/rtsp.cpp`, `src/stream.cpp` and `src/config.cpp`.

The parser and the PipeWire stream setup are adapted from Nova (https://github.com/F-e-n-y-x/nova-host, GPL-3.0).
