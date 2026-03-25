#!/usr/bin/env python3
"""
symbolstream_recv.py — SymbolStream Protocol v2 Reference Receiver

Listens for a trunk-recorder symbolstream plugin connection and decodes
incoming messages. Demonstrates both binary and JSON framing modes.

The trunk-recorder plugin acts as the TCP client — it connects TO this
receiver. Configure symbolstream with the address/port of this host.

Usage:
    python symbolstream_recv.py [--port 9090] [--bind 0.0.0.0]
    python symbolstream_recv.py --json          # JSON mode
    python symbolstream_recv.py --verbose       # print every frame

Protocol specification: SPEC.md
"""

import argparse
import json
import socket
import struct
import sys
from typing import Optional

# ── Protocol constants ────────────────────────────────────────────────────────

MAGIC           = b'\x53\x59'   # 'SY'
VERSION         = 0x02
HEADER_SIZE     = 8             # bytes

MSG_CODEC_FRAME = 0x01
MSG_CALL_START  = 0x02
MSG_CALL_END    = 0x03
MSG_HEARTBEAT   = 0x04

CODEC_NAMES = {
    0: 'IMBE/P25-P1',
    1: 'AMBE+2/P25-P2',
    2: 'AMBE/DMR',
    3: 'AMBE/D-STAR',
    4: 'AMBE/YSF-Full',
    5: 'AMBE/YSF-Half',
    6: 'AMBE+2/NXDN',
}

# ── I/O helpers ───────────────────────────────────────────────────────────────

def recv_exact(conn: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from conn, blocking until all arrive."""
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf.extend(chunk)
    return bytes(buf)

# ── Binary mode ───────────────────────────────────────────────────────────────

def read_binary_message(conn: socket.socket):
    """Read one v2 binary message. Returns (msg_type, payload_bytes)."""
    hdr = recv_exact(conn, HEADER_SIZE)
    magic   = hdr[0:2]
    version = hdr[2]
    msg_type, payload_len = struct.unpack_from('<BI', hdr, 3)

    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r} (expected {MAGIC!r})")
    if version != VERSION:
        print(f"warning: version {version:#04x} (expected {VERSION:#04x})",
              file=sys.stderr)

    payload = recv_exact(conn, payload_len) if payload_len else b''
    return msg_type, payload


def decode_codec_frame(payload: bytes) -> dict:
    """Decode a CODEC_FRAME payload (see SPEC.md §3.2)."""
    tg, src, call_id      = struct.unpack_from('<III', payload, 0)  # offsets 0-11
    ts_us,                = struct.unpack_from('<Q',   payload, 12) # offsets 12-19
    codec, n_params, errs, flags = struct.unpack_from('<BBBB', payload, 20)
    params = struct.unpack_from(f'<{n_params}I', payload, 24)
    return dict(tg=tg, src=src, call_id=call_id, ts_us=ts_us,
                codec=codec, errs=errs, flags=flags, params=list(params))


def decode_call_start(payload: bytes) -> dict:
    """Decode a CALL_START payload (see SPEC.md §3.3)."""
    # '<IQQIB': tg(4) freq(8) ts_us(8) call_id(4) name_len(1) = 25 bytes
    tg, freq, ts_us, call_id, name_len = struct.unpack_from('<IQQIB', payload, 0)
    sys_name = payload[25:25 + name_len].decode('utf-8', errors='replace')
    return dict(tg=tg, freq=freq, ts_us=ts_us, call_id=call_id, sys=sys_name)


def decode_call_end(payload: bytes) -> dict:
    """Decode a CALL_END payload (see SPEC.md §3.4)."""
    # '<IIIQIIBB': tg(4) call_id(4) src(4) freq(8) dur_ms(4) errs(4) enc(1) name_len(1) = 30 bytes
    tg, call_id, src, freq, dur_ms, err_count, enc, name_len = \
        struct.unpack_from('<IIIQIIBB', payload, 0)
    sys_name = payload[30:30 + name_len].decode('utf-8', errors='replace')
    return dict(tg=tg, call_id=call_id, src=src, freq=freq,
                dur_ms=dur_ms, errs=err_count, enc=bool(enc), sys=sys_name)


def run_binary(conn: socket.socket, verbose: bool = False) -> None:
    """Process a v2 binary-mode connection until it closes."""
    calls: dict = {}   # call_id → call metadata from call_start
    frame_count = 0

    while True:
        msg_type, payload = read_binary_message(conn)

        if msg_type == MSG_CALL_START:
            m = decode_call_start(payload)
            calls[m['call_id']] = m
            print(f"[CALL START] tg={m['tg']:>8}  call_id={m['call_id']}  "
                  f"freq={m['freq'] / 1e6:.4f} MHz  sys={m['sys']!r}")

        elif msg_type == MSG_CALL_END:
            m = decode_call_end(payload)
            calls.pop(m['call_id'], None)
            print(f"[CALL END]   tg={m['tg']:>8}  call_id={m['call_id']}  "
                  f"src={m['src']}  dur={m['dur_ms'] / 1000:.1f}s  "
                  f"errs={m['errs']}  enc={m['enc']}")

        elif msg_type == MSG_CODEC_FRAME:
            m = decode_codec_frame(payload)
            frame_count += 1
            codec_name = CODEC_NAMES.get(m['codec'], f"codec_{m['codec']}")
            silence = ' [silence]' if m['flags'] & 0x01 else ''
            if verbose:
                print(f"[FRAME] tg={m['tg']}  src={m['src']}  {codec_name}"
                      f"  errs={m['errs']}{silence}  params={m['params'][:4]}...")
            elif frame_count % 50 == 1:
                print(f"[FRAME] #{frame_count}  tg={m['tg']}  {codec_name}"
                      f"  errs={m['errs']}{silence}")

        elif msg_type == MSG_HEARTBEAT:
            if verbose:
                print("[HEARTBEAT]")

        else:
            # Unknown message type — payload already consumed, safe to continue.
            print(f"[UNKNOWN type={msg_type:#04x}, {len(payload)} bytes skipped]",
                  file=sys.stderr)

# ── JSON mode ─────────────────────────────────────────────────────────────────

def read_json_message(conn: socket.socket) -> dict:
    """Read one length-prefixed JSON message (v2 JSON framing, SPEC.md §4)."""
    len_bytes = recv_exact(conn, 4)
    json_len, = struct.unpack('<I', len_bytes)
    raw = recv_exact(conn, json_len)
    return json.loads(raw.decode('utf-8'))


def run_json(conn: socket.socket, verbose: bool = False) -> None:
    """Process a v2 (or v1-compatible) JSON-mode connection."""
    frame_count = 0

    while True:
        msg = read_json_message(conn)

        # v2 uses "type"; v1 uses "event" — handle both
        msg_type = msg.get('type') or msg.get('event', '')

        if msg_type == 'call_start':
            tg   = msg.get('tg', msg.get('talkgroup', 0))
            freq = msg.get('freq', 0)
            sys_name = msg.get('sys', msg.get('short_name', ''))
            call_id  = msg.get('call_id', '?')
            print(f"[CALL START] tg={tg:>8}  call_id={call_id}  "
                  f"freq={freq / 1e6:.4f} MHz  sys={sys_name!r}")

        elif msg_type == 'call_end':
            tg   = msg.get('tg', msg.get('talkgroup', 0))
            dur  = msg.get('dur_ms', msg.get('duration', 0) * 1000) / 1000
            errs = msg.get('errs', msg.get('error_count', 0))
            enc  = msg.get('enc', msg.get('encrypted', False))
            call_id = msg.get('call_id', '?')
            print(f"[CALL END]   tg={tg:>8}  call_id={call_id}  "
                  f"dur={dur:.1f}s  errs={errs}  enc={enc}")

        elif msg_type == 'codec_frame':
            frame_count += 1
            tg     = msg.get('tg', msg.get('talkgroup', 0))
            codec  = msg.get('codec', msg.get('codec_type', 0))
            errs   = msg.get('errs', 0)
            params = msg.get('params', [])
            codec_name = CODEC_NAMES.get(codec, f"codec_{codec}")
            if verbose:
                print(f"[FRAME] tg={tg}  {codec_name}  errs={errs}"
                      f"  params={params[:4]}...")
            elif frame_count % 50 == 1:
                print(f"[FRAME] #{frame_count}  tg={tg}  {codec_name}  errs={errs}")

        elif msg_type == 'heartbeat':
            if verbose:
                print("[HEARTBEAT]")

        elif msg_type:
            if verbose:
                print(f"[UNKNOWN type={msg_type!r}]", file=sys.stderr)

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description='SymbolStream v2 reference receiver',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('--port',    type=int, default=9090, metavar='PORT',
                   help='TCP port to listen on')
    p.add_argument('--bind',    default='0.0.0.0',     metavar='ADDR',
                   help='Address to bind')
    p.add_argument('--json',    action='store_true',
                   help='JSON framing mode (default: binary)')
    p.add_argument('--verbose', action='store_true',
                   help='Print every codec frame (default: print every 50th)')
    args = p.parse_args()

    mode = 'JSON' if args.json else 'binary'

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)
    print(f"SymbolStream v2 receiver ({mode}) listening on {args.bind}:{args.port}")
    print("Waiting for trunk-recorder to connect...\n")

    try:
        while True:
            conn, addr = srv.accept()
            print(f"Connected: {addr[0]}:{addr[1]}")
            try:
                if args.json:
                    run_json(conn, verbose=args.verbose)
                else:
                    run_binary(conn, verbose=args.verbose)
            except (EOFError, ConnectionError) as e:
                print(f"Disconnected: {e}")
            except KeyboardInterrupt:
                break
            finally:
                conn.close()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        print("\nDone.")


if __name__ == '__main__':
    main()
