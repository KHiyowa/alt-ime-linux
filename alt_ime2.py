#!/usr/bin/env python3
"""Generic Linux input-method keyboard bridge for KDE Wayland."""
import argparse
import logging
import signal

# Stable Linux input-event-codes.h values; the state machine needs no evdev.
LEFTALT, RIGHTALT = 56, 100
SHIFT = {42, 54}
ALT = {LEFTALT, RIGHTALT}
HENKAN = {LEFTALT: 94, RIGHTALT: 92}


class KeyboardBridge:
    """Transform key events; emit(code, value) is injected."""

    def __init__(self, emit, double_ms=200):
        self.emit = emit
        self.double_ms = double_ms
        self.held = set()
        self.output_held = set()
        self.pending = {}
        self.last_tap = {}

    def send(self, code, value):
        self.emit(code, value)
        if value == 1:
            self.output_held.add(code)
        elif value == 0:
            self.output_held.discard(code)

    def commit(self, code):
        state = self.pending[code]
        if not state['forwarded']:
            self.send(code, 1)
            state['forwarded'] = True

    def key(self, code, value, now):
        # evdev has microsecond resolution. Integer math avoids float boundary drift.
        now = round(now * 1_000_000)
        if code in ALT:
            if value == 1 and code not in self.held:
                # Other held keys (including the opposite Alt) are chords too.
                chord = bool(self.held - SHIFT)
                for other in self.pending:
                    self.commit(other)
                self.held.add(code)
                previous = self.last_tap.pop(code, float('-inf'))
                self.pending[code] = dict(
                    start=now, forwarded=False, shift=bool(self.held & SHIFT),
                    double=(now - previous) < self.double_ms * 1000)
                if chord:
                    self.commit(code)
            elif value == 0 and code in self.pending:
                self.held.discard(code)
                state = self.pending.pop(code)
                if state['forwarded']:
                    self.send(code, 0)
                elif state['double']:
                    self.send(code, 1)
                    self.send(code, 0)
                elif state['shift']:
                    # Keep Shift+conversion even if physical Shift was released first.
                    synthetic_shift = not (self.output_held & SHIFT)
                    if synthetic_shift:
                        self.send(42, 1)
                    self.send(HENKAN[code], 1)
                    self.send(HENKAN[code], 0)
                    if synthetic_shift:
                        self.send(42, 0)
                    self.last_tap[code] = state['start']
                else:
                    key = HENKAN[code]
                    self.send(key, 1)
                    self.send(key, 0)
                    self.last_tap[code] = state['start']
            elif value == 2 and code in self.pending:
                if self.pending[code]['forwarded']:
                    self.send(code, value)
            return

        if value == 1:
            if code in SHIFT:
                for state in self.pending.values():
                    state['shift'] = True
            else:
                self.last_tap.clear()
                for alt in self.pending:
                    self.commit(alt)
            self.held.add(code)
        elif value == 0:
            self.held.discard(code)
        self.send(code, value)

    def release(self):
        for code in sorted(self.output_held.copy()):
            self.send(code, 0)
        self.held.clear()
        self.pending.clear()
        self.last_tap.clear()


def load_evdev():
    try:
        import evdev
    except ImportError as exc:
        raise RuntimeError('Install python3-evdev: sudo apt install python3-evdev') from exc
    return evdev


def open_keyboard(evdev, path):
    dev = evdev.InputDevice(path)
    try:
        if dev.name == 'Alt-IME2 Virtual Keyboard':
            raise RuntimeError('Do not select the virtual keyboard')
        if not set(ALT).issubset(dev.capabilities().get(evdev.ecodes.EV_KEY, [])):
            raise RuntimeError('Selected device must report both Alt keys')
        return dev
    except BaseException:
        dev.close()
        raise


def run_bridge(evdev, path, double_ms):
    import fcntl
    import struct
    import time
    E = evdev.ecodes
    dev = open_keyboard(evdev, path)
    ui = None
    bridge = None
    grabbed = False
    try:
        # EVIOCSCLOCKID = _IOW('E', 0xa0, int). python-evdev has no public wrapper.
        fcntl.ioctl(dev.fd, 0x400445A0, struct.pack('i', time.CLOCK_MONOTONIC))
        caps = dev.capabilities()
        caps.pop(E.EV_SYN, None)
        caps.pop(E.EV_FF, None)
        caps[E.EV_KEY] = sorted(set(caps[E.EV_KEY]) | set(HENKAN.values()))
        # Conversion keys are often missing from a physical US keyboard.
        ui = evdev.UInput(caps, name='Alt-IME2 Virtual Keyboard')

        def emit(code, value):
            ui.write(E.EV_KEY, code, value)
            ui.syn()

        bridge = KeyboardBridge(emit, double_ms)
        dev.grab()
        grabbed = True
        if dev.active_keys():
            raise RuntimeError('Release all keys before starting the bridge')
        logging.info('Grabbed %s (%s). Stop with Ctrl+C or systemctl --user stop alt-ime2.',
                     dev.name, path)
        for event in dev.read_loop():
            if event.type == E.EV_SYN:
                if event.code == E.SYN_DROPPED:
                    raise RuntimeError('Input events lost; releasing keyboard for safety')
                continue
            if event.type == E.EV_KEY:
                bridge.key(event.code, event.value, event.timestamp())
            else:
                ui.write_event(event)
                ui.syn()
    finally:
        try:
            if bridge is not None:
                bridge.release()
        finally:
            try:
                if grabbed:
                    dev.ungrab()
            finally:
                dev.close()
                if ui is not None:
                    ui.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', help='Physical /dev/input/by-id or by-path keyboard')
    parser.add_argument('--double-ms', type=int, default=200)
    parser.add_argument('--debug', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list-devices', action='store_true', help='List keyboards without grabbing')
    mode.add_argument('--check', action='store_true', help='Check keyboard and uinput access without grabbing')
    args = parser.parse_args(argv)
    if args.double_ms <= 0:
        parser.error('--double-ms must be positive')
    if not args.list_devices and not args.device:
        parser.error('--device is required (or use --list-devices)')
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        evdev = load_evdev()
        if args.list_devices:
            for path in sorted(evdev.list_devices()):
                try:
                    dev = open_keyboard(evdev, path)
                except (OSError, RuntimeError):
                    continue
                print(f'{path}\t{dev.name}')
                dev.close()
            return 0
        if args.check:
            import os
            dev = open_keyboard(evdev, args.device)
            dev.close()
            with open('/dev/uinput', 'wb', buffering=0):
                pass
            logging.info('Keyboard and uinput access checks passed; device=%s (not grabbed)',
                         os.path.realpath(args.device))
            return 0

        def stop(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        run_bridge(evdev, args.device, args.double_ms)
        return 0
    except KeyboardInterrupt:
        logging.info('Stopped; keyboard released')
        return 0
    except (OSError, RuntimeError) as exc:
        logging.error('%s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
