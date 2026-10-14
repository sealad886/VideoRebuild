from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.bmff import FormatError, boxes, make_box, parse_movie, rewrite_offsets, valid_sample, _sample_sizes
from videorebuild.storage import iter_blocks


class BoxTests(unittest.TestCase):
    def test_short_header_rejected(self):
        with self.assertRaises(FormatError):
            list(boxes(b'1234567'))

    def test_oversized_box_rejected(self):
        with self.assertRaises(FormatError):
            list(boxes(struct.pack('>I4s', 200, b'moov')))

    def test_undersized_box_rejected(self):
        with self.assertRaises(FormatError):
            list(boxes(struct.pack('>I4s', 4, b'mdat')))

    def test_extended_size(self):
        b = next(boxes(struct.pack('>I4sQ', 1, b'mdat', 20) + b'abcd'))
        self.assertEqual((b.payload, b.end), (16, 20))

    def test_size_zero(self):
        b = next(boxes(struct.pack('>I4s', 0, b'mdat') + b'abcd'))
        self.assertEqual(b.end, 12)

    def test_multiple_roots_not_movie(self):
        with self.assertRaises(FormatError):
            parse_movie(make_box(b'moov', b'') * 2)

    def test_empty_movie_rejected(self):
        with self.assertRaises(FormatError):
            parse_movie(make_box(b'moov', b''))

    def test_fragmented_movie_rejected_explicitly(self):
        with self.assertRaisesRegex(FormatError, 'mvex'):
            parse_movie(make_box(b'moov', make_box(b'mvex', b'')))

    def test_avc_length_chain(self):
        # One SEI NAL followed by one IDR NAL; total sample boundaries are exact.
        data = struct.pack('>I', 3)+b'\x06ab'+struct.pack('>I', 4)+b'\x65xyz'
        self.assertTrue(valid_sample(data, b'avc1', 4))
        self.assertFalse(valid_sample(data[:-1], b'avc1', 4))
        self.assertFalse(valid_sample(data+b'X', b'avc1', 4))

    def test_no_vcl_is_not_video_sample(self):
        self.assertFalse(valid_sample(struct.pack('>I', 3)+b'\x06ab', b'avc1', 4))

    def test_hevc_header(self):
        self.assertTrue(valid_sample(struct.pack('>I', 3)+b'\x26\x01x', b'hvc1', 4))
        self.assertFalse(valid_sample(struct.pack('>I', 3)+b'\x26\x00x', b'hvc1', 4))

    def test_zero_length_nal_rejected(self):
        self.assertFalse(valid_sample(b'\0\0\0\0', b'avc1', 4))

    def test_forbidden_bit_rejected(self):
        self.assertFalse(valid_sample(b'\0\0\0\2\xe5X', b'avc1', 4))

    def test_two_byte_nal_lengths(self):
        self.assertTrue(valid_sample(b'\0\2\x65X', b'avc1', 2))

    def test_stz2_nibbles(self):
        stz2 = make_box(b'stz2', b'\0'*7 + b'\4' + struct.pack('>I', 3) + b'\x12\x30')
        stbl = make_box(b'stbl', stz2)
        self.assertEqual(_sample_sizes(stbl, next(boxes(stbl))), [1,2,3])

    def test_stz2_zero_sample_rejected(self):
        stz2 = make_box(b'stz2', b'\0'*7 + b'\x08' + struct.pack('>I', 1) + b'\0')
        stbl = make_box(b'stbl', stz2)
        with self.assertRaises(FormatError):
            _sample_sizes(stbl, next(boxes(stbl)))

    def test_block_ownership_exactly_once(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.bin'; raw=bytes(range(100)); p.write_bytes(raw)
            with patch('videorebuild.storage.BLOCK', 17):
                blocks=list(iter_blocks(p, 16))
            self.assertEqual(b''.join(b[:n] for _,b,n in blocks),raw)
            self.assertEqual([p for p,_,_ in blocks], [0,17,34,51,68,85])
