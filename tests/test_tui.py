"""Keyboard workflows and the TUI's canonical CLI handoff."""
from pathlib import Path
import io
import os
import sqlite3
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from videorebuild import cli, tui
from videorebuild.progress import phase
from videorebuild.tui_model import Settings
from videorebuild.storage import Case, scan_logical
from test_deep_witnesses import structural_movie


class TerminalError(Exception):
    pass


class Screen:
    def __init__(self, keys=(), size=(24, 80)):
        self.keys = iter(keys)
        self.size = size
        self.lines = {}
        self.frames = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.lines = {}

    def clear(self):
        self.lines = {}

    def addnstr(self, row, col, text, length, attributes):
        self.lines[row] = text[:length]

    def get_wch(self):
        return next(self.keys)

    def timeout(self, delay):
        pass

    def keypad(self, enabled):
        pass

    def refresh(self):
        self.frames.append(dict(self.lines))


def terminal():
    return SimpleNamespace(A_REVERSE=1, error=TerminalError,
        KEY_UP=1000, KEY_DOWN=1001, KEY_LEFT=1002, KEY_RIGHT=1003,
        KEY_ENTER=1004, KEY_BACKSPACE=1005, KEY_RESIZE=1006, KEY_NPAGE=1007, KEY_PPAGE=1008,
        curs_set=lambda _: None, has_colors=lambda: False)


class TerminalInterfaceTests(unittest.TestCase):
    def app(self, keys=(), settings=None, watch=False, size=(24, 80)):
        return tui.Application(Screen(keys, size), terminal(), settings or Settings(), watch=watch)

    def test_command_routes_to_tui_without_opening_case(self):
        with patch('videorebuild.tui.run', return_value=0) as run, patch.object(cli, 'Case') as case:
            self.assertEqual(cli.main(['tui', '--case', '/case folder', '--watch']), 0)
        run.assert_called_once_with(Path('/case folder'), watch=True, no_progress=False)
        case.assert_not_called()

    def test_noninteractive_terminal_explains_cli_alternative(self):
        output = io.StringIO()
        with patch.object(tui.sys, 'stdin', io.StringIO()), patch.object(tui.sys, 'stderr', output):
            self.assertEqual(tui.run(), 2)
        self.assertIn('interactive terminal', output.getvalue())
        self.assertIn('recover --help', output.getvalue())

    def test_navigation_toggles_option_without_starting_work(self):
        app = self.app([1001] * 6 + ['\n', 'q'])
        with patch.object(app, 'execute') as execute:
            self.assertEqual(app.loop(), 0)
        self.assertTrue(app.settings.include_recovered)
        self.assertTrue(app.settings.reconstruct_only)
        execute.assert_not_called()

    def test_search_worker_menu_cycles_bounded_values_without_launching(self):
        app = self.app([1001] * 8 + ['\n'] * 8 + ['q'])
        values = []
        draw = app.draw_menu
        def observe():
            values.append(app.settings.search_workers)
            draw()
        with patch.object(app, 'draw_menu', side_effect=observe), patch.object(app, 'execute') as execute:
            self.assertEqual(app.loop(), 0)
        self.assertEqual(set(values), set(range(1, 9)))
        self.assertEqual(app.settings.search_workers, 1)
        execute.assert_not_called()

    def test_cli_workers_are_bounded_before_case_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory) / 'not-created'
            for workers in ('0', '9', 'bad'):
                with self.subTest(workers=workers), patch('sys.stderr', io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        cli.parser().parse_args(['recover', '--case', str(case), '--search-workers', workers])
                    self.assertEqual(error.exception.code, 2)
            args = cli.parser().parse_args(['recover', '--case', str(case), '--search-workers', '3'])
            self.assertEqual(args.search_workers, 3)
            self.assertFalse(case.exists())

    def test_optional_path_prompt_preserves_literal_text(self):
        app = self.app(list('/tmp/case $(literal)') + ['\n'])
        self.assertEqual(app.prompt('Go to path'), '/tmp/case $(literal)')

    def test_cancelled_editor_keeps_original_setting(self):
        app = self.app(['\x15', 'x', '\x1b'])
        self.assertIsNone(app.prompt('Go to path', '/original'))

    def test_review_defaults_to_cancel_then_requires_explicit_start(self):
        self.assertFalse(self.app(['\n']).review(['scan', '--case', '/case', '--input', '/source']))
        self.assertTrue(self.app([1003, '\n']).review(['scan', '--case', '/case', '--input', '/source']))

    def test_review_refuses_hidden_long_command(self):
        app = self.app([1003, '\n'], size=(18, 40))
        self.assertFalse(app.review(['run', '--case', '/' + 'long path/' * 80]))

    def test_review_cannot_start_when_terminal_hides_confirmation(self):
        for size in ((5, 120), (24, 30)):
            with self.subTest(size=size):
                self.assertFalse(self.app([1003, '\n'], size=size).review(['scan']))

    def test_busy_case_directs_user_to_watch_without_cli_handoff(self):
        app = self.app(settings=Settings(case='/case'))
        with patch.object(tui, 'build_arguments', return_value=['recover', '--case', '/case']), \
             patch.object(tui, 'snapshot', return_value={'busy': True}), \
             patch.object(app, 'review') as review, patch.object(app, 'execute') as execute:
            app.launch('recover')
        self.assertIn('Watch existing case', app.message)
        review.assert_not_called()
        execute.assert_not_called()

    def test_watch_quit_returns_to_menu_and_never_runs_recovery(self):
        app = self.app(['q', 'q'], Settings(case='/case'), watch=True)
        observed = {'case': '/case', 'error': None, 'busy': True, 'jobs': {'reconstructed': 2},
                    'sources': 3, 'free_bytes': 4096, 'log_lines': ['Progress: running'],
                    'log_mtime': None, 'report_path': '/case/reports/index.html'}
        with patch.object(tui, 'snapshot', return_value=observed) as snapshot, \
             patch.object(app, 'execute') as execute:
            self.assertEqual(app.loop(), 0)
        snapshot.assert_called_once_with('/case')
        execute.assert_not_called()

    def test_foreground_execution_restores_terminal_after_cli_failure(self):
        app = self.app()
        calls = []
        app.terminal.def_prog_mode = lambda: calls.append('save')
        app.terminal.endwin = lambda: calls.append('leave')
        app.terminal.reset_prog_mode = lambda: calls.append('restore')
        with patch.object(cli, 'main', side_effect=OSError('output unavailable')), \
             patch('sys.stdout', io.StringIO()):
            with self.assertRaises(OSError):
                app.execute(['recover', '--case', '/case'])
        self.assertEqual(calls, ['save', 'leave', 'restore'])

    def test_foreground_handoff_preserves_progress_preference(self):
        def recover(args, feedback):
            self.assertEqual(args.command, 'recover')
            with phase('Synthetic recovery', total=1) as work:
                work.advance()
            return 0

        def foreground(app):
            app.launch('recover')
            return 0

        term = terminal()
        term.wrapper = lambda callback: callback(Screen())
        term.def_prog_mode = term.endwin = term.reset_prog_mode = lambda: None
        for arguments in (['--no-progress', 'tui'], ['tui', '--no-progress'], ['tui']):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / 'case.sqlite3').touch()
                output, errors = io.StringIO(), io.StringIO()
                with patch.dict('sys.modules', {'curses': term}), \
                     patch('sys.stdin.isatty', return_value=True), \
                     patch('sys.stdout', output), patch.object(output, 'isatty', return_value=True), \
                     patch('sys.stderr', errors), patch('builtins.input', return_value=''), \
                     patch.object(tui.Application, 'loop', autospec=True, side_effect=foreground), \
                     patch.object(tui.Application, 'review', return_value=True), \
                     patch.object(tui, 'snapshot', return_value={'busy': False}), \
                     patch.object(cli, '_execute', side_effect=recover) as recovery:
                    self.assertEqual(cli.main([*arguments, '--case', directory]), 0)
                recovery.assert_called_once()
                self.assertEqual('Progress:' in errors.getvalue(), '--no-progress' not in arguments)

    def test_foreground_recovery_keeps_search_storage_in_each_selected_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ambient = root / 'ambient-temp'
            ambient.mkdir()
            movie = structural_movie([1000], [8])
            source = root / 'source.bin'
            source.write_bytes(movie.data + bytes(992 - len(movie.data)) +
                               struct.pack('>I4s', 16, b'mdat') + b'\0\0\0\x04\x65ABC')
            original = source.read_bytes()
            connect = sqlite3.connect
            for name in ('first-case', 'second-case'):
                case_root = root / name
                with Case(case_root) as case:
                    scan_logical(case, case.register_source(source))
                search_paths = []

                def observe_connection(*args, **kwargs):
                    connection = connect(*args, **kwargs)
                    filename = connection.execute('PRAGMA database_list').fetchone()[2]
                    if filename and Path(filename) != case_root / 'case.sqlite3':
                        search_paths.append(Path(filename))
                    return connection

                app = self.app(settings=Settings(case=str(case_root)))
                app.terminal.def_prog_mode = app.terminal.endwin = app.terminal.reset_prog_mode = lambda: None
                with self.subTest(case=name), \
                     patch.dict(os.environ, {'TMPDIR': str(ambient)}), \
                     patch.object(tempfile, 'tempdir', str(ambient)), \
                     patch.object(sqlite3, 'connect', side_effect=observe_connection), \
                     patch.object(app, 'review', return_value=True), \
                     patch('builtins.input', return_value=''), \
                     patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
                    environment = dict(os.environ)
                    app.launch('recover')
                    self.assertEqual(dict(os.environ), environment)
                self.assertTrue(search_paths, 'Foreground recovery did not open search storage')
                self.assertTrue(all(path.is_relative_to(case_root / 'work') for path in search_paths))
                ephemeral = [path for path in search_paths if path.parent.name.startswith('videorebuild-search-')]
                durable = [path for path in search_paths if path.parent.name == 'structural-index']
                self.assertTrue(ephemeral)
                self.assertTrue(durable)
                self.assertTrue(all(not path.exists() for path in ephemeral))
                self.assertTrue(all(path.is_file() for path in durable))
                self.assertEqual(list(ambient.iterdir()), [])
                with Case(case_root) as case:
                    job = case.job('ordinary', movie.digest)
                    self.assertEqual(job['status'], 'reconstructed')
                    self.assertFalse(job['detail']['decode_checked'])
            self.assertEqual(source.read_bytes(), original)

    def test_display_removes_terminal_control_sequences(self):
        app = self.app()
        app.write(4, '/case\x1b[2J\nname')
        self.assertNotIn('\x1b', app.screen.lines[4])
        self.assertNotIn('\n', app.screen.lines[4])

    def progress_snapshot(self, **changes):
        observed = {'case': '/case', 'error': None, 'busy': True,
                    'jobs': {'ordinary/reconstructed': 12}, 'sources': 30,
                    'free_bytes': 4096, 'log_lines': ['work continues'],
                    'log_mtime': None, 'report_path': '/case/reports/index.html',
                    'progress_note': 'Measured phase work; overall total unknown.',
                    'progress': [{'label': 'Deep structural indexing', 'status': 'running',
                                  'completed': 250, 'total': 1000, 'unit': 'bytes',
                                  'elapsed_seconds': 5.0, 'fraction': 0.25,
                                  'eta_seconds': 15.0, 'eta_basis': 'phase_average',
                                  'historical': False, 'stale': False}]}
        observed.update(changes)
        return observed

    def test_watch_shows_measured_phase_fraction_and_estimate_scope(self):
        app = self.app()
        app.draw_watch(self.progress_snapshot())
        text = '\n'.join(app.screen.lines.values())
        for expected in ('Deep structural indexing', '250/1,000 bytes', '25.0%', '#',
                         'running', 'phase-average', 'overall total unknown'):
            self.assertIn(expected, text)
        self.assertNotIn('100.0%', text)
        self.assertNotIn('Recovery complete', text)

    def test_watch_unknown_total_does_not_invent_fraction(self):
        observed = self.progress_snapshot()
        observed['progress'][0].update(total=None, fraction=None, eta_seconds=None)
        app = self.app()
        app.draw_watch(observed)
        text = '\n'.join(app.screen.lines.values())
        self.assertIn('250/? bytes', text)
        self.assertIn('total unknown', text)
        self.assertIn('???', text)
        self.assertNotIn('%', text)
        self.assertNotIn('ETA', text)

    def test_watch_interrupted_and_historical_progress_keeps_recorded_fraction(self):
        observed = self.progress_snapshot(busy=False)
        observed['progress'][0].update(status='interrupted', historical=True, stale=True)
        app = self.app()
        app.draw_watch(observed)
        text = '\n'.join(app.screen.lines.values())
        for expected in ('25.0%', 'interrupted', 'historical', 'STALE', 'completion unverified'):
            self.assertIn(expected, text)
        self.assertNotIn('Phase ETA', text)
        self.assertNotIn('100.0%', text)

    def test_incomplete_fraction_never_rounds_up_to_complete(self):
        progress = self.progress_snapshot()['progress'][0]
        progress.update(completed=99999, total=100000, fraction=0.99999)
        text = '\n'.join(tui.phase_lines(progress, 77))
        self.assertIn('99.9%', text)
        self.assertNotIn('100.0%', text)
        self.assertIn('-', text)

    def test_watch_no_progress_preference_hides_bars(self):
        app = self.app()
        app.no_progress = True
        app.draw_watch(self.progress_snapshot())
        text = '\n'.join(app.screen.lines.values())
        self.assertNotIn('Deep structural indexing', text)
        self.assertNotIn('25.0%', text)
        self.assertIn('Recorded jobs', text)

    def test_watch_progress_fits_resize_without_overwriting_footer(self):
        observed = self.progress_snapshot()
        observed['progress'] *= 3
        for size in ((24, 80), (18, 40), (8, 20)):
            with self.subTest(size=size):
                app = self.app(size=size)
                app.draw_watch(observed)
                height, width = size
                self.assertTrue(all(len(text) <= width - 3 for text in app.screen.lines.values()))
                self.assertIn('R:', app.screen.lines[height - 2])
                self.assertTrue(all(row < height - 1 for row in app.screen.lines))
                if height >= 18:
                    self.assertIn('writer lock held', app.screen.lines[5])
                    self.assertIn('25.0%', '\n'.join(app.screen.lines.values()))

    def test_small_terminal_can_exit(self):
        self.assertEqual(self.app(['q'], size=(8, 20)).loop(), 0)


class FilesystemBrowserTests(unittest.TestCase):
    def app(self, keys=(), settings=None, watch=False, size=(24, 80)):
        return tui.Application(Screen(keys, size), terminal(), settings or Settings(), watch=watch)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'alpha').mkdir()
        (self.root / 'beta').mkdir()
        (self.root / 'image.dat').write_bytes(b'fixture')
        (self.root / 'policy.json').write_text('{}')

    def browser(self, field, keys, *, initial=None, size=(24, 80)):
        settings = Settings(**{field: str(self.root if initial is None else initial)})
        app = self.app(keys, settings, size=size)
        app.edit(field, 'Choose path')
        return app

    def test_enter_directory_and_c_selects_it_without_changes(self):
        before = sorted(self.root.rglob('*'))
        app = self.browser('case', [1001, '\n', 'c'])
        self.assertEqual(app.settings.case, str(self.root / 'alpha'))
        self.assertEqual(before, sorted(self.root.rglob('*')))

    def test_left_backspace_and_parent_entry_navigate_up(self):
        for key in (1002, 1005, '\n'):
            with self.subTest(key=key):
                app = self.browser('input', [key, 'c'], initial=self.root / 'alpha')
                self.assertEqual(app.settings.input, str(self.root))

    def test_file_fields_descend_but_require_regular_file_selection(self):
        for field in ('image', 'allocation_map'):
            with self.subTest(field=field):
                app = self.browser(field, ['c', 1001, 1001, 1001, '\n'])
                self.assertEqual(getattr(app.settings, field), str(self.root / 'image.dat'))
                self.assertTrue(any('does not accept directories' in line for frame in app.screen.frames for line in frame.values()))

    def test_case_rejects_file_selection(self):
        app = self.browser('case', ['\n', 'c'], initial=self.root / 'image.dat')
        self.assertEqual(app.settings.case, str(self.root))
        self.assertTrue(any('Choose a directory for the case' in line for frame in app.screen.frames for line in frame.values()))

    def test_cancel_keeps_original_path_after_navigation(self):
        app = self.browser('case', [1001, '\n', 'q'])
        self.assertEqual(app.settings.case, str(self.root))

    def test_new_case_name_is_selected_without_creating_directory(self):
        app = self.browser('case', ['n', *list('new recovery'), '\n'])
        self.assertEqual(app.settings.case, str(self.root / 'new recovery'))
        self.assertFalse((self.root / 'new recovery').exists())

    def test_new_name_cannot_traverse_parent_or_replace_existing_entry(self):
        for name in ('../other', 'alpha', '..'):
            with self.subTest(name=name):
                app = self.browser('case', ['n', *list(name), '\n', 'q'])
                self.assertEqual(app.settings.case, str(self.root))

    def test_optional_fields_can_be_cleared(self):
        for field in ('input', 'image', 'allocation_map'):
            with self.subTest(field=field):
                app = self.browser(field, ['d'])
                self.assertEqual(getattr(app.settings, field), '')

    def test_go_to_file_handles_literal_metacharacters(self):
        path = self.root / 'input $(literal).mp4'
        path.write_bytes(b'fixture')
        app = self.browser('input', ['g', '\x15', *list(str(path)), '\n', '\n'])
        self.assertEqual(app.settings.input, str(path))

    def test_parent_components_preserve_file_focus_on_jump_and_initial_path(self):
        app = self.browser('image', ['g', '\x15', *list('alpha/../image.dat'), '\n', '\n'])
        self.assertEqual(app.settings.image, str(self.root / 'image.dat'))
        app = self.browser('image', ['\n'], initial=self.root / 'alpha' / '..' / 'image.dat')
        self.assertEqual(app.settings.image, str(self.root / 'image.dat'))

    def test_permission_error_keeps_browser_usable(self):
        from videorebuild.tui_browser import Browser
        listing = Browser.listing
        def guarded(browser, path):
            if path == self.root / 'alpha':
                raise PermissionError('fixture denied')
            return listing(browser, path)
        with patch.object(Browser, 'listing', guarded):
            app = self.browser('case', [1001, '\n', 'c'])
        self.assertEqual(app.settings.case, str(self.root))
        self.assertTrue(any('Cannot open this directory' in line for frame in app.screen.frames for line in frame.values()))

    def test_nonregular_entries_cannot_be_selected(self):
        fifo = self.root / 'z-fifo'
        os.mkfifo(fifo)
        app = self.browser('image', ['\n', 'q'], initial=fifo)
        self.assertEqual(app.settings.image, str(fifo))
        self.assertTrue(any('Choose a regular file' in line for frame in app.screen.frames for line in frame.values()))

    def test_paginated_listing_and_resize_keep_selection(self):
        folder = self.root / 'many'
        folder.mkdir()
        for index in range(30):
            (folder / f'file-{index:02}.mp4').write_bytes(b'fixture')
        app = self.browser('input', [1007, 1007, 1001, '\n'], initial=folder, size=(18, 70))
        self.assertEqual(app.settings.input, str(folder / 'file-12.mp4'))
        self.assertEqual(self.browser('case', ['q'], size=(8, 20)).settings.case, str(self.root))


if __name__ == '__main__':
    unittest.main()
