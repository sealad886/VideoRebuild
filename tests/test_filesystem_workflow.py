"""Real filesystem-generated extents through the public recovery CLI."""
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

from helpers import generate, payload
from videorebuild.ranges import iter_dfxml


@unittest.skipUnless(shutil.which('fiwalk'), 'optional Sleuth Kit fiwalk unavailable')
class FilesystemWorkflowTests(unittest.TestCase):
    def run_tool(self, arguments, *, timeout=60, max_output=1024*1024):
        # Spool process output so an unexpected tool diagnostic cannot consume
        # unbounded Python memory. The input fixture contains one small file.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(arguments, stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr, timeout=timeout)
            stdout.seek(0)
            stderr.seek(0)
            output, error = stdout.read(max_output+1), stderr.read(max_output+1)
        self.assertLessEqual(len(output), max_output, 'tool stdout limit exceeded')
        self.assertLessEqual(len(error), max_output, 'tool stderr limit exceeded')
        self.assertEqual(result.returncode, 0, error.decode('utf-8', errors='replace'))
        return output, error

    def fat16_disk(self, raw):
        """Build a bounded MBR/FAT16 image without mounting any filesystem."""
        partition_sector, partition_sectors = 2048, 8192
        partition = partition_sector*512
        disk = bytearray((partition_sector+partition_sectors)*512)
        struct.pack_into('<B3sB3sII', disk, 446, 0x80, b'\xfe\xff\xff',
                         0x06, b'\xfe\xff\xff', partition_sector, partition_sectors)
        disk[510:512] = b'\x55\xaa'
        disk[partition:partition+11] = b'\xeb\x3c\x90MSDOS5.0'
        struct.pack_into('<HBHBHHBHHHII', disk, partition+11,
                         512, 1, 1, 2, 512, partition_sectors, 0xf8,
                         32, 32, 64, partition_sector, 0)
        disk[partition+36:partition+39] = bytes((0x80, 0, 0x29))
        struct.pack_into('<I', disk, partition+39, 12345)
        disk[partition+43:partition+54] = b'VIDEOTEST  '
        disk[partition+54:partition+62] = b'FAT16   '
        disk[partition+510:partition+512] = b'\x55\xaa'
        clusters = tuple(2+index*3 for index in range((len(raw)+511)//512))
        self.assertGreater(len(clusters), 2)
        self.assertLess(clusters[-1], partition_sectors-97+2,
                        'generated video exceeds the fixed filesystem fixture')
        links = [(0, 0xfff8), (1, 0xffff)]
        links.extend(zip(clusters, (*clusters[1:], 0xffff)))
        for fat_sector in (1, 33):
            for cluster, successor in links:
                struct.pack_into('<H', disk, partition+fat_sector*512+cluster*2, successor)
        directory = partition+65*512
        disk[directory:directory+11] = b'VIDEO   MP4'
        disk[directory+11] = 0x20
        struct.pack_into('<H', disk, directory+26, clusters[0])
        struct.pack_into('<I', disk, directory+28, len(raw))
        for index, cluster in enumerate(clusters):
            chunk = raw[index*512:(index+1)*512]
            physical = partition+(97+cluster-2)*512
            disk[physical:physical+len(chunk)] = chunk
        return disk, partition

    def test_real_fiwalk_partition_extents_recover_video_without_changing_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root/'original.mp4'
            generate(original, faststart=True, seconds=1.0)
            raw = original.read_bytes()
            disk, partition = self.fat16_disk(raw)
            image = root/'disk.img'
            image.write_bytes(disk)
            report = root/'filesystem.xml'
            xml, _ = self.run_tool([shutil.which('fiwalk'), '-x', '-z', '-g', '-G0', str(image)])
            report.write_bytes(xml)
            mapping = next(record for record in iter_dfxml(report, len(disk))
                           if record.name.upper() == 'VIDEO.MP4')
            self.assertIsNone(mapping.reason)
            self.assertEqual(mapping.size, len(raw))
            self.assertGreater(len(mapping.extents), 2)
            self.assertTrue(all(physical >= partition for _, physical, _ in mapping.extents))
            self.assertTrue(any(left[1]+left[2] != right[1]
                                for left, right in zip(mapping.extents, mapping.extents[1:])))
            self.assertEqual(b''.join(disk[physical:physical+length]
                                      for _, physical, length in mapping.extents), raw)
            mapfile = root/'rescue.map'
            mapfile.write_text(f'# Synthetic all-rescued acquisition\n0 + 1\n0 {len(disk)} +\n')
            inputs = (original, image, report, mapfile)
            before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
            case = root/'case'
            self.run_tool([sys.executable, '-m', 'videorebuild', 'run', '--case', str(case),
                           '--image', str(image), '--extents', str(report), '--mapfile', str(mapfile)],
                          timeout=90)
            outputs = list((case/'reconstructed').glob('*.mp4'))
            self.assertEqual(len(outputs), 1)
            self.assertEqual(payload(outputs[0]), payload(original))
            jobs = json.loads((case/'reports'/'results.json').read_text())['jobs']
            self.assertTrue(any(job['status'] == 'matched' for job in jobs))
            _, diagnostics = self.run_tool(['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error',
                                           '-xerror', '-i', str(outputs[0]), '-map', '0',
                                           '-f', 'null', '-'], max_output=64*1024)
            self.assertEqual(diagnostics, b'')
            self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}, before)


if __name__ == '__main__':
    unittest.main()
