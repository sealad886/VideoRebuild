# Format and implementation references

These primary sources informed the format handling and validation strategy. The matching engine and its tests are original implementation work in this repository; the references are not evidence that this new engine works on an untested camera.

- [CGSecurity: PhotoRec camera-video recovery](https://www.cgsecurity.org/testdisk_doc/photorec_video.html): the documented two-piece `ftyp`/`mdat` case and its limited scope.
- [Apple: sample-to-chunk atom](https://developer.apple.com/documentation/quicktime-file-format/sample-to-chunk_atom): the `stsc` table.
- [Apple: chunk-offset atom](https://developer.apple.com/documentation/quicktime-file-format/chunk_offset_atom): file-relative chunk offsets.
- [Apple: sample-size atom](https://developer.apple.com/documentation/quicktime-file-format/sample_size_atom): sample sizes needed to locate encoded packets.
- [FFprobe documentation](https://ffmpeg.org/ffprobe.html): machine-readable stream data and frame/packet counting.
- [FFmpeg documentation](https://ffmpeg.org/ffmpeg.html): decoding, stream selection, and strict error handling.
- [W3C: ISO BMFF byte-stream format](https://www.w3.org/TR/mse-byte-stream-format-isobmff/): initialization/media segments and the distinction between ordinary and fragmented MP4. The local fragmented backend implements its own narrower recovery contract, not the browser MSE profile.
- [Homebrew FFmpeg](https://formulae.brew.sh/formula/ffmpeg): available FFmpeg formulae, including the version-7 line used as the release reference.
- [Pinned setuptools build dependency](https://pypi.org/project/setuptools/82.0.1/): release and checksum provenance.

Consulted September 29, 2026. Tool versions used for actual execution are recorded separately in the test report and in each recovery case.

## Fragmented backend references

- PhotoRec MOV carver: https://raw.githubusercontent.com/cgsecurity/testdisk/master/src/file_mov.c
- FFmpeg 7.1.5 MOV demuxer: https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.5/libavformat/mov.c
- W3C ISO BMFF browser byte-stream note (MSE profile, not a universal requirement for local files): https://www.w3.org/TR/mse-byte-stream-format-isobmff/

The parser is independently implemented for a narrow recovery contract; no external decoder source or binary is bundled.

The October 16, 2026 accuracy audit checked the installed FFmpeg 9.0.2 behavior against [its MOV demuxer source](https://github.com/FFmpeg/FFmpeg/blob/n9.0.2/libavformat/mov.c), especially `mov_update_dts_shift`, `mov_read_trun`, and `mov_finalize_packet`. See [the accuracy audit](AUDIT_ACCURACY.md) for executed evidence and limits.
