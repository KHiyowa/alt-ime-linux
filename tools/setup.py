#!/usr/bin/env python3
"""Install/uninstall Alt-IME2. No package installation and no immediate input grab."""
import argparse
from contextlib import contextmanager
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent.parent
MODULE = 'etc/modules-load.d/alt-ime2.conf'
RULE = 'etc/udev/rules.d/70-alt-ime2-uinput.rules'
SYSTEM_FILES = {
    MODULE: '# Managed by alt-ime2 installer\nuinput\n',
    RULE: '# Managed by alt-ime2 installer\nKERNEL=="uinput", GROUP="input", MODE="0660"\n',
}
SERVICE = '.config/systemd/user/alt-ime2.service'
ENVIRONMENT = '.config/alt-ime2/environment'
PROGRAM = '.local/lib/alt-ime2/alt_ime2.py'
SETUP = '.local/lib/alt-ime2/setup.py'
UNINSTALL = '.local/bin/alt-ime2-uninstall'
USER_FILES = {SERVICE, ENVIRONMENT, PROGRAM, SETUP, UNINSTALL}


@contextmanager
def exclusive_lock(path):
    if path.parent.is_symlink():
        raise RuntimeError(f'管理記録のディレクトリがシンボリックリンクです: {path.parent}')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError(f'シンボリックリンクには書き込みません: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.alt-ime2-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ManagedFiles:
    """Journal files we create; never overwrite unrelated/locally changed files."""

    def __init__(self, root, manifest, allowed):
        self.root = Path(root)
        self.manifest = Path(manifest)
        self.allowed = set(allowed)
        if self.manifest.is_symlink():
            raise RuntimeError(f'不正な状態ファイル: {self.manifest}')
        self.state = json.loads(self.manifest.read_text()) if self.manifest.exists() else {}
        self.state.setdefault('files', {})
        if not set(self.state['files']).issubset(self.allowed):
            raise RuntimeError('状態ファイルに想定外のパスがあります')

    def path(self, name):
        if name not in self.allowed:
            raise RuntimeError(f'管理対象外のパス: {name}')
        path = self.root / name
        # Do not traverse redirected directories for privileged writes either.
        for parent in [path, *path.parents]:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise RuntimeError(f'シンボリックリンクは扱いません: {parent}')
        return path

    def save(self):
        atomic_write(self.manifest, (json.dumps(self.state, indent=2) + '\n').encode())

    def preflight(self, contents):
        for name, (data, mode) in contents.items():
            path = self.path(name)
            if not path.exists():
                continue
            actual = digest(path.read_bytes())
            expected = self.state['files'].get(name)
            if actual != (expected or digest(data)):
                raise RuntimeError(f'既存または編集済みファイルを上書きしません: {path}')

    def install(self, contents):
        self.preflight(contents)
        for name, (data, mode) in contents.items():
            path = self.path(name)
            if name not in self.state['files'] and path.exists():
                # Identical pre-existing files are reused, not claimed as ours.
                continue
            old = self.state['files'].get(name)
            self.state['files'][name] = digest(data)
            self.save()  # Journal before writing, including interrupted installations.
            try:
                atomic_write(path, data, mode)
            except BaseException:
                if old is not None:
                    self.state['files'][name] = old
                else:
                    self.state['files'].pop(name)
                self.save()
                raise

    def remove(self):
        for name, expected in list(self.state['files'].items()):
            path = self.path(name)
            if path.exists():
                if digest(path.read_bytes()) != expected:
                    print(f'編集済みのため保持: {path}', file=sys.stderr)
                    continue
                path.unlink()
            del self.state['files'][name]
        self.save()


def system_setup(action, username, remove_group=False, root=Path('/'), runner=run):
    """Root-side operations; root/runner injection is only used by unit tests."""
    with exclusive_lock(root / 'var/lib/alt-ime2/setup.lock'):
        _system_setup(action, username, remove_group, root, runner)


def _system_setup(action, username, remove_group, root, runner):
    account = pwd.getpwnam(username)
    group = grp.getgrnam('input')
    managed = ManagedFiles(root, root / 'var/lib/alt-ime2/install.json', SYSTEM_FILES)
    users = managed.state.setdefault('users', {})
    contents = {name: (text.encode(), 0o644) for name, text in SYSTEM_FILES.items()}
    if action == 'install':
        managed.preflight(contents)
        users.setdefault(username, {'group_added': False})
        managed.save()
        managed.install(contents)
        runner(['modprobe', 'uinput'])
        if account.pw_gid != group.gr_gid and username not in group.gr_mem:
            users[username]['group_added'] = True
            managed.save()
            runner(['usermod', '-aG', 'input', username])
    else:
        record = users.get(username, {})
        if remove_group and record.get('group_added'):
            if username in group.gr_mem:
                runner(['gpasswd', '-d', username, 'input'])
            record['group_added'] = False
        elif record.get('group_added'):
            print('inputグループ所属は保持します。不要なら --remove-input-group を指定して再実行してください。')
        # Retain group provenance for a later explicit cleanup, but not an active user.
        record['active'] = False
        if username in users:
            users[username] = record
        if not any(v.get('active', True) for v in users.values()):
            managed.remove()
        managed.save()
    if action == 'install':
        users[username]['active'] = True
        managed.save()
    runner(['udevadm', 'control', '--reload-rules'])
    runner(['udevadm', 'trigger', '--subsystem-match=misc'])
    # Do not unload uinput, delete the input group, or touch ATOK/Fcitx settings.


def discover_devices(paths=None):
    import evdev
    if paths is None:
        paths = sorted(Path('/dev/input/by-id').glob('*-event-kbd'))
        paths += sorted(Path('/dev/input/by-path').glob('*-event-kbd'))
    seen, devices = set(), []
    for path in paths:
        target = str(path.resolve())
        if target in seen:
            continue
        try:
            dev = evdev.InputDevice(str(path))
            try:
                keys = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
                if {56, 100}.issubset(keys) and dev.name != 'Alt-IME2 Virtual Keyboard':
                    devices.append({'path': str(path), 'name': dev.name})
                    seen.add(target)
            finally:
                dev.close()
        except OSError:
            continue
    return devices


def choose_device(devices, requested=None, interactive=True):
    if requested:
        for dev in devices:
            if dev['path'] == requested or (
                Path(requested).parent in (Path('/dev/input/by-id'), Path('/dev/input/by-path'))
                and Path(requested).is_symlink()
                and Path(requested).resolve() == Path(dev['path']).resolve()
            ):
                return requested
        raise RuntimeError('--device には検出された by-id/by-path のキーボードを指定してください')
    if len(devices) == 1:
        return devices[0]['path']
    if not devices:
        raise RuntimeError('左右Altを持つ安定したキーボードパスが見つかりません（接続とby-pathも確認してください）')
    for number, dev in enumerate(devices, 1):
        print(f"{number}: {dev['name']}\n   {dev['path']}")
    if not interactive:
        raise RuntimeError('キーボードが複数あります。--device で指定してください')
    try:
        number = int(input('使用するキーボードの番号: '))
    except ValueError as exc:
        raise RuntimeError('番号を指定してください') from exc
    if not 1 <= number <= len(devices):
        raise RuntimeError('番号が範囲外です')
    return devices[number - 1]['path']


def confirm(message, yes):
    print(message)
    if not yes and input('続行しますか? [y/N]: ').lower() not in ('y', 'yes'):
        raise RuntimeError('中止しました（設定変更なし）')


def privileged(*args, capture=False):
    return run(['sudo', '/usr/bin/python3', str(Path(__file__).resolve()), *args],
               capture_output=capture)


def quote_environment(value):
    if any(c in value for c in '\n\r\0'):
        raise RuntimeError('設定値に使用できない文字があります')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def user_install(args, home):
    try:
        import evdev  # noqa: F401
    except ImportError as exc:
        raise RuntimeError('先に sudo apt install python3-evdev を実行してください') from exc
    run(['systemctl', '--user', 'show-environment'], capture_output=True)
    devices = json.loads(privileged('_devices', capture=True).stdout)
    device = choose_device(devices, args.device, not args.yes)
    environment = f'ALT_IME_DEVICE={quote_environment(device)}\n'
    helper = home / SETUP
    uninstall = f'#!/bin/sh\nexec /usr/bin/python3 {shlex.quote(str(helper))} uninstall "$@"\n'
    contents = {
        PROGRAM: ((SOURCE / 'alt_ime2.py').read_bytes(), 0o755),
        SETUP: (Path(__file__).read_bytes(), 0o755),
        SERVICE: ((SOURCE / 'systemd/alt-ime2.service').read_bytes(), 0o644),
        ENVIRONMENT: (environment.encode(), 0o600),
        UNINSTALL: (uninstall.encode(), 0o755),
    }
    managed = ManagedFiles(home, home / '.local/state/alt-ime2/install.json', USER_FILES)
    managed.preflight(contents)
    confirm(f'キーボード: {device}\n左Alt単押し→無変換、右Alt単押し→変換を送信します。\n'
            'IME側で「変換→ON、無変換→OFF」を設定してください（自動では変更しません）。\n'
            'sudoでuinputの起動・専用udevルールの配置・inputグループへの追加を行います。\n'
            'inputグループはキーボード入力全体を読める強い権限です。\n'
            '自動起動を登録しますが、今は開始しません。\n'
            '有効にするにはコンピューターを再起動してください。\n'
            '別キーボードかSSHでの復旧手段を確保してください。', args.yes)
    # Stop an existing installation before replacing its executable or configuration.
    status = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'alt-ime2.service'])
    if status.returncode == 0:
        run(['systemctl', '--user', 'stop', 'alt-ime2.service'])
    managed.install(contents)
    privileged('_system-install', pwd.getpwuid(os.getuid()).pw_name)
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', 'alt-ime2.service'])
    print('インストール完了。有効にするにはコンピューターを再起動してください。\n'
          '確認: systemctl --user status alt-ime2.service\n'
          'ログ: journalctl --user -u alt-ime2.service -b\n'
          f'削除: {home / UNINSTALL}')


def user_uninstall(args, home):
    managed = ManagedFiles(home, home / '.local/state/alt-ime2/install.json', USER_FILES)
    confirm('Alt-IME2を停止し、自動起動とインストーラーが作った未編集のファイルを削除します。\n'
            'IMEの設定・辞書、手動配置ファイル、他ツールのuinput設定は変更しません。' +
            ('\nこのインストーラーが追加したinputグループ所属も削除します。' if args.remove_input_group else ''), args.yes)
    # Must finish disabling before deleting the unit or root-side permissions.
    status = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'alt-ime2.service'])
    if status.returncode == 0:
        run(['systemctl', '--user', 'stop', 'alt-ime2.service'])
    if (home / SERVICE).exists():
        run(['systemctl', '--user', 'disable', 'alt-ime2.service'])
    extra = ['--remove-input-group'] if args.remove_input_group else []
    privileged('_system-uninstall', pwd.getpwuid(os.getuid()).pw_name, *extra)
    managed.remove()
    run(['systemctl', '--user', 'daemon-reload'])
    print('アンインストール完了。inputグループを外した場合はコンピューターを再起動してください。\n'
          '編集済み・既存ファイルと管理記録は保持しています。uinputモジュールはアンロードしません。')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    if argv and argv[0].startswith('_'):
        parser.add_argument('action', choices=['_devices', '_system-install', '_system-uninstall'])
        parser.add_argument('username', nargs='?')
        parser.add_argument('--remove-input-group', action='store_true')
    else:
        actions = parser.add_subparsers(dest='action', required=True)
        install = actions.add_parser('install', prog='./install.sh', help='インストールと自動起動の設定（完了後に再起動）')
        install.add_argument('--device', help='使用するby-id/by-pathキーボード（1台なら自動検出）')
        install.add_argument('--yes', '-y', action='store_true', help='確認を省略（複数台なら--device必須）')
        uninstall = actions.add_parser('uninstall', prog='./uninstall.sh', help='停止・自動起動解除・管理対象の削除')
        uninstall.add_argument('--yes', '-y', action='store_true', help='確認を省略')
        uninstall.add_argument('--remove-input-group', action='store_true',
                               help='本ツールが追加したinputグループ所属も削除')
    args = parser.parse_args(argv)
    try:
        if args.action.startswith('_'):
            if os.geteuid() != 0:
                raise RuntimeError('内部コマンドには管理者権限が必要です')
            if args.action == '_devices':
                print(json.dumps(discover_devices()))
            elif args.username:
                system_setup(args.action.removeprefix('_system-'), args.username, args.remove_input_group)
            else:
                raise RuntimeError('ユーザー名が必要です')
        else:
            if os.geteuid() == 0:
                raise RuntimeError('sudoでスクリプト全体を実行せず、デスクトップの一般ユーザーとして実行してください')
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
            with exclusive_lock(home / '.local/state/alt-ime2/setup.lock'):
                if args.action == 'install':
                    user_install(args, home)
                else:
                    user_uninstall(args, home)
        return 0
    except (OSError, RuntimeError, ValueError, EOFError, subprocess.SubprocessError) as exc:
        print(f'エラー: {exc}\n途中まで配置された場合も uninstall.sh で管理対象を撤去できます。', file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n中止しました。途中の配置は uninstall.sh で撤去できます。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
