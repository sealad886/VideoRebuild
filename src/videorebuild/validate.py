"""Bounded subprocess validation; no shell, network input, or external MP4 drefs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .bmff import Movie
from .storage import SafetyError


def run_tool(args: list[str], timeout: float, output_limit: int = 2*1024*1024) -> dict:
    """Spool output to temporary files rather than accumulate unbounded pipe data."""
    env = os.environ.copy()
    env['AV_LOG_FORCE_NOCOLOR'] = '1'
    # Do not let inherited FFREPORT create files outside the case.
    env.pop('FFREPORT', None)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       env=env, start_new_session=True)
        except OSError as e:
            raise SafetyError(f'Cannot launch {args[0]}: {e}') from e
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        out.seek(0)
        err.seek(0)
        stdout = out.read(output_limit+1)
        stderr = err.read(64*1024+1)
        return {'argv': args, 'returncode': process.returncode, 'timeout': timed_out,
                'stdout': stdout[:output_limit].decode('utf-8', 'replace'),
                'stderr': stderr[:64*1024].decode('utf-8', 'replace'),
                'stdout_truncated': len(stdout) > output_limit,
                'stderr_truncated': len(stderr) > 64*1024}


def doctor() -> dict:
    result = {}
    for name in ('ffmpeg', 'ffprobe'):
        exe = shutil.which(name)
        if not exe:
            raise SafetyError(f'{name} is missing. On macOS: brew install ffmpeg')
        r = run_tool([exe, '-version'], 15)
        if r['returncode'] or r['timeout']:
            raise SafetyError(f'{name} -version failed: {r["stderr"]}')
        lines = r['stdout'].splitlines()
        version = lines[0] if lines else ''
        release = re.match(rf'^{name} version n?(\d+)\.\d+(?:\.\d+)*(?=[\s+~\-]|$)', version)
        # Older MOV demuxers can report codec-derived packet durations rather
        # than trun durations. Those cannot independently prove final-sample
        # timing, which has no following DTS to cross-check.
        if release is None:
            raise SafetyError(f'Cannot identify {name} release version; FFmpeg and ffprobe 8.0 or newer are required')
        if int(release[1]) < 8:
            raise SafetyError(f'{name} is too old; FFmpeg and ffprobe 8.0 or newer are required for exact container packet timing')
        result[name] = {'path': exe, 'version': version}
    return result


def validate(path: Path, movie: Movie, tools: dict, timeout: float) -> dict:
    base = ['-v', 'error', '-threads', '1', '-f', 'mov', '-protocol_whitelist', 'file',
            '-enable_drefs', '0', '-ignore_editlist', '1']
    probe = run_tool([tools['ffprobe']['path'], *base, '-count_packets', '-count_frames',
                      '-show_entries', 'stream=index,codec_type,codec_name,nb_read_frames,nb_read_packets,width,height',
                      '-of', 'json', str(path)], timeout)
    result = {'ok': False, 'probe': probe}
    if (probe['returncode'] or probe['timeout'] or probe['stderr'].strip() or
            probe['stdout_truncated']):
        result['reason'] = 'probe_or_decode_error'
        return result
    try:
        streams = json.loads(probe['stdout'])['streams']
    except (ValueError, KeyError, TypeError):
        result['reason'] = 'invalid_probe_output'
        return result
    if len(streams) != len(movie.tracks):
        result['reason'] = 'track_count_mismatch'
        return result
    expected = []
    for t, stream in zip(movie.tracks, streams):
        kind = 'video' if t.handler == b'vide' else 'audio'
        codec = ('h264' if t.codec in {b'avc1', b'avc3'} else
                 'hevc' if t.codec in {b'hvc1', b'hev1'} else 'aac')
        try:
            packets = int(stream.get('nb_read_packets', -1))
            frames = int(stream.get('nb_read_frames', -1))
        except (ValueError, TypeError):
            result['reason'] = 'missing_packet_or_frame_count'
            return result
        if stream.get('codec_type') != kind or stream.get('codec_name') != codec or packets != t.sample_count:
            result['reason'] = 'stream_or_sample_count_mismatch'
            return result
        if kind == 'video' and frames != t.sample_count:
            result['reason'] = 'decoded_video_frame_count_mismatch'
            return result
        if kind == 'audio' and frames != t.sample_count:
            result['reason'] = 'decoded_audio_frame_count_mismatch'
            return result
        expected.append({'track': t.track_id, 'expected_packets': t.sample_count,
                         'packets': packets, 'frames': frames, 'codec': codec})
    # Independent strict decode after the probe/count pass. Edit lists are ignored
    # only while validating; the output retains the original movie's edit lists.
    decode = run_tool([tools['ffmpeg']['path'], '-hide_banner', '-nostdin', '-xerror',
                       *base, '-err_detect', 'explode', '-i', str(path),
                       '-map', '0:v', '-map', '0:a?', '-threads', '1',
                       '-filter_threads', '1', '-f', 'null', '-'], timeout)
    result['decode'] = decode
    result['counts'] = expected
    result['ok'] = not (decode['returncode'] or decode['timeout'] or decode['stderr'].strip())
    result['reason'] = 'decode_checked' if result['ok'] else 'strict_decode_failed'
    return result
