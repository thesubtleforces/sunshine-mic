#!/usr/bin/env python3
"""
mic_e2e.py - isolated end-to-end harness for the Sunshine client-microphone path.

The harness runs a real ``sunshine`` binary against a private PipeWire daemon inside a private user +
network + mount namespace (``unshare -rnm``), drives it with a synthetic Moonlight client (HTTPS launch,
RTSP, ENet control, UDP microphone datagrams with real Opus frames) and asserts behaviour on observable
state: PipeWire nodes, PipeWire default-source metadata, recorded audio, UDP port ownership and HTTP
status codes.

    python3 tests/e2e/mic_e2e.py --sunshine <path-to-sunshine> [--scenario NAME ...] [--list] [--keep]

Nothing outside the harness temp dir (``~/.cache/sunshine-mic/e2e.XXXXXX``) and the failure-log
directory (``~/.cache/sunshine-mic/e2e-logs/<timestamp>/``) is written.  See ``README.md`` for the
isolation model and the scenario table.
"""
from __future__ import annotations

import argparse
import array
import ctypes
import datetime
import errno
import http.client
import json
import math
import os
import random
import re
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------------------------------
# Constants.  Log substrings are only used where Sunshine offers no other observable.
# --------------------------------------------------------------------------------------------------
NODE_NAME = "moonlight-mic"
DEFAULT_KEY = "default.configured.audio.source"
DEFAULT_TYPE = "Spa:String:JSON"
OWN_VALUE = '{"name":"moonlight-mic"}'
PREVIOUS_VALUE = '{"name":"some-user-source"}'
USER_VALUE = '{"name":"user-chose-this"}'

AMBIGUOUS_LOG = "ambiguous"  # expected in the host log while two sessions share one client IP
LOG_CLIENT_CONNECTED = "CLIENT CONNECTED"  # ENet control peer attached (only used as a sync point)

APP_NAME = "E2E Desktop"
BASE_PORT_FIRST = 41000
PING_TIMEOUT_MS = 8000  # < 10 s: Sunshine's join watchdog is 10 s (see README, "Sunshine behaviours")
FAKE_X_WIDTH, FAKE_X_HEIGHT = 1280, 720
OPUS_RATE = 48000
FRAME_SAMPLES = 960  # 20 ms (one Opus frame)
WIN = 480  # analysis window: 10 ms
TONE_AMPLITUDE = 8000
EXPECTED_RMS = TONE_AMPLITUDE / math.sqrt(2)  # ~5657
MIN_RMS = 2000
FREQ_A = 440.0
FREQ_B = 660.0

REAL_HOME = os.path.expanduser("~")
CACHE_ROOT = os.path.join(REAL_HOME, ".cache", "sunshine-mic")
LOG_ROOT = os.path.join(CACHE_ROOT, "e2e-logs")

INNER_ENV = "MIC_E2E_INNER"
WORKDIR_ENV = "MIC_E2E_WORKDIR"
LOGDIR_ENV = "MIC_E2E_LOGDIR"


# --------------------------------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------------------------------
def now() -> float:
    return time.monotonic()


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def wait_until(pred: Callable[[], object], timeout: float, interval: float = 0.05):
    """Poll *pred* until it returns a truthy value (returned) or *timeout* elapses (returns falsy)."""
    deadline = now() + timeout
    while True:
        value = pred()
        if value:
            return value
        if now() >= deadline:
            return value
        time.sleep(interval)


class ScenarioAbort(Exception):
    """Raised by ctx.require() when a precondition fails and the scenario cannot continue."""


_libc = ctypes.CDLL(None, use_errno=True)
PR_SET_PDEATHSIG = 1


def _pdeathsig_preexec():  # runs in the forked child, only async-signal-safe work
    _libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


class Child:
    """A subprocess that is always stopped by PID (SIGTERM, then SIGKILL).  Never pkill/pgrep."""

    registry: List["Child"] = []

    def __init__(self, name: str, argv: List[str], env: Dict[str, str], logpath: Optional[str] = None,
                 cwd: Optional[str] = None, stdout=None):
        self.name = name
        self.argv = argv
        self.env = env
        self.logpath = logpath
        self.cwd = cwd
        self.stdout = stdout
        self.proc: Optional[subprocess.Popen] = None
        self._logfile = None

    def start(self) -> "Child":
        if self.stdout is not None:
            out = self.stdout
        else:
            self._logfile = open(self.logpath, "ab") if self.logpath else open(os.devnull, "wb")
            out = self._logfile
        self.proc = subprocess.Popen(self.argv, env=self.env, cwd=self.cwd, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                     preexec_fn=_pdeathsig_preexec)
        Child.registry.append(self)
        return self

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def returncode(self) -> Optional[int]:
        return self.proc.poll() if self.proc else None

    def send_sig(self, sig: int) -> None:
        if self.alive():
            try:
                os.kill(self.proc.pid, sig)
            except ProcessLookupError:
                pass

    def stop(self, grace: float = 3.0, sig: int = signal.SIGTERM) -> Optional[int]:
        if self.proc is None:
            return None
        if self.alive():
            self.send_sig(sig)
            try:
                self.proc.wait(grace)
            except subprocess.TimeoutExpired:
                self.send_sig(signal.SIGKILL)
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
        self._close()
        return self.proc.returncode

    def kill(self) -> Optional[int]:
        return self.stop(grace=0.0, sig=signal.SIGKILL)

    def _close(self):
        for fh in (self._logfile, self.stdout):
            if fh:
                try:
                    fh.close()
                except OSError:
                    pass
        self._logfile = None
        self.stdout = None
        if self in Child.registry:
            Child.registry.remove(self)


def kill_all_children() -> None:
    for child in list(Child.registry):
        try:
            child.stop(grace=1.0)
        except Exception:
            pass


# --------------------------------------------------------------------------------------------------
# Analysis of recorded audio
# --------------------------------------------------------------------------------------------------
def goertzel_amplitude(samples, freq: float, rate: int = OPUS_RATE) -> float:
    """Approximate amplitude of *freq* inside *samples* (Goertzel)."""
    n = len(samples)
    if n == 0:
        return 0.0
    k = freq * n / rate
    w = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2 = s1
        s1 = s0
    power = s2 * s2 + s1 * s1 - coeff * s1 * s2
    return 2.0 * math.sqrt(max(power, 0.0)) / n


@dataclass
class Window:
    index: int
    rms: float
    label: str  # "silence", "tone:<freq>" or "other"


@dataclass
class AudioReport:
    samples: int = 0
    windows: List[Window] = field(default_factory=list)
    freqs: Tuple[float, ...] = ()

    @property
    def seconds(self) -> float:
        return self.samples / OPUS_RATE

    def active(self, freq: Optional[float] = None) -> List[Window]:
        out = []
        for w in self.windows:
            if freq is None:
                if w.label != "silence":
                    out.append(w)
            elif w.label == f"tone:{freq:g}":
                out.append(w)
        return out

    def active_ms(self, freq: Optional[float] = None) -> int:
        return len(self.active(freq)) * WIN * 1000 // OPUS_RATE

    def peak_rms(self, freq: Optional[float] = None) -> float:
        act = self.active(freq)
        return max((w.rms for w in act), default=0.0)

    def mean_active_rms(self, freq: Optional[float] = None) -> float:
        act = [w.rms for w in self.active(freq)]
        return sum(act) / len(act) if act else 0.0

    def median_active_rms(self, freq: Optional[float] = None) -> float:
        act = sorted(w.rms for w in self.active(freq))
        return act[len(act) // 2] if act else 0.0

    def max_rms(self) -> float:
        return max((w.rms for w in self.windows), default=0.0)

    def describe(self) -> str:
        parts = []
        for f in self.freqs:
            parts.append(f"{f:g}Hz:{self.active_ms(f)}ms(rms~{self.mean_active_rms(f):.0f})")
        parts.append(f"max_rms={self.max_rms():.0f} total={self.seconds:.2f}s")
        return " ".join(parts)


def analyze(samples: array.array, freqs: Iterable[float] = (FREQ_A, FREQ_B), silence_rms: float = 100.0,
            min_rms: float = 800.0) -> AudioReport:
    freqs = tuple(freqs)
    rep = AudioReport(samples=len(samples), freqs=freqs)
    n = len(samples) // WIN
    for i in range(n):
        win = samples[i * WIN:(i + 1) * WIN]
        rms = math.sqrt(sum(x * x for x in win) / len(win))
        if rms < silence_rms:
            rep.windows.append(Window(i, rms, "silence"))
            continue
        label = "other"
        if rms >= min_rms:
            best, best_amp = None, 0.0
            for f in freqs:
                amp = goertzel_amplitude(win, f)
                if amp > best_amp:
                    best, best_amp = f, amp
            # a pure tone of amplitude A has rms A/sqrt(2); require most of the energy in one bin
            if best is not None and best_amp * 0.7071 > 0.6 * rms:
                label = f"tone:{best:g}"
        rep.windows.append(Window(i, rms, label))
    return rep


def read_wav_samples(path: str) -> array.array:
    """Read s16 mono samples from a pw-record WAV; tolerant of an unfinalised header."""
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 44 or data[:4] != b"RIFF":
        return array.array("h")
    # locate the data chunk
    pos = 12
    start = 44
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if cid == b"data":
            start = pos + 8
            break
        pos += 8 + size + (size & 1)
    raw = data[start:]
    raw = raw[: len(raw) // 2 * 2]
    out = array.array("h")
    out.frombytes(raw)
    if sys.byteorder != "little":
        out.byteswap()
    return out


# --------------------------------------------------------------------------------------------------
# Opus (ctypes) and the microphone datagram sender
# --------------------------------------------------------------------------------------------------
class OpusEncoder:
    OPUS_APPLICATION_VOIP = 2048

    def __init__(self):
        import ctypes.util
        name = ctypes.util.find_library("opus") or "libopus.so.0"
        self.lib = ctypes.CDLL(name)
        self.lib.opus_encoder_create.restype = ctypes.c_void_p
        self.lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.lib.opus_encode.restype = ctypes.c_int32
        self.lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                         ctypes.c_char_p, ctypes.c_int32]
        self.lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        err = ctypes.c_int(0)
        self.enc = self.lib.opus_encoder_create(OPUS_RATE, 1, self.OPUS_APPLICATION_VOIP, ctypes.byref(err))
        if err.value != 0 or not self.enc:
            raise RuntimeError(f"opus_encoder_create failed: {err.value}")
        self._out = ctypes.create_string_buffer(1400)

    def encode(self, pcm: array.array) -> bytes:
        buf = (ctypes.c_int16 * len(pcm)).from_buffer(pcm)
        n = self.lib.opus_encode(self.enc, buf, len(pcm), self._out, len(self._out))
        if n < 0:
            raise RuntimeError(f"opus_encode failed: {n}")
        return self._out.raw[:n]

    def close(self):
        if self.enc:
            self.lib.opus_encoder_destroy(self.enc)
            self.enc = None


class MicSender:
    """Sends microphone datagrams (flags 0 | type 0x61 | seq | timestamp | ssrc | Opus) to base+12."""

    SSRC = 0x12345678

    def __init__(self, dst_port: int, src_ip: str = "127.0.0.1", dst_ip: str = "127.0.0.1"):
        self.dst = (dst_ip, dst_port)
        self.src_ip = src_ip
        self.sock: Optional[socket.socket] = None
        self.enc = OpusEncoder()
        self.seq = 0
        self.sample_index = 0
        self.t0 = now()
        self.sent: List[Tuple[int, float, float]] = []  # (seq, monotonic time, freq)
        self.new_socket()

    def new_socket(self) -> int:
        """Open a fresh source socket (new source port), like a client toggling its microphone."""
        if self.sock:
            self.sock.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.src_ip, 0))
        return self.sock.getsockname()[1]

    @property
    def src_port(self) -> int:
        return self.sock.getsockname()[1]

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None
        self.enc.close()

    def _pcm(self, freq: float, amp: float) -> array.array:
        pcm = array.array("h", [0] * FRAME_SAMPLES)
        w = 2.0 * math.pi * freq / OPUS_RATE
        base = self.sample_index
        for i in range(FRAME_SAMPLES):
            pcm[i] = int(amp * math.sin(w * (base + i)))
        self.sample_index += FRAME_SAMPLES
        return pcm

    def packet(self, seq: int, freq: float = FREQ_A, amp: float = TONE_AMPLITUDE) -> bytes:
        payload = self.enc.encode(self._pcm(freq, amp))
        ts_ms = int((now() - self.t0) * 1000) & 0xFFFFFFFF
        return struct.pack("<BBHII", 0x00, 0x61, seq & 0xFFFF, ts_ms, self.SSRC) + payload

    def send(self, seq: Optional[int] = None, freq: float = FREQ_A, drop: bool = False) -> int:
        """Send one frame.  With *seq* None the running counter is used and advanced."""
        if seq is None:
            seq = self.seq
        self.seq = (seq + 1) & 0xFFFF
        pkt = self.packet(seq, freq)  # always encode: the encoder state follows the sender, not the loss
        if not drop:
            self.sock.sendto(pkt, self.dst)
            self.sent.append((seq, now(), freq))
        return seq

    def play(self, frames: int, freq: float = FREQ_A, pace: float = 0.02, start_seq: Optional[int] = None,
             drop: Iterable[int] = ()) -> None:
        """Send *frames* frames paced at *pace* seconds; sequence numbers listed in *drop* are lost."""
        lost = set(drop)
        if start_seq is not None:
            self.seq = start_seq & 0xFFFF
        nxt = now()
        for _ in range(frames):
            self.send(None, freq, drop=(self.seq in lost))
            nxt += pace
            delay = nxt - now()
            if delay > 0:
                time.sleep(delay)


# --------------------------------------------------------------------------------------------------
# ENet control channel (just enough of ENet 1.3 to attach to Sunshine's control host and stay alive)
# --------------------------------------------------------------------------------------------------
class EnetControl:
    SIZES = {1: 8, 2: 48, 3: 44, 4: 8, 5: 4, 6: 6, 7: 8, 8: 24, 9: 8, 10: 12, 11: 16, 12: 24}
    FLAG_ACK = 0x80
    HDR_SENT_TIME = 0x8000

    def __init__(self, host: str, port: int, connect_data: int, log: Callable[[str], None] = lambda m: None):
        self.addr = (host, port)
        self.connect_data = connect_data & 0xFFFFFFFF
        self.log = log
        self.sock: Optional[socket.socket] = None
        self.peer_id = 0
        self.session_bits = 0
        self.connected = False
        self.host_disconnected = False
        self.thread: Optional[threading.Thread] = None
        self.stop_evt = threading.Event()
        self.rx_commands = 0

    @staticmethod
    def _ms() -> int:
        return int(time.monotonic() * 1000) & 0xFFFF

    def connect(self, timeout: float = 4.0) -> bool:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        connect_id = random.getrandbits(32)
        hdr = struct.pack(">HH", self.HDR_SENT_TIME | 0x0FFF, self._ms())
        cmd = struct.pack(">BBH", 0x02 | self.FLAG_ACK, 0xFF, 1)
        cmd += struct.pack(">HBB", 0, 0xFF, 0xFF)
        cmd += struct.pack(">10I", 1392, 32768, 1, 0, 0, 5000, 2, 2, connect_id, self.connect_data)
        deadline = now() + timeout
        self.sock.settimeout(0.25)
        last_send = 0.0
        while now() < deadline:
            if now() - last_send > 0.5:  # CONNECT is reliable: resend until verified
                self.sock.sendto(hdr + cmd, self.addr)
                last_send = now()
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            if self._handle_packet(data, handshake=True):
                self.connected = True
                break
        if not self.connected:
            return False
        self.sock.settimeout(0.25)
        self.thread = threading.Thread(target=self._loop, name="enet-ctl", daemon=True)
        self.thread.start()
        return True

    def _parse(self, data: bytes):
        if len(data) < 2:
            return None
        peer_field = struct.unpack(">H", data[:2])[0]
        sent_time = None
        off = 2
        if peer_field & self.HDR_SENT_TIME:
            if len(data) < 4:
                return None
            sent_time = struct.unpack(">H", data[2:4])[0]
            off = 4
        cmds = []
        while off + 4 <= len(data):
            flags = data[off]
            num = flags & 0x0F
            chan = data[off + 1]
            rseq = struct.unpack(">H", data[off + 2:off + 4])[0]
            size = self.SIZES.get(num)
            if not size or off + size > len(data):
                break
            extra = 0
            if num == 6:
                extra = struct.unpack(">H", data[off + 4:off + 6])[0]
            elif num in (7, 9, 8, 12):
                extra = struct.unpack(">H", data[off + 6:off + 8])[0]
            body = data[off:off + size]
            cmds.append((num, flags, chan, rseq, body))
            off += size + extra
        return sent_time, cmds

    def _send_ack(self, chan: int, rseq: int, sent_time: int) -> None:
        head = struct.pack(">H", (self.peer_id & 0x0FFF) | (self.session_bits << 12))
        ack = struct.pack(">BBHHH", 0x01, chan, rseq, rseq, sent_time & 0xFFFF)
        try:
            self.sock.sendto(head + ack, self.addr)
        except OSError:
            pass

    def _handle_packet(self, data: bytes, handshake: bool = False) -> bool:
        parsed = self._parse(data)
        if parsed is None:
            return False
        sent_time, cmds = parsed
        verified = False
        for num, flags, chan, rseq, body in cmds:
            self.rx_commands += 1
            if num == 3:  # VERIFY_CONNECT
                out_peer, _in_sess, out_sess = struct.unpack(">HBB", body[4:8])
                self.peer_id = out_peer
                self.session_bits = out_sess & 0x03
                verified = True
            elif num == 4:  # DISCONNECT
                self.host_disconnected = True
                self.log("enet: host sent DISCONNECT")
            if (flags & self.FLAG_ACK) and sent_time is not None:
                self._send_ack(chan, rseq, sent_time)
        return verified

    def _loop(self):
        while not self.stop_evt.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            self._handle_packet(data)

    def disconnect(self) -> None:
        """Leave the way a client does when the user closes the stream window (the app keeps running)."""
        if self.sock and self.connected and not self.host_disconnected:
            head = struct.pack(">HH", self.HDR_SENT_TIME | (self.peer_id & 0x0FFF) | (self.session_bits << 12), self._ms())
            cmd = struct.pack(">BBHI", 0x04 | self.FLAG_ACK, 0xFF, 2, 0)
            try:
                self.sock.sendto(head + cmd, self.addr)
            except OSError:
                pass
            time.sleep(0.1)

    def close(self) -> None:
        self.stop_evt.set()
        if self.thread:
            self.thread.join(1.0)
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.connected = False


# --------------------------------------------------------------------------------------------------
# RTSP client: one new TCP connection per request, formatted like moonlight-common-c
# --------------------------------------------------------------------------------------------------
@dataclass
class RtspResponse:
    status: int
    headers: Dict[str, str]
    body: str
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    def server_port(self) -> Optional[int]:
        m = re.search(r"server_port=(\d+)", self.headers.get("transport", ""))
        return int(m.group(1)) if m else None


class RtspClient:
    CLIENT_VERSION = 14  # APP_VERSION_AT_LEAST(7, 1, 431)

    def __init__(self, host: str, port: int, url: str, log: Callable[[str], None] = lambda m: None):
        self.host, self.port, self.url = host, port, url
        self.seq = 1
        self.session_id: Optional[str] = None
        self.log = log

    def request(self, method: str, target: str, extra: Optional[List[Tuple[str, str]]] = None,
                body: Optional[str] = None, timeout: float = 10.0) -> RtspResponse:
        lines = [f"{method} {target} RTSP/1.0", f"CSeq: {self.seq}", f"X-GS-ClientVersion: {self.CLIENT_VERSION}",
                 f"Host: {self.host}"]
        self.seq += 1
        for k, v in (extra or []):
            lines.append(f"{k}: {v}")
        if body is not None:
            lines.append(f"Content-length: {len(body.encode())}")
        msg = "\r\n".join(lines) + "\r\n\r\n" + (body or "")
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            s.sendall(msg.encode())
            chunks = []
            s.settimeout(timeout)
            while True:
                try:
                    d = s.recv(65536)
                except socket.timeout:
                    break
                if not d:
                    break
                chunks.append(d)
        finally:
            s.close()
        raw = b"".join(chunks).decode("latin1")
        if not raw.startswith("RTSP/"):
            resp = RtspResponse(0, {}, "", raw)
        else:
            head, _, rest = raw.partition("\r\n\r\n")
            first, *hdrs = head.split("\r\n")
            parts = first.split(" ", 2)
            headers = {}
            for h in hdrs:
                k, _, v = h.partition(":")
                headers[k.strip().lower()] = v.strip()
            resp = RtspResponse(int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0, headers, rest, raw)
        self.log(f"rtsp {method} {target} -> {resp.status}")
        return resp

    def options(self):
        return self.request("OPTIONS", self.url)

    def describe(self):
        return self.request("DESCRIBE", self.url, [("Accept", "application/sdp"),
                                                    ("If-Modified-Since", "Thu, 01 Jan 1970 00:00:00 GMT")])

    def setup(self, stream: str) -> RtspResponse:
        extra = []
        if self.session_id:
            extra.append(("Session", self.session_id))
        extra += [("Transport", "unicast;X-GS-ClientPort=50000-50001"),
                  ("If-Modified-Since", "Thu, 01 Jan 1970 00:00:00 GMT")]
        resp = self.request("SETUP", f"streamid={stream}", extra)
        if resp.ok and self.session_id is None and "session" in resp.headers:
            self.session_id = resp.headers["session"].split(";")[0].strip()
        return resp

    def announce(self, sdp: str) -> RtspResponse:
        extra = []
        if self.session_id:
            extra.append(("Session", self.session_id))
        extra.append(("Content-type", "application/sdp"))
        return self.request("ANNOUNCE", "streamid=control/13/0", extra, body=sdp)

    def play(self, stream: str) -> RtspResponse:
        extra = [("Session", self.session_id)] if self.session_id else []
        return self.request("PLAY", f"streamid={stream}", extra)


# Attributes cmd_announce() reads with args.at() and no default: a missing one is answered with 400.
REQUIRED_ANNOUNCE_ATTRS = [
    ("x-nv-audio.surround.numChannels", "2"),
    ("x-nv-audio.surround.channelMask", "3"),
    ("x-nv-audio.surround.AudioQuality", "0"),
    ("x-nv-video[0].packetSize", "1024"),
    ("x-nv-video[0].clientViewportHt", str(FAKE_X_HEIGHT)),
    ("x-nv-video[0].clientViewportWd", str(FAKE_X_WIDTH)),
    ("x-nv-video[0].maxFPS", "30"),
    ("x-nv-vqos[0].bw.maximumBitrateKbps", "2000"),
    ("x-nv-video[0].videoEncoderSlicesPerFrame", "1"),
    ("x-nv-video[0].maxNumReferenceFrames", "1"),
]


def build_sdp(width: int = FAKE_X_WIDTH, height: int = FAKE_X_HEIGHT, fps: int = 30,
              required_only: bool = False, omit: Iterable[str] = ()) -> str:
    """ANNOUNCE body.  Default: what a Moonlight client sends (a superset of the required attributes)."""
    omit = set(omit)
    attrs = [
        ("x-ml-general.featureFlags", "3"),  # FEC status + session-id (X-SS-Connect-Data) support
        ("x-ss-general.encryptionEnabled", "0"),
        ("x-ss-video[0].chromaSamplingType", "0"),
        ("x-nv-video[0].clientViewportWd", str(width)),
        ("x-nv-video[0].clientViewportHt", str(height)),
        ("x-nv-video[0].maxFPS", str(fps)),
        ("x-nv-video[0].packetSize", "1024"),
        ("x-nv-video[0].rateControlMode", "4"),
        ("x-nv-video[0].timeoutLengthMs", "7000"),
        ("x-nv-video[0].framesWithInvalidRefThreshold", "0"),
        ("x-nv-vqos[0].bw.maximumBitrateKbps", "2000"),
        ("x-ml-video.configuredBitrateKbps", "2000"),
        ("x-nv-vqos[0].fec.minRequiredFecPackets", "2"),
        ("x-nv-vqos[0].qosTrafficType", "5"),
        ("x-nv-aqos.qosTrafficType", "4"),
        ("x-nv-general.useReliableUdp", "1"),  # plaintext control channel (13 would require AES-GCM)
        ("x-nv-video[0].videoEncoderSlicesPerFrame", "1"),
        ("x-nv-vqos[0].bitStreamFormat", "0"),
        ("x-nv-video[0].dynamicRangeMode", "0"),
        ("x-nv-video[0].maxNumReferenceFrames", "1"),
        ("x-nv-video[0].clientRefreshRateX100", "0"),
        ("x-nv-audio.surround.numChannels", "2"),
        ("x-nv-audio.surround.channelMask", "3"),
        ("x-nv-audio.surround.enable", "0"),
        ("x-nv-audio.surround.AudioQuality", "0"),
        ("x-nv-aqos.packetDuration", "5"),
        ("x-nv-video[0].encoderCscMode", "0"),
    ]
    if required_only:
        attrs = list(REQUIRED_ANNOUNCE_ATTRS)
    out = "v=0\r\no=android 0 14 IN IPv4 127.0.0.1\r\ns=NVIDIA Streaming Client\r\n"
    for k, v in attrs:
        if k not in omit:
            out += f"a={k}:{v} \r\n"
    out += "t=0 0\r\nm=video 47998  \r\n"
    return out


# --------------------------------------------------------------------------------------------------
# Fake X11 server: the only thing Sunshine needs from a display is that the software encoder probe
# (run at start-up and on /launch) can open an X display, enumerate one output and fetch an image.
# --------------------------------------------------------------------------------------------------
class FakeXServer:
    RANDR_MAJOR, RANDR_EVENT, RANDR_ERROR = 140, 89, 147
    XFIXES_MAJOR, XFIXES_EVENT, XFIXES_ERROR = 138, 87, 165
    ROOT, COLORMAP, VISUAL, CRTC, OUTPUT, MODE = 0x2C, 0x20, 0x21, 0x100, 0x200, 0x300
    NOREPLY_OPCODES = {55, 60}  # CreateGC, FreeGC

    def __init__(self, path: str, width: int = FAKE_X_WIDTH, height: int = FAKE_X_HEIGHT,
                 log: Callable[[str], None] = lambda m: None):
        self.path, self.width, self.height, self.log = path, width, height, log
        self.sock: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None
        self.stop_evt = threading.Event()
        self.requests = 0
        self.images = 0
        self.unknown: Dict[Tuple, int] = {}
        self.frame = bytes(width * height * 4)

    def start(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o700)
        self.sock.listen(32)
        self.sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._accept_loop, name="fakex", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self.thread:
            self.thread.join(2)
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _accept_loop(self):
        while not self.stop_evt.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), name="fakex-conn", daemon=True).start()

    @staticmethod
    def _recvn(conn, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)

    @staticmethod
    def _pad4(n):
        return (n + 3) & ~3

    def _setup_reply(self) -> bytes:
        vendor = b"E2E fake X server"
        fmts = [(1, 1, 32), (24, 32, 32), (32, 32, 32)]
        body = struct.pack("<IIIIHHBBBBBBBBxxxx", 12101004, 0x00400000, 0x001FFFFF, 256, len(vendor), 0xFFFF,
                           1, len(fmts), 0, 0, 32, 32, 8, 255)
        body += vendor + b"\0" * (self._pad4(len(vendor)) - len(vendor))
        for d, bpp, pad in fmts:
            body += struct.pack("<BBBxxxxx", d, bpp, pad)
        mm_w, mm_h = int(self.width * 25.4 / 96), int(self.height * 25.4 / 96)
        depths = [(1, []), (24, [(self.VISUAL, 4, 8, 256, 0xFF0000, 0x00FF00, 0x0000FF)])]
        body += struct.pack("<IIIIIHHHHHHIBBBB", self.ROOT, self.COLORMAP, 0xFFFFFF, 0, 0, self.width, self.height,
                            mm_w, mm_h, 1, 1, self.VISUAL, 0, 0, 24, len(depths))
        for d, visuals in depths:
            body += struct.pack("<BxHxxxx", d, len(visuals))
            for vid, cls, bits, cmap, rm, gm, bm in visuals:
                body += struct.pack("<IBBHIIIxxxx", vid, cls, bits, cmap, rm, gm, bm)
        return struct.pack("<BxHHH", 1, 11, 0, len(body) // 4) + body

    def _reply(self, seq: int, byte1: int = 0, fixed: bytes = b"", extra: bytes = b"") -> bytes:
        fixed = fixed.ljust(24, b"\0")
        extra = extra + b"\0" * (self._pad4(len(extra)) - len(extra))
        return struct.pack("<BBHI", 1, byte1, seq, len(extra) // 4) + fixed + extra

    def _serve(self, conn: socket.socket):
        try:
            conn.settimeout(None)
            first = self._recvn(conn, 12)
            if first[0] != 0x6C:
                return
            nlen, dlen = struct.unpack("<HH", first[6:10])
            self._recvn(conn, self._pad4(nlen) + self._pad4(dlen))
            conn.sendall(self._setup_reply())
            seq = 0
            exts: Dict[int, str] = {}
            while True:
                hdr = self._recvn(conn, 4)
                opcode, data, length = hdr[0], hdr[1], struct.unpack("<H", hdr[2:4])[0]
                if length == 0:
                    length = struct.unpack("<I", self._recvn(conn, 4))[0]
                    body = self._recvn(conn, length * 4 - 8)
                else:
                    body = self._recvn(conn, length * 4 - 4)
                seq = (seq + 1) & 0xFFFF
                self.requests += 1
                out = self._handle(opcode, data, body, seq, exts)
                if out:
                    conn.sendall(out)
        except (EOFError, ConnectionError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, op: int, data: int, body: bytes, seq: int, exts: Dict[int, str]) -> bytes:
        if op == 3:  # GetWindowAttributes
            fixed = struct.pack("<IHBBIIBBBBIIIHxx", self.VISUAL, 1, 0, 1, 0xFFFFFFFF, 0, 0, 1, 2, 0,
                                self.COLORMAP, 0, 0, 0)
            return struct.pack("<BBHI", 1, 0, seq, 3) + fixed
        if op == 14:  # GetGeometry
            return self._reply(seq, 24, struct.pack("<IhhHHH", self.ROOT, 0, 0, self.width, self.height, 0))
        if op == 98:  # QueryExtension
            nlen = struct.unpack("<H", body[0:2])[0]
            name = body[4:4 + nlen].decode("latin1")
            if name == "RANDR":
                exts[self.RANDR_MAJOR] = "RANDR"
                return self._reply(seq, 0, struct.pack("<BBBB", 1, self.RANDR_MAJOR, self.RANDR_EVENT, self.RANDR_ERROR))
            if name == "XFIXES":
                exts[self.XFIXES_MAJOR] = "XFIXES"
                return self._reply(seq, 0, struct.pack("<BBBB", 1, self.XFIXES_MAJOR, self.XFIXES_EVENT, self.XFIXES_ERROR))
            return self._reply(seq, 0, struct.pack("<BBBB", 0, 0, 0, 0))
        if op == 73:  # GetImage
            _d, _x, _y, w, h, _m = struct.unpack("<IhhHHI", body[:16])
            size = w * h * 4
            payload = self.frame[:size] if size <= len(self.frame) else bytes(size)
            self.images += 1
            return struct.pack("<BBHI", 1, 24, seq, len(payload) // 4) + struct.pack("<I", self.VISUAL).ljust(24, b"\0") + payload
        if op == 43:  # GetInputFocus
            return self._reply(seq, 1, struct.pack("<I", 1))
        if op == 16:  # InternAtom
            return self._reply(seq, 0, struct.pack("<I", 0x100 + (seq & 0xFF)))
        if op == 20:  # GetProperty
            return self._reply(seq, 0, struct.pack("<III", 0, 0, 0))
        if op in self.NOREPLY_OPCODES or op == 127:
            return b""
        if op >= 128:
            ext = exts.get(op)
            if ext == "RANDR":
                return self._randr(data, seq)
            if ext == "XFIXES":
                return self._xfixes(data, seq)
        key = (op, data)
        self.unknown[key] = self.unknown.get(key, 0) + 1
        return b""

    def _randr(self, minor: int, seq: int) -> bytes:
        if minor == 0:  # QueryVersion
            return self._reply(seq, 0, struct.pack("<II", 1, 5))
        if minor in (8, 25):  # GetScreenResources / GetScreenResourcesCurrent
            name = f"{self.width}x{self.height}".encode()
            mode = struct.pack("<IHHIHHHHHHHHI", self.MODE, self.width, self.height, 148500000, self.width + 8,
                               self.width + 16, self.width + 24, 0, self.height + 4, self.height + 8,
                               self.height + 12, len(name), 0)
            fixed = struct.pack("<IIHHHH", 1, 1, 1, 1, 1, len(name)) + b"\0" * 8
            extra = struct.pack("<II", self.CRTC, self.OUTPUT) + mode + name
            return self._reply(seq, 0, fixed, extra)
        if minor == 9:  # GetOutputInfo
            name = b"E2E-0"
            mm_w, mm_h = int(self.width * 25.4 / 96), int(self.height * 25.4 / 96)
            full = struct.pack("<IIIIBBHHHHH", 1, self.CRTC, mm_w, mm_h, 0, 0, 1, 1, 1, 0, len(name))
            extra = full[24:] + struct.pack("<II", self.CRTC, self.MODE) + name
            return self._reply(seq, 0, full[:24], extra)
        if minor == 20:  # GetCrtcInfo
            fixed = struct.pack("<IhhHHIHHHH", 1, 0, 0, self.width, self.height, self.MODE, 1, 1, 1, 1)
            return self._reply(seq, 0, fixed, struct.pack("<II", self.OUTPUT, self.OUTPUT))
        if minor == 4:  # SelectInput
            return b""
        self.unknown[("randr", minor)] = self.unknown.get(("randr", minor), 0) + 1
        return b""

    def _xfixes(self, minor: int, seq: int) -> bytes:
        if minor == 0:
            return self._reply(seq, 0, struct.pack("<II", 5, 0))
        if minor == 4:  # GetCursorImage
            return self._reply(seq, 0, struct.pack("<hhHHHHI", 0, 0, 1, 1, 0, 0, 1), struct.pack("<I", 0))
        if minor == 25:  # GetCursorImageAndName
            return self._reply(seq, 0, struct.pack("<hhHHHHIIHH", 0, 0, 1, 1, 0, 0, 1, 0, 0, 0), struct.pack("<I", 0))
        self.unknown[("xfixes", minor)] = self.unknown.get(("xfixes", minor), 0) + 1
        return b""


# --------------------------------------------------------------------------------------------------
# PipeWire: private daemon, CLI wrappers, a stand-in session manager (PortConfig + link) and a recorder
# --------------------------------------------------------------------------------------------------
@dataclass
class NodeInfo:
    id: int
    name: str
    media_class: str
    state: str
    n_in: int
    n_out: int
    props: Dict


META_RE = re.compile(r"^update: id:(\d+) key:'([^']*)' value:'(.*)' type:'([^']*)'\s*$")
META_REMOVE_RE = re.compile(r"^remove: id:(\d+) key:'([^']*)'")


class PipeWire:
    PORTCFG_FMT = "{ mediaType = audio, mediaSubtype = raw, format = F32P, rate = 48000, channels = 1, position = [ MONO ] }"

    def __init__(self, sdir: str, env: Dict[str, str], log: Callable[[str], None]):
        self.sdir = sdir
        self.env = env
        self.log = log
        self.child: Optional[Child] = None
        self.monitor: Optional["MetadataMonitor"] = None
        self.nodewatch: Optional["NodeWatcher"] = None

    def start(self):
        cfgdir = os.path.join(self.sdir, "cfg", "pipewire", "pipewire.conf.d")
        os.makedirs(cfgdir, exist_ok=True)
        with open(os.path.join(cfgdir, "10-default-metadata.conf"), "w") as fh:
            fh.write("context.objects = [\n    { factory = metadata\n      args = { metadata.name = default }\n    }\n]\n")
        self.child = Child("pipewire", ["pipewire"], self.env, os.path.join(self.sdir, "pipewire.log")).start()
        sock = os.path.join(self.env["XDG_RUNTIME_DIR"], "pipewire-0")
        if not wait_until(lambda: os.path.exists(sock) and self.child.alive(), 10.0, 0.05):
            raise RuntimeError("private pipewire daemon did not start")
        # the core socket exists slightly before the daemon answers
        if not wait_until(lambda: self.run(["pw-cli", "info", "0"], timeout=3).returncode == 0, 10.0, 0.1):
            raise RuntimeError("private pipewire daemon does not answer")

    def stop(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
        if self.nodewatch:
            self.nodewatch.stop()
        if self.child:
            self.child.stop(grace=3.0)

    def run(self, argv: List[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(argv, 124, exc.stdout or "", exc.stderr or "timeout")

    # -- graph ----------------------------------------------------------------------------------
    def dump(self) -> List[Dict]:
        cp = self.run(["pw-dump"], timeout=10)
        try:
            return json.loads(cp.stdout) if cp.stdout.strip() else []
        except json.JSONDecodeError:
            return []

    def nodes(self, objs: Optional[List[Dict]] = None) -> List[NodeInfo]:
        out = []
        for o in objs if objs is not None else self.dump():
            if o.get("type") != "PipeWire:Interface:Node":
                continue
            info = o.get("info", {}) or {}
            props = info.get("props", {}) or {}
            out.append(NodeInfo(o["id"], props.get("node.name", ""), props.get("media.class", ""),
                                info.get("state", ""), info.get("n-input-ports", 0) or 0,
                                info.get("n-output-ports", 0) or 0, props))
        return out

    def find_nodes(self, name: str) -> List[NodeInfo]:
        return [n for n in self.nodes() if n.name == name]

    def mic_nodes(self) -> List[NodeInfo]:
        return self.find_nodes(NODE_NAME)

    # -- metadata ---------------------------------------------------------------------------------
    def metadata(self) -> Dict[str, Tuple[str, str]]:
        """All key -> (value, type) pairs of subject 0 in the 'default' metadata object."""
        cp = self.run(["pw-metadata", "-n", "default", "0"], timeout=10)
        out: Dict[str, Tuple[str, str]] = {}
        for line in cp.stdout.splitlines():
            m = META_RE.match(line.strip())
            if m:
                out[m.group(2)] = (m.group(3), m.group(4))
        return out

    def get_default_source(self) -> Optional[Tuple[str, str]]:
        return self.metadata().get(DEFAULT_KEY)

    def set_default_source(self, value: str, mtype: str = DEFAULT_TYPE) -> None:
        cp = self.run(["pw-metadata", "-n", "default", "0", DEFAULT_KEY, value, mtype])
        if cp.returncode != 0:
            raise RuntimeError(f"pw-metadata set failed: {cp.stderr or cp.stdout}")

    def clear_default_source(self) -> None:
        cp = self.run(["pw-metadata", "-n", "default", "-d", "0", DEFAULT_KEY])
        if cp.returncode != 0:
            raise RuntimeError(f"pw-metadata delete failed: {cp.stderr or cp.stdout}")

    def start_monitor(self) -> "MetadataMonitor":
        self.monitor = MetadataMonitor(self)
        self.monitor.start()
        return self.monitor

    # -- stand-in session manager ----------------------------------------------------------------
    def configure_ports(self, node_id: int, direction: str) -> None:
        self.run(["pw-cli", "set-param", str(node_id), "PortConfig",
                  f"{{ direction = {direction}, mode = dsp, monitor = false, control = false, "
                  f"format = {self.PORTCFG_FMT} }}"], timeout=5)

    def link_nodes(self, src: NodeInfo, dst: NodeInfo, timeout: float = 5.0) -> bool:
        self.configure_ports(src.id, "Output")
        self.configure_ports(dst.id, "Input")
        end = now() + timeout
        out_ports = in_ports = []
        while now() < end:
            objs = self.dump()
            out_ports, in_ports = [], []
            for o in objs:
                if o.get("type") == "PipeWire:Interface:Port":
                    props = (o.get("info", {}) or {}).get("props", {}) or {}
                    if props.get("node.id") == src.id and props.get("port.direction") == "out":
                        out_ports.append(o["id"])
                    if props.get("node.id") == dst.id and props.get("port.direction") == "in":
                        in_ports.append(o["id"])
            if out_ports and in_ports:
                break
            time.sleep(0.1)
        if not (out_ports and in_ports):
            self.log(f"pw: ports missing for link (out={out_ports} in={in_ports})")
            return False
        cp = self.run(["pw-link", str(out_ports[0]), str(in_ports[0])], timeout=5)
        if cp.returncode != 0:
            self.log(f"pw-link failed: {cp.stderr.strip()}")
            return False
        return True

    def record(self, path: str, tag: str = "e2e-rec") -> "Recorder":
        return Recorder(self, path, tag).start()


class MetadataMonitor:
    """Follows the `default` metadata object so write/restore sequences are visible afterwards."""

    def __init__(self, pw: PipeWire):
        self.pw = pw
        self.events: List[Tuple[float, str, Optional[str], Optional[str]]] = []
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    def start(self):
        argv = ["pw-metadata", "-n", "default", "-m"]
        self.proc = subprocess.Popen(argv, env=self.pw.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, stdin=subprocess.DEVNULL, start_new_session=True,
                                     preexec_fn=_pdeathsig_preexec)
        self.thread = threading.Thread(target=self._read, daemon=True, name="metamon")
        self.thread.start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            m = META_RE.match(line)
            if m:
                _id, key, value, _type = m.groups()
                with self.lock:
                    self.events.append((now(), key, value, _type))
                continue
            m = META_REMOVE_RE.match(line)
            if m:
                with self.lock:
                    self.events.append((now(), m.group(2), None, None))
                continue
            if line.startswith("remove:") and "key" not in line:
                with self.lock:
                    self.events.append((now(), "*", None, None))

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        if self.thread:
            self.thread.join(1)

    def history(self, key: str = DEFAULT_KEY) -> List[Optional[str]]:
        with self.lock:
            return [v for (_t, k, v, _ty) in self.events if k == key or k == "*"]

    def writes_of(self, value: str) -> int:
        return sum(1 for v in self.history() if v == value)


class NodeWatcher:
    """Event-driven view of the graph (`pw-dump -m`): how often moonlight-mic appeared and how many existed at once."""

    def __init__(self, pw: PipeWire):
        self.pw = pw
        self.live: Dict[int, str] = {}
        self.created = 0
        self.max_simultaneous = 0
        self.timeline: List[Tuple[float, str, int]] = []
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    def start(self):
        self.proc = subprocess.Popen(["pw-dump", "-m"], env=self.pw.env, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, stdin=subprocess.DEVNULL,
                                     start_new_session=True, preexec_fn=_pdeathsig_preexec)
        self.thread = threading.Thread(target=self._read, daemon=True, name="nodewatch")
        self.thread.start()

    def _apply(self, objs: List[Dict]) -> None:
        with self.lock:
            for o in objs:
                oid = o.get("id")
                info = o.get("info")
                if info is None:
                    if oid in self.live:
                        del self.live[oid]
                        self.timeline.append((now(), "removed", oid))
                    continue
                if o.get("type") != "PipeWire:Interface:Node":
                    continue
                name = (info.get("props") or {}).get("node.name", "")
                if name == NODE_NAME and oid not in self.live:
                    self.live[oid] = name
                    self.created += 1
                    self.timeline.append((now(), "added", oid))
                    self.max_simultaneous = max(self.max_simultaneous, len(self.live))

    def _read(self):
        dec = json.JSONDecoder()
        buf = ""
        while True:
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                return
            buf += chunk
            while True:
                text = buf.lstrip()
                if not text:
                    buf = ""
                    break
                try:
                    obj, end = dec.raw_decode(text)
                except json.JSONDecodeError:
                    buf = text
                    break
                buf = text[end:]
                if isinstance(obj, list):
                    self._apply([o for o in obj if isinstance(o, dict)])

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        if self.thread:
            self.thread.join(1)


class Recorder:
    """pw-record into a WAV file, linked by hand to the moonlight-mic node (no session manager)."""

    def __init__(self, pw: PipeWire, path: str, tag: str):
        self.pw, self.path, self.tag = pw, path, tag
        self.child: Optional[Child] = None
        self.linked = False
        self.samples = array.array("h")

    def start(self) -> "Recorder":
        argv = ["pw-record", "--rate", "48000", "--channels", "1", "--format", "s16",
                "-P", f"{{ node.name = {self.tag} node.passive = false }}", self.path]
        self.child = Child(self.tag, argv, self.pw.env, self.path + ".log").start()
        return self

    def link(self, timeout: float = 6.0) -> bool:
        end = now() + timeout
        src = dst = None
        while now() < end:
            nodes = self.pw.nodes()
            src = next((n for n in nodes if n.name == NODE_NAME), None)
            dst = next((n for n in nodes if n.name == self.tag), None)
            if src and dst:
                break
            time.sleep(0.1)
        if not (src and dst):
            self.pw.log(f"recorder: nodes missing (src={bool(src)} dst={bool(dst)})")
            return False
        self.linked = self.pw.link_nodes(src, dst, timeout=max(1.0, end - now()))
        return self.linked

    def stop(self) -> array.array:
        if self.child:
            self.child.send_sig(signal.SIGINT)
            try:
                self.child.proc.wait(4)
            except subprocess.TimeoutExpired:
                self.child.stop(grace=2.0)
            self.child._close()
        if os.path.exists(self.path):
            self.samples = read_wav_samples(self.path)
        return self.samples


# --------------------------------------------------------------------------------------------------
# Identities (client certificates), the Sunshine instance, the HTTPS API
# --------------------------------------------------------------------------------------------------
@dataclass
class Identity:
    label: str
    cert_path: str
    key_path: str
    pem: str
    uniqueid: str
    uuid: str


def make_identity(directory: str, label: str) -> Identity:
    os.makedirs(directory, exist_ok=True)
    cert = os.path.join(directory, f"{label}.crt")
    key = os.path.join(directory, f"{label}.key")
    cp = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                         "-subj", "/CN=NVIDIA GameStream Client", "-keyout", key, "-out", cert],
                        capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        raise RuntimeError(f"openssl failed: {cp.stderr}")
    os.chmod(key, 0o600)
    with open(cert) as fh:
        pem = fh.read()
    return Identity(label, cert, key, pem, secrets_hex(8).upper(), str(uuid.uuid4()))


def secrets_hex(nbytes: int) -> str:
    return os.urandom(nbytes).hex()


@dataclass
class ApiResult:
    http_status: int
    status_code: Optional[int]
    message: str
    fields: Dict[str, str]
    text: str
    elapsed: float

    @property
    def ok(self) -> bool:
        return self.http_status == 200 and self.status_code == 200


class HttpsApi:
    """GameStream HTTPS API with a client certificate (mutual TLS)."""

    def __init__(self, port: int, ident: Identity):
        self.port = port
        self.ident = ident
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.ctx.load_cert_chain(ident.cert_path, ident.key_path)

    def get(self, path: str, params: Optional[Dict[str, str]] = None, timeout: float = 20.0) -> ApiResult:
        q = ""
        if params:
            from urllib.parse import urlencode
            q = "?" + urlencode(params)
        t0 = now()
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ctx, timeout=timeout)
        try:
            conn.request("GET", path + q, headers={"Connection": "close"})
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
        finally:
            conn.close()
        code, message, fields = None, "", {}
        try:
            root = ET.fromstring(text)
            if root.get("status_code") is not None:
                code = int(root.get("status_code"))
            message = root.get("status_message", "")
            fields = {child.tag: (child.text or "") for child in root}
        except ET.ParseError:
            pass
        return ApiResult(status, code, message, fields, text, now() - t0)


def plain_http_get(port: int, path: str, timeout: float = 3.0) -> Tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def udp_port_free(port: int) -> bool:
    """True when nobody holds the UDP port (the harness can bind it)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return False
        raise
    finally:
        s.close()


class SunshineInstance:
    """One configured sunshine process (config, apps, paired-client state) inside the scenario dir."""

    def __init__(self, ctx: "ScenarioCtx", base: int, options: Dict[str, Optional[str]], idents: List[Identity],
                 args: Iterable[str] = ()):
        self.ctx = ctx
        self.base = base
        self.options = options
        self.args = list(args)  # extra `name=value` command-line arguments, which override the config file
        self.idents = idents
        self.sdir = ctx.sdir
        self.appdata = os.path.join(self.sdir, "cfg", "sunshine")
        self.conf_path = os.path.join(self.sdir, "sunshine.conf")
        self.log_path = os.path.join(self.sdir, "sunshine.log")
        self.stdout_path = os.path.join(self.sdir, "sunshine.stdout")
        self.child: Optional[Child] = None
        self.generation = 0
        self.killed_by_harness = False
        self.appid = abs(ctypes.c_int32(zlib.crc32(APP_NAME.encode())).value)

    # ports ------------------------------------------------------------------------------------
    @property
    def http_port(self) -> int: return self.base
    @property
    def https_port(self) -> int: return self.base - 5
    @property
    def video_port(self) -> int: return self.base + 9
    @property
    def control_port(self) -> int: return self.base + 10
    @property
    def audio_port(self) -> int: return self.base + 11
    @property
    def mic_port(self) -> int: return self.base + 12
    @property
    def rtsp_port(self) -> int: return self.base + 21

    # files ------------------------------------------------------------------------------------
    def write_files(self) -> None:
        os.makedirs(os.path.join(self.appdata, "credentials"), exist_ok=True)
        conf = {
            "port": self.base,
            "address_family": "ipv4",
            "upnp": "disabled",
            "system_tray": "disabled",
            "stream_audio": "disabled",
            "min_log_level": "debug",
            "log_path": self.log_path,
            "file_apps": os.path.join(self.appdata, "apps.json"),
            "file_state": os.path.join(self.appdata, "sunshine_state.json"),
            "credentials_file": os.path.join(self.appdata, "credentials.json"),
            "pkey": os.path.join(self.appdata, "credentials", "cakey.pem"),
            "cert": os.path.join(self.appdata, "credentials", "cacert.pem"),
            "ping_timeout": PING_TIMEOUT_MS,
            "sunshine_name": "e2e-sunshine",
            "encoder": "software",
            "capture": "x11",
            "hevc_mode": 1,
            "av1_mode": 1,
            "lan_encryption_mode": 0,
            "wan_encryption_mode": 0,
            "controller": "disabled",
            "keyboard": "disabled",
            "mouse": "disabled",
            "notify_pre_releases": "disabled",
            "dd_configuration_option": "disabled",
            "locale": "en",
            "origin_web_ui_allowed": "pc",
            # Both microphone options are disabled by default in the host, so every scenario opts in explicitly
            # here. A scenario passes None for a key to leave it out of the file and exercise the default.
            "stream_mic": "enabled",
            "mic_default_source": "enabled",
        }
        conf.update(self.options)
        conf = {k: v for k, v in conf.items() if v is not None}
        with open(self.conf_path, "w") as fh:
            for k, v in conf.items():
                fh.write(f"{k} = {v}\n")
        with open(os.path.join(self.appdata, "apps.json"), "w") as fh:
            json.dump({"env": {}, "apps": [{"name": APP_NAME, "cmd": ""}]}, fh)
        state = os.path.join(self.appdata, "sunshine_state.json")
        if not os.path.exists(state):
            devices = [{"name": f"e2e-client-{i.label}", "cert": i.pem, "uuid": i.uuid, "enabled": "true"}
                       for i in self.idents]
            with open(state, "w") as fh:
                json.dump({"root": {"uniqueid": "E2E0E2E0E2E0E2E0", "named_devices": devices}}, fh, indent=1)

    # lifecycle --------------------------------------------------------------------------------
    def start(self, timeout: float = 40.0) -> None:
        self.write_files()
        self.generation += 1
        out = open(self.stdout_path, "ab")
        env = self.ctx.sunshine_env()
        argv = [self.ctx.h.sunshine_bin, self.conf_path, *self.args]
        self.child = Child("sunshine", argv, env, cwd=self.sdir, stdout=out).start()
        self.ctx.log(f"sunshine started pid={self.child.pid} base={self.base} opts={self.options} args={self.args}")

        def ready():
            if not self.child.alive():
                raise RuntimeError(f"sunshine exited during start-up (rc={self.child.returncode()}); see {self.stdout_path}")
            try:
                status, _ = plain_http_get(self.http_port, "/serverinfo", timeout=1.0)
                return status == 200
            except OSError:
                return False

        if not wait_until(ready, timeout, 0.1):
            raise RuntimeError("sunshine did not become ready")

        def rtsp_up():
            try:
                socket.create_connection(("127.0.0.1", self.rtsp_port), timeout=1.0).close()
                return True
            except OSError:
                return False

        if not wait_until(rtsp_up, 10.0, 0.1):
            raise RuntimeError("sunshine RTSP listener did not come up")

    def alive(self) -> bool:
        return self.child is not None and self.child.alive()

    def kill(self) -> Optional[int]:
        """SIGKILL on purpose (the killed-host scenario)."""
        self.ctx.log("sunshine: SIGKILL")
        self.killed_by_harness = True
        return self.child.kill() if self.child else None

    def stop(self) -> None:
        if self.child and self.child.alive():
            self.killed_by_harness = True
            self.child.kill()

    def log_text(self) -> str:
        try:
            with open(self.log_path, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def log_size(self) -> int:
        try:
            return os.path.getsize(self.log_path)
        except OSError:
            return 0

    def log_since(self, offset: int) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(offset)
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def serverinfo(self) -> Dict[str, str]:
        try:
            status, text = plain_http_get(self.http_port, "/serverinfo", timeout=3.0)
            root = ET.fromstring(text)
            return {c.tag: (c.text or "") for c in root}
        except (OSError, ET.ParseError):
            return {}


@dataclass
class StartResult:
    launch: ApiResult
    rtsp: Dict[str, RtspResponse]
    control: bool

    @property
    def mic_port(self) -> Optional[int]:
        r = self.rtsp.get("SETUP mic")
        return r.server_port() if r else None


class AsyncCall:
    def __init__(self, fn: Callable[[], object]):
        self.result = None
        self.error: Optional[BaseException] = None
        self.t0 = now()
        self.t1: Optional[float] = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn):
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
        self.t1 = now()

    def wait(self, timeout: float = 40.0):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("async call still running")
        if self.error:
            raise self.error
        return self.result

    @property
    def done(self) -> bool:
        return not self.thread.is_alive()


class StreamClient:
    """A synthetic Moonlight client: /launch|/resume, RTSP, ENet control, microphone UDP."""

    def __init__(self, ctx: "ScenarioCtx", sun: SunshineInstance, ident: Identity, label: str):
        self.ctx, self.sun, self.ident, self.label = ctx, sun, ident, label
        self.api = HttpsApi(sun.https_port, ident)
        self.session_url: Optional[str] = None
        self.connect_data = 0
        self.enet: Optional[EnetControl] = None
        self.mic: Optional[MicSender] = None
        self.steps: Dict[str, RtspResponse] = {}
        self.launch_result: Optional[ApiResult] = None

    def log(self, msg: str) -> None:
        self.ctx.log(f"[{self.label}] {msg}")

    # HTTPS ------------------------------------------------------------------------------------
    def launch(self, resume: bool = False, appid: Optional[int] = None) -> ApiResult:
        params = {
            "uniqueid": self.ident.uniqueid, "uuid": str(uuid.uuid4()),
            "appid": str(appid if appid is not None else self.sun.appid),
            "mode": f"{FAKE_X_WIDTH}x{FAKE_X_HEIGHT}x30", "additionalStates": "1", "sops": "0",
            "rikey": secrets_hex(16), "rikeyid": str(random.randint(1, 2 ** 30)),
            "localAudioPlayMode": "0", "surroundAudioInfo": "196610", "remoteControllersBitmap": "0",
            "gcmap": "0", "gcpersist": "0",
        }
        res = self.api.get("/resume" if resume else "/launch", params)
        self.launch_result = res
        url = res.fields.get("sessionUrl0")
        if url:
            self.session_url = url
        self.log(f"{'resume' if resume else 'launch'} -> http={res.http_status} status={res.status_code} "
                 f"msg={res.message!r} url={url}")
        return res

    def serverinfo(self) -> ApiResult:
        return self.api.get("/serverinfo", {"uniqueid": self.ident.uniqueid})

    def cancel(self) -> ApiResult:
        res = self.api.get("/cancel", timeout=60.0)
        self.log(f"cancel -> http={res.http_status} status={res.status_code} ({res.elapsed:.1f}s)")
        return res

    def cancel_async(self) -> AsyncCall:
        return AsyncCall(self.cancel)

    # RTSP -------------------------------------------------------------------------------------
    def rtsp_attempt(self, mic: bool = True, announce: bool = True, play: bool = True, audio: bool = True,
                     video: bool = True, control: bool = True, sdp: Optional[str] = None) -> Dict[str, RtspResponse]:
        if not self.session_url:
            raise ScenarioAbort(f"[{self.label}] no RTSP session URL (launch failed?)")
        m = re.match(r"rtsp://([^:/]+):(\d+)", self.session_url)
        host, port = m.group(1), int(m.group(2))
        rtsp = RtspClient(host, port, self.session_url, self.log)
        steps: Dict[str, RtspResponse] = {}
        steps["OPTIONS"] = rtsp.options()
        steps["DESCRIBE"] = rtsp.describe()
        if audio:
            steps["SETUP audio"] = rtsp.setup("audio/0/0")
        if video:
            steps["SETUP video"] = rtsp.setup("video/0/0")
        if mic:
            steps["SETUP mic"] = rtsp.setup("mic/0/0")
        if control:
            resp = rtsp.setup("control/13/0")
            steps["SETUP control"] = resp
            try:
                self.connect_data = int(resp.headers.get("x-ss-connect-data", "0"))
            except ValueError:
                self.connect_data = 0
        if announce:
            steps["ANNOUNCE"] = rtsp.announce(sdp if sdp is not None else build_sdp())
            if play:
                steps["PLAY video"] = rtsp.play("video")
                steps["PLAY audio"] = rtsp.play("audio")
        self.steps.update(steps)
        return steps

    # ENet control ----------------------------------------------------------------------------
    def connect_control(self, timeout: float = 4.0) -> bool:
        offset = self.sun.log_size()
        self.enet = EnetControl("127.0.0.1", self.sun.control_port, self.connect_data, self.log)
        ok = self.enet.connect(timeout)
        self.log(f"control connect -> {ok}")
        if ok:
            # let the control thread run get_session() (it clears the raised launch session)
            wait_until(lambda: LOG_CLIENT_CONNECTED in self.sun.log_since(offset), 3.0, 0.05)
        return ok

    def disconnect(self) -> None:
        if self.enet:
            self.enet.disconnect()
            self.log("control disconnected (client left, app keeps running)")

    # whole flow -------------------------------------------------------------------------------
    def start(self, mic: bool = True, resume: bool = False, control: bool = True) -> StartResult:
        launch = self.launch(resume=resume)
        if launch.status_code != 200 or not self.session_url:
            return StartResult(launch, {}, False)
        steps = self.rtsp_attempt(mic=mic)
        ok = False
        if control and steps.get("ANNOUNCE") and steps["ANNOUNCE"].ok:
            ok = self.connect_control()
        return StartResult(launch, steps, ok)

    def mic_sender(self, src_ip: str = "127.0.0.1", port: Optional[int] = None) -> MicSender:
        if self.mic:
            self.mic.close()
        self.mic = MicSender(port or self.sun.mic_port, src_ip=src_ip)
        return self.mic

    def close(self) -> None:
        if self.mic:
            self.mic.close()
            self.mic = None
        if self.enet:
            self.enet.close()
            self.enet = None


# --------------------------------------------------------------------------------------------------
# Scenario framework
# --------------------------------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ScenarioDef:
    name: str
    covers: str  # "existing" | "new"
    doc: str
    fn: Callable[["ScenarioCtx"], None]
    ping_timeout_ms: int = PING_TIMEOUT_MS


SCENARIOS: Dict[str, ScenarioDef] = {}


def scenario(name: str, covers: str, doc: str):
    def deco(fn):
        SCENARIOS[name] = ScenarioDef(name, covers, doc, fn)
        return fn
    return deco


@dataclass
class ScenarioResult:
    name: str
    covers: str
    status: str  # PASS | FAIL | ERROR
    checks: List[CheckResult]
    notes: List[str]
    seconds: float
    error: str = ""
    logdir: str = ""

    @property
    def failed_checks(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.ok]


class ScenarioCtx:
    def __init__(self, h: "Harness", sdef: ScenarioDef, index: int):
        self.h = h
        self.sdef = sdef
        self.name = sdef.name
        self.sdir = os.path.join(h.workdir, "s", sdef.name)
        self.base_port = BASE_PORT_FIRST + 100 * index
        self.checks: List[CheckResult] = []
        self.notes: List[str] = []
        self.suns: List[SunshineInstance] = []
        self.clients: List[StreamClient] = []
        self.recorders: List[Recorder] = []
        self.sockets: List[socket.socket] = []
        self._log_fh = None
        self._rec_n = 0
        for d in ("run", "cfg", "state", "home", "cache", "data"):
            os.makedirs(os.path.join(self.sdir, d), mode=0o700, exist_ok=True)
        self._log_fh = open(os.path.join(self.sdir, "harness.log"), "a")
        self.pw = PipeWire(self.sdir, self.base_env(), self.log)

    # environment ------------------------------------------------------------------------------
    def base_env(self) -> Dict[str, str]:
        s = self.sdir
        return {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": os.path.join(s, "home"),
            "XDG_CONFIG_HOME": os.path.join(s, "cfg"),
            "XDG_STATE_HOME": os.path.join(s, "state"),
            "XDG_CACHE_HOME": os.path.join(s, "cache"),
            "XDG_DATA_HOME": os.path.join(s, "data"),
            "XDG_RUNTIME_DIR": os.path.join(s, "run"),
            "PIPEWIRE_RUNTIME_DIR": os.path.join(s, "run"),
            "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
        }

    def sunshine_env(self) -> Dict[str, str]:
        env = self.base_env()
        env["DISPLAY"] = self.h.fake_x.path  # a private fake X server (absolute socket path)
        env["PULSE_SERVER"] = "unix:/nonexistent"
        return env

    # logging / checks -------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{ts()} {msg}"
        if self._log_fh:
            self._log_fh.write(line + "\n")
            self._log_fh.flush()
        if self.h.verbose:
            print(f"    | {line}", flush=True)

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        self.log(f"NOTE {msg}")

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        self.checks.append(CheckResult(name, bool(cond), detail))
        self.log(f"CHECK {'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail else ""))
        return bool(cond)

    def require(self, cond: bool, msg: str) -> None:
        if not cond:
            self.check(msg, False)
            raise ScenarioAbort(msg)

    # building blocks --------------------------------------------------------------------------
    def start_pipewire(self) -> PipeWire:
        self.pw.start()
        self.pw.start_monitor()
        self.pw.nodewatch = NodeWatcher(self.pw)
        self.pw.nodewatch.start()
        return self.pw

    def start_sunshine(self, options: Optional[Dict[str, Optional[str]]] = None, idents: Iterable[str] = ("A",),
                       base: Optional[int] = None, args: Iterable[str] = ()) -> SunshineInstance:
        opts = {"ping_timeout": self.sdef.ping_timeout_ms}
        opts.update(options or {})
        sun = SunshineInstance(self, base or self.base_port, opts, [self.h.identities[i] for i in idents], args)
        sun.start()
        self.suns.append(sun)
        return sun

    def client(self, sun: SunshineInstance, ident: str = "A", label: Optional[str] = None) -> StreamClient:
        c = StreamClient(self, sun, self.h.identities[ident], label or f"client-{ident}{len(self.clients)}")
        self.clients.append(c)
        return c

    def hold_udp_port(self, port: int) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", port))
        self.sockets.append(s)
        return s

    def release(self, s: socket.socket) -> None:
        s.close()
        if s in self.sockets:
            self.sockets.remove(s)

    def mic_nodes(self) -> List[NodeInfo]:
        return self.pw.mic_nodes()

    def wait_node(self, present: bool, timeout: float = 4.0) -> bool:
        return bool(wait_until(lambda: bool(self.mic_nodes()) == present, timeout, 0.1))

    def default_source(self) -> Optional[Tuple[str, str]]:
        return self.pw.get_default_source()

    def wait_default(self, expected: Optional[Tuple[str, str]], timeout: float = 4.0) -> bool:
        return bool(wait_until(lambda: self.default_source() == expected, timeout, 0.1))

    def record(self, tag: str = "e2e-rec") -> Recorder:
        self._rec_n += 1
        path = os.path.join(self.sdir, f"rec-{self._rec_n}.wav")
        rec = self.pw.record(path, tag=f"{tag}-{self._rec_n}")
        self.recorders.append(rec)
        ok = rec.link()
        self.check(f"recorder {self._rec_n} linked to {NODE_NAME}", ok)
        time.sleep(0.15)
        return rec

    def stop_record(self, rec: Recorder, freqs=(FREQ_A, FREQ_B)) -> AudioReport:
        samples = rec.stop()
        rep = analyze(samples, freqs)
        self.log(f"recording {os.path.basename(rec.path)}: {rep.describe()}")
        return rep

    def snapshot(self, tag: str) -> None:
        try:
            snap = {"nodes": [dataclasses_asdict(n) for n in self.pw.nodes()],
                    "metadata": self.pw.metadata()}
            with open(os.path.join(self.sdir, f"snapshot-{tag}.json"), "w") as fh:
                json.dump(snap, fh, indent=1)
        except Exception as exc:  # noqa: BLE001
            self.log(f"snapshot failed: {exc}")

    def sunshine_log_has(self, sun: SunshineInstance, needle: str, offset: int = 0) -> bool:
        return needle.lower() in sun.log_since(offset).lower()

    # cleanup ----------------------------------------------------------------------------------
    def cleanup(self) -> None:
        for rec in self.recorders:
            try:
                if rec.child and rec.child.alive():
                    rec.child.stop(grace=1.0)
            except Exception:  # noqa: BLE001
                pass
        for c in self.clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        for s in self.sockets:
            try:
                s.close()
            except OSError:
                pass
        for sun in self.suns:
            try:
                sun.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.pw.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None


def dataclasses_asdict(obj) -> Dict:
    return {k: getattr(obj, k) for k in obj.__dataclass_fields__}


# --------------------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------------------
def start_checks(ctx: ScenarioCtx, sun: SunshineInstance, st: StartResult, mic: bool = True,
                 label: str = "") -> bool:
    """Common assertions for a stream start; returns True when the session reached ANNOUNCE 200."""
    p = f"{label}: " if label else ""
    ctx.check(f"{p}/launch|/resume accepted (status_code 200)", st.launch.status_code == 200,
              f"status_code={st.launch.status_code} message={st.launch.message!r}")
    if mic:
        r = st.rtsp.get("SETUP mic")
        ctx.check(f"{p}SETUP mic -> 200", bool(r and r.status == 200), f"status={r.status if r else None}")
        ctx.check(f"{p}SETUP mic Transport server_port == base+12 ({sun.mic_port})",
                  bool(r and r.server_port() == sun.mic_port), f"server_port={r.server_port() if r else None}")
    ann = st.rtsp.get("ANNOUNCE")
    ok = bool(ann and ann.status == 200)
    ctx.check(f"{p}ANNOUNCE -> 200 (session started)", ok, f"status={ann.status if ann else None}")
    if ok:
        ctx.check(f"{p}control channel attached", st.control)
    return ok


def cancel_and_check_gone(ctx: ScenarioCtx, c: StreamClient, sun: SunshineInstance, within: float = 3.0) -> float:
    """Issue /cancel and wait for the node to disappear; returns the observed node-removal latency."""
    t0 = now()
    call = c.cancel_async()
    gone = bool(wait_until(lambda: not ctx.mic_nodes(), within, 0.05))
    latency = now() - t0
    ctx.check(f"node removed within {within:.0f} s of /cancel", gone, f"{latency:.2f}s")
    ctx.log(f"node teardown latency after /cancel: {latency:.2f}s")
    try:
        res = call.wait(40)
        ctx.check("/cancel -> status_code 200", res.status_code == 200,
                  f"status_code={res.status_code} took {res.elapsed:.1f}s")
    except Exception as exc:  # noqa: BLE001
        ctx.check("/cancel returned", False, repr(exc))
    return latency


def final_state_checks(ctx: ScenarioCtx, sun: SunshineInstance, expect_default: Optional[Tuple[str, str]],
                       what: str = "after teardown") -> None:
    ctx.check(f"default key {what}: {expect_default[0] if expect_default else 'absent'}",
              ctx.wait_default(expect_default, 3.0), f"got {ctx.default_source()}")
    ctx.check(f"UDP base+12 free {what}", bool(wait_until(lambda: udp_port_free(sun.mic_port), 3.0, 0.1)))
    ctx.check(f"no moonlight-mic node {what}", not ctx.mic_nodes())
    ctx.check(f"sunshine still running {what}", sun.alive())


def tone_burst(ctx: ScenarioCtx, c: StreamClient, frames: int = 75, freq: float = FREQ_A) -> AudioReport:
    rec = ctx.record()
    mic = c.mic_sender()
    mic.play(frames, freq)
    time.sleep(0.3)
    return ctx.stop_record(rec)


@scenario("baseline", "existing",
          "mic enabled: SETUP mic 200 + server_port, node, default key, tone RMS, /cancel => node gone, key absent, port free")
def sc_baseline(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    st = c.start(mic=True)
    ctx.require(start_checks(ctx, sun, st), "session did not start")
    ctx.check("node 'moonlight-mic' appears", ctx.wait_node(True))
    nodes = ctx.mic_nodes()
    ctx.check("exactly one moonlight-mic node", len(nodes) == 1, f"count={len(nodes)}")
    if nodes:
        ctx.check("node media.class == Audio/Source", nodes[0].media_class == "Audio/Source", nodes[0].media_class)
    ctx.check("default source key == moonlight-mic", ctx.wait_default((OWN_VALUE, DEFAULT_TYPE)),
              f"got {ctx.default_source()}")
    ctx.check("UDP base+12 held by sunshine", not udp_port_free(sun.mic_port))
    rep = tone_burst(ctx, c, 100)
    rms = rep.median_active_rms(FREQ_A)
    ctx.check(f"recorded 440 Hz tone RMS plausible (> {MIN_RMS})", rms > MIN_RMS, f"median rms={rms:.0f}; {rep.describe()}")
    ctx.check("recorded tone lasts >= 1.2 s", rep.active_ms(FREQ_A) >= 1200, f"{rep.active_ms(FREQ_A)} ms")
    ctx.note(f"recorded tone RMS ~{rms:.0f} (expected ~{EXPECTED_RMS:.0f}), {rep.active_ms(FREQ_A)} ms active")
    cancel_and_check_gone(ctx, c, sun)
    final_state_checks(ctx, sun, None)


@scenario("previous-default", "existing",
          "a previously configured default source is restored exactly after teardown")
def sc_previous_default(ctx: ScenarioCtx):
    pw = ctx.start_pipewire()
    pw.set_default_source(PREVIOUS_VALUE)
    ctx.check("precondition: previous default set", ctx.default_source() == (PREVIOUS_VALUE, DEFAULT_TYPE))
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.check("node appears", ctx.wait_node(True))
    ctx.check("default source switched to moonlight-mic", ctx.wait_default((OWN_VALUE, DEFAULT_TYPE)),
              f"got {ctx.default_source()}")
    cancel_and_check_gone(ctx, c, sun)
    final_state_checks(ctx, sun, (PREVIOUS_VALUE, DEFAULT_TYPE))
    hist = pw.monitor.history() if pw.monitor else []
    ctx.note(f"metadata history of {DEFAULT_KEY}: {hist}")


def conf_value(sun: SunshineInstance, key: str) -> Optional[str]:
    """The value the generated sunshine.conf of this instance sets for the key, None when it is absent."""
    with open(sun.conf_path) as fh:
        for line in fh:
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                return value.strip()
    return None


def conf_has_key(sun: SunshineInstance, key: str) -> bool:
    """True when the generated sunshine.conf of this instance sets the key (the harness never writes comments)."""
    return conf_value(sun, key) is not None


def closed_mic_session(ctx: ScenarioCtx, options: Dict[str, Optional[str]]) -> None:
    """Run an ordinary stream whose client sets the microphone up against a host that must not open it.

    The explicit `stream_mic = disabled` scenario and the "options absent" scenario share this body so that both
    assert the same boundary: SETUP mic is still answered, but there is no UDP socket, no PipeWire source and no
    write of the default source, and the stream itself is unaffected.
    """
    pw = ctx.start_pipewire()
    pw.set_default_source(PREVIOUS_VALUE)
    sun = ctx.start_sunshine(options)
    for key, value in options.items():
        if value is None:
            ctx.check(f"config file really lacks {key}", not conf_has_key(sun, key))
    c = ctx.client(sun)
    st = c.start(mic=True)
    ctx.require(start_checks(ctx, sun, st), "session did not start")
    time.sleep(1.5)
    ctx.check("no moonlight-mic node while the session runs", not ctx.mic_nodes())
    ctx.check("UDP base+12 not bound by sunshine", udp_port_free(sun.mic_port))
    ctx.check("default key untouched", ctx.default_source() == (PREVIOUS_VALUE, DEFAULT_TYPE),
              f"got {ctx.default_source()}")
    info = sun.serverinfo()
    ctx.check("session otherwise fine: app running (state SUNSHINE_SERVER_BUSY)",
              info.get("state") == "SUNSHINE_SERVER_BUSY", f"state={info.get('state')}")
    ctx.check("sunshine alive", sun.alive())
    c.cancel()
    final_state_checks(ctx, sun, (PREVIOUS_VALUE, DEFAULT_TYPE))
    ctx.check("metadata monitor saw no write of the mic node",
              (pw.monitor.writes_of(OWN_VALUE) == 0) if pw.monitor else True)


@scenario("mic-disabled", "existing",
          "stream_mic=disabled: SETUP mic still 200, but no node, no UDP socket, key untouched, session fine")
def sc_mic_disabled(ctx: ScenarioCtx):
    closed_mic_session(ctx, {"stream_mic": "disabled"})


@scenario("defaults-absent", "new",
          "both mic options absent from the config (the defaults): SETUP mic still 200, but no node, no UDP socket, "
          "key untouched, session fine")
def sc_defaults_absent(ctx: ScenarioCtx):
    closed_mic_session(ctx, {"stream_mic": None, "mic_default_source": None})


@scenario("mic-only-optin", "new",
          "stream_mic=enabled and mic_default_source absent: node + audio, but the default key is never written")
def sc_mic_only_optin(ctx: ScenarioCtx):
    pw = ctx.start_pipewire()
    pw.set_default_source(PREVIOUS_VALUE)
    sun = ctx.start_sunshine({"mic_default_source": None})
    ctx.check("config file opts in with stream_mic", conf_has_key(sun, "stream_mic"))
    ctx.check("config file really lacks mic_default_source", not conf_has_key(sun, "mic_default_source"))
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.check("node appears", ctx.wait_node(True))
    rep = tone_burst(ctx, c, 40)
    ctx.check("node carries the tone", rep.active_ms(FREQ_A) >= 400, rep.describe())
    ctx.check("previous default untouched during the session",
              ctx.default_source() == (PREVIOUS_VALUE, DEFAULT_TYPE), f"got {ctx.default_source()}")
    cancel_and_check_gone(ctx, c, sun)
    final_state_checks(ctx, sun, (PREVIOUS_VALUE, DEFAULT_TYPE))
    ctx.check("metadata monitor saw no write of the mic node",
              (pw.monitor.writes_of(OWN_VALUE) == 0) if pw.monitor else True)


@scenario("cli-optin", "existing",
          "saved config says disabled, the command line says stream_mic=enabled (a runtime override): node + audio, "
          "default key untouched")
def sc_cli_optin(ctx: ScenarioCtx):
    pw = ctx.start_pipewire()
    pw.set_default_source(PREVIOUS_VALUE)
    sun = ctx.start_sunshine({"stream_mic": "disabled", "mic_default_source": "disabled"},
                             args=["stream_mic=enabled", "mic_default_source=disabled"])
    ctx.check("saved config has stream_mic = disabled", conf_value(sun, "stream_mic") == "disabled",
              f"got {conf_value(sun, 'stream_mic')!r}")
    ctx.check("saved config has mic_default_source = disabled", conf_value(sun, "mic_default_source") == "disabled",
              f"got {conf_value(sun, 'mic_default_source')!r}")
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.check("node appears (command line wins over the saved config)", ctx.wait_node(True))
    rep = tone_burst(ctx, c, 40)
    ctx.check("node carries the tone", rep.active_ms(FREQ_A) >= 400, rep.describe())
    ctx.check("previous default untouched during the session",
              ctx.default_source() == (PREVIOUS_VALUE, DEFAULT_TYPE), f"got {ctx.default_source()}")
    cancel_and_check_gone(ctx, c, sun)
    final_state_checks(ctx, sun, (PREVIOUS_VALUE, DEFAULT_TYPE))
    ctx.check("metadata monitor saw no write of the mic node",
              (pw.monitor.writes_of(OWN_VALUE) == 0) if pw.monitor else True)


@scenario("default-disabled", "existing",
          "mic_default_source=disabled: node exists and carries audio, the default key is never written")
def sc_default_disabled(ctx: ScenarioCtx):
    pw = ctx.start_pipewire()
    sun = ctx.start_sunshine({"mic_default_source": "disabled"})
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.check("node appears", ctx.wait_node(True))
    rep = tone_burst(ctx, c, 40)
    ctx.check("node carries the tone", rep.active_ms(FREQ_A) >= 400, rep.describe())
    ctx.check("default key absent during the session", ctx.default_source() is None, f"got {ctx.default_source()}")
    cancel_and_check_gone(ctx, c, sun)
    final_state_checks(ctx, sun, None)
    ctx.check("key never written (metadata monitor)", (not pw.monitor.history()) if pw.monitor else True,
              f"history={pw.monitor.history() if pw.monitor else None}")


@scenario("toggle-restart", "existing",
          "mid-session the client stops, then resumes from a new source port with the counter restarted at 0")
def sc_toggle_restart(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.require(ctx.wait_node(True), "node did not appear")
    rec = ctx.record()
    mic = c.mic_sender()
    first_port = mic.src_port
    mic.play(50, FREQ_A)  # seq 0..49
    time.sleep(0.5)  # user mutes the microphone
    second_port = mic.new_socket()  # ... and unmutes: new socket, counter restarts
    mic.play(50, FREQ_B, start_seq=0)
    time.sleep(0.3)
    rep = ctx.stop_record(rec)
    ctx.check("source port changed on toggle", first_port != second_port, f"{first_port} -> {second_port}")
    ctx.check("audio before the toggle (440 Hz >= 600 ms)", rep.active_ms(FREQ_A) >= 600, rep.describe())
    ctx.check("audio resumed after the toggle (660 Hz >= 600 ms)", rep.active_ms(FREQ_B) >= 600, rep.describe())
    c.cancel()
    final_state_checks(ctx, sun, None)


@scenario("toggle-restart-lost-first", "new",
          "counter restarts at 1 (packet 0 lost) after ~300 packets: audio must resume within 5 packets")
def sc_toggle_restart_lost_first(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.require(ctx.wait_node(True), "node did not appear")
    rec = ctx.record()
    mic = c.mic_sender()
    mic.play(310, FREQ_A, pace=0.004)  # counter reaches ~310 (sent fast; only the order matters)
    mic.play(25, FREQ_A)  # a little real-time audio, then the user toggles
    time.sleep(0.5)
    mic.new_socket()
    mic.play(5, FREQ_B, start_seq=1)  # counter restarts at 1: packet 0 was lost
    time.sleep(0.4)
    rep = ctx.stop_record(rec)
    got = rep.active_ms(FREQ_B)
    ctx.check("audio resumed within 5 packets after a restart that lost packet 0 (>= 1 decoded frame of 660 Hz)",
              got >= 10, f"660 Hz active {got} ms; {rep.describe()}")
    # Control: keep counting (6, 7, ...) past the old high-water mark.  Whatever the host does with the
    # first packets, audio must be accepted again once the counter has passed the previous sequence numbers;
    # this proves the datagrams reach the host and that the check above is about restart recovery.
    rec2 = ctx.record()
    mic.play(340, FREQ_B, pace=0.004)
    time.sleep(0.4)
    rep2 = ctx.stop_record(rec2)
    ctx.check("control: audio accepted again once the counter passed the old high-water mark (>= 100 ms)",
              rep2.active_ms(FREQ_B) >= 100, rep2.describe())
    c.cancel()
    final_state_checks(ctx, sun, None)


@scenario("reconnect", "existing",
          "/cancel then a second launch with mic works again; exactly one node at any time")
def sc_reconnect(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c1 = ctx.client(sun, label="session-1")
    ctx.require(start_checks(ctx, sun, c1.start(mic=True), label="session 1"), "session 1 did not start")
    ctx.check("session 1: node appears", ctx.wait_node(True))
    ctx.check("session 1: tone audible", tone_burst(ctx, c1, 40).active_ms(FREQ_A) >= 400)
    cancel_and_check_gone(ctx, c1, sun)
    final_state_checks(ctx, sun, None, "after session 1")
    c1.close()
    # second launch (the app was terminated by /cancel, so a plain /launch is valid)
    c2 = ctx.client(sun, label="session-2")
    ctx.require(start_checks(ctx, sun, c2.start(mic=True), label="session 2"), "session 2 did not start")
    ctx.check("session 2: node appears", ctx.wait_node(True))
    counts = []
    for _ in range(5):
        counts.append(len(ctx.mic_nodes()))
        time.sleep(0.2)
    ctx.check("session 2: exactly one moonlight-mic node at every sample", set(counts) == {1}, f"counts={counts}")
    ctx.check("session 2: default key == moonlight-mic", ctx.wait_default((OWN_VALUE, DEFAULT_TYPE)))
    ctx.check("session 2: tone audible", tone_burst(ctx, c2, 40).active_ms(FREQ_A) >= 400)
    cancel_and_check_gone(ctx, c2, sun)
    final_state_checks(ctx, sun, None, "after session 2")
    nw = ctx.pw.nodewatch
    ctx.check("the node was created once per session (event-driven count)", bool(nw) and nw.created == 2,
              f"created={nw.created if nw else None} max simultaneous={nw.max_simultaneous if nw else None}")


@scenario("reconnect-concurrent", "existing",
          "a second launch+mic session while the first is alive from the same cert: documents /launch vs /resume")
def sc_reconnect_concurrent(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c1 = ctx.client(sun, label="session-1")
    ctx.require(start_checks(ctx, sun, c1.start(mic=True), label="session 1"), "session 1 did not start")
    ctx.check("session 1: node appears", ctx.wait_node(True))
    c2 = ctx.client(sun, label="session-2")
    res = c2.launch(resume=False)
    ctx.note(f"/launch while a session is alive (same cert): http={res.http_status} status_code={res.status_code} "
             f"message={res.message!r}")
    if res.status_code == 200:
        ctx.note("a concurrent /launch is accepted")
        st2 = StartResult(res, c2.rtsp_attempt(mic=True), False)
    else:
        ctx.note("a concurrent /launch is refused; using /resume")
        res = c2.launch(resume=True)
        ctx.check("/resume accepted while a session is alive", res.status_code == 200,
                  f"status_code={res.status_code} message={res.message!r}")
        ctx.require(res.status_code == 200, "/resume refused")
        st2 = StartResult(res, c2.rtsp_attempt(mic=True), False)
    ann = st2.rtsp.get("ANNOUNCE")
    ctx.check("session 2: SETUP mic -> 200", bool(st2.rtsp.get("SETUP mic") and st2.rtsp["SETUP mic"].ok))
    ctx.check("session 2: ANNOUNCE -> 200", bool(ann and ann.ok))
    ctx.require(bool(ann and ann.ok), "session 2 did not start")
    ctx.check("session 2: control attached", c2.connect_control())
    counts = []
    for _ in range(8):
        counts.append(len(ctx.mic_nodes()))
        time.sleep(0.2)
    ctx.check("exactly one moonlight-mic node while both sessions are alive", set(counts) == {1}, f"counts={counts}")
    ctx.check("default key == moonlight-mic", ctx.wait_default((OWN_VALUE, DEFAULT_TYPE)))
    ctx.check("tone from the newer session is audible", tone_burst(ctx, c2, 40).active_ms(FREQ_A) >= 400)
    # /cancel stops and joins the sessions one after the other; the first join can sit in the initial-ping
    # wait for the rest of ping_timeout, so the node (kept alive by the second session) outlives /cancel.
    lat = cancel_and_check_gone(ctx, c1, sun, within=PING_TIMEOUT_MS / 1000 + 3)
    ctx.note(f"node removed {lat:.1f}s after /cancel with two sessions (sessions are joined one after the other)")
    final_state_checks(ctx, sun, None)


@scenario("no-packets", "existing",
          "mic SETUP + ANNOUNCE but no datagram ever sent: no crash, node exists, silent, clean teardown")
def sc_no_packets(ctx: ScenarioCtx):
    pw = ctx.start_pipewire()
    pw.set_default_source(PREVIOUS_VALUE)
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.check("node exists", ctx.wait_node(True))
    rec = ctx.record()
    time.sleep(2.5)
    rep = ctx.stop_record(rec)
    ctx.check("node is silent (no datagrams)", rep.max_rms() < 100, rep.describe())
    ctx.check("sunshine alive and answering", sun.alive() and bool(sun.serverinfo()))
    cancel_and_check_gone(ctx, c, sun)
    final_state_checks(ctx, sun, (PREVIOUS_VALUE, DEFAULT_TYPE))


@scenario("port-blocked", "existing",
          "UDP base+12 already held: no node, key untouched, session continues; after release a new session works")
def sc_port_blocked(ctx: ScenarioCtx):
    pw = ctx.start_pipewire()
    pw.set_default_source(PREVIOUS_VALUE)
    sun = ctx.start_sunshine()
    blocker = ctx.hold_udp_port(sun.mic_port)
    c = ctx.client(sun, label="blocked")
    st = c.start(mic=True)
    ctx.require(start_checks(ctx, sun, st, label="blocked"), "session did not start")
    time.sleep(1.5)
    ctx.check("no moonlight-mic node while the port is blocked", not ctx.mic_nodes())
    ctx.check("default key untouched", ctx.default_source() == (PREVIOUS_VALUE, DEFAULT_TYPE),
              f"got {ctx.default_source()}")
    ctx.check("metadata monitor saw no write of the mic node",
              (pw.monitor.writes_of(OWN_VALUE) == 0) if pw.monitor else True)
    info = sun.serverinfo()
    ctx.check("session continues (app running)", info.get("state") == "SUNSHINE_SERVER_BUSY", f"state={info.get('state')}")
    ctx.check("sunshine alive", sun.alive())
    ctx.release(blocker)
    c.cancel()
    final_state_checks(ctx, sun, (PREVIOUS_VALUE, DEFAULT_TYPE), "after blocked session")
    c.close()
    c2 = ctx.client(sun, label="after-release")
    ctx.require(start_checks(ctx, sun, c2.start(mic=True), label="after release"), "second session did not start")
    ctx.check("after release: node appears", ctx.wait_node(True))
    ctx.check("after release: tone audible", tone_burst(ctx, c2, 40).active_ms(FREQ_A) >= 400)
    cancel_and_check_gone(ctx, c2, sun)
    final_state_checks(ctx, sun, (PREVIOUS_VALUE, DEFAULT_TYPE), "after second session")


@scenario("fresh-launch", "existing",
          "mic session, /cancel, then a launch without SETUP mic: no node, no registration")
def sc_fresh_launch(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c1 = ctx.client(sun, label="with-mic")
    ctx.require(start_checks(ctx, sun, c1.start(mic=True), label="with mic"), "first session did not start")
    ctx.check("first session: node appears", ctx.wait_node(True))
    cancel_and_check_gone(ctx, c1, sun)
    final_state_checks(ctx, sun, None, "after first session")
    c1.close()
    c2 = ctx.client(sun, label="without-mic")
    st = c2.start(mic=False)
    ctx.require(start_checks(ctx, sun, st, mic=False, label="without mic"), "second session did not start")
    time.sleep(1.5)
    ctx.check("no moonlight-mic node after a launch without SETUP mic", not ctx.mic_nodes())
    ctx.check("UDP base+12 not bound", udp_port_free(sun.mic_port))
    ctx.check("default key untouched (absent)", ctx.default_source() is None, f"got {ctx.default_source()}")
    c2.cancel()


@scenario("sticky-setup", "new",
          "within ONE raised launch: attempt 1 does SETUP mic but never ANNOUNCEs, attempt 2 (no mic) must not register")
def sc_sticky_setup(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    res = c.launch()
    ctx.require(res.status_code == 200, "launch failed")
    a1 = c.rtsp_attempt(mic=True, announce=False, play=False, control=True)
    ctx.check("attempt 1: SETUP mic -> 200", bool(a1.get("SETUP mic") and a1["SETUP mic"].ok))
    a2 = c.rtsp_attempt(mic=False, announce=True, play=True, control=True)
    ctx.check("attempt 2: ANNOUNCE -> 200", bool(a2.get("ANNOUNCE") and a2["ANNOUNCE"].ok))
    ctx.require(bool(a2.get("ANNOUNCE") and a2["ANNOUNCE"].ok), "attempt 2 did not start a session")
    ctx.check("control channel attached", c.connect_control())
    time.sleep(1.5)
    ctx.check("no mic session registered by attempt 2 (no node)", not ctx.mic_nodes(),
              f"nodes={[n.name for n in ctx.mic_nodes()]}")
    ctx.check("UDP base+12 not bound", udp_port_free(sun.mic_port))
    ctx.check("default key untouched (absent)", ctx.default_source() is None, f"got {ctx.default_source()}")
    c.cancel()


@scenario("killed-host", "existing",
          "SIGKILL sunshine mid-session: stale key remains; a fresh sunshine's session restores to ABSENT")
def sc_killed_host(ctx: ScenarioCtx):
    pw = ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c = ctx.client(sun, label="before-kill")
    ctx.require(start_checks(ctx, sun, c.start(mic=True), label="before kill"), "session did not start")
    ctx.check("node appears", ctx.wait_node(True))
    ctx.check("default key == moonlight-mic", ctx.wait_default((OWN_VALUE, DEFAULT_TYPE)))
    rc = sun.kill()
    c.close()
    ctx.note(f"sunshine killed (returncode {rc})")
    ctx.check("node removed after the host died", ctx.wait_node(False, 4.0))
    stale = ctx.default_source()
    ctx.check("stale key still names moonlight-mic (PipeWire keeps it)", stale == (OWN_VALUE, DEFAULT_TYPE), f"got {stale}")
    # a fresh sunshine, same state directory and ports
    sun2 = ctx.start_sunshine(base=sun.base)
    c2 = ctx.client(sun2, label="after-restart")
    ctx.require(start_checks(ctx, sun2, c2.start(mic=True), label="after restart"), "second session did not start")
    ctx.check("fresh host: node appears", ctx.wait_node(True))
    ctx.check("fresh host: default key == moonlight-mic", ctx.wait_default((OWN_VALUE, DEFAULT_TYPE)))
    cancel_and_check_gone(ctx, c2, sun2)
    final_state_checks(ctx, sun2, None, "after teardown (stale value treated as no previous)")
    ctx.note(f"metadata history: {pw.monitor.history() if pw.monitor else None}")


@scenario("user-replaced-default", "new",
          "the user picks another default during the session: it must survive the teardown")
def sc_user_replaced_default(ctx: ScenarioCtx):
    pw = ctx.start_pipewire()
    pw.set_default_source(PREVIOUS_VALUE)
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.check("default switched to moonlight-mic", ctx.wait_default((OWN_VALUE, DEFAULT_TYPE)))
    pw.set_default_source(USER_VALUE)
    ctx.check("precondition: user value in place", ctx.default_source() == (USER_VALUE, DEFAULT_TYPE))
    time.sleep(0.5)
    cancel_and_check_gone(ctx, c, sun)
    final_state_checks(ctx, sun, (USER_VALUE, DEFAULT_TYPE))


@scenario("user-cleared-default", "new",
          "the user clears the default during the session: it must stay absent after teardown")
def sc_user_cleared_default(ctx: ScenarioCtx):
    pw = ctx.start_pipewire()
    pw.set_default_source(PREVIOUS_VALUE)
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.check("default switched to moonlight-mic", ctx.wait_default((OWN_VALUE, DEFAULT_TYPE)))
    pw.clear_default_source()
    ctx.check("precondition: key cleared", ctx.default_source() is None)
    time.sleep(0.5)
    cancel_and_check_gone(ctx, c, sun)
    final_state_checks(ctx, sun, None)


@scenario("foreign-source-ip", "existing",
          "datagrams from an address other than the RTSP client's are ignored (bonus scenario)")
def sc_foreign_source_ip(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    ctx.require(start_checks(ctx, sun, c.start(mic=True)), "session did not start")
    ctx.require(ctx.wait_node(True), "node did not appear")
    rec = ctx.record()
    foreign = MicSender(sun.mic_port, src_ip="127.0.0.2")
    try:
        foreign.play(40, FREQ_B)
    finally:
        foreign.close()
    mic = c.mic_sender()
    mic.play(40, FREQ_A)
    time.sleep(0.3)
    rep = ctx.stop_record(rec)
    ctx.check("datagrams from 127.0.0.2 dropped (no 660 Hz)", rep.active_ms(FREQ_B) == 0, rep.describe())
    ctx.check("datagrams from the client IP accepted (440 Hz >= 400 ms)", rep.active_ms(FREQ_A) >= 400, rep.describe())
    c.cancel()
    final_state_checks(ctx, sun, None)


@scenario("announce-attributes", "existing",
          "ANNOUNCE contract: the 10 attributes without a default suffice; a missing one is answered with 400")
def sc_announce_attributes(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c = ctx.client(sun)
    res = c.launch()
    ctx.require(res.status_code == 200, "launch failed")
    for attr, _v in REQUIRED_ANNOUNCE_ATTRS:
        steps = c.rtsp_attempt(mic=False, play=False, control=False, sdp=build_sdp(required_only=True, omit=[attr]))
        a = steps.get("ANNOUNCE")
        ctx.check(f"ANNOUNCE without {attr} -> 400", bool(a and a.status == 400), f"status={a.status if a else None}")
    steps = c.rtsp_attempt(mic=True, sdp=build_sdp(required_only=True))
    a = steps.get("ANNOUNCE")
    ctx.check("ANNOUNCE with only the required attributes -> 200", bool(a and a.status == 200),
              f"status={a.status if a else None}")
    ctx.check("session started: control attaches", c.connect_control())
    ctx.check("session started: node appears", ctx.wait_node(True))
    c.cancel()


@scenario("pending-launch", "existing",
          "documents how a raised launch session is kept/replaced (the scope of setup_mic); informational")
def sc_pending_launch(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun = ctx.start_sunshine()
    c = ctx.client(sun)

    def payload(steps: Dict[str, RtspResponse]) -> Optional[str]:
        r = steps.get("SETUP audio")
        return r.headers.get("x-ss-ping-payload") if r else None

    ctx.require(c.launch().status_code == 200, "launch failed")
    p1 = payload(c.rtsp_attempt(mic=False, announce=False, control=False, video=False))
    r2 = c.launch(resume=True)
    ctx.check("/resume while the app is running accepted", r2.status_code == 200, f"status_code={r2.status_code}")
    p2 = payload(c.rtsp_attempt(mic=False, announce=False, control=False, video=False))
    ctx.check("RTSP SETUP audio answered with a ping payload both times", bool(p1 and p2), f"{p1} {p2}")
    ctx.note("second /resume while the first launch session is still pending (no control attach yet): "
             + ("DISCARDED - RTSP still serves the first launch session (same X-SS-Ping-Payload)" if p1 == p2
                else "replaced - RTSP serves the new launch session (new X-SS-Ping-Payload)"))
    # establish a session: the ENet control attach clears the raised launch session
    steps = c.rtsp_attempt(mic=True)
    ann = steps.get("ANNOUNCE")
    ctx.check("ANNOUNCE -> 200", bool(ann and ann.ok))
    ctx.require(bool(ann and ann.ok), "session did not start")
    ctx.check("control attached", c.connect_control())
    r3 = c.launch(resume=True)
    ctx.check("/resume after the control attach accepted", r3.status_code == 200, f"status_code={r3.status_code}")
    p3 = payload(c.rtsp_attempt(mic=False, announce=False, control=False, video=False))
    ctx.note("/resume after the control attach: "
             + ("new launch session served (fresh X-SS-Ping-Payload)" if p3 and p3 != p1 else "still the first one"))
    c.cancel()


def _two_sessions(ctx: ScenarioCtx, ident_b: str):
    sun = ctx.start_sunshine(idents=("A", "B"), options={"ping_timeout": 9500})
    a = ctx.client(sun, "A", "client-A")
    b = ctx.client(sun, ident_b, "client-B")
    ctx.require(start_checks(ctx, sun, a.start(mic=True), label="client A"), "client A did not start")
    ctx.check("client A: node appears", ctx.wait_node(True))
    st_b = b.start(mic=True, resume=True)  # the app is running already: /resume
    ctx.require(start_checks(ctx, sun, st_b, label="client B"), "client B did not start")
    return sun, a, b


@scenario("ambiguous-same-ip", "new",
          "two sessions, same IP, different certs: no mic audio accepted while both live; resumes after one leaves")
def sc_ambiguous_same_ip(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun, a, b = _two_sessions(ctx, "B")
    counts = [len(ctx.mic_nodes()) for _ in range(3)]
    ctx.check("one shared moonlight-mic node", set(counts) == {1}, f"counts={counts}")
    off = sun.log_size()
    rec = ctx.record()
    mic = a.mic_sender()
    mic.play(40, FREQ_A)
    time.sleep(0.2)
    rep1 = ctx.stop_record(rec)
    ctx.check("no mic audio accepted while two sessions share the IP (silence)", rep1.active_ms() == 0, rep1.describe())
    ctx.check(f"host log mentions '{AMBIGUOUS_LOG}'", ctx.sunshine_log_has(sun, AMBIGUOUS_LOG, off))
    b.disconnect()  # session B ends; the app keeps running, A stays
    time.sleep(1.2)
    rec2 = ctx.record()
    mic.play(40, FREQ_A)
    time.sleep(0.2)
    rep2 = ctx.stop_record(rec2)
    ctx.check("audio resumes after one of the sessions left (440 Hz >= 400 ms)", rep2.active_ms(FREQ_A) >= 400,
              rep2.describe())
    a.cancel()
    final_state_checks(ctx, sun, None)


@scenario("same-client-supersede", "new",
          "the same cert twice: the newer session owns the mic (audio keeps flowing after the older one leaves)")
def sc_same_client_supersede(ctx: ScenarioCtx):
    ctx.start_pipewire()
    sun, a1, a2 = _two_sessions(ctx, "A")
    rec = ctx.record()
    mic = a2.mic_sender()
    mic.play(40, FREQ_A)
    time.sleep(0.2)
    rep1 = ctx.stop_record(rec)
    ctx.check("audio accepted with two sessions of the same client (440 Hz >= 400 ms)", rep1.active_ms(FREQ_A) >= 400,
              rep1.describe())
    a1.disconnect()  # the older session leaves
    time.sleep(1.2)
    rec2 = ctx.record()
    mic.play(40, FREQ_B)
    time.sleep(0.2)
    rep2 = ctx.stop_record(rec2)
    ctx.check("newer session still owns the mic after the older left (660 Hz >= 400 ms)", rep2.active_ms(FREQ_B) >= 400,
              rep2.describe())
    a2.cancel()
    final_state_checks(ctx, sun, None)


# --------------------------------------------------------------------------------------------------
# Harness: isolation bootstrap, scenario runner, reporting
# --------------------------------------------------------------------------------------------------
class SetupError(RuntimeError):
    pass


MS_BIND = 4096
MS_REC = 16384
MS_PRIVATE = 1 << 18


def bind_null_over(path: str) -> None:
    rc = _libc.mount(b"/dev/null", path.encode(), None, ctypes.c_ulong(MS_BIND), None)
    if rc != 0:
        err = ctypes.get_errno()
        raise SetupError(f"cannot neutralise {path}: {os.strerror(err)}")
    st = os.stat(path)
    if not (os.major(st.st_rdev) == 1 and os.minor(st.st_rdev) == 3):
        raise SetupError(f"{path} is not /dev/null after the bind mount")


def read_input_devices() -> List[str]:
    """Names of the input devices the kernel lists (world-readable, not namespaced)."""
    try:
        with open("/proc/bus/input/devices") as fh:
            return sorted(re.findall(r'^N: Name="(.*)"$', fh.read(), flags=re.M))
    except OSError:
        return []


class Harness:
    def __init__(self, args, workdir: str, logroot: str):
        self.args = args
        self.workdir = workdir
        self.logroot = logroot
        self.sunshine_bin = os.path.abspath(args.sunshine)
        self.verbose = args.verbose
        self.identities: Dict[str, Identity] = {}
        self.fake_x: Optional[FakeXServer] = None
        self.results: List[ScenarioResult] = []
        self.audit: List[str] = []
        self.failed_dirs: List[str] = []
        self.input_before: List[str] = []
        self.binary_sha = "?"
        self.binary_origin = self.sunshine_bin
        self._shut = False
        self.vdev_failures = 0
        self.vdev_other = 0

    # bootstrap ----------------------------------------------------------------------------------
    def bootstrap(self) -> None:
        os.nice(15)
        _libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        cp = subprocess.run(["ip", "link", "set", "lo", "up"], capture_output=True, text=True)
        if cp.returncode != 0:
            raise SetupError(f"cannot bring lo up: {cp.stderr.strip()}")
        ifaces = sorted(name for _idx, name in socket.if_nameindex())  # /sys still shows the parent netns
        if ifaces != ["lo"]:
            raise SetupError(f"network namespace is not private: interfaces {ifaces}")
        if os.geteuid() != 0:
            raise SetupError("not running as the namespace root")
        self.audit.append(f"private network namespace: interfaces={ifaces}")
        # Mount changes must never propagate to the parent namespace.
        rc = _libc.mount(b"none", b"/", None, ctypes.c_ulong(MS_REC | MS_PRIVATE), None)
        if rc != 0:
            raise SetupError(f"cannot make the mount namespace private: {os.strerror(ctypes.get_errno())}")
        # Devices through which a second Sunshine could disturb the live desktop.
        targets = ["/dev/uinput", "/dev/uhid", "/dev/input/uinput"]
        if os.path.isdir("/dev/dri"):
            targets += [os.path.join("/dev/dri", n) for n in sorted(os.listdir("/dev/dri")) if n.startswith("card")]
        done = []
        for t in targets:
            if os.path.exists(t):
                bind_null_over(t)
                done.append(t)
        self.audit.append(f"neutralised (bind-mounted /dev/null over): {', '.join(done) or 'nothing present'}")
        # a private /tmp (best effort) so stray temp files never reach the real one
        rc = _libc.mount(b"tmpfs", b"/tmp", b"tmpfs", ctypes.c_ulong(0), b"mode=1777,size=64m")
        self.audit.append("private /tmp tmpfs: " + ("yes" if rc == 0 else f"no ({os.strerror(ctypes.get_errno())})"))
        # Paths the live desktop session owns: hidden behind empty tmpfs mounts (this namespace only), so even
        # a harness or sunshine bug cannot read or write them.
        real_uid = os.environ.get("MIC_E2E_REAL_UID", "1000")
        hidden = []
        for path in (f"/run/user/{real_uid}", os.path.join(REAL_HOME, ".config", "sunshine"),
                     os.path.join(REAL_HOME, ".config", "pipewire"), os.path.join(REAL_HOME, ".config", "voxtype"),
                     os.path.join(REAL_HOME, ".local", "state", "wireplumber")):
            rc = _libc.mount(b"none", path.encode(), b"tmpfs", ctypes.c_ulong(0), b"mode=0700,size=1m")
            if rc == 0:
                hidden.append(path)
        self.audit.append(f"hidden behind empty tmpfs: {', '.join(hidden) or 'none'}")
        self.input_before = read_input_devices()
        import hashlib
        self.binary_origin = self.sunshine_bin
        bindir = os.path.join(self.workdir, "bin")
        os.makedirs(bindir, exist_ok=True)
        private_copy = os.path.join(bindir, "sunshine")
        shutil.copy2(self.sunshine_bin, private_copy)  # a rebuild of the original cannot change the run
        self.sunshine_bin = private_copy
        h256 = hashlib.sha256()
        with open(self.sunshine_bin, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h256.update(chunk)
        self.binary_sha = h256.hexdigest()
        os.makedirs(os.path.join(self.workdir, "s"), exist_ok=True)
        idir = os.path.join(self.workdir, "ids")
        for label in ("A", "B"):
            self.identities[label] = make_identity(idir, label)
        self.fake_x = FakeXServer(os.path.join(self.workdir, "x11.sock"), log=lambda m: None)
        self.fake_x.start()

    def shutdown(self) -> None:
        if self._shut:
            return
        self._shut = True
        kill_all_children()
        if self.fake_x:
            self.fake_x.stop()
        after = read_input_devices()
        added = [n for n in after if after.count(n) > self.input_before.count(n)]
        removed = [n for n in self.input_before if self.input_before.count(n) > after.count(n)]
        if not added and not removed:
            self.audit.append("host input device list unchanged during the run")
        else:
            self.audit.append(f"host input device list changed during the run (added {sorted(set(added))}, removed "
                              f"{sorted(set(removed))}); the harness neutralised uinput/uhid, its {self.vdev_failures} "
                              f"virtual-device creation attempts all failed, so these come from the live session")
        if self.fake_x:
            unk = {str(k): v for k, v in self.fake_x.unknown.items()}
            self.audit.append(f"fake X server: {self.fake_x.requests} requests, {self.fake_x.images} GetImage, unhandled={unk or 'none'}")

    # running ------------------------------------------------------------------------------------
    def run_scenario(self, sdef: ScenarioDef, index: int) -> ScenarioResult:
        ctx = ScenarioCtx(self, sdef, index)
        t0 = now()
        status, err = "PASS", ""
        try:
            sdef.fn(ctx)
        except ScenarioAbort as exc:
            status, err = "FAIL", f"aborted: {exc}"
        except Exception as exc:  # noqa: BLE001
            status, err = "ERROR", f"{type(exc).__name__}: {exc}"
            ctx.log("EXCEPTION\n" + traceback.format_exc())
        finally:
            try:
                ctx.snapshot("final")
            except Exception:  # noqa: BLE001
                pass
            nw = ctx.pw.nodewatch
            if nw is not None:
                time.sleep(0.2)
                ctx.check("never more than one moonlight-mic node at any instant", nw.max_simultaneous <= 1,
                          f"max simultaneous={nw.max_simultaneous}, created={nw.created}")
            for sun in ctx.suns:
                died = sun.child is not None and not sun.alive() and not sun.killed_by_harness
                ctx.check(f"sunshine (base {sun.base}, start #{sun.generation}) never crashed", not died,
                          f"exited on its own with rc={sun.child.returncode() if sun.child else None}")
                fatal = [ln for ln in sun.log_text().splitlines() if "]: Fatal:" in ln]
                ctx.check(f"sunshine (base {sun.base}) logged no Fatal lines", not fatal, "; ".join(fatal[:3]))
            ctx.cleanup()
        if any(not c.ok for c in ctx.checks) and status == "PASS":
            status = "FAIL"
        res = ScenarioResult(sdef.name, sdef.covers, status, ctx.checks, ctx.notes, now() - t0, err)
        self.scan_logs_for_leaks(ctx)
        if status != "PASS":
            res.logdir = self.save_logs(ctx)
        return res

    def scan_logs_for_leaks(self, ctx: ScenarioCtx) -> None:
        needles = (os.path.join(REAL_HOME, ".config"), "/run/user/", os.path.join(REAL_HOME, ".local", "state"))
        for sun in ctx.suns:
            for ln in sun.log_text().splitlines():
                if "libvirtualhid" in ln or "uinput" in ln:
                    if "failed to create uinput device" in ln:
                        self.vdev_failures += 1
                    else:
                        self.vdev_other += 1
                if any(n in ln for n in needles):
                    self.audit.append(f"{ctx.name}: sunshine log mentions a real-home path: {ln.strip()[:160]}")

    def save_logs(self, ctx: ScenarioCtx) -> str:
        dest = os.path.join(self.logroot, ctx.name)
        try:
            os.makedirs(self.logroot, exist_ok=True)
            shutil.copytree(ctx.sdir, dest, ignore=shutil.ignore_patterns("credentials", "*.key", "home", "cache",
                                                                         "data", "state", "run"), dirs_exist_ok=True)
        except OSError as exc:
            print(f"warning: could not save logs for {ctx.name}: {exc}", file=sys.stderr)
        self.failed_dirs.append(dest)
        return dest

    def run(self, names: List[str]) -> None:
        order = list(SCENARIOS.keys())
        for name in names:
            sdef = SCENARIOS[name]
            index = order.index(name)
            print(f"--- {name} [{sdef.covers}] ...", flush=True)
            res = self.run_scenario(sdef, index)
            self.results.append(res)
            self.print_result(res)

    def print_result(self, r: ScenarioResult) -> None:
        tag = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "FAIL"}[r.status]
        print(f"{tag}  {r.name:<28} [{r.covers:<8}] {r.seconds:5.1f}s  checks {len(r.checks) - len(r.failed_checks)}/{len(r.checks)}"
              + (f"  ({r.status}: {r.error})" if r.error else ""), flush=True)
        for c in r.failed_checks:
            print(f"        x {c.name}" + (f"  -- {c.detail}" if c.detail else ""), flush=True)
        for n in r.notes:
            print(f"        . {n}", flush=True)
        if r.logdir:
            print(f"        logs: {r.logdir}", flush=True)

    def summarize(self) -> int:
        failed = [r for r in self.results if r.status != "PASS"]
        print("")
        print("=" * 100)
        for r in self.results:
            mark = "PASS" if r.status == "PASS" else "FAIL"
            print(f"  {mark}  {r.name:<28} [{r.covers}]")
        print("=" * 100)
        print(f"{len(self.results) - len(failed)}/{len(self.results)} scenarios passed"
              + (f"; failed: {', '.join(r.name for r in failed)}" if failed else ""))
        print(f"binary under test: {self.binary_origin} (sha256 {self.binary_sha})")
        if self.failed_dirs:
            print(f"failure logs: {self.logroot}")
        print("isolation audit:")
        for line in self.audit:
            print(f"  - {line}")
        return 1 if failed else 0


# --------------------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Isolated end-to-end tests for the Sunshine client-microphone path")
    p.add_argument("--sunshine", help="path to the sunshine binary under test")
    p.add_argument("--scenario", action="append", default=[], metavar="NAME", help="run only this scenario (repeatable)")
    p.add_argument("--list", action="store_true", help="list scenarios and exit")
    p.add_argument("--keep", action="store_true", help="keep the temp dir (and print its path)")
    p.add_argument("-v", "--verbose", action="store_true", help="print the harness log while running")
    return p


def list_scenarios() -> None:
    print(f"{'scenario':<28} {'covers':<9} description")
    for s in SCENARIOS.values():
        print(f"{s.name:<28} {s.covers:<9} {s.doc}")


def select(names: List[str]) -> List[str]:
    if not names:
        return list(SCENARIOS)
    out = []
    for n in names:
        if n not in SCENARIOS:
            raise SystemExit(f"unknown scenario {n!r}; use --list")
        if n not in out:
            out.append(n)
    return out


def outer_main(args) -> int:
    for tool in ("unshare", "ip", "openssl", "pipewire", "pw-cli", "pw-metadata", "pw-record", "pw-dump", "pw-link"):
        if shutil.which(tool) is None:
            print(f"error: required tool not found: {tool}", file=sys.stderr)
            return 2
    if not os.path.isfile(args.sunshine) or not os.access(args.sunshine, os.X_OK):
        print(f"error: --sunshine {args.sunshine!r} is not an executable file", file=sys.stderr)
        return 2
    select(args.scenario)  # validate names early
    probe = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "--mount", "--", "true"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        print("error: cannot create a private user+network+mount namespace (unshare -rnm): "
              + (probe.stderr.strip() or f"exit status {probe.returncode}"), file=sys.stderr)
        return 2
    os.makedirs(CACHE_ROOT, mode=0o700, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="e2e.", dir=CACHE_ROOT)
    os.chmod(workdir, 0o700)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    logdir = os.path.join(LOG_ROOT, stamp)
    env = dict(os.environ)
    env[INNER_ENV] = "1"
    env["MIC_E2E_REAL_UID"] = str(os.getuid())
    env[WORKDIR_ENV] = workdir
    env[LOGDIR_ENV] = logdir
    argv = ["unshare", "--user", "--map-root-user", "--net", "--mount", "--",
            sys.executable, "-I", os.path.abspath(__file__)] + sys.argv[1:]
    proc = subprocess.Popen(argv, env=env)

    def forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)
    rc = proc.wait()
    if args.keep:
        print(f"kept temp dir: {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    if rc not in (0,) and os.path.isdir(logdir):
        print(f"logs: {logdir}")
    elif os.path.isdir(logdir) and not os.listdir(logdir):
        os.rmdir(logdir)
    return rc


def inner_main(args) -> int:
    workdir = os.environ[WORKDIR_ENV]
    logdir = os.environ[LOGDIR_ENV]
    h = Harness(args, workdir, logdir)
    rc = 2

    def on_term(signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    try:
        h.bootstrap()
        h.run(select(args.scenario))
        h.shutdown()
        rc = h.summarize()
    except SetupError as exc:
        print(f"setup error: {exc}", file=sys.stderr)
        rc = 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        rc = 130
    finally:
        h.shutdown()
        kill_all_children()
    return rc


def main() -> int:
    args = build_parser().parse_args()
    if args.list:
        list_scenarios()
        return 0
    if not args.sunshine:
        print("error: --sunshine is required", file=sys.stderr)
        return 2
    if os.environ.get(INNER_ENV) == "1":
        return inner_main(args)
    return outer_main(args)


if __name__ == "__main__":
    sys.exit(main())
