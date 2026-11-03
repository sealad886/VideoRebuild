from datetime import datetime
import unittest

from videorebuild.tui_progress import progress_observations


STAMP = '2026-11-02 12:00:00,000'
NOW = datetime.strptime(STAMP, '%Y-%m-%d %H:%M:%S,%f').timestamp()


def record(label='Scanning image', status='running', count='25/100 bytes', elapsed=10, stamp=STAMP):
    return f'{stamp} INFO Progress: {label} | {status} | {count} | elapsed {elapsed}s'


def observe(lines, **options):
    return progress_observations(lines, busy=options.pop('busy', True),
                                 now=options.pop('now', NOW), **options)[0]


class ProgressObservationTests(unittest.TestCase):
    def test_counts_define_fraction_not_rounded_logged_percentage(self):
        value = observe([record(count='25/100 bytes (80.0%)')])[0]
        self.assertEqual(value['completed'], 25)
        self.assertEqual(value['total'], 100)
        self.assertEqual(value['fraction'], .25)
        self.assertEqual(value['eta_seconds'], 30)
        self.assertEqual(value['eta_basis'], 'phase_average')

    def test_unknown_empty_and_overrun_totals_do_not_invent_percentages(self):
        for count, total in [('25 jobs', None), ('0/0 bytes', 0), ('101/100 bytes', 100)]:
            with self.subTest(count=count):
                value = observe([record(count=count)])[0]
                self.assertEqual(value['total'], total)
                self.assertIsNone(value['fraction'])
                self.assertIsNone(value['eta_seconds'])
        self.assertIn('exceeds', observe([record(count='101/100 bytes')])[0]['detail'])

    def test_finished_incomplete_and_failed_phases_keep_actual_work(self):
        for status in ['finished', 'failed', 'interrupted', 'timeout', 'search_limited']:
            with self.subTest(status=status):
                value = observe([record(status=status)])[0]
                self.assertEqual(value['status'], status)
                self.assertEqual(value['fraction'], .25)
                self.assertIsNone(value['eta_seconds'])
        for status in ['failed', 'interrupted', 'timeout']:
            self.assertIsNone(observe([record(status=status, count='100/100 bytes')])[0]['fraction'])

    def test_all_work_read_does_not_manufacture_a_finished_status(self):
        value = observe([record(count='100/100 bytes')])[0]
        self.assertEqual(value['fraction'], 1)
        self.assertEqual(value['status'], 'running')
        self.assertIsNone(value['eta_seconds'])

    def test_only_fresh_writer_byte_phase_has_estimate(self):
        for options in [{'busy': False}, {'busy': None}, {'now': NOW + 31}]:
            value = observe([record()], **options)[0]
            self.assertIsNone(value['eta_seconds'])
        self.assertTrue(observe([record()], busy=False)[0]['historical'])
        self.assertTrue(observe([record()], now=NOW + 31)[0]['stale'])
        self.assertIsNone(observe([record(count='25/100 candidates')])[0]['eta_seconds'])

    def test_missing_record_timestamp_cannot_borrow_freshness_from_other_log_lines(self):
        line = record().split(' INFO ', 1)[1]
        value = observe([line])[0]
        self.assertEqual(value['fraction'], .25)
        self.assertIsNone(value['age_seconds'])
        self.assertTrue(value['stale'])
        self.assertIsNone(value['eta_seconds'])

    def test_nested_phase_completion_restores_parent_and_retains_child_outcome(self):
        lines = [record('Recovery strategy', 'started', '0/1 strategies', 0),
                 record('Scan', 'started', '0/100 bytes', 0), record('Scan'),
                 record('Scan', 'finished')]
        values = observe(lines)
        self.assertEqual(values[0]['label'], 'Scan')
        by_label = {value['label']: value for value in values}
        self.assertEqual(by_label['Recovery strategy']['completed'], 0)
        self.assertEqual(by_label['Scan']['status'], 'finished')
        # A child failure is not hidden behind a still-open parent.
        self.assertEqual(observe(lines[:-1] + [record('Scan', 'failed')])[0]['status'], 'failed')

    def test_new_start_resets_same_label_instead_of_accumulating(self):
        values = observe([record(), record(status='interrupted'), record(status='started', count='0/100 bytes')])
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]['completed'], 0)

    def test_bounded_tail_may_begin_inside_phase_without_inventing_parent(self):
        values = observe([record(count='48,722,477,056/388,125,877,625 bytes')])
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]['completed'], 48722477056)

    def test_recovery_start_hides_previous_run_and_error_retires_active_state(self):
        self.assertEqual(observe([record()], run_started=NOW + 5), [])
        values = observe([record(), '2026-11-02 12:00:01,000 ERROR disk full'])
        self.assertTrue(values[0]['historical'])
        self.assertIsNone(values[0]['eta_seconds'])
        self.assertFalse(observe([record(), 'ERROR: disk full', record(status='started')])[0]['historical'])

    def test_malformed_records_and_delimiter_labels_are_not_progress(self):
        for line in [record(label='bad | label'), record(count='1,00/100 bytes'),
                     record(elapsed='NaN'), record(elapsed='1.2.3'),
                     record(count='-1/100 bytes'), record().replace(' | elapsed ', ' | bogus '),
                     'prefix ' + record(), 'Progress: partial']:
            with self.subTest(line=line):
                self.assertEqual(observe([line]), [])

    def test_note_never_claims_overall_completion(self):
        values, note = progress_observations([record(status='finished', count='100/100 bytes')],
                                            busy=False, now=NOW)
        self.assertTrue(values[0]['historical'])
        self.assertIn('overall recovery total and ETA are unknown', note)


if __name__ == '__main__':
    unittest.main()
