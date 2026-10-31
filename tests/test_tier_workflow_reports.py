"""The operator's fallback choice and search scope survive public workflows/reports."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.cli import parser
from videorebuild.reports import report
from videorebuild.storage import Case
from videorebuild.workflow import reconstruct


class TierWorkflowTests(unittest.TestCase):
    def test_fallback_choice_reaches_both_recovery_strategies(self):
        for command in ('recover', 'run'):
            for enabled in (False, True):
                with self.subTest(command=command, enabled=enabled), tempfile.TemporaryDirectory() as directory:
                    argv = [command, '--case', directory, '--reconstruct-only']
                    if enabled:
                        argv.append('--include-recovered')
                    args = parser().parse_args(argv)
                    with Case(Path(directory)) as case, patch('videorebuild.workflow.recover') as ordinary, \
                            patch('videorebuild.image_recovery.recover_fragmented') as fragmented:
                        reconstruct(case, args)
                    self.assertEqual(ordinary.call_args.args[1].include_recovered, enabled)
                    self.assertEqual(fragmented.call_args.args[3].include_recovered, enabled)

    def test_catalogues_distinguish_unsearched_second_tier_and_historical_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            with Case(Path(directory)) as case:
                case.result('first', 'reconstructed', {'outputs': [], 'search_tiers': {
                    'searched': [1], 'second_tier_unsearched': True}})
                case.result('fallback', 'unresolved', {'outputs': [], 'search_tiers': {
                    'searched': [1, 2], 'second_tier_unsearched': False}})
                case.result('historical', 'unresolved', {'outputs': [], 'historical_outputs': ['reconstructed/old.mp4']})
                report(case)
                reports = case.root/'reports'
                with (reports/'results.csv').open() as stream:
                    rows = {row['key']: row for row in csv.DictReader(stream)}
                self.assertEqual(rows['first']['searched_tiers'], '1')
                self.assertEqual(rows['first']['second_tier_unsearched'], 'true')
                self.assertEqual(rows['fallback']['searched_tiers'], '1,2')
                self.assertEqual(rows['fallback']['second_tier_unsearched'], 'false')
                self.assertEqual(rows['historical']['searched_tiers'], '')
                self.assertEqual(rows['historical']['outputs'], '')
                self.assertEqual(rows['historical']['historical_outputs'], 'reconstructed/old.mp4')
                jobs = {j['key']: j for j in json.loads((reports/'results.json').read_text())['jobs']}
                self.assertTrue(jobs['first']['detail']['search_tiers']['second_tier_unsearched'])
                self.assertNotIn('search_tiers', jobs['historical']['detail'])
                self.assertIn('recovered tier not searched', (reports/'index.html').read_text())


if __name__ == '__main__':
    unittest.main()
