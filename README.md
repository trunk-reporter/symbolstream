# symbolstream — Voice Codec Streaming Plugin for trunk-recorder

Streams raw voice codec symbols (IMBE, AMBE, etc.) from trunk-recorder to a remote server via TCP or UDP. Where `simplestream` sends decoded PCM audio (post-vocoder), symbolstream sends the raw codec parameters before vocoding — giving downstream applications direct access to the pre-vocoder data.

Use cases include:
- **Speech recognition** directly from codec parameters (skip lossy audio reconstruction)
- **Hardware vocoder offloading** (ThumbDV, DV3000, AMBEserver)
- **Codec quality analysis** and FEC error monitoring
- **Compact archival** (codec symbols are ~50x smaller than PCM audio)
- **Remote/distributed vocoding** — move the CPU-intensive vocoder off the scanner machine

The plugin follows the same configuration pattern as `simplestream`.

## Protocol Specification

The full wire-format specification is in **[SPEC.md](SPEC.md)**. It covers:

- Binary format (v2) — compact, versioned framing with per-frame timestamps and call IDs
- JSON format (v2) — length-prefixed, fully self-contained JSON (no binary tail)
- All message types: `CODEC_FRAME`, `CALL_START`, `CALL_END`, `HEARTBEAT`
- Codec type registry (IMBE, AMBE+2, AMBE, and reserved slots for Codec2/MELPe)
- Forward-compatibility rules for unknown message and codec types
- v1 legacy format reference (what the current plugin sends)

A ready-to-run Python receiver is in **[symbolstream_recv.py](symbolstream_recv.py)**:

```bash
# Binary mode (default)
python symbolstream_recv.py --port 9090

# JSON mode
python symbolstream_recv.py --port 9090 --json

# Verbose — print every frame
python symbolstream_recv.py --verbose
```

Then point a symbolstream stream at this host/port in your `config.json`.

## Requirements

Requires the `voice_codec_data()` plugin API callback, available in the [trunk-reporter fork of trunk-recorder](https://github.com/trunk-reporter/trunk-recorder).

## Configuration

Add to your `config.json` plugins array:

```json
{
  "name": "symbolstream",
  "library": "libsymbolstream",
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

## Supported Codecs

The plugin streams whatever `voice_codec_data()` provides. The `codec_type` field identifies the codec:

| Value | Codec | Params | Param Size | Frame Rate | Description |
|-------|-------|--------|------------|------------|-------------|
| 0 | P25 Phase 1 IMBE | 8 | 32 bytes | 50 fps | IMBE codewords u[0..7] |
| 1 | P25 Phase 2 AMBE+2 | 4 | 16 bytes | 50 fps | AMBE+2 codewords |
| 2 | DMR AMBE | 4 | 16 bytes | 50 fps | AMBE codewords |
| 3 | D-STAR AMBE | variable | variable | — | AMBE2400 parameters |
| 4 | YSF Full Rate | 8 | 32 bytes | — | Same format as P25 IMBE |
| 5 | YSF Half Rate | variable | variable | — | AMBE2250 parameters |

All codec parameters are transmitted as uint32_t (little-endian), with only the lower bits significant. The values are FEC-decoded and ready for direct use by a vocoder or analysis pipeline.

## Wire Format

### Codec Frame (sendJSON=false)

A compact fixed-size packet per voice frame:

```
Offset  Size  Type        Field
0       4     uint32_t    talkgroup ID (little-endian)
4       4     uint32_t    source radio ID (little-endian, 0 if unknown)
8       N*4   uint32_t[]  codec parameters (N depends on codec_type)
```

For P25 IMBE (codec_type 0): 8 + 32 = **40 bytes per frame**.
For DMR/AMBE (codec_type 2): 8 + 16 = **24 bytes per frame**.

### Codec Frame (sendJSON=true)

Each frame is preceded by a length-prefixed JSON metadata header:

```
Offset  Size  Type        Field
0       4     uint32_t    JSON length in bytes (little-endian)
4       N     char[N]     JSON metadata string
4+N     P*4   uint32_t[]  codec parameters
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

### Call Start Event (sendJSON=true only)

Sent when a new call begins. JSON only, no codec data follows.

```json
{
  "event": "call_start",
  "talkgroup": 9170,
  "freq": 855737500,
  "short_name": "butco"
}
```

### Call End Event (sendJSON=true only)

Sent when a call terminates. JSON only, no codec data follows.

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

## Example: Receiving Frames in Python

### With JSON metadata

```python
import socket, struct, json

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
    meta = json.loads(conn.recv(jlen).decode('utf-8'))

    if meta['event'] == 'codec_frame':
        # Parameter count depends on codec_type
        codec_type = meta.get('codec_type', 0)
        n_params = 8 if codec_type in (0, 4) else 4
        raw = conn.recv(n_params * 4)
        params = struct.unpack('<%dI' % n_params, raw)
        print("TG=%d codec=%d params=%s" % (meta['talkgroup'], codec_type, params))

    elif meta['event'] == 'call_start':
        print("Call start TG=%d" % meta['talkgroup'])

    elif meta['event'] == 'call_end':
        print("Call end TG=%d duration=%.1fs" % (
            meta['talkgroup'], meta['duration']))
```

### Without JSON (P25 IMBE, fixed 40-byte frames)

```python
while True:
    data = conn.recv(40)
    if len(data) < 40:
        break
    tgid, src_id = struct.unpack('<II', data[:8])
    u = struct.unpack('<8I', data[8:40])
    print("TG=%d src=%d u=%s" % (tgid, src_id, u))
```

## Use Cases

### Audio reconstruction via software vocoder

```python
# Decode IMBE codewords to PCM audio via libimbe
import ctypes
lib = ctypes.CDLL('libimbe.so')
dec = lib.imbe_create()
fv = (ctypes.c_int16 * 8)(*[int(x) for x in params])
snd = (ctypes.c_int16 * 160)()
lib.imbe_decode(dec, fv, snd)
# snd now contains 160 samples of 8kHz PCM audio (20ms)
```

### Audio reconstruction via hardware vocoder (ThumbDV / DV3000)

Forward the raw codewords to an AMBEserver instance connected to a DVSI hardware vocoder for reference-quality audio decode.

### ASR (speech recognition) from codec parameters

Decode codewords to 170-dim parameter vectors via `imbe_decode_params()`, normalize, and feed directly into a Conformer-CTC neural network — bypassing audio reconstruction entirely.

### Compact recording

At 2 KB/s per channel (vs ~128 KB/s for PCM audio), codec symbols are ~50x more compact. Record the raw stream and decode later with any vocoder or analysis tool.

## Building

Place the plugin source in `trunk-recorder/plugins/symbolstream/` and add to `CMakeLists.txt`:

```cmake
add_subdirectory(plugins/symbolstream)
```

Build as part of the trunk-recorder build:

```bash
mkdir build && cd build
cmake ..
make symbolstream
sudo make install
```

No external dependencies beyond trunk-recorder's existing libraries (Boost.Asio, Boost.Log).

## Multiple Streams

Send different talkgroups to different servers, or the same data to multiple consumers:

```json
{
  "name": "symbolstream",
  "library": "libsymbolstream",
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
| No JSON (IMBE) | 40 bytes | 20 KB | 2 KB/s |
| No JSON (AMBE) | 24 bytes | 12 KB | 1.2 KB/s |
| With JSON | ~140 bytes | 70 KB | 7 KB/s |

Even with JSON metadata on a busy system with 10 simultaneous calls: ~70 KB/s total.
