"""Stream canonical case reports without retaining every job in memory."""
from __future__ import annotations

import csv
import html
import json
from pathlib import PurePosixPath
import time

from . import __version__
from .storage import Case, atomic_text, write_json


def _outputs(detail: dict):
    for entry in detail.get('outputs', []):
        path = entry if isinstance(entry, str) else entry.get('path', '')
        p = PurePosixPath(path)
        if path and not p.is_absolute() and '..' not in p.parts:
            yield path


def report(case: Case) -> dict:
    counts = dict(case.db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status'))
    source = case.db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM sources').fetchone()
    pending = case.db.execute("SELECT COUNT(*) FROM movies m WHERE NOT EXISTS "
                             "(SELECT 1 FROM jobs j WHERE j.kind='ordinary' AND j.key=m.digest)").fetchone()[0]
    overview = {'tool': 'VideoRebuild', 'version': __version__, 'generated_unix': time.time(),
                'sources': source[0], 'source_bytes': source[1],
                'atom_candidates': dict(case.db.execute('SELECT kind,COUNT(*) FROM atoms GROUP BY kind')),
                'movie_metadata_records': case.db.execute('SELECT COUNT(*) FROM movies').fetchone()[0],
                'status_counts': counts, 'pending': pending, 'processed': sum(counts.values()),
                'warnings': ['Decoded media and byte preservation do not establish historical recording identity.',
                             'Unrescued and unmapped byte ranges are unavailable; absent acquisition evidence is unknown.',
                             'Later segments are separate clips; missing frames and wall-clock chronology are not inferred.']}
    write_json(case.root/'reports'/'summary.json', overview)
    with atomic_text(case.root/'reports'/'results.json') as stream:
        stream.write('{"summary":'+json.dumps(overview)+',"jobs":[')
        for index, job in enumerate(case.get_jobs()):
            if index:
                stream.write(',')
            json.dump(job, stream)
        stream.write('],"sources":[')
        for index, row in enumerate(case.db.execute('SELECT * FROM sources ORDER BY id')):
            if index:
                stream.write(',')
            json.dump(dict(row), stream)
        stream.write(']}\n')
    with atomic_text(case.root/'reports'/'intake-issues.jsonl') as stream:
        for row in case.db.execute("SELECT detail FROM events WHERE kind='blocked_mapping' ORDER BY id"):
            stream.write(row['detail']+'\n')
    with atomic_text(case.root/'reports'/'results.csv') as stream:
        writer = csv.writer(stream)
        writer.writerow(['kind', 'key', 'status', 'outputs', 'reason'])
        for job in case.get_jobs():
            writer.writerow([job['kind'], job['key'], job['status'], ';'.join(_outputs(job['detail'])),
                             job['detail'].get('reason', '')])
    with atomic_text(case.root/'reports'/'index.html') as stream:
        stream.write('<!doctype html><html lang="en"><meta charset="utf-8">'
                     '<meta name="viewport" content="width=device-width"><title>VideoRebuild case</title>'
                     '<style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:1rem}'
                     'table{border-collapse:collapse;width:100%}td,th{padding:.6rem;text-align:left;border-bottom:1px solid #bbb}'
                     'code{overflow-wrap:anywhere}</style><h1>VideoRebuild case</h1><pre>')
        stream.write(html.escape(json.dumps(overview, indent=2)))
        stream.write('</pre><table><thead><tr><th>Strategy / location</th><th>Status</th><th>Outputs</th>'
                     '<th>Reason</th></tr></thead><tbody>')
        for job in case.get_jobs():
            detail = job['detail']
            links = '<br>'.join('<a href="../'+html.escape(p, quote=True)+'">'+html.escape(p)+'</a>'
                                for p in _outputs(detail))
            stream.write('<tr><td><code>'+html.escape(job['kind']+':'+job['key'])+'</code></td><td>'+
                         html.escape(job['status'])+'</td><td>'+links+'</td><td>'+
                         html.escape(str(detail.get('reason', '')))+'</td></tr>')
        stream.write('</tbody></table><p>See results.json for acquisition, association, validation and byte provenance.</p></html>\n')
    return overview
