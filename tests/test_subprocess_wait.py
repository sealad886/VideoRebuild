"""Subprocess completion preserves limits and joins every owned waiter."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event as RealEvent, enumerate as threads
import unittest
from unittest.mock import patch

from videorebuild import validate
from videorebuild.storage import SafetyError


class InterruptedEvent:
    def __init__(self):
        self.event = RealEvent()

    def set(self):
        self.event.set()

    def wait(self, timeout=None):
        raise KeyboardInterrupt


class SubprocessWaitTests(unittest.TestCase):
    def setUp(self):
        self.launched = []
        self.waiter_threads = {thread.ident for thread in threads()}
        self.popen = subprocess.Popen
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for process, _ in self.launched:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def launch(self, *args, **kwargs):
        process = self.popen(*args, **kwargs)
        self.launched.append((process, (kwargs['stdout'], kwargs['stderr'])))
        return process

    def run_child(self, code, *, timeout=5, output_limit=2*1024*1024):
        with patch.object(validate.subprocess, 'Popen', self.launch):
            return validate.run_tool([sys.executable, '-B', '-c', code], timeout, output_limit)

    def assert_reaped(self):
        self.assertTrue(self.launched)
        for process, outputs in self.launched:
            self.assertIsNotNone(process.returncode)
            self.assertTrue(all(output.closed for output in outputs))
        self.assertFalse([thread for thread in threads()
                          if thread.ident not in self.waiter_threads and thread.name == 'videorebuild-tool-wait'])

    def test_real_success_has_no_shell_and_preserves_environment_and_devnull(self):
        code = '''import json,os,sys
print(json.dumps({'stdin':sys.stdin.read(),'color':os.environ.get('AV_LOG_FORCE_NOCOLOR'),
                  'ffreport':os.environ.get('FFREPORT'),'arg':sys.argv[1]}))'''
        argument = '$HOME; $(touch must-not-run)'
        argv = [sys.executable, '-B', '-c', code, argument]
        with patch.dict(os.environ, {'FFREPORT': 'private-file.log'}), \
                patch.object(validate.subprocess, 'Popen', self.launch):
            result = validate.run_tool(argv, 5)
            self.assertEqual(os.environ['FFREPORT'], 'private-file.log')
        self.assertEqual(json.loads(result['stdout']),
                         {'stdin': '', 'color': '1', 'ffreport': None, 'arg': argument})
        self.assertEqual(result['argv'], argv)
        self.assertEqual(result['returncode'], 0)
        self.assertFalse(result['timeout'])
        self.assert_reaped()

    def test_real_nonzero_exit_and_bounded_binary_output(self):
        result = self.run_child('import os,sys;os.write(1,b"\\xff"*100);'
                                'os.write(2,b"E"*70000);sys.exit(7)', output_limit=23)
        self.assertEqual(result['returncode'], 7)
        self.assertFalse(result['timeout'])
        self.assertEqual(result['stdout'], '\ufffd'*23)
        self.assertEqual(result['stderr'], 'E'*(64*1024))
        self.assertTrue(result['stdout_truncated'])
        self.assertTrue(result['stderr_truncated'])
        self.assert_reaped()

    def test_deadline_kills_and_reaps_real_child_before_spool_closes(self):
        result = self.run_child('from threading import Event;Event().wait()', timeout=0.05)
        self.assertTrue(result['timeout'])
        self.assertLess(result['returncode'], 0)
        self.assert_reaped()

    def test_main_thread_interruption_kills_reaps_and_joins(self):
        states = []

        def launch(*args, **kwargs):
            process = self.launch(*args, **kwargs)
            original = process.wait

            def wait(*wait_args, **wait_kwargs):
                result = original(*wait_args, **wait_kwargs)
                states.append(tuple(output.closed for output in self.launched[-1][1]))
                return result

            process.wait = wait
            return process

        with patch.object(validate, 'Event', InterruptedEvent), \
                patch.object(validate.subprocess, 'Popen', launch):
            with self.assertRaises(KeyboardInterrupt):
                validate.run_tool([sys.executable, '-B', '-c', 'from threading import Event;Event().wait()'], 5)
        self.assertTrue(states)
        self.assertTrue(all(state == (False, False) for state in states))
        self.assert_reaped()

    def test_waiter_error_propagates_after_child_cleanup(self):
        failure = OSError('synthetic wait failure')

        def launch(*args, **kwargs):
            process = self.launch(*args, **kwargs)
            original = process.wait
            calls = []

            def wait(*wait_args, **wait_kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise failure
                return original(*wait_args, **wait_kwargs)

            process.wait = wait
            return process

        with patch.object(validate.subprocess, 'Popen', launch):
            with self.assertRaises(OSError) as raised:
                validate.run_tool([sys.executable, '-B', '-c', 'from threading import Event;Event().wait()'], 5)
        self.assertIs(raised.exception, failure)
        self.assert_reaped()

    def test_waiter_launch_failure_still_kills_and_reaps_child(self):
        class FailedThread:
            ident = None

            def __init__(self, **kwargs):
                pass

            def start(self):
                raise RuntimeError('synthetic thread launch failure')

            def join(self):
                raise AssertionError('an unstarted thread cannot be joined')

        with patch.object(validate, 'Thread', FailedThread), \
                patch.object(validate.subprocess, 'Popen', self.launch):
            with self.assertRaises(RuntimeError):
                validate.run_tool([sys.executable, '-B', '-c', 'from threading import Event;Event().wait()'], 5)
        self.assert_reaped()

    def test_missing_executable_is_clear_and_leaves_no_waiter(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory)/'missing-tool')
            with self.assertRaises(SafetyError) as raised:
                validate.run_tool([missing], 1)
        self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)
        self.assertFalse([thread for thread in threads()
                          if thread.ident not in self.waiter_threads and thread.name == 'videorebuild-tool-wait'])


if __name__ == '__main__':
    unittest.main()
