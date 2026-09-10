#!/usr/bin/env python3
"""Install Disk Pulse with preflight validation and private rollback copies."""
from pathlib import Path
import datetime
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time

PLUGIN_ID = 'nixfred.disk-pulse'
FILES = ('manifest.json', 'Panel.qml', 'Model.js', 'DiskChip.qml',
         'HistoryGraph.qml', 'disk_pulse.py', 'README.md')
# How long to give the shell's plugin scan before falling back to editing the
# layout file directly. The scan is a subprocess and IPC answers before it
# returns, so the first put after a rescan can honestly say "not ready".
PLACEMENT_TRIES = 6


def atomic_write(path, payload, mode):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def update_layout(raw):
    data = json.loads(raw)
    bar = data.get('bar') if isinstance(data, dict) else None
    layout = bar.get('layout') if isinstance(bar, dict) else None
    if not isinstance(layout, dict):
        raise ValueError('shell.json must contain an object at bar.layout.')
    for section in ('left', 'center', 'right'):
        entries = layout.get(section)
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError('bar.layout.' + section + ' must be an array of entry objects.')
    found = False
    for section in ('left', 'center', 'right'):
        entries = []
        for entry in layout[section]:
            if entry.get('id') == PLUGIN_ID:
                if found:
                    continue
                found = True
            entries.append(entry)
        layout[section] = entries
    if not found:
        layout['right'].append({'id': PLUGIN_ID, 'displayMode': 0, 'animated': True})
    return (json.dumps(data, indent=2) + '\n').encode('utf-8')


def symlinked_ancestors(*paths):
    """Symlinks at or above each path, stopping below $HOME.

    Path.is_symlink() tests only the final component, so checking the files
    alone catches a symlinked shell.json while missing the far more common
    dotfiles layout where ~/.config or ~/.config/omarchy is itself the link
    into a tracked repo. Publishing through one of those writes into the
    dotfiles repo rather than the destination the installer reports, and the
    rollback copies then describe files that were never the live ones. $HOME
    itself is not checked: a symlinked home directory is a system choice, not
    an install destination the user picked.
    """
    home = Path.home()
    found = set()
    for path in paths:
        for candidate in (path, *path.parents):
            if candidate == home:
                break
            if candidate.is_symlink():
                found.add(str(candidate))
    return sorted(found)


def place_through_shell():
    """Ask the running shell to put the widget on the bar, in its own writer.

    The shell serialises every edit to shell.json through one mutator, so a
    placement made this way cannot race a setting another widget saves in
    the same second. put is the unattended verb: a widget that is already on
    the bar stays where its owner put it. Returns True when the shell did it.
    """
    for attempt in range(PLACEMENT_TRIES):
        try:
            result = subprocess.run(['omarchy-shell', 'shell', 'putBarWidget', PLUGIN_ID, '{"section": "right"}'],
                                    capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        answer = (getattr(result, 'stdout', '') or '').strip()
        if result.returncode == 0 and answer == 'ok':
            return True
        if answer != 'not ready':
            return False
        time.sleep(0.5)
    return False


def restore(backup, dest, unit, published):
    """Put the previous release back after a failed publication.

    A publication that fails half way would otherwise leave a new manifest
    beside old QML, or new QML beside an old daemon, and the shell would load
    the mixture. The rollback copies are the release that was running.
    """
    previous = backup / 'plugin'
    for name in published:
        source = previous / name
        if source.is_file():
            shutil.copy2(source, dest / name)
        elif (dest / name).is_file():
            (dest / name).unlink()
    if not previous.exists() and dest.exists() and not any(dest.iterdir()):
        dest.rmdir()
    unit_copy = backup / 'disk-pulse.service'
    if unit_copy.is_file():
        shutil.copy2(unit_copy, unit)
    elif 'disk-pulse.service' in published:
        unit.unlink(missing_ok=True)


def main():
    source = Path(__file__).resolve().parent
    home = Path.home()
    config = home / '.config/omarchy/shell.json'
    dest = config.parent / 'plugins' / PLUGIN_ID
    unit = home / '.config/systemd/user/disk-pulse.service'
    linked = symlinked_ancestors(config, dest, unit)
    if linked:
        raise RuntimeError('Resolve symlinked install destinations explicitly '
                           'before installing: ' + ', '.join(linked))
    # Validate the layout before anything is published, so a broken file
    # stops the install rather than being discovered after the copy.
    update_layout(config.read_bytes())
    config_mode = stat.S_IMODE(config.stat().st_mode) & 0o777
    payloads = {}
    for name in (*FILES, 'disk-pulse.service'):
        path = source / name
        if not path.is_file():
            raise RuntimeError('Missing install payload: ' + name)
        payloads[name] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode) & 0o777)

    backups = home / '.local/state/omarchy/backups'
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = Path(tempfile.mkdtemp(prefix='disk-pulse-' + stamp + '-', dir=backups))
    shutil.copy2(config, backup / 'shell.json')
    if dest.exists():
        shutil.copytree(dest, backup / 'plugin', symlinks=True)
    if unit.exists():
        shutil.copy2(unit, backup / 'disk-pulse.service')

    published = []
    try:
        dest.mkdir(parents=True, exist_ok=True)
        unit.parent.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            atomic_write(dest / name, *payloads[name])
            published.append(name)
        atomic_write(unit, *payloads['disk-pulse.service'])
        published.append('disk-pulse.service')
    except Exception:
        restore(backup, dest, unit, published)
        print('Publication failed; the previous release was put back. Rollback copies: ' + str(backup))
        raise

    try:
        for command in (['systemctl', '--user', 'daemon-reload'],
                        ['systemctl', '--user', 'enable', 'disk-pulse.service'],
                        ['systemctl', '--user', 'restart', 'disk-pulse.service'],
                        ['omarchy-shell', 'shell', 'rescanPlugins']):
            subprocess.run(command, check=True, timeout=30)
        # The bar layout goes through the shell whenever the shell is there to
        # take it. Without a shell (a headless install, a first boot) the file
        # is edited directly, re-read at the moment of writing so a setting
        # saved while the files were being copied is kept.
        if not place_through_shell():
            atomic_write(config, update_layout(config.read_bytes()), config_mode)
    except Exception:
        print('Install did not complete. Rollback copies: ' + str(backup))
        raise
    print('Installed Disk Pulse. Backup: ' + str(backup))


if __name__ == '__main__':
    main()
