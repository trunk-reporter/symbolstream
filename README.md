# Symbol Stream Plugin for trunk-recorder

Streams raw IMBE voice codec data from trunk-recorder to a remote server via TCP or UDP. This gives downstream applications direct access to the pre-vocoder codec parameters — the same data the vocoder uses to synthesize audio, but before any lossy reconstruction.

The plugin follows the same configuration pattern as `simplestream`. The receiving server handles all processing (decoding, ASR inference, audio reconstruction, etc).

## Requirements

Requires the `voice_codec_data()` plugin API callback, available in trunk-recorder builds with the voice codec plugin API patch.

## Configuration

Add to your `config.json` plugins array:

```json
{
  "name": "symbolstream",
  "library": "libsymbolstream_plugin",
  "streams": [
    {
      "address": "127.0.0.1",
      "port": 9090,
      "TGID": 0,
      "shortName": "",
      "useTCP": true,
      "sendJSON": true
    }
  ]
}
```

### Stream Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `address` | string | `"127.0.0.1"` | Destination IP address |
| `port` | int | `9090` | Destination port |
| `TGID` | int | `0` | Talkgroup filter. `0` = stream all talkgroups |
| `shortName` | string | `""` | System short name filter. Empty = all systems |
| `useTCP` | bool | `true` | Use TCP (`true`) or UDP (`false`) |
| `sendJSON` | bool | `false` | Include JSON metadata with each frame |

Multiple streams can be configured to send to different servers or filter different talkgroups.

## Wire Format

### Codec Frame (sendJSON=false)

Each IMBE voice frame (20ms, 50 frames/sec) is sent as a 40-byte packet:

```
Offset  Size  Type        Field
0       4     uint32_t    talkgroup ID (little-endian)
4       4     uint32_t    source radio ID (little-endian, 0 if unknown)
8       32    uint32_t[8] IMBE codewords u[0]..u[7] (little-endian)
```

Total: **40 bytes per frame**, **2000 bytes/sec** per active call.

### Codec Frame (sendJSON=true)

Each frame is preceded by a JSON metadata header:

```
Offset  Size  Type        Field
0       4     uint32_t    JSON length in bytes (little-endian)
4       N     char[N]     JSON metadata string
4+N     32    uint32_t[8] IMBE codewords u[0]..u[7] (little-endian)
```

JSON metadata fields:

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

### Call Start Event (sendJSON=true only)

Sent when a new call begins. No codec data follows — JSON only.

```
Offset  Size  Type        Field
0       4     uint32_t    JSON length in bytes (little-endian)
4       N     char[N]     JSON string
```

```json
{
  "event": "call_start",
  "talkgroup": 9170,
  "freq": 855737500,
  "short_name": "butco"
}
```

### Call End Event (sendJSON=true only)

Sent when a call terminates. No codec data follows — JSON only.

```json
{
  "event": "call_end",
  "talkgroup": 9170,
  "src": 1234567,
  "freq": 855737500,
  "duration": 4.5,
  "short_name": "butco",
  "error_count": 3,
  "encrypted": false
}
```

## Codec Types

The `codec_type` field in JSON metadata identifies the voice codec:

| Value | Codec | Parameters | Description |
|-------|-------|------------|-------------|
| 0 | P25 Phase 1 IMBE | 8 × uint32_t | IMBE codewords u[0]..u[7] |
| 1 | P25 Phase 2 AMBE+2 | 4 × uint32_t | AMBE+2 codewords |
| 2 | DMR AMBE | 4 × uint32_t | AMBE codewords |
| 3 | D-STAR AMBE | variable | AMBE2400 parameters |
| 4 | YSF Full Rate | 8 × uint32_t | Same format as P25 IMBE |
| 5 | YSF Half Rate | variable | AMBE2250 parameters |

Currently only codec_type 0 (P25 Phase 1 IMBE) is implemented.

## IMBE Codeword Format

The 8 IMBE codewords encode one 20ms voice frame:

| Codeword | Bits | Description |
|----------|------|-------------|
| u[0] | 12 | Fundamental frequency (pitch) |
| u[1] | 12 | Voicing decisions |
| u[2] | 12 | Spectral amplitudes (block 1) |
| u[3] | 12 | Spectral amplitudes (block 2) |
| u[4] | 11 | Spectral amplitudes (block 3) |
| u[5] | 11 | Spectral amplitudes (block 4) |
| u[6] | 11 | Gain |
| u[7] | 8 | Reserved |

These are the FEC-decoded codewords, ready for vocoder synthesis or direct ASR processing. Values are stored as uint32_t but only the lower bits are significant.

## Example: Receiving Frames in Python

```python
import socket
import struct

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(('0.0.0.0', 9090))
sock.listen(1)
conn, addr = sock.accept()

while True:
    # Read 4-byte JSON length
    hdr = conn.recv(4)
    if len(hdr) < 4:
        break
    jlen = struct.unpack('<I', hdr)[0]

    # Read JSON metadata
    jdata = conn.recv(jlen).decode('utf-8')
    meta = json.loads(jdata)

    if meta['event'] == 'codec_frame':
        # Read 32 bytes of IMBE codewords
        raw = conn.recv(32)
        u = struct.unpack('<8I', raw)
        print("TG=%d src=%d u=%s" % (meta['talkgroup'], meta['src'], u))

    elif meta['event'] == 'call_start':
        print("Call start TG=%d" % meta['talkgroup'])

    elif meta['event'] == 'call_end':
        print("Call end TG=%d duration=%.1fs" % (
            meta['talkgroup'], meta['duration']))
```

## Example: Receiving Frames Without JSON

```python
while True:
    data = conn.recv(40)
    if len(data) < 40:
        break
    tgid, src_id = struct.unpack('<II', data[:8])
    u = struct.unpack('<8I', data[8:40])
    print("TG=%d src=%d u=%s" % (tgid, src_id, u))
```

## Processing IMBE Codewords

The codewords can be used for:

1. **Audio reconstruction** — Pass through an IMBE vocoder (libimbe) to synthesize 8kHz PCM audio
2. **ASR inference** — Decode to 170-dim raw parameters via `imbe_decode_params()`, normalize, and run through a Conformer-CTC model
3. **Codec analysis** — Monitor FEC error rates, signal quality, encryption detection
4. **Archival** — Store raw codec data for later processing (much smaller than audio)

For ASR, the 170-dim parameter vector layout per frame is:

```
[0]       f0 (fundamental frequency)
[1]       L (number of harmonics, 0-56)
[2:58]    spectral amplitudes (56 slots, zero-padded)
[58:114]  voiced/unvoiced flags (56 slots)
[114:170] binary harmonic validity mask (1=real, 0=pad)
```

## Building

The plugin is built as part of the trunk-recorder build:

```bash
mkdir build && cd build
cmake ..
make symbolstream_plugin
sudo make install
```

The plugin has no external dependencies beyond trunk-recorder's existing libraries (Boost.Asio, Boost.Log).

## Multiple Streams

Configure multiple streams to send different talkgroups to different servers:

```json
{
  "name": "symbolstream",
  "library": "libsymbolstream_plugin",
  "streams": [
    {
      "address": "10.0.0.1",
      "port": 9090,
      "TGID": 9170,
      "useTCP": true,
      "sendJSON": true
    },
    {
      "address": "10.0.0.2",
      "port": 9091,
      "TGID": 0,
      "useTCP": false,
      "sendJSON": false
    }
  ]
}
```

## Bandwidth

| Mode | Per Frame | Per Call (10s) | Per Active Channel |
|------|-----------|----------------|--------------------|
| No JSON | 40 bytes | 20 KB | 2 KB/s |
| With JSON | ~140 bytes | 70 KB | 7 KB/s |

At 50fps, even with JSON metadata, a single active channel uses only 7 KB/s — negligible for any network.
