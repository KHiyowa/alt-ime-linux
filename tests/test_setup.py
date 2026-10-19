import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import setup


class TemporaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp/opencode')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


class ManagedFileTests(TemporaryTests):
    def manager(self):
        return setup.ManagedFiles(self.root, self.root / 'state/install.json', {'config/a'})

    def test_install_upgrade_and_remove(self):
        manager = self.manager()
        manager.install({'config/a': (b'first', 0o644)})
        manager = self.manager()
        manager.install({'config/a': (b'second', 0o600)})
        self.assertEqual((self.root / 'config/a').read_bytes(), b'second')
        self.assertEqual((self.root / 'config/a').stat().st_mode & 0o777, 0o600)
        manager.remove()
        self.assertFalse((self.root / 'config/a').exists())
        manager.remove()  # Idempotent.

    def test_unrelated_existing_file_is_not_overwritten(self):
        setup.atomic_write(self.root / 'config/a', b'unrelated')
        with self.assertRaisesRegex(RuntimeError, '上書きしません'):
            self.manager().install({'config/a': (b'new', 0o600)})
        self.assertEqual((self.root / 'config/a').read_bytes(), b'unrelated')

    def test_identical_preexisting_file_is_not_claimed_or_removed(self):
        setup.atomic_write(self.root / 'config/a', b'same')
        manager = self.manager()
        manager.install({'config/a': (b'same', 0o600)})
        manager.remove()
        self.assertTrue((self.root / 'config/a').exists())

    def test_edited_file_preserved_with_provenance(self):
        manager = self.manager()
        manager.install({'config/a': (b'installed', 0o600)})
        (self.root / 'config/a').write_bytes(b'edited')
        with self.assertRaises(RuntimeError):
            manager.install({'config/a': (b'upgrade', 0o600)})
        manager.remove()
        self.assertEqual((self.root / 'config/a').read_bytes(), b'edited')
        self.assertIn('config/a', self.manager().state['files'])

    def test_parent_symlink_is_rejected(self):
        target = self.root / 'target'
        target.mkdir()
        (self.root / 'config').symlink_to(target, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.manager().install({'config/a': (b'bad', 0o600)})
        self.assertFalse((target / 'a').exists())

    def test_manifest_cannot_inject_deletion_path(self):
        setup.atomic_write(self.root / 'state/install.json',
                           json.dumps({'files': {'../../important': 'x'}}).encode())
        with self.assertRaises(RuntimeError):
            self.manager()

    def test_failed_write_does_not_claim_unwritten_file(self):
        manager = self.manager()
        real_write = setup.atomic_write

        def write(path, data, mode=0o600):
            if path == self.root / 'config/a':
                raise OSError('full')
            return real_write(path, data, mode)

        with patch.object(setup, 'atomic_write', side_effect=write):
            with self.assertRaises(OSError):
                manager.install({'config/a': (b'new', 0o600)})
        self.assertEqual(self.manager().state['files'], {})


class SystemSetupTests(TemporaryTests):
    def setUp(self):
        super().setUp()
        self.runner = Mock()
        self.members = []
        patcher = patch.object(setup.pwd, 'getpwnam', return_value=SimpleNamespace(pw_gid=1000))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(setup.grp, 'getgrnam', side_effect=lambda _: SimpleNamespace(
            gr_gid=104, gr_mem=self.members))
        patcher.start()
        self.addCleanup(patcher.stop)

    def action(self, action, user='alice', remove=False):
        setup.system_setup(action, user, remove, root=self.root, runner=self.runner)

    def commands(self):
        return [c.args[0] for c in self.runner.call_args_list]

    def test_install_and_explicit_full_uninstall(self):
        self.action('install')
        self.assertIn(['usermod', '-aG', 'input', 'alice'], self.commands())
        self.assertTrue((self.root / setup.MODULE).exists())
        self.members.append('alice')
        self.action('uninstall', remove=True)
        self.assertIn(['gpasswd', '-d', 'alice', 'input'], self.commands())
        self.assertFalse((self.root / setup.MODULE).exists())
        self.assertFalse((self.root / setup.RULE).exists())

    def test_preexisting_group_membership_is_never_removed(self):
        self.members.append('alice')
        self.action('install')
        self.action('uninstall', remove=True)
        self.assertFalse(any(c[0] in ('usermod', 'gpasswd') for c in self.commands()))

    def test_default_keeps_group_then_explicit_cleanup_works(self):
        self.action('install')
        self.members.append('alice')
        self.action('uninstall')
        self.assertNotIn(['gpasswd', '-d', 'alice', 'input'], self.commands())
        self.action('uninstall', remove=True)
        self.assertIn(['gpasswd', '-d', 'alice', 'input'], self.commands())

    def test_does_not_remove_files_used_by_another_user(self):
        self.action('install', 'alice')
        self.action('install', 'bob')
        self.action('uninstall', 'alice')
        self.assertTrue((self.root / setup.RULE).exists())
        self.action('uninstall', 'bob')
        self.assertFalse((self.root / setup.RULE).exists())

    def test_existing_conflicting_rule_aborts_before_commands(self):
        setup.atomic_write(self.root / setup.RULE, b'unrelated rule')
        with self.assertRaises(RuntimeError):
            self.action('install')
        self.runner.assert_not_called()
        self.assertFalse((self.root / setup.MODULE).exists())

    def test_failed_modprobe_is_still_uninstallable(self):
        self.runner.side_effect = OSError('modprobe failure')
        with self.assertRaises(OSError):
            self.action('install')
        self.runner.side_effect = None
        self.action('uninstall')
        self.assertFalse((self.root / setup.RULE).exists())


class SelectionTests(unittest.TestCase):
    def test_single_device_is_automatic(self):
        self.assertEqual(setup.choose_device([{'path': '/dev/input/by-path/kbd'}]),
                         '/dev/input/by-path/kbd')

    def test_multiple_devices_require_selection_even_with_yes(self):
        devices = [{'path': 'one', 'name': 'First'}, {'path': 'two', 'name': 'Second'}]
        with self.assertRaises(RuntimeError):
            setup.choose_device(devices, interactive=False)
        self.assertEqual(setup.choose_device(devices, requested='two'), 'two')

    def test_empty_and_unstable_paths_rejected(self):
        with self.assertRaises(RuntimeError):
            setup.choose_device([])
        with self.assertRaises(RuntimeError):
            setup.choose_device([{'path': '/dev/input/by-path/kbd'}], '/dev/input/event0')

    def test_environment_escaping(self):
        self.assertEqual(setup.quote_environment('a "b"\\c'), '"a \\"b\\"\\\\c"')
        with self.assertRaises(RuntimeError):
            setup.quote_environment('a\nb')


class PreflightTests(unittest.TestCase):
    def test_installer_rejects_old_atok_option(self):
        with patch.object(setup, 'user_install'), patch.object(setup, 'exclusive_lock'), \
                self.assertRaises(SystemExit) as exc:
            setup.main(['install', '--atok', 'atokx3'])
        self.assertEqual(exc.exception.code, 2)

    def test_internal_actions_cannot_run_without_root(self):
        with patch.object(setup.os, 'geteuid', return_value=1000), \
                patch.object(setup, 'system_setup') as system:
            self.assertEqual(setup.main(['_system-install', 'alice']), 1)
            system.assert_not_called()

    def test_outer_script_refuses_sudo(self):
        with patch.object(setup.os, 'geteuid', return_value=0), \
                patch.object(setup, 'user_install') as install:
            self.assertEqual(setup.main(['install']), 1)
            install.assert_not_called()


class UserSetupTests(TemporaryTests):
    def args(self, **kwargs):
        defaults = dict(yes=True, device=None, remove_input_group=False)
        return SimpleNamespace(**(defaults | kwargs))

    def test_install_enables_without_start_and_uninstaller_survives_repo_removal(self):
        devices = [{'name': 'Keyboard', 'path': '/dev/input/by-path/test-event-kbd'}]
        with patch.dict('sys.modules', {'evdev': Mock()}), \
                patch.object(setup, 'run', return_value=SimpleNamespace(stdout='')) as run, \
                patch.object(setup, 'privileged', return_value=SimpleNamespace(stdout=json.dumps(devices))), \
                patch.object(setup.subprocess, 'run', return_value=SimpleNamespace(returncode=3)), \
                patch('builtins.print') as output:
            setup.user_install(self.args(), self.root)
        messages = '\n'.join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn('有効にするにはコンピューターを再起動してください', messages)
        self.assertNotIn('次回ログイン', messages)
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(['systemctl', '--user', 'enable', 'alt-ime2.service'], commands)
        self.assertFalse(any('--now' in c or 'start' in c for c in commands))
        self.assertTrue(all(c[0] == 'systemctl' for c in commands))
        self.assertTrue((self.root / setup.SETUP).exists())
        self.assertIn(str(self.root / setup.SETUP), (self.root / setup.UNINSTALL).read_text())
        self.assertNotIn('ALT_IME_ATOK', (self.root / setup.ENVIRONMENT).read_text())
        self.assertNotIn('--atok', (self.root / setup.SERVICE).read_text())
        self.assertNotIn('--us', (self.root / setup.SERVICE).read_text())

        with patch.object(setup, 'run') as run, patch.object(setup, 'privileged'), \
                patch.object(setup.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            setup.user_uninstall(self.args(), self.root)
        self.assertEqual(run.call_args_list[0].args[0],
                         ['systemctl', '--user', 'stop', 'alt-ime2.service'])
        for path in setup.USER_FILES:
            self.assertFalse((self.root / path).exists())

    def test_cancel_does_not_write_or_run_root_install(self):
        devices = [{'name': 'Keyboard', 'path': '/dev/input/by-path/test-event-kbd'}]
        with patch.dict('sys.modules', {'evdev': Mock()}), \
                patch.object(setup, 'run', return_value=SimpleNamespace(stdout='')), \
                patch.object(setup, 'privileged', return_value=SimpleNamespace(stdout=json.dumps(devices))) as root, \
                patch('builtins.input', return_value='n'):
            with self.assertRaises(RuntimeError):
                setup.user_install(self.args(yes=False), self.root)
        self.assertEqual([c.args for c in root.call_args_list], [('_devices',)])
        self.assertFalse((self.root / setup.PROGRAM).exists())

    def test_upgrade_replaces_tracked_direct_selection_service_and_environment(self):
        managed = setup.ManagedFiles(self.root, self.root / '.local/state/alt-ime2/install.json',
                                     setup.USER_FILES)
        managed.install({
            setup.ENVIRONMENT: (b'ALT_IME_DEVICE="/dev/input/by-path/test-event-kbd"\nALT_IME_ATOK=atokx3\n', 0o600),
            setup.SERVICE: (b'ExecStart=python3 alt_ime2.py --us keyboard-us --atok ${ALT_IME_ATOK}\n', 0o644),
        })
        devices = [{'name': 'Keyboard', 'path': '/dev/input/by-path/test-event-kbd'}]
        with patch.dict('sys.modules', {'evdev': Mock()}), \
                patch.object(setup, 'run', return_value=SimpleNamespace(stdout='')), \
                patch.object(setup, 'privileged', return_value=SimpleNamespace(stdout=json.dumps(devices))), \
                patch.object(setup.subprocess, 'run', return_value=SimpleNamespace(returncode=3)):
            setup.user_install(self.args(), self.root)
        self.assertNotIn('ATOK', (self.root / setup.ENVIRONMENT).read_text())
        self.assertNotIn('--atok', (self.root / setup.SERVICE).read_text())
        self.assertIn('変換', (self.root / setup.SETUP).read_text())


if __name__ == '__main__':
    unittest.main()
