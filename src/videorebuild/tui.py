"""Small, dependency-free terminal menus over the canonical recovery CLI."""
from __future__ import annotations

from pathlib import Path
import shlex
import sys
import textwrap
import time

from . import __version__
from .tui_model import Settings, build_arguments, snapshot


def clean_text(value: object) -> str:
    """Display paths and logs as text, never as terminal control sequences."""
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


class Application:
    def __init__(self, screen, terminal, settings: Settings, *, watch: bool = False,
                 no_progress: bool = False):
        self.screen, self.terminal, self.settings = screen, terminal, settings
        self.watching = watch
        self.no_progress = no_progress
        self.selected = len(self.menu_rows()) - 2 if settings.case else 0
        self.message = ''
        self.highlight = terminal.A_REVERSE

    def write(self, y: int, text: object, *, selected: bool = False) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= y < height - 1 and width > 2:
            try:
                self.screen.addnstr(y, 2, clean_text(text), width - 3,
                                    self.highlight if selected else 0)
            except self.terminal.error:
                # A resize can invalidate coordinates between getmaxyx and draw.
                pass

    def frame(self, title: str, footer: str) -> tuple[int, int]:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        self.write(0, f'VideoRebuild {__version__} - MP4 Recovery')
        self.write(1, '=' * max(0, width - 4))
        self.write(2, title)
        self.write(height - 3, '-' * max(0, width - 4))
        self.write(height - 2, footer)
        return height, width

    def menu_rows(self) -> list[str]:
        s = self.settings
        return [
            f'Case/output folder       : {s.case or "(choose a folder)"}',
            f'Recovered files          : {s.input or "(optional file or folder)"}',
            f'Raw image file           : {s.image or "(optional regular image file)"}',
            f'Unallocated policy       : {s.allocation_map or "(none; not free-space restricted)"}',
            f'Deep sample search       : {"ON" if s.deep else "OFF"}',
            f'Codec validation         : {"OFF - structural export" if s.reconstruct_only else "ON - FFmpeg/ffprobe"}',
            f'Second-tier fallback     : {"ON" if s.include_recovered else "OFF"}',
            f'Retry unresolved jobs    : {"ON" if s.retry else "OFF"}',
            f'Search workers           : {s.search_workers} (Enter cycles 1-8)',
            '[ Recover existing index ]',
            '[ Scan inputs only       ]',
            '[ Scan + recover         ]',
            '[ Watch existing case    ]',
            '[ Quit                   ]',
        ]

    def draw_menu(self) -> None:
        height, _ = self.frame('Choose settings, then an action. Sources stay read-only.',
                               'Up/Down: select  Enter: edit/toggle/open  Q: quit')
        available = max(1, height - 9)
        first = max(0, self.selected - available + 1)
        for row, label in enumerate(self.menu_rows()[first:first + available], start=5):
            self.write(row, label, selected=row - 5 + first == self.selected)
        self.write(height - 4, self.message or 'Image scanning includes allocated bytes. Policy applies to Recover only.')
        self.screen.refresh()

    def prompt(self, label: str, value: str = '') -> str | None:
        """Optional text entry for a path jump or new case name."""
        self.screen.timeout(-1)
        try:
            while True:
                height, width = self.frame(label, 'Enter: save  Esc: cancel  Ctrl-U: clear')
                self.write(4, 'Type a value without surrounding quotes.')
                self.write(6, '> ' + clean_text(value)[-max(1, width - 8):])
                self.screen.refresh()
                key = self.screen.get_wch()
                if key in ('\n', '\r', self.terminal.KEY_ENTER):
                    return value
                if key == '\x1b':
                    return None
                if key == '\x15':
                    value = ''
                elif key in ('\b', '\x7f', self.terminal.KEY_BACKSPACE):
                    value = value[:-1]
                elif isinstance(key, str) and key.isprintable():
                    value += key
        finally:
            self.screen.timeout(250)

    def edit(self, field: str, label: str) -> None:
        """PhotoRec-style selection: Enter navigates, C chooses a directory."""
        from .tui_browser import Browser
        browser = Browser(getattr(self.settings, field))
        directory_allowed = field in {'case', 'input'}
        message = ''
        self.screen.timeout(-1)
        try:
            while True:
                height, _ = self.frame(label + ' - filesystem browser',
                    'Up/Down: move  Enter/Right: open  Left: parent  Q/Esc: cancel')
                self.write(4, f'Directory: {browser.current}')
                choices = 'C: choose directory  ' if directory_allowed else 'Enter: choose file  '
                choices += 'G: go to path  R: refresh'
                if field == 'case':
                    choices += '  N: new case name'
                else:
                    choices += '  D: clear'
                self.write(5, choices)
                available = max(1, height - 12)
                first = max(0, browser.selected - available + 1)
                for row, entry in enumerate(browser.entries[first:first + available], 7):
                    kind = '[dir]' if entry.directory else '[file]' if entry.regular_file else '[other]'
                    self.write(row, f'{kind:<7} {entry.name}', selected=row - 7 + first == browser.selected)
                self.write(height - 5, f'Selected: {browser.entries[browser.selected].path}')
                self.write(height - 4, message or browser.error or 'Browsing changes no files. Selection returns to the menu.')
                self.screen.refresh()
                key = self.screen.get_wch()
                message = ''
                if key in ('q', 'Q', '\x1b'):
                    return
                if key in (self.terminal.KEY_UP, 'k'):
                    browser.selected = max(0, browser.selected - 1)
                elif key in (self.terminal.KEY_DOWN, 'j'):
                    browser.selected = min(len(browser.entries) - 1, browser.selected + 1)
                elif key in (getattr(self.terminal, 'KEY_NPAGE', -100),):
                    browser.selected = min(len(browser.entries) - 1, browser.selected + available)
                elif key in (getattr(self.terminal, 'KEY_PPAGE', -101),):
                    browser.selected = max(0, browser.selected - available)
                elif key in (self.terminal.KEY_LEFT, self.terminal.KEY_BACKSPACE, '\b', '\x7f'):
                    browser.enter(browser.current.parent)
                elif key in ('r', 'R'):
                    browser.refresh()
                elif key in ('g', 'G'):
                    value = self.prompt('Go to directory or file', str(browser.current))
                    self.screen.timeout(-1)
                    if value:
                        browser.jump(value)
                elif key in ('n', 'N') and field == 'case':
                    value = self.prompt('New case folder name (created when recovery starts)')
                    self.screen.timeout(-1)
                    if value is not None:
                        if not value or value in {'.', '..'} or Path(value).name != value or '/' in value:
                            message = 'Enter a single new folder name; do not include a path.'
                        else:
                            target = browser.current / value
                            if target.exists() or target.is_symlink():
                                message = 'That name already exists. Choose it in the list or use another name.'
                            else:
                                setattr(self.settings, field, str(target))
                                self.message = 'New case path selected; it will be created only when an operation starts.'
                                return
                elif key in ('d', 'D') and field != 'case':
                    setattr(self.settings, field, '')
                    self.message = ''
                    return
                elif key in ('c', 'C'):
                    if directory_allowed:
                        setattr(self.settings, field, str(browser.current))
                        self.message = ''
                        return
                    message = 'Choose a regular file with Enter; this field does not accept directories.'
                elif key in ('\n', '\r', self.terminal.KEY_ENTER, self.terminal.KEY_RIGHT):
                    entry = browser.entries[browser.selected]
                    if entry.directory:
                        browser.enter(entry.path)
                    elif key == self.terminal.KEY_RIGHT:
                        message = 'Right opens directories. Press Enter to choose a file.'
                    elif entry.regular_file and field != 'case':
                        try:
                            target = entry.path.resolve(strict=True)
                            if not target.is_file():
                                raise ValueError('Selection is no longer a regular file')
                            setattr(self.settings, field, str(target))
                            self.message = ''
                            return
                        except (OSError, RuntimeError, ValueError) as error:
                            message = f'Cannot select file: {error}. Refresh or choose another entry.'
                    else:
                        message = ('Choose a directory for the case.' if field == 'case' else
                                   'Choose a regular file or directory.' if field == 'input' else
                                   'Choose a regular file.')
        finally:
            self.screen.timeout(250)

    def review(self, arguments: list[str]) -> bool:
        """Require deliberate selection of Start; Enter initially cancels."""
        start = False
        self.screen.timeout(-1)
        try:
            while True:
                height, width = self.frame('Review command before starting',
                                           'Left/Right: choose  Enter: confirm  Esc: cancel')
                command = 'videorebuild ' + shlex.join(arguments)
                wrapped = textwrap.wrap(clean_text(command), max(10, width - 5))
                for row, line in enumerate(wrapped[:max(1, height - 12)], 4):
                    self.write(row, line)
                if height < 18 or width < 50 or len(wrapped) > max(1, height - 12):
                    self.write(max(3, height - 8), 'Enlarge terminal (at least 50 x 18) to review and start.')
                    can_start = False
                else:
                    can_start = True
                self.write(height - 7, 'Recovery runs in the foreground and writes run.log.')
                self.write(height - 6, 'Ctrl-C there interrupts this operation; run the same command to resume.')
                self.write(height - 5, '[ Start ]' if start else '[ Cancel ]', selected=True)
                self.screen.refresh()
                key = self.screen.get_wch()
                if key in ('\x1b', 'q', 'Q'):
                    return False
                if key in (self.terminal.KEY_LEFT, self.terminal.KEY_RIGHT, '\t'):
                    start = not start
                elif key in ('\n', '\r', self.terminal.KEY_ENTER):
                    return start and can_start
        finally:
            self.screen.timeout(250)

    def execute(self, arguments: list[str]) -> None:
        """Hand off to the existing CLI, restoring the terminal on every return."""
        from .cli import main
        self.terminal.def_prog_mode()
        self.terminal.endwin()
        try:
            print('\n' + clean_text('videorebuild ' + shlex.join(arguments)), flush=True)
            result = main(arguments)
            print(f'\nOperation exited with code {result}. Check case reports for recovery scope.', flush=True)
            try:
                input('Press Enter to return to the menu. ')
            except (EOFError, KeyboardInterrupt):
                pass
            self.message = f'Last operation exit code: {result}. Watch case or review reports for details.'
        finally:
            self.terminal.reset_prog_mode()
            self.screen.clear()
            self.screen.refresh()

    def launch(self, action: str) -> None:
        try:
            arguments = build_arguments(self.settings, action)
            if self.no_progress:
                arguments.insert(0, '--no-progress')
            observed = snapshot(self.settings.case)
            if observed['busy']:
                self.message = 'Another process owns this case. Choose Watch existing case to observe it.'
                return
            if self.review(arguments):
                self.execute(arguments)
        except (OSError, ValueError) as error:
            self.message = clean_text(error)

    def draw_watch(self, observed: dict) -> None:
        height, _ = self.frame('Watch case - read-only; no process control',
                               'R: refresh  Q/Esc: return to menu; recovery keeps running')
        self.write(4, f'Case: {observed["case"]}')
        busy = observed['busy']
        lock = ('writer lock held' if busy is True else
                'no writer lock held; completion unverified' if busy is False else 'writer lock unknown')
        self.write(5, f'Status: {lock}')
        free = observed['free_bytes']
        storage = f'{free / 1024**3:.1f} GiB free' if free is not None else 'free space unavailable'
        sources = observed['sources']
        self.write(6, f'Sources: {sources if sources is not None else "unknown"} | Output storage: {storage}')
        counts = ', '.join(f'{clean_text(status)}={count:,}' for status, count in observed['jobs'].items())
        self.write(7, f'Jobs: {counts or "no job counts available"}')
        self.write(8, f'Report: {observed["report_path"]}')
        modified = observed['log_mtime']
        age = f'{max(0, time.time() - modified):.0f}s ago' if modified is not None else 'unavailable'
        self.write(9, f'Latest log update: {age}; observations do not establish completion.')
        if observed['error']:
            self.write(10, observed['error'])
        self.write(11, 'Recent run.log:')
        lines = observed['log_lines'][-max(0, height - 16):] if height > 16 else []
        for row, line in enumerate(lines, 12):
            self.write(row, line)
        self.screen.refresh()

    def loop(self) -> int:
        self.screen.keypad(True)
        self.screen.timeout(250)
        try:
            self.terminal.curs_set(0)
        except self.terminal.error:
            pass
        if self.terminal.has_colors():
            try:
                self.terminal.start_color()
                self.terminal.init_pair(1, self.terminal.COLOR_WHITE, self.terminal.COLOR_BLUE)
                self.screen.bkgd(' ', self.terminal.color_pair(1))
            except self.terminal.error:
                pass  # Reverse-video selection also works on monochrome terminals.
        observed, refreshed = None, 0.0
        while True:
            if self.watching:
                if observed is None or time.monotonic() - refreshed >= 2:
                    observed = snapshot(self.settings.case)
                    refreshed = time.monotonic()
                self.draw_watch(observed)
            else:
                self.draw_menu()
            try:
                key = self.screen.get_wch()
            except self.terminal.error:
                continue
            if self.watching:
                if key in ('q', 'Q', '\x1b'):
                    self.watching, observed = False, None
                elif key in ('r', 'R'):
                    observed = None
                continue
            if key in ('q', 'Q', '\x1b'):
                return 0
            if key in (self.terminal.KEY_UP, 'k'):
                self.selected = (self.selected - 1) % len(self.menu_rows())
            elif key in (self.terminal.KEY_DOWN, 'j', '\t'):
                self.selected = (self.selected + 1) % len(self.menu_rows())
            elif key in ('\n', '\r', self.terminal.KEY_ENTER, ' '):
                if self.selected < 4:
                    field = ('case', 'input', 'image', 'allocation_map')[self.selected]
                    self.edit(field, self.menu_rows()[self.selected].split(':')[0].strip())
                elif self.selected < 8:
                    field = ('deep', 'reconstruct_only', 'include_recovered', 'retry')[self.selected - 4]
                    setattr(self.settings, field, not getattr(self.settings, field))
                elif self.selected == 8:
                    self.settings.search_workers = self.settings.search_workers % 8 + 1
                elif self.selected < 12:
                    self.launch(('recover', 'scan', 'run')[self.selected - 9])
                elif self.selected == 12:
                    if self.settings.case:
                        self.watching, observed = True, None
                    else:
                        self.message = 'Choose a case folder first.'
                else:
                    return 0


def run(case: Path | None = None, *, watch: bool = False, no_progress: bool = False) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('ERROR: The TUI needs an interactive terminal. Use run/recover --help for scriptable commands.', file=sys.stderr)
        return 2
    if watch and case is None:
        print('ERROR: Watch mode needs --case PATH. Choose an existing case folder.', file=sys.stderr)
        return 2
    try:
        import curses
    except ImportError:
        print('ERROR: Python curses support is unavailable. Use the regular run/recover commands.', file=sys.stderr)
        return 2
    try:
        return curses.wrapper(lambda screen: Application(screen, curses, Settings(case=str(case) if case else ''),
                                                          watch=watch, no_progress=no_progress).loop())
    except curses.error as error:
        print(f'ERROR: Could not use this terminal: {clean_text(error)}. Check TERM or use the regular CLI.', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
