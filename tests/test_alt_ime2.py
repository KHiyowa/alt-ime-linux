import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alt_ime2 import KeyboardBridge, main, run_bridge, LEFTALT as L, RIGHTALT as R


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.bridge = KeyboardBridge(lambda c, v: self.events.append((c, v)), double_ms=200)

    def key(self, code, value, ms):
        self.bridge.key(code, value, ms / 1000)

    def tap(self, code, ms):
        self.key(code, 1, ms)
        self.key(code, 0, ms + 10)

    def test_single_taps(self):
        self.tap(L, 0)
        self.tap(R, 50)
        self.assertEqual(self.events, [(94, 1), (94, 0), (92, 1), (92, 0)])

    def test_double_uses_press_start(self):
        for alt, conversion in [(L, 94), (R, 92)]:
            with self.subTest(alt=alt):
                self.setUp()
                self.key(alt, 1, 0)
                self.key(alt, 0, 150)
                self.tap(alt, 199)
                self.assertEqual(self.events, [(conversion, 1), (conversion, 0), (alt, 1), (alt, 0)])

    def test_boundary_is_not_double(self):
        self.tap(L, 123456700)
        self.tap(L, 123456900)
        self.assertEqual(self.events, [(94, 1), (94, 0), (94, 1), (94, 0)])

    def test_alternating_taps_independent(self):
        self.tap(L, 0)
        self.tap(R, 40)
        self.tap(L, 80)
        self.assertEqual(self.events, [(94, 1), (94, 0), (92, 1), (92, 0), (L, 1), (L, 0)])

    def test_alt_tab_and_repeat(self):
        self.key(L, 1, 0)
        self.key(L, 2, 10)
        self.key(15, 1, 20)
        self.key(15, 2, 30)
        self.key(15, 0, 40)
        self.key(L, 0, 50)
        self.assertEqual(self.events, [(L, 1), (15, 1), (15, 2), (15, 0), (L, 0)])

    def test_shift_conversion_both_sides(self):
        for alt, conversion in [(L, 94), (R, 92)]:
            with self.subTest(alt=alt):
                self.setUp()
                self.key(42, 1, 0)
                self.tap(alt, 10)
                self.key(42, 0, 30)
                self.assertEqual(self.events, [(42, 1), (conversion, 1),
                                               (conversion, 0), (42, 0)])

    def test_shift_released_before_alt(self):
        self.key(L, 1, 0)
        self.key(54, 1, 10)
        self.key(54, 0, 20)
        self.key(L, 0, 30)
        self.assertEqual(self.events, [(54, 1), (54, 0), (42, 1),
                                       (94, 1), (94, 0), (42, 0)])

    def test_other_key_already_held_is_chord(self):
        for code in [29, 97, 125, 126, 30]:
            with self.subTest(code=code):
                self.setUp()
                self.key(code, 1, 0)
                self.tap(L, 10)
                self.key(code, 0, 30)
                self.assertEqual(self.events, [(code, 1), (L, 1), (L, 0), (code, 0)])

    def test_both_alts_held_are_chord(self):
        self.key(L, 1, 0)
        self.key(R, 1, 10)
        self.key(L, 0, 20)
        self.key(R, 0, 30)
        self.assertEqual(self.events, [(L, 1), (R, 1), (L, 0), (R, 0)])

    def test_intervening_key_invalidates_double(self):
        self.tap(L, 0)
        self.tap(30, 30)
        self.tap(L, 60)
        self.assertEqual(self.events, [(94, 1), (94, 0), (30, 1), (30, 0), (94, 1), (94, 0)])

    def test_release_only_output_keys(self):
        self.key(L, 1, 0)
        self.bridge.release()
        self.assertEqual(self.events, [])
        self.key(R, 1, 30)
        self.key(30, 1, 40)
        self.bridge.release()
        self.assertEqual(set(self.events[-2:]), {(R, 0), (30, 0)})
        self.assertEqual(self.bridge.output_held, set())

    def test_stray_release_and_duplicate_down(self):
        self.key(L, 0, 0)
        self.key(L, 1, 10)
        self.key(L, 1, 20)
        self.key(L, 0, 30)
        self.assertEqual(self.events, [(94, 1), (94, 0)])


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.dev = Mock(name='device')
        self.dev.name = 'Physical keyboard'
        self.dev.fd = 10
        self.dev.capabilities.side_effect = lambda: {0: [], 1: [L, R, 30], 21: []}
        self.dev.active_keys.return_value = []
        self.dev.read_loop.return_value = []
        self.ui = Mock()
        self.evdev = SimpleNamespace(
            InputDevice=Mock(return_value=self.dev), UInput=Mock(return_value=self.ui),
            ecodes=SimpleNamespace(EV_SYN=0, EV_KEY=1, EV_FF=21, SYN_DROPPED=3))
        self.ioctl = patch('fcntl.ioctl').start()
        self.addCleanup(patch.stopall)

    def run_bridge(self):
        run_bridge(self.evdev, '/dev/input/test', 200)

    def event(self, code, value):
        return SimpleNamespace(type=1, code=code, value=value, timestamp=lambda: 1.0)

    def test_capabilities_and_normal_cleanup(self):
        self.run_bridge()
        caps = self.evdev.UInput.call_args.args[0]
        self.assertTrue({92, 94}.issubset(caps[1]))
        self.assertNotIn(0, caps)
        self.assertNotIn(21, caps)
        self.dev.ungrab.assert_called_once()
        self.dev.close.assert_called_once()
        self.ui.close.assert_called_once()

    def test_held_startup_key_fails_safe(self):
        self.dev.active_keys.return_value = [L]
        with self.assertRaisesRegex(RuntimeError, 'Release all keys'):
            self.run_bridge()
        self.dev.ungrab.assert_called_once()

    def test_uinput_creation_failure_closes_device_without_grab(self):
        self.evdev.UInput.side_effect = PermissionError('uinput')
        with self.assertRaises(PermissionError):
            self.run_bridge()
        self.dev.close.assert_called_once()
        self.dev.grab.assert_not_called()

    def test_grab_failure_closes_virtual_device(self):
        self.dev.grab.side_effect = OSError('busy')
        with self.assertRaises(OSError):
            self.run_bridge()
        self.dev.ungrab.assert_not_called()
        self.ui.close.assert_called_once()

    def test_syn_dropped_releases_device(self):
        self.dev.read_loop.return_value = [SimpleNamespace(type=0, code=3)]
        with self.assertRaisesRegex(RuntimeError, 'events lost'):
            self.run_bridge()
        self.dev.ungrab.assert_called_once()

    def test_disconnect_releases_output_keys(self):
        def events():
            yield self.event(L, 1)
            yield self.event(30, 1)
            raise OSError('disconnected')
        self.dev.read_loop.return_value = events()
        with self.assertRaises(OSError):
            self.run_bridge()
        writes = [c.args for c in self.ui.write.call_args_list]
        self.assertIn((1, L, 0), writes)
        self.assertIn((1, 30, 0), writes)
        self.dev.ungrab.assert_called_once()

    def test_output_failure_still_releases_physical_grab(self):
        self.dev.read_loop.return_value = [self.event(30, 1), self.event(30, 0)]
        self.ui.write.side_effect = [None, OSError('write'), OSError('cleanup')]
        with self.assertRaises(OSError):
            self.run_bridge()
        self.dev.ungrab.assert_called_once()
        self.dev.close.assert_called_once()
        self.ui.close.assert_called_once()

    def test_rejects_virtual_keyboard(self):
        self.dev.name = 'Alt-IME2 Virtual Keyboard'
        with self.assertRaisesRegex(RuntimeError, 'virtual keyboard'):
            self.run_bridge()
        self.dev.close.assert_called_once()
        self.dev.grab.assert_not_called()


    def test_interrupt_releases_grab_and_output_keys(self):
        def events():
            yield self.event(30, 1)
            raise KeyboardInterrupt
        self.dev.read_loop.return_value = events()
        with self.assertRaises(KeyboardInterrupt):
            self.run_bridge()
        self.assertIn((1, 30, 0), [c.args for c in self.ui.write.call_args_list])
        self.dev.ungrab.assert_called_once()
        self.ui.close.assert_called_once()


class CliTests(unittest.TestCase):
    def test_removed_switch_options_are_rejected(self):
        for option, value in [('--switch', 'atok'), ('--us', 'keyboard-us'), ('--atok', 'atokx3')]:
            with self.subTest(option=option), patch('alt_ime2.load_evdev'), \
                    patch('subprocess.run', side_effect=AssertionError('No external commands')), \
                    self.assertRaises(SystemExit) as exc:
                main(['--device', '/dev/input/test', option, value])
            self.assertEqual(exc.exception.code, 2)

    def test_check_does_not_create_virtual_device_or_run_commands(self):
        evdev = Mock()
        dev = evdev.InputDevice.return_value
        dev.name = 'Physical keyboard'
        dev.capabilities.return_value = {1: [L, R]}
        evdev.ecodes.EV_KEY = 1
        with patch('alt_ime2.load_evdev', return_value=evdev), patch('builtins.open'), \
                patch('subprocess.run', side_effect=AssertionError('No external commands')), \
                patch.dict('os.environ', {}, clear=True):
            self.assertEqual(main(['--check', '--device', '/dev/input/test']), 0)
            evdev.UInput.assert_not_called()
            dev.grab.assert_not_called()

    def test_list_devices_does_not_require_fcitx(self):
        evdev = Mock()
        evdev.list_devices.return_value = []
        with patch('alt_ime2.load_evdev', return_value=evdev), \
                patch('subprocess.run', side_effect=AssertionError('No external commands')), \
                patch.dict('os.environ', {}, clear=True):
            self.assertEqual(main(['--list-devices']), 0)

    def test_double_ms_must_be_positive(self):
        for value in ['0', '-1']:
            with self.subTest(value=value), self.assertRaises(SystemExit):
                main(['--double-ms', value, '--list-devices'])


if __name__ == '__main__':
    unittest.main()
