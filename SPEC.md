# SymbolStream Protocol v2

Framing specification for streaming raw voice codec parameters (IMBE, AMBE+2, AMBE, etc.)
from trunk-recorder to external consumers via TCP or UDP.

**Status:** Draft specification. The current plugin implements [v1 (legacy)](#version-1-legacy-reference).
A future plugin update will add v2 support.

---

## Contents

1. [Overview](#1-overview)
2. [Transport](#2-transport)
3. [Binary Format](#3-binary-format)
4. [JSON Format](#4-json-format)
5. [Message Types](#5-message-types)
6. [Codec Type Registry](#6-codec-type-registry)
7. [Codec Parameter Tables](#7-codec-parameter-tables)
8. [C Reference Structures and Examples](#8-c-reference-structures-and-examples)
9. [Receiver Guide](#9-receiver-guide)
10. [Bandwidth](#10-bandwidth)
11. [Compatibility](#11-compatibility)
12. [Version 1 Legacy Reference](#12-version-1-legacy-reference)

---

## 1. Overview

The symbolstream plugin taps trunk-recorder's `voice_codec_data()` callback and forwards
pre-vocoder codec parameters in real time. Where `simplestream` sends decoded PCM audio,
symbolstream sends the raw codec codewords — IMBE u[0..7] for P25 Phase 1, AMBE+2 frames
for P25 Phase 2 and DMR, etc.

This document specifies **protocol version 2**. It improves on v1 by:

- Adding an 8-byte versioned header to every binary frame (enables resync, version detection)
- Carrying all metadata (timestamps, call IDs, codec type, FEC errors) in-band
- Separating binary and JSON modes cleanly — no more hybrid JSON+binary frames
- Supporting all current codec types and providing a registry for future codecs
- Including call lifecycle events (`call_start`, `call_end`) in both modes

---

## 2. Transport

**TCP** (recommended): The plugin connects **to** the receiver. One long-lived connection per
configured stream. Messages arrive in order; calls may be interleaved on one connection.

**UDP**: One datagram per message. Simpler, fire-and-forget. No reliability guarantee.

Default port: **9090** (configurable per stream).

The framing format is identical for TCP and UDP. On UDP the header magic bytes serve as
a per-datagram sanity check.

---

## 3. Binary Format

### 3.1 Frame Header (8 bytes, every message)

```
 Offset  Size  Type    Field
 0       1     uint8   magic[0] = 0x53  ('S')
 1       1     uint8   magic[1] = 0x59  ('Y')
 2       1     uint8   version  = 0x02
 3       1     uint8   msg_type
 4       4     uint32  payload_len  (little-endian; bytes following the header)
```

The 2-byte magic `SY` (0x5359) is a resync anchor. On a corrupted stream, scan for the
next `0x53 0x59 0x02` triplet.

Receivers **must** skip messages with unknown `msg_type` by reading and discarding
`payload_len` bytes — do not abort the connection.

### 3.2 CODEC_FRAME (msg_type = 0x01)

One decoded voice frame (20 ms at 50 fps for IMBE and AMBE variants).

```
 Offset  Size  Type      Field
 0       4     uint32    talkgroup_id
 4       4     uint32    src_id         (source radio ID; 0 if unknown)
 8       4     uint32    call_id        (links to CALL_START.call_id; 0 if not tracked)
 12      8     uint64    timestamp_us   (µs since Unix epoch; 0 if unknown)
 20      1     uint8     codec_type     (see §6)
 21      1     uint8     param_count    (number of uint32 codec params that follow)
 22      1     uint8     errs           (FEC error count for this frame)
 23      1     uint8     flags          (bit 0: silence/null frame; bits 1–7: reserved, set 0)
 24      N×4   uint32[]  codec_params   (param_count values, each little-endian)
```

**Total size for IMBE (param_count=8):**  8 + 24 + 32 = **64 bytes**
**Total size for AMBE+2 (param_count=4):** 8 + 24 + 16 = **48 bytes**

All codec_param values are FEC-decoded uint32 (little-endian, lower bits significant),
exactly as provided by trunk-recorder's `voice_codec_data()` callback.

### 3.3 CALL_START (msg_type = 0x02)

Sent when a new call begins on a monitored talkgroup.

```
 Offset  Size  Type      Field
 0       4     uint32    talkgroup_id
 4       8     uint64    frequency_hz   (RF frequency in Hz)
 12      8     uint64    timestamp_us   (µs since Unix epoch)
 20      4     uint32    call_id        (session-unique; monotonically increasing, wraps at 2³²)
 24      1     uint8     system_name_len  (0 = no system name)
 25      N     uint8[]   system_name    (UTF-8, not null-terminated; N = system_name_len)
```

`call_id` is assigned by the plugin. Receivers should create a call context keyed on
`call_id` when this message arrives, and release it on the matching CALL_END.

### 3.4 CALL_END (msg_type = 0x03)

Sent when a call terminates.

```
 Offset  Size  Type      Field
 0       4     uint32    talkgroup_id
 4       4     uint32    call_id
 8       4     uint32    src_id         (final/dominant source radio ID)
 12      8     uint64    frequency_hz
 20      4     uint32    duration_ms
 24      4     uint32    error_count    (total FEC errors across the call)
 28      1     uint8     encrypted      (0 = no, 1 = yes)
 29      1     uint8     system_name_len
 30      N     uint8[]   system_name    (UTF-8)
```

### 3.5 HEARTBEAT (msg_type = 0x04)

`payload_len = 0`, no payload. Sent periodically (default: 30 s) by the plugin to detect
dead connections. Receivers should reset a watchdog timer on receipt.

---

## 4. JSON Format

JSON mode uses **length-prefixed framing**: each message is a 4-byte LE uint32 length
followed by a UTF-8 JSON object.

```
 Offset  Size  Type    Field
 0       4     uint32  json_len  (little-endian; bytes of the JSON object)
 4       N     char[]  JSON object (UTF-8, no null terminator)
```

**Unlike v1**, codec parameters are carried inline as a JSON integer array (`"params"`).
There is no binary data after the JSON object. This makes JSON mode fully self-contained
and parseable by any standard JSON library.

### 4.1 codec_frame

```json
{
  "v": 2,
  "type": "codec_frame",
  "tg": 9170,
  "src": 1234567,
  "call_id": 42,
  "ts": 1711234567890123,
  "codec": 0,
  "errs": 0,
  "flags": 0,
  "params": [40960, 4096, 12288, 8192, 16384, 2048, 1024, 512]
}
```

| Field   | Type   | Description                                         |
|---------|--------|-----------------------------------------------------|
| v       | int    | Protocol version (2)                                |
| type    | string | `"codec_frame"`                                     |
| tg      | int    | Talkgroup ID                                        |
| src     | int    | Source radio ID (0 if unknown)                      |
| call_id | int    | Call identifier (0 if not tracking)                 |
| ts      | int    | Timestamp in µs since Unix epoch (0 if unknown)     |
| codec   | int    | Codec type (see §6)                                 |
| errs    | int    | FEC error count for this frame                      |
| flags   | int    | Bitmask: bit 0 = silence frame                      |
| params  | int[]  | Codec parameters (FEC-decoded uint32 values)        |

### 4.2 call_start

```json
{
  "v": 2,
  "type": "call_start",
  "tg": 9170,
  "call_id": 42,
  "freq": 855737500,
  "ts": 1711234567890123,
  "sys": "butco"
}
```

| Field   | Type   | Description                        |
|---------|--------|------------------------------------|
| tg      | int    | Talkgroup ID                       |
| call_id | int    | Session-unique call identifier     |
| freq    | int    | RF frequency in Hz                 |
| ts      | int    | Start timestamp (µs since epoch)   |
| sys     | string | System short name (may be absent)  |

### 4.3 call_end

```json
{
  "v": 2,
  "type": "call_end",
  "tg": 9170,
  "call_id": 42,
  "src": 1234567,
  "freq": 855737500,
  "dur_ms": 4500,
  "errs": 3,
  "enc": false,
  "sys": "butco"
}
```

| Field   | Type   | Description                               |
|---------|--------|-------------------------------------------|
| tg      | int    | Talkgroup ID                              |
| call_id | int    | Matches the call_start call_id            |
| src     | int    | Final/dominant source radio ID            |
| freq    | int    | RF frequency in Hz                        |
| dur_ms  | int    | Call duration in milliseconds             |
| errs    | int    | Total FEC errors across the call          |
| enc     | bool   | True if the call was encrypted            |
| sys     | string | System short name (may be absent)         |

### 4.4 heartbeat

```json
{"v": 2, "type": "heartbeat"}
```

---

## 5. Message Types

| Binary value | JSON `type`    | Description                     |
|--------------|----------------|---------------------------------|
| 0x01         | `codec_frame`  | One voice codec frame           |
| 0x02         | `call_start`   | New call beginning              |
| 0x03         | `call_end`     | Call terminated                 |
| 0x04         | `heartbeat`    | Keep-alive                      |
| 0x05–0xFF    | —              | Reserved; skip via payload_len  |

---

## 6. Codec Type Registry

All codec_param values are the raw, FEC-decoded uint32 words from trunk-recorder's
`voice_codec_data()` callback, in the order provided. Bit semantics are defined by
each codec's standard; symbolstream does not transform them.

| Value | Codec    | Protocol     | param_count | Notes                            |
|-------|----------|--------------|-------------|----------------------------------|
| 0     | IMBE     | P25 Phase 1  | 8           | u[0..7], widths: 12×4 + 11×3 + 7 bits (see §7.1) |
| 1     | AMBE+2   | P25 Phase 2  | 4           |                                  |
| 2     | AMBE     | DMR          | 4           |                                  |
| 3     | AMBE     | D-STAR       | variable    | AMBE2400                         |
| 4     | AMBE     | YSF Full     | 8           | Same layout as IMBE              |
| 5     | AMBE     | YSF Half     | variable    | AMBE2250                         |
| 6     | AMBE+2   | NXDN         | 4           |                                  |
| 7–127 | —        | —            | —           | Reserved for future Tier II      |
| 128   | Codec2   | —            | variable    | Future                           |
| 129   | MELPe    | —            | variable    | Future                           |
| 130+  | —        | —            | —           | Reserved                         |

When receiving a codec type not in this table, use `param_count` to consume the correct
number of uint32 params and continue. Do not abort.

---

---

## 7. Codec Parameter Tables

### 7.1 IMBE — P25 Phase 1

IMBE (Improved Multi-Band Excitation) encodes a 20 ms voice frame as 88 information bits,
divided into 8 FEC-protected groups that produce 8 codewords **u[0..7]** after decoding.
These are the values in `codec_params[]` when `codec_type = 0`.

Each u[i] is carried as a `uint32_t` (4 bytes, little-endian). Only the lower N bits are
significant; upper bits are zero-padded by trunk-recorder.

**Codeword bit widths and content:**

| Index | Bits | Content |
|-------|------|---------|
| u[0] | 12 | Fundamental frequency index (bits 11–7, 5 bits → ~69–220 Hz pitch period) + per-band voicing bitmap (bits 6–0, 7 bits — one bit per spectral band 1–7; 1=voiced) |
| u[1] | 12 | Spectral amplitude parameter b[1] |
| u[2] | 12 | Spectral amplitude parameter b[2] |
| u[3] | 12 | Spectral amplitude parameter b[3] |
| u[4] | 11 | Spectral amplitude parameter b[4] |
| u[5] | 11 | Spectral amplitude parameter b[5] |
| u[6] | 11 | Spectral amplitude parameter b[6] |
| u[7] |  7 | Spectral amplitude parameter b[7] |

**Totals:** 88 information bits per frame = 4 × 12 + 3 × 11 + 7.

**Interpretation of u[0]:**

```
u[0] bits:  11  10   9   8   7  |  6   5   4   3   2   1   0
            ┌───────────────────┼───────────────────────────┐
            │  pitch index (5b) │  voicing bitmap (7b)      │
            └───────────────────┴───────────────────────────┘
```

- Pitch index 0–31 maps to fundamental frequency F₀ via the IMBE pitch table
  (approximately 69.2 Hz at index 0 to 220.2 Hz at index 31).
- Each voicing bit corresponds to one spectral band; voiced bands contribute harmonic
  energy, unvoiced bands contribute noise.

**Using IMBE params with a software vocoder:**

```python
import ctypes
lib = ctypes.CDLL('libimbe.so')
decoder = lib.imbe_create()

# params = list of 8 uint32 values from CODEC_FRAME
frame_vec  = (ctypes.c_int16 * 8)(*[int(p) for p in params])
pcm_output = (ctypes.c_int16 * 160)()
lib.imbe_decode(decoder, frame_vec, pcm_output)
# pcm_output: 160 samples × int16 at 8 kHz (20 ms of audio)
```

**FEC error guidance for IMBE:**

| errs | Signal | Audio quality |
|------|--------|---------------|
| 0 | Clean | Reference quality |
| 1–2 | Marginal | Slightly degraded, intelligible |
| 3–4 | Poor | Noisy; may become unintelligible |
| ≥ 5 | Severe | Substitute silence or discard |

### 7.2 AMBE+2 — P25 Phase 2 and DMR

AMBE+2 (Advanced Multi-Band Excitation Plus Two) is used in P25 Phase 2 TDMA and DMR.
`codec_type = 1` for P25 Phase 2; `codec_type = 2` for DMR. Both use the same 4 × uint32_t
layout in `codec_params[]`.

**Frame layout:**

| Index | Bits used | Content |
|-------|-----------|---------|
| params[0] | 24 | AMBE+2 channel data bytes 0–2 |
| params[1] | 24 | AMBE+2 channel data bytes 3–5 |
| params[2] | 24 | AMBE+2 channel data bytes 6–8 |
| params[3] | 8 | AMBE+2 channel data byte 9 (last); upper 24 bits zero |

The AMBE+2 full-rate vocoder produces **72 bits** (9 bytes) of channel data per 20 ms frame.
Trunk-recorder distributes these 9 bytes across 4 × uint32_t words, 3 bytes per word
(lower 24 bits), with the last word carrying the remaining byte in the lowest 8 bits.

**Reconstructing the 9-byte AMBE+2 channel frame:**

```python
def ambe2_params_to_bytes(params: list[int]) -> bytes:
    """Reconstruct 9-byte AMBE+2 channel frame from 4 codec params."""
    out = bytearray(9)
    for i in range(3):
        out[i*3 + 0] =  params[i]        & 0xFF
        out[i*3 + 1] = (params[i] >>  8) & 0xFF
        out[i*3 + 2] = (params[i] >> 16) & 0xFF
    out[8] = params[3] & 0xFF
    return bytes(out)
```

**Forwarding to a DVSI hardware vocoder (DV3000 / DVstick30):**

```python
import struct

def build_ambeserver_packet(params: list[int]) -> bytes:
    """Wrap AMBE+2 channel frame in AMBEserver CH_RX packet format."""
    channel_bytes = ambe2_params_to_bytes(params)
    # AMBEserver packet: start_byte(1) + type(1) + length(2) + data
    START = 0x61
    TYPE_CH_RX = 0x01
    pkt_len = len(channel_bytes) + 4
    return struct.pack('>BBH', START, TYPE_CH_RX, pkt_len) + channel_bytes
```

**Note on codec_type 1 vs 2:** P25 Phase 2 (type 1) and DMR (type 2) both use AMBE+2 with
the same bit rate and frame structure. The codec type field distinguishes the **system
origin** so consumers can apply system-appropriate FEC threshold policies or routing rules.
The channel bytes themselves are processed identically.

**FEC error guidance for AMBE+2:**

| errs | Meaning |
|------|---------|
| 0 | Clean frame |
| 1–3 | Recoverable errors corrected by AMBE+2 FEC |
| ≥ 4 | Significant degradation; consider substituting silence |

---

## 8. C Reference Structures and Examples

### 8.1 Header Definitions

```c
#include <stdint.h>
#include <string.h>
#include <time.h>

/* Protocol constants */
#define SSSP_MAGIC_0          0x53   /* 'S' */
#define SSSP_MAGIC_1          0x59   /* 'Y' */
#define SSSP_VERSION          0x02
#define SSSP_HEADER_SIZE      8

#define SSSP_MSG_CODEC_FRAME  0x01
#define SSSP_MSG_CALL_START   0x02
#define SSSP_MSG_CALL_END     0x03
#define SSSP_MSG_HEARTBEAT    0x04

#define SSSP_CODEC_IMBE       0x00
#define SSSP_CODEC_AMBE2_P25  0x01
#define SSSP_CODEC_AMBE_DMR   0x02
#define SSSP_CODEC_AMBE_DSTAR 0x03
#define SSSP_CODEC_AMBE_YSF_F 0x04
#define SSSP_CODEC_AMBE_YSF_H 0x05
#define SSSP_CODEC_AMBE2_NXDN 0x06

/* 8-byte frame header — packed, always sent before payload */
typedef struct __attribute__((packed)) {
    uint8_t  magic[2];     /* { SSSP_MAGIC_0, SSSP_MAGIC_1 } */
    uint8_t  version;      /* SSSP_VERSION                   */
    uint8_t  msg_type;     /* SSSP_MSG_*                     */
    uint32_t payload_len;  /* little-endian; bytes after header */
} sssp_header_t;

/* CODEC_FRAME payload fixed header (24 bytes).
 * Followed immediately by param_count × uint32_t codec parameters. */
typedef struct __attribute__((packed)) {
    uint32_t talkgroup;    /* talkgroup ID                   */
    uint32_t src_id;       /* source radio ID (0 = unknown)  */
    uint32_t call_id;      /* session call ID (0 = not tracking) */
    uint64_t timestamp_us; /* µs since Unix epoch (0 = unknown) */
    uint8_t  codec_type;   /* SSSP_CODEC_*                   */
    uint8_t  param_count;  /* N: number of uint32 params     */
    uint8_t  errs;         /* FEC error count this frame     */
    uint8_t  flags;        /* bit 0 = silence/null frame     */
} sssp_codec_hdr_t;

/* Convenience: complete IMBE frame (header + params in one struct) */
typedef struct __attribute__((packed)) {
    sssp_header_t    frame_hdr;
    sssp_codec_hdr_t codec_hdr;
    uint32_t         params[8];   /* u[0..7] */
} sssp_imbe_frame_t;

/* Convenience: complete AMBE+2 frame */
typedef struct __attribute__((packed)) {
    sssp_header_t    frame_hdr;
    sssp_codec_hdr_t codec_hdr;
    uint32_t         params[4];
} sssp_ambe2_frame_t;
```

### 8.2 Utility: Build a Frame Header

```c
static void sssp_fill_header(sssp_header_t *h, uint8_t msg_type,
                             uint32_t payload_len) {
    h->magic[0]   = SSSP_MAGIC_0;
    h->magic[1]   = SSSP_MAGIC_1;
    h->version    = SSSP_VERSION;
    h->msg_type   = msg_type;
    h->payload_len = payload_len;
}

static int sssp_check_header(const sssp_header_t *h) {
    return (h->magic[0] == SSSP_MAGIC_0 &&
            h->magic[1] == SSSP_MAGIC_1 &&
            h->version  == SSSP_VERSION) ? 0 : -1;
}

static uint64_t sssp_now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000ULL + ts.tv_nsec / 1000;
}
```

### 8.3 Sender: Transmit an IMBE Frame

```c
#include <sys/socket.h>

int sssp_send_imbe(int sockfd, uint32_t tgid, uint32_t src, uint32_t call_id,
                   const uint32_t *u, int errs) {
    sssp_imbe_frame_t pkt;
    uint32_t plen = sizeof(sssp_codec_hdr_t) + 8 * sizeof(uint32_t);

    sssp_fill_header(&pkt.frame_hdr, SSSP_MSG_CODEC_FRAME, plen);

    pkt.codec_hdr.talkgroup   = tgid;
    pkt.codec_hdr.src_id      = src;
    pkt.codec_hdr.call_id     = call_id;
    pkt.codec_hdr.timestamp_us = sssp_now_us();
    pkt.codec_hdr.codec_type  = SSSP_CODEC_IMBE;
    pkt.codec_hdr.param_count = 8;
    pkt.codec_hdr.errs        = (uint8_t)errs;
    pkt.codec_hdr.flags       = 0;
    memcpy(pkt.params, u, 8 * sizeof(uint32_t));

    return send(sockfd, &pkt, sizeof(pkt), 0);
}
```

### 8.4 Receiver: Binary Frame Loop

```c
#include <sys/socket.h>
#include <stdio.h>

/* Read exactly n bytes into buf; returns 0 on success, -1 on disconnect. */
static int recv_exact(int fd, void *buf, size_t n) {
    size_t got = 0;
    while (got < n) {
        ssize_t r = recv(fd, (char *)buf + got, n - got, 0);
        if (r <= 0) return -1;
        got += r;
    }
    return 0;
}

/* Scan forward until the 2-byte magic sequence is found. */
static int sssp_resync(int fd) {
    uint8_t b;
    while (recv_exact(fd, &b, 1) == 0) {
        if (b != SSSP_MAGIC_0) continue;
        if (recv_exact(fd, &b, 1) != 0) return -1;
        if (b == SSSP_MAGIC_1) return 0;  /* found */
    }
    return -1;
}

void sssp_receive_loop(int fd) {
    sssp_header_t hdr;
    uint8_t payload[65536];

    while (1) {
        if (recv_exact(fd, &hdr, SSSP_HEADER_SIZE) != 0) break;

        if (sssp_check_header(&hdr) != 0) {
            fprintf(stderr, "bad magic — resyncing\n");
            if (sssp_resync(fd) != 0) break;
            continue;
        }

        if (hdr.payload_len > sizeof(payload)) {
            fprintf(stderr, "payload too large (%u) — dropping\n", hdr.payload_len);
            break;
        }

        if (recv_exact(fd, payload, hdr.payload_len) != 0) break;

        switch (hdr.msg_type) {
        case SSSP_MSG_CODEC_FRAME: {
            if (hdr.payload_len < sizeof(sssp_codec_hdr_t)) break;
            sssp_codec_hdr_t *c = (sssp_codec_hdr_t *)payload;
            uint32_t *params = (uint32_t *)(payload + sizeof(*c));
            printf("CODEC tg=%u src=%u codec=%u errs=%u params[0]=%u\n",
                   c->talkgroup, c->src_id, c->codec_type,
                   c->errs, c->param_count > 0 ? params[0] : 0);
            break;
        }
        case SSSP_MSG_CALL_START:
            printf("CALL START (payload %u bytes)\n", hdr.payload_len);
            break;
        case SSSP_MSG_CALL_END:
            printf("CALL END (payload %u bytes)\n", hdr.payload_len);
            break;
        case SSSP_MSG_HEARTBEAT:
            /* no-op */
            break;
        default:
            /* unknown type — payload already consumed, safe to continue */
            fprintf(stderr, "unknown msg_type=0x%02x, skipping %u bytes\n",
                    hdr.msg_type, hdr.payload_len);
            break;
        }
    }
}
```

---

## 9. Receiver Guide

### 9.1 Binary Mode State Machine

```
1. Create TCP server socket, bind to port, listen.
2. Accept connection from trunk-recorder plugin.
3. Loop:
   a. Read 8 bytes → header.
   b. Check magic[0]==0x53, magic[1]==0x59, version==0x02.
      - Wrong magic: attempt resync by scanning for 0x53 0x59 0x02.
      - Wrong version: warn, but continue (format may still be parseable).
   c. Read payload_len bytes → payload.
   d. Dispatch on msg_type:
      0x01 CODEC_FRAME → decode and process (see §3.2)
      0x02 CALL_START  → create call context keyed by call_id (see §3.3)
      0x03 CALL_END    → finalize and release call context (see §3.4)
      0x04 HEARTBEAT   → reset watchdog timer
      unknown          → discard payload, continue
   e. On EOF or socket error: log and reconnect (or exit).
```

### 9.2 JSON Mode State Machine

```
1. Create TCP server socket, bind, listen.
2. Accept connection.
3. Loop:
   a. Read 4 bytes, decode uint32 LE → json_len.
   b. Read json_len bytes, decode as UTF-8, parse JSON.
   c. Dispatch on msg["type"]:
      "codec_frame" → use msg["codec"], msg["params"], msg["tg"], etc.
      "call_start"  → create call context
      "call_end"    → finalize call context
      "heartbeat"   → reset watchdog
      unknown       → log and ignore
```

### 9.3 Error Handling

| Situation            | Recommended action                                       |
|----------------------|----------------------------------------------------------|
| Unknown msg_type     | Skip payload_len bytes, continue                        |
| Unknown codec value  | Use param_count to skip, continue                       |
| FEC errs > 0         | Audio quality reduced; errs ≥ 4 (IMBE) → unintelligible |
| Silence frame (flag) | Null/inband frame; skip vocoder call                    |
| Truncated payload    | Connection lost; close and reconnect                    |
| UDP packet loss      | Normal; gaps in call_id are not errors                  |

### 9.4 Minimal Receiver (Python sketch)

See `symbolstream_recv.py` in this repository for a ~150-line reference implementation
handling both binary and JSON modes, including call lifecycle tracking.

---

## 10. Bandwidth

At 50 fps (20 ms frames):

| Mode         | Frame size   | Rate       | 10 calls     |
|--------------|--------------|------------|--------------|
| Binary IMBE  | 64 bytes     | 3.2 KB/s   | 32 KB/s      |
| Binary AMBE  | 48 bytes     | 2.4 KB/s   | 24 KB/s      |
| JSON IMBE    | ~180 bytes   | 9 KB/s     | 90 KB/s      |
| JSON AMBE    | ~160 bytes   | 8 KB/s     | 80 KB/s      |

Binary mode costs 24 bytes of overhead per frame vs v1 (timestamp, call_id, header).
That's an extra 1.2 KB/s for IMBE — worthwhile for the metadata gained.

JSON mode is intended for development, debugging, and low-volume monitoring only.

---

## 11. Compatibility

### 11.1 v2 vs v1

v2 is a **breaking wire-format change** from v1.

- **Binary**: v1 has no frame header (raw tgid + src_id + codec bytes). v2 adds the 8-byte
  header. Receivers can detect v1 by inspecting the first bytes: v1 binary frames start
  with a talkgroup ID (any value), not with `0x53 0x59`.
- **JSON**: v1 sends `4-byte-length + JSON-metadata + binary-codec-tail`. v2 sends
  `4-byte-length + JSON-only` with params inline. Receivers can detect v2 JSON by checking
  for `"v":2` in the parsed object.

### 11.2 Forward Compatibility

- Receivers must skip unknown `msg_type` values using `payload_len`.
- Receivers must skip unknown `codec_type` values using `param_count`.
- Receivers must ignore unknown JSON fields.
- The `version` byte in the binary header allows a future v3 to change the header layout;
  receivers should warn but attempt to continue on unexpected version values.

---

## 12. Version 1 (Legacy) Reference

The current symbolstream C++ plugin sends one of two formats, controlled by `sendJSON` in config.

### 12.1 sendJSON = false (binary only)

Fixed 40-byte packet per IMBE frame. No codec_type, no version, no call events.

```
 Offset  Size  Type      Field
 0       4     uint32    talkgroup_id (little-endian)
 4       4     uint32    src_id (little-endian)
 8       32    uint32[8] IMBE codewords u[0..7]
```

### 12.2 sendJSON = true (hybrid JSON + binary)

Per-frame message:

```
 Offset  Size  Type    Field
 0       4     uint32  JSON length (little-endian)
 4       N     char[]  JSON metadata (see below)
 4+N     32    uint32[8]  IMBE codewords (binary, appended after JSON)
```

JSON metadata:

```json
{
  "event": "codec_frame",
  "talkgroup": 9170,
  "src": 1234567,
  "codec_type": 0,
  "errs": 0,
  "short_name": "butco"
}
```

Call events (JSON only, no binary tail):

```json
{"event": "call_start", "talkgroup": 9170, "freq": 855737500, "short_name": "butco"}
{"event": "call_end",   "talkgroup": 9170, "src": 1234567, "freq": 855737500,
 "duration": 4.5, "short_name": "butco", "error_count": 3, "encrypted": false}
```

**v1 limitations addressed by v2:**
- No frame header — cannot resync after corruption, cannot version-detect
- No timestamp — receivers cannot reconstruct absolute call timing
- No call_id — cannot correlate frames to calls when multiple talkgroups are multiplexed
- Hybrid JSON+binary — awkward to parse, not composable with standard JSON tools
- Codec type missing from binary mode — receiver must infer from context
- Only IMBE is sent (codec_type != 0 is filtered out in the plugin)
