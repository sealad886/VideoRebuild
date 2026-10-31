"""Stream canonical case reports without retaining every job in memory."""
from __future__ import annotations

import csv
import html
import json
from pathlib import PurePosixPath
import time

from . import __version__
from .progress import phase
from .storage import Case, atomic_text, write_json


def _outputs(detail: dict):
    for entry in detail.get('outputs', []):
        path = entry if isinstance(entry, str) else entry.get('path', '')
        p = PurePosixPath(path)
        if path and not p.is_absolute() and '..' not in p.parts:
            yield path


def _search_scope(detail: dict) -> tuple[str, str]:
    tiers = detail.get('search_tiers', {})
    searched = ','.join(str(value) for value in tiers.get('searched', []))
    unsearched = ('true' if tiers['second_tier_unsearched'] else 'false') if 'second_tier_unsearched' in tiers else ''
    return searched, unsearched


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
                             'Later segments are separate clips; missing frames and wall-clock chronology are not inferred.',
                             'Tiered results describe only searched tiers; unsearched recovered ranges do not prove absence or global uniqueness.']}
    with phase('Writing summary report', total=1, unit='files') as task:
        write_json(case.root/'reports'/'summary.json', overview)
        task.advance()
    with phase('Writing JSON catalogue', total=overview['processed']+source[0], unit='records') as task, atomic_text(case.root/'reports'/'results.json') as stream:
        stream.write('{"summary":'+json.dumps(overview)+',"jobs":[')
        for index, job in enumerate(case.get_jobs()):
            if index:
                stream.write(',')
            json.dump(job, stream)
            task.advance()
        stream.write('],"sources":[')
        for index, row in enumerate(case.db.execute('SELECT * FROM sources ORDER BY id')):
            if index:
                stream.write(',')
            json.dump(dict(row), stream)
            task.advance()
        stream.write(']}\n')
    with phase('Writing intake issues', unit='records') as task, atomic_text(case.root/'reports'/'intake-issues.jsonl') as stream:
        for row in case.db.execute("SELECT detail FROM events WHERE kind='blocked_mapping' ORDER BY id"):
            stream.write(row['detail']+'\n')
            task.advance()
    with phase('Writing CSV catalogue', total=overview['processed'], unit='jobs') as task, atomic_text(case.root/'reports'/'results.csv') as stream:
        writer = csv.writer(stream)
        writer.writerow(['kind', 'key', 'status', 'outputs', 'reason', 'searched_tiers', 'second_tier_unsearched', 'historical_outputs'])
        for job in case.get_jobs():
            writer.writerow([job['kind'], job['key'], job['status'], ';'.join(_outputs(job['detail'])),
                             job['detail'].get('reason', ''), *_search_scope(job['detail']),
                             ';'.join(_outputs({'outputs': job['detail'].get('historical_outputs', [])}))])
            task.advance()
    with phase('Writing HTML catalogue', total=overview['processed'], unit='jobs') as task, atomic_text(case.root/'reports'/'index.html') as stream:
        stream.write('<!doctype html><html lang="en"><meta charset="utf-8">'
                     '<meta name="viewport" content="width=device-width"><title>VideoRebuild case</title>'
                     '<style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:1rem}'
                     'table{border-collapse:collapse;width:100%}td,th{padding:.6rem;text-align:left;border-bottom:1px solid #bbb}'
                     'code{overflow-wrap:anywhere}</style><h1>VideoRebuild case</h1><pre>')
        stream.write(html.escape(json.dumps(overview, indent=2)))
        stream.write('</pre><table><thead><tr><th>Strategy / location</th><th>Status</th><th>Outputs</th>'
                     '<th>Reason</th><th>Search scope</th></tr></thead><tbody>')
        for job in case.get_jobs():
            detail = job['detail']
            links = '<br>'.join('<a href="../'+html.escape(p, quote=True)+'">'+html.escape(p)+'</a>'
                                for p in _outputs(detail))
            historical = '<br>'.join('Historical: <a href="../'+html.escape(p, quote=True)+'">'+html.escape(p)+'</a>'
                                     for p in _outputs({'outputs': detail.get('historical_outputs', [])}))
            if historical:
                links = '<br>'.join(value for value in (links, historical) if value)
            tiers, unsearched = _search_scope(detail)
            scope = ('tiers '+tiers if tiers else 'not recorded')
            if unsearched == 'true':
                scope += '; recovered tier not searched'
            stream.write('<tr><td><code>'+html.escape(job['kind']+':'+job['key'])+'</code></td><td>'+
                         html.escape(job['status'])+'</td><td>'+links+'</td><td>'+
                         html.escape(str(detail.get('reason', '')))+'</td><td>'+html.escape(scope)+'</td></tr>')
            task.advance()
        stream.write('</tbody></table><p>See results.json for acquisition, association, validation and byte provenance.</p></html>\n')
    return overview
