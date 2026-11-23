# Initial AVC branch rejection

Ordinary strict recovery screens the first complete video sample of each AVC
track before enumerating deep-assembly combinations. A sample containing only
partition B/C and known ancillary NAL types cannot supply an initial picture
header. Every branch completion containing those exact group bytes would fail
the existing strict FFmpeg check, so the search removes that content choice
before copying a candidate or starting a decoder.

The optional screen recognizes only NAL types 3, 4, 6, 7, 8, 9, 10, 11 and 12
as a provably insufficient initial packet. Types 1, 2 and 5, extension/reserved
types, nonstandard packet-wrapper signatures and samples larger than 16 MiB
continue through the existing path. The entire bounded sample is examined:
SEI or partition C followed by a primary slice remains eligible.

This is a necessary rejection check, never an acceptance test. Surviving
candidates still undergo sample/range checks, reconstruction, strict FFmpeg
decode and exact packet/frame counting. Structural-only recovery, later
samples, HEVC, association certificates and durable association checkpoints
retain their existing semantics.

Each rejection uses an available, source-identity-checked physical witness for
one exact group fingerprint. Unavailable witnesses are retried through other
physical alternatives; inability to read a sample does not create a content
negative. Domain pruning affects the current movie/group overlay, preserving
shared aliases and saved association rows. Fingerprints stream in pages of
256; neither the complete domain nor its Cartesian suffix is materialized.
Proved rejections do not consume the assembly budget.

Job details/progress include `initial_avc_fingerprints_checked`,
`initial_avc_fingerprints_rejected` and `initial_avc_locations_rejected`.
These count screened content alternatives and their physical locations;
they do not count restored recordings.

## Basis and alternatives

FFmpeg's MOV demuxer marks AVC packets for header parsing, and the demux layer
marks complete-frame packets. The H.264 parser reports a missing picture for a
complete packet without a primary slice. The decoder likewise cannot create
an initial picture from partition B/C or ancillary NALs alone. Later packets
cannot repair that initial strict error. See the upstream
[MOV demuxer](https://github.com/FFmpeg/FFmpeg/blob/n8.0/libavformat/mov.c),
[demux parser setup](https://github.com/FFmpeg/FFmpeg/blob/n8.0/libavformat/demux.c),
[AVC parser](https://github.com/FFmpeg/FFmpeg/blob/n8.0/libavcodec/h264_parser.c)
and [AVC decoder](https://github.com/FFmpeg/FFmpeg/blob/n8.0/libavcodec/h264dec.c).
Partition A remains eligible conservatively, including with newer parsers.

Caching arbitrary decoder-prefix failures would require proof about reference
pictures, delayed frames, codec initialization and the precise error context.
Sharing a decoder across candidates also requires reliable state isolation.
An in-process libavcodec backend would introduce ABI/dependency and validation
parity work. None is needed for this narrow, independently provable rejection.

## Verification

Validated on 2026-11-11 with Python 3.14.10 and FFmpeg 9.0.2:

- All 11 new regressions and the combined 30-test focused suite passed. They
  cover real decoder rejection, a valid H.264/AAC control with B frames,
  surviving physical/content alternatives, source changes, unavailable
  witnesses, saved/aliased domains, bounded paging and conservative fallbacks.
- The complete suite reported 651 tests, with two optional real-fiwalk tests
  skipped because fiwalk was unavailable. Resource warnings were treated as
  errors. The full log is
  `test-results/initial-avc-pruning-20261111/unittest-python314.log`.
- Independent review found no blocking correctness or scope issues.

A bounded generated H.264/AAC fixture contained one impossible initial
partition-C-only group and five independent binary suffix groups. The baseline
performed 32 real reconstructions and 32 strict decoder calls, copying
1,885,056 encoded-media bytes in 4.057 seconds. The optimized path rejected one
initial content fingerprint, performed zero reconstructions/decoder calls and
copied zero candidate-media bytes in 0.001319 seconds. The required first-sample
screen still reads its source bytes. These timings were collected alongside an
active recovery; they establish avoided fixture work, not whole-case speedup.

This execution-only optimization preserves final acceptance checks and needs
no version change. An already running recovery retains its loaded runtime;
source edits alone do not activate this gate in that process.
