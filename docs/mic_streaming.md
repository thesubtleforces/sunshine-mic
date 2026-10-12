# Client microphone streaming (sunshine-mic)

This fork adds a client-to-host microphone stream. A client that sends its microphone appears on the host as a
PipeWire audio source named **Moonlight Mic**, so dictation tools, voice chat and recorders can use it like a real
microphone.

## Compatible clients

Stock Moonlight clients do not send a microphone. These do, using the same wire format that Sunshine-Foundation
introduced:

| Client | Platform |
|---|---|
| VoidLink | iPhone / iPad (App Store) |
| qiin2333 `moonlight-qt` (AUR: `moonlight-qt-qiin2333`) | Linux, Windows, macOS |
| qiin2333 `moonlight-android` | Android |

Turn the client's microphone option on **before** connecting; the client negotiates the stream when the session
starts.

## What the host does

1. The client's RTSP `SETUP streamid=mic` is answered with `200` and `server_port=<base port + 12>` (UDP 48001 with
   the default base port). The reply is always `200`, even when `stream_mic` is disabled, because some clients abort
   the whole connection otherwise.
2. When the stream session starts, the host binds that UDP port, creates the PipeWire source `moonlight-mic`
   (S16, 48 kHz, mono) and, if `mic_default_source` is enabled, makes it the default audio source.
3. Datagrams from the client's IP address are decoded with Opus and played into the source. Gaps of up to five
   frames are concealed; duplicates and late packets are dropped; a sequence restart (the client toggled its
   microphone) resyncs the decoder.
4. When the last session ends, the socket is closed, the source is removed and the previous default source is
   restored.

Only one client owns the microphone at a time; the most recently started session wins.

## Options

| Option | Default | Description |
|---|---|---|
| `stream_mic` | `enabled` | Accept the client's microphone stream. |
| `mic_default_source` | `enabled` | Make "Moonlight Mic" the default audio source while a client microphone is connected. |

Both are Linux-only and are available in the web UI under **Audio/Video**.

## Firewall

Allow UDP `base port + 12` (48001 by default) from the networks your clients use. With UFW, mirroring the rules
Sunshine already uses:

```bash
for cidr in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
  sudo ufw allow in proto udp from "$cidr" to any port 48001
done
sudo ufw allow in on tailscale0 to any port 48001 proto udp
```

## Wire format

Little-endian, one Opus packet per datagram (at most 1400 bytes in total):

```
u8 flags (0x00) | u8 type (0x61) | u16 sequence | u32 timestamp (ms) | u32 ssrc (0x12345678) | Opus payload
```

A 13-byte variant with a 16-bit type of `0x5504` is also accepted. Any valid Opus frame length is accepted.

## Limitations

- **Plaintext only.** The host does not advertise microphone encryption (`SS_ENC_MIC`, `0x08`), so clients send
  unencrypted Opus. Use the stream over Tailscale/WireGuard or a trusted LAN. (VoidLink currently pads encrypted
  microphone payloads twice, which needs a workaround on the host before encryption can be enabled.)
- **Linux/PipeWire only.** Windows and macOS builds accept the stream setup but have no virtual device, so the
  audio is dropped with a warning.
- **One microphone at a time.** There is no mixing of several clients.

## Development

- Unit tests: `build/tests/test_sunshine --gtest_filter='Mic*'`.
- Manual PipeWire check (briefly makes "Moonlight Mic" the default source and plays a 440 Hz tone for ten seconds):
  `test_sunshine --gtest_also_run_disabled_tests --gtest_filter='MicSinkManualTest.*'`, then inspect `wpctl status`
  and `pw-record --target moonlight-mic out.wav` from another terminal.
- Code: `src/mic_packet.h` (wire parser), `src/mic_stream.{h,cpp}` (socket, decoding, sessions),
  `src/platform/linux/mic_source.{h,cpp}` (PipeWire source), plus small hooks in `src/rtsp.cpp`, `src/stream.cpp`
  and `src/config.cpp`.

The parser and the PipeWire stream setup are adapted from Nova (https://github.com/F-e-n-y-x/nova-host, GPL-3.0).
