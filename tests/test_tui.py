"""Keyboard workflows and the TUI's canonical CLI handoff."""
from pathlib import Path
import io
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from videorebuild import cli, tui
from videorebuild.tui_model import Settings


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
        run.assert_called_once_with(Path('/case folder'), watch=True)
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

    def test_display_removes_terminal_control_sequences(self):
        app = self.app()
        app.write(4, '/case\x1b[2J\nname')
        self.assertNotIn('\x1b', app.screen.lines[4])
        self.assertNotIn('\n', app.screen.lines[4])

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
