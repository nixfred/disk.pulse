"""Telemetry regressions: fixtures and temporary directories only, never the live machine."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('disk', Path(__file__).resolve().parents[1] / 'disk_pulse.py')
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)

MOUNTINFO = '\n'.join([
    '22 1 0:21 / /proc rw,nosuid - proc proc rw',
    '23 1 0:22 / /sys rw,nosuid - sysfs sysfs rw',
    '42 1 0:35 /@ / rw,relatime shared:1 - btrfs /dev/mapper/root rw,compress=zstd:3,ssd,space_cache=v2,subvolid=256,subvol=/@',
    '65 42 0:35 /@home /home rw,relatime shared:198 - btrfs /dev/mapper/root rw,compress=zstd:3,ssd,subvolid=257,subvol=/@home',
    '105 42 0:35 /@log /var/log rw,relatime shared:210 - btrfs /dev/mapper/root rw,compress=zstd:3,ssd,subvolid=258,subvol=/@log',
    '70 42 0:40 / /tmp rw,nosuid - tmpfs tmpfs rw',
    '215 42 259:1 / /boot ro,relatime shared:216 - vfat /dev/nvme0n1p1 ro,fmask=0077',
    '300 42 0:60 / /run/user/1000/doc rw,nosuid - fuse.portal portal rw,user_id=1000',
    '301 42 0:61 / /home/pi/google rw,nosuid,nodev - fuse.rclone gdrive: rw,user_id=1000',
    '302 42 8:17 / /run/media/pi/my\\040stick rw,nosuid - vfat /dev/sdb1 rw',
    '303 42 0:62 / /mnt/nas rw - nfs4 nas:/export rw,vers=4.2',
])

STAT_A = '100 5 8000 50 20 2 4000 40 0 300 400 0 0 0 0 0 0'
STAT_B = '160 5 20000 80 30 2 8000 60 0 1200 1000 0 0 0 0 0 0'


def fake_block(root, name, stat_line, size=1000000, extra=None):
    base = root / name
    (base / 'device').mkdir(parents=True)
    (base / 'queue').mkdir()
    (base / 'size').write_text(str(size))
    (base / 'stat').write_text(stat_line)
    (base / 'inflight').write_text('1 2')
    (base / 'device/model').write_text('Fake Drive 9000\n')
    (base / 'queue/rotational').write_text('0')
    (base / 'queue/scheduler').write_text('[none] mq-deadline')
    (base / 'queue/nr_requests').write_text('64')
    (base / 'queue/discard_max_bytes').write_text('4096')
    (base / 'queue/write_cache').write_text('write back')
    (base / 'queue/logical_block_size').write_text('512')
    for key, value in (extra or {}).items():
        (base / key).parent.mkdir(parents=True, exist_ok=True)
        (base / key).write_text(value)
    return base


class MountTests(unittest.TestCase):
    def test_empty_state_home_uses_same_fallback_as_panel(self):
        with patch.dict(disk.os.environ, {'XDG_STATE_HOME': ''}):
            module = runpy.run_path(disk.__file__)
        self.assertEqual(module['STATE'], Path.home() / '.local/state/disk-pulse')

    def test_mounts_fold_subvolumes_and_skip_pseudo_filesystems(self):
        rows = disk.mounts(MOUNTINFO)
        self.assertEqual([r['mount'] for r in rows], ['/', '/boot', '/home/pi/google', '/run/media/pi/my stick', '/mnt/nas'])
        root = rows[0]
        # Five mounts of one pool are one filesystem; the shortest leads.
        self.assertEqual(root['also'], ['/home', '/var/log'])
        self.assertEqual(root['compress'], 'zstd:3')
        self.assertEqual(root['subvol'], '/@')
        self.assertEqual(root['flags'], ['ssd'])
        self.assertFalse(root['remote'])
        self.assertTrue(rows[1]['readonly'])
        # Network and userspace filesystems are measured on a deadline.
        self.assertTrue(rows[2]['remote'])
        self.assertTrue(rows[4]['remote'])
        self.assertFalse(rows[3]['remote'])
        self.assertEqual(rows[3]['source'], '/dev/sdb1')

    def test_pseudo_and_portal_mounts_never_appear(self):
        for row in disk.mounts(MOUNTINFO):
            self.assertNotIn(row['fstype'], disk.PSEUDO_FS)
            self.assertFalse(row['mount'].startswith(('/proc', '/sys', '/run/user')))

    def test_malformed_mountinfo_lines_are_ignored(self):
        self.assertEqual(disk.mounts('garbage\n1 2 3\n - \n'), [])

    def test_remote_probe_marks_a_hung_share_unresponsive_then_recovers(self):
        gate = threading.Event()
        real = os.statvfs
        def statvfs(mount):
            if mount == '/slow':
                gate.wait(2)
            return real('/')
        probe = disk.RemoteProbe()
        with patch.object(disk.os, 'statvfs', side_effect=statvfs), patch.object(disk, 'REMOTE_DEADLINE', 0.05):
            row = disk.usage({'mount': '/slow', 'remote': True}, probe)
            self.assertFalse(row['responsive'])
            self.assertEqual(row['total'], 0)
            self.assertIsNone(row['freePct'])
            # One probe in flight per mount: asking again does not stack threads.
            self.assertEqual(len(probe.pending), 1)
            gate.set()
            time.sleep(0.1)
            row = disk.usage({'mount': '/slow', 'remote': True}, probe)
            self.assertTrue(row['responsive'])
            self.assertGreater(row['total'], 0)
            self.assertEqual(len(probe.pending), 0)

    def test_local_usage_reports_df_semantics(self):
        class V:
            f_frsize = 4096; f_blocks = 1000; f_bfree = 400; f_bavail = 350
        with patch.object(disk.os, 'statvfs', return_value=V()):
            row = disk.usage({'mount': '/', 'remote': False}, disk.RemoteProbe())
        self.assertEqual(row['total'], 4096000)
        self.assertEqual(row['free'], 4096 * 350)
        self.assertEqual(row['used'], 4096 * 600)
        self.assertAlmostEqual(row['freePct'], 35)
        self.assertAlmostEqual(row['usedPct'], 60)
        with patch.object(disk.os, 'statvfs', side_effect=OSError):
            row = disk.usage({'mount': '/gone', 'remote': False}, disk.RemoteProbe())
        self.assertFalse(row['responsive'])


class DiskTests(unittest.TestCase):
    def test_rates_from_counter_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_block(root, 'sda', STAT_A, extra={'sda1/partition': '1', 'sda1/size': '2048', 'device/hwmon/hwmon3/temp1_input': '41000', 'device/hwmon/hwmon3/temp1_max': '65000'})
            (root / 'loop0/device').mkdir(parents=True)
            (root / 'loop0/size').write_text('100')
            (root / 'dm-0').mkdir()
            (root / 'dm-0/size').write_text('100')
            with patch.object(disk, 'SYS_BLOCK', root):
                first = disk.disks(None, 1000.0)
                self.assertEqual([d['name'] for d in first], ['sda'])
                d = first[0]
                self.assertEqual(d['rates']['read'], 0)
                self.assertEqual(d['model'], 'Fake Drive 9000')
                self.assertEqual(d['scheduler'], 'none')
                self.assertEqual(d['inflight'], [1, 2])
                self.assertEqual(d['temp'], 41)
                self.assertEqual(d['tempMax'], 65)
                self.assertEqual(d['partitions'], [{'name': 'sda1', 'size': 2048 * 512}])
                (root / 'sda/stat').write_text(STAT_B)
                second = disk.disks({d['name']: d for d in first}, 1003.0)
            r = second[0]['rates']
            # 12000 sectors read over 3s, 900ms of I/O over 3000ms.
            self.assertAlmostEqual(r['read'], 12000 * 512 / 3)
            self.assertAlmostEqual(r['write'], 4000 * 512 / 3)
            self.assertAlmostEqual(r['readIops'], 20)
            self.assertAlmostEqual(r['util'], 30)
            self.assertAlmostEqual(r['awaitRead'], 30 / 60)
            self.assertAlmostEqual(r['queue'], 0.2)
            self.assertNotIn('counters', disk.public({'disks': second})['disks'][0])

    def test_counter_reset_reads_as_no_traffic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_block(root, 'nvme0n1', STAT_B)
            with patch.object(disk, 'SYS_BLOCK', root):
                first = disk.disks(None, 1000.0)
                (root / 'nvme0n1/stat').write_text(STAT_A)
                second = disk.disks({d['name']: d for d in first}, 1003.0)
            self.assertEqual(second[0]['rates']['read'], 0)
            self.assertEqual(second[0]['rates']['util'], 0)
            self.assertEqual(second[0]['transport'], 'NVMe')

    def test_physical_device_is_found_through_dm_and_partitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / 'devices/nvme0n1'
            (real / 'nvme0n1p2').mkdir(parents=True)
            (real / 'nvme0n1p2/partition').write_text('2')
            (root / 'nvme0n1').symlink_to(real)
            (root / 'nvme0n1p2').symlink_to(real / 'nvme0n1p2')
            (root / 'dm-0/slaves').mkdir(parents=True)
            (root / 'dm-0/dm').mkdir()
            (root / 'dm-0/dm/uuid').write_text('CRYPT-LUKS2-abc-root')
            (root / 'dm-0/slaves/nvme0n1p2').symlink_to(root / 'nvme0n1p2')
            with patch.object(disk, 'SYS_BLOCK', root):
                self.assertEqual(disk.physical_of('dm-0'), 'nvme0n1')
                self.assertEqual(disk.physical_of('nvme0n1p2'), 'nvme0n1')
                self.assertTrue(disk.encrypted('dm-0'))
                self.assertFalse(disk.encrypted('nvme0n1'))
                self.assertEqual(disk.physical_of('missing'), '')

    def test_btrfs_pool_from_sysfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / 'btrfs/1111-2222'
            for kind, profile, used, total, disk_total in (('data', 'single', 100, 120, 120), ('metadata', 'dup', 10, 12, 24), ('system', 'dup', 1, 2, 4)):
                (pool / 'allocation' / kind / profile).mkdir(parents=True)
                (pool / 'allocation' / kind / 'bytes_used').write_text(str(used))
                (pool / 'allocation' / kind / 'total_bytes').write_text(str(total))
                (pool / 'allocation' / kind / 'disk_total').write_text(str(disk_total))
                (pool / 'allocation' / kind / 'disk_used').write_text(str(used))
            (pool / 'allocation/global_rsv_size').write_text('5')
            (pool / 'allocation/global_rsv_reserved').write_text('5')
            (pool / 'devices').mkdir()
            (pool / 'devices/dm-0').mkdir()
            (pool / 'devinfo/1').mkdir(parents=True)
            (pool / 'devinfo/1/error_stats').write_text('write_errs 0\nread_errs 2\nflush_errs 0\ncorruption_errs 1\ngeneration_errs 0\n')
            (pool / 'commit_stats').write_text('commits 12\ncur_commit_ms 0\nlast_commit_ms 35\nmax_commit_ms 3620\ntotal_commit_ms 500\n')
            (pool / 'label').write_text('pool\n')
            (pool / 'discard').mkdir()
            (pool / 'discard/discard_bytes_saved').write_text('99')
            (pool / 'features/compress_zstd').mkdir(parents=True)
            (pool / 'nodesize').write_text('16384')
            (pool / 'sectorsize').write_text('4096')
            (root / 'block/dm-0').mkdir(parents=True)
            (root / 'block/dm-0/size').write_text('1')  # one 512-byte sector
            with patch.object(disk, 'SYS_BTRFS', root / 'btrfs'), patch.object(disk, 'SYS_BLOCK', root / 'block'):
                pools = disk.btrfs_pools()
            p = pools['1111-2222']
            self.assertEqual(p['label'], 'pool')
            self.assertEqual(p['spaces']['metadata']['profile'], 'dup')
            self.assertEqual(p['spaces']['metadata']['diskTotal'], 24)
            self.assertEqual(p['deviceBytes'], 512)
            self.assertEqual(p['unallocated'], 512 - 148)
            self.assertEqual(p['errorTotal'], 3)
            self.assertEqual(p['errors']['read_errs'], 2)
            self.assertEqual(p['commits']['max_commit_ms'], 3620)
            self.assertEqual(p['discardSaved'], 99)
            self.assertEqual(p['features'], ['compress_zstd'])
            self.assertEqual(p['globalReserve'], {'size': 5, 'reserved': 5})


class HealthTests(unittest.TestCase):
    def test_trim_status_parses_unix_stamps_and_passes_prose_through(self):
        def query(args):
            if 'fstrim.timer' in args:
                return 'ActiveState=active\nLastTriggerUSec=@1788758883\nNextElapseUSecRealtime=Mon 2026-09-14 00:13:26 EDT\n'
            return 'Result=success\n'
        self.assertEqual(disk.trim_status(query), {'timer': 'active', 'last': 1788758883, 'next': 'Mon 2026-09-14 00:13:26 EDT', 'result': 'success'})
        self.assertEqual(disk.trim_status(lambda args: ''), {'timer': None, 'last': None, 'next': None, 'result': None})

    def test_variant_unwraps_busctl_json(self):
        raw = {'type': 'a{sv}', 'data': [{'percent_used': {'type': 'y', 'data': 1}, 'temps': {'type': 'aq', 'data': [361, 0]}}]}
        self.assertEqual(disk.variant(raw), [{'percent_used': 1, 'temps': [361, 0]}])
        self.assertEqual(disk.bytes_string([47, 100, 101, 118, 47, 115, 100, 97, 0]), '/dev/sda')
        self.assertEqual(disk.bytes_string(None), '')

    def test_smart_maps_udisks_drives_to_block_names(self):
        drive = '/org/freedesktop/UDisks2/drives/SAMSUNG_X'
        ata_drive = '/org/freedesktop/UDisks2/drives/WD_Y'
        objects = {
            drive: {'org.freedesktop.UDisks2.Drive': {'Vendor': '', 'ConnectionBus': '', 'Removable': False},
                    'org.freedesktop.UDisks2.NVMe.Controller': {'SmartTemperature': 341, 'SmartPowerOnHours': 3170, 'SmartCriticalWarning': [], 'SmartSelftestStatus': 'success', 'SmartUpdated': 1789049307, 'NVMeRevision': '2.0'}},
            ata_drive: {'org.freedesktop.UDisks2.Drive': {'Vendor': 'WD', 'ConnectionBus': 'usb', 'Removable': True},
                        'org.freedesktop.UDisks2.Drive.Ata': {'SmartSupported': True, 'SmartTemperature': 310, 'SmartPowerOnSeconds': 7200, 'SmartFailing': False, 'SmartNumBadSectors': 3, 'SmartNumAttributesFailing': 0, 'SmartSelftestStatus': 'success', 'SmartUpdated': 5}},
            '/org/freedesktop/UDisks2/block_devices/nvme0n1': {'org.freedesktop.UDisks2.Block': {'Drive': drive, 'Device': [47, 100, 101, 118, 47, 110, 118, 109, 101, 48, 110, 49, 0]}},
            '/org/freedesktop/UDisks2/block_devices/nvme0n1p1': {'org.freedesktop.UDisks2.Block': {'Drive': drive, 'Device': [47, 100, 101, 118, 47, 120, 0]}, 'org.freedesktop.UDisks2.Partition': {}},
            '/org/freedesktop/UDisks2/block_devices/sda': {'org.freedesktop.UDisks2.Block': {'Drive': ata_drive, 'Device': [47, 100, 101, 118, 47, 115, 100, 97, 0]}},
            '/org/freedesktop/UDisks2/block_devices/dm_2d0': {'org.freedesktop.UDisks2.Block': {'Drive': '/', 'Device': [47, 100, 101, 118, 47, 100, 109, 45, 48, 0]}},
        }
        def query(args):
            if 'GetManagedObjects' in args:
                return [objects]
            if 'SmartGetAttributes' in args:
                return [{'percent_used': 1, 'avail_spare': 100, 'spare_thresh': 5, 'total_data_read': 10, 'total_data_written': 20, 'power_cycles': 89, 'unsafe_shutdowns': 30, 'media_errors': 0, 'num_err_log_entries': 0, 'ctrl_busy_time': 6070, 'wctemp': 358, 'cctemp': 358}]
            return None
        out = disk.smart(query)
        self.assertEqual(set(out), {'nvme0n1', 'sda'})
        n = out['nvme0n1']
        self.assertEqual(n['kind'], 'nvme')
        self.assertAlmostEqual(n['temp'], 67.9, places=1)
        self.assertEqual(n['percentUsed'], 1)
        self.assertEqual(n['totalWritten'], 20)
        self.assertAlmostEqual(n['warnTemp'], 84.9, places=1)
        self.assertEqual(n['warnings'], [])
        a = out['sda']
        self.assertEqual(a['kind'], 'ata')
        self.assertEqual(a['powerOnHours'], 2)
        self.assertEqual(a['badSectors'], 3)
        self.assertFalse(a['failing'])
        # Nothing identifying is carried: no serial, no WWN.
        for info in out.values():
            self.assertNotIn('serial', {k.lower() for k in info})

    def test_smart_without_udisks_is_empty_not_an_error(self):
        self.assertEqual(disk.smart(lambda args: None), {})
        self.assertEqual(disk.smart(lambda args: ['not a dict']), {})


class HogTests(unittest.TestCase):
    def test_hogs_rank_by_rate_then_lifetime_and_skip_idle(self):
        procs = {1: {'pid': 1, 'start': '1', 'ppid': 0, 'name': 'init'},
                 20: {'pid': 20, 'start': '5', 'ppid': 1, 'name': 'quiet'},
                 30: {'pid': 30, 'start': '6', 'ppid': 1, 'name': 'busy'},
                 40: {'pid': 40, 'start': '7', 'ppid': 1, 'name': 'big'},
                 50: {'pid': 50, 'start': '8', 'ppid': 1, 'name': 'zero'}}
        first = [(procs[20], {'read_bytes': 100, 'write_bytes': 0}, True),
                 (procs[30], {'read_bytes': 1000, 'write_bytes': 0}, True),
                 (procs[40], {'read_bytes': 50000, 'write_bytes': 50000}, False),
                 (procs[50], {'read_bytes': 0, 'write_bytes': 0}, True)]
        second = [(procs[20], {'read_bytes': 100, 'write_bytes': 0}, True),
                  (procs[30], {'read_bytes': 4000, 'write_bytes': 3000}, True),
                  (procs[40], {'read_bytes': 50000, 'write_bytes': 50000}, False),
                  (procs[50], {'read_bytes': 0, 'write_bytes': 0}, True)]
        with patch.object(disk, 'clients', return_value=[]), patch.object(disk, 'target_for', return_value={'address': '0x1', 'title': 'w', 'workspace': '1', 'host': {}}) as target:
            rows, counters = disk.hogs(None, 100.0, scan=lambda: (procs, first))
            # No rate yet: lifetime alone orders the list, and the idle one is gone.
            self.assertEqual([r['name'] for r in rows], ['big', 'busy', 'quiet'])
            self.assertEqual(rows[0]['readRate'], 0)
            rows, _ = disk.hogs(counters, 103.0, scan=lambda: (procs, second))
            self.assertEqual([r['name'] for r in rows], ['busy', 'big', 'quiet'])
            self.assertAlmostEqual(rows[0]['readRate'], 1000)
            self.assertAlmostEqual(rows[0]['writeRate'], 1000)
            # Only owned processes are routed to a window.
            self.assertEqual(rows[1]['target'], {})
            self.assertEqual(rows[0]['target']['address'], '0x1')
            self.assertEqual(target.call_count, 4)

    def test_recycled_pid_does_not_inherit_a_rate(self):
        p = {'pid': 7, 'start': '1', 'ppid': 1, 'name': 'a'}
        q = {'pid': 7, 'start': '2', 'ppid': 1, 'name': 'b'}
        with patch.object(disk, 'clients', return_value=[]), patch.object(disk, 'target_for', return_value={}):
            _, counters = disk.hogs(None, 100.0, scan=lambda: ({7: p}, [(p, {'read_bytes': 10, 'write_bytes': 0}, True)]))
            rows, _ = disk.hogs(counters, 103.0, scan=lambda: ({7: q}, [(q, {'read_bytes': 5000, 'write_bytes': 0}, True)]))
        self.assertEqual(rows[0]['readRate'], 0)

    def test_io_counters_reject_partial_files(self):
        with patch.object(disk, 'read', return_value='rchar: 1\nread_bytes: 5\n'):
            self.assertIsNone(disk.io_counters(1))
        with patch.object(disk, 'read', return_value='read_bytes: 5\nwrite_bytes: x\n'):
            self.assertIsNone(disk.io_counters(1))
        with patch.object(disk, 'read', return_value='rchar: 1\nread_bytes: 5\nwrite_bytes: 7\n'):
            self.assertEqual(disk.io_counters(1), {'read_bytes': 5, 'write_bytes': 7})


class HistoryTests(unittest.TestCase):
    def sample(self, ts, used, read, write, util):
        return {'ts': ts, 'filesystems': [{'mount': '/', 'usedPct': used, 'device': 'sda'}],
                'disks': [{'name': 'sda', 'rates': {'read': read, 'write': write, 'util': util}, 'temp': 40}],
                'rates': {'read': read, 'write': write}, 'psi': {'some': {'avg10': 1.5}}}

    def test_history_buckets_carry_averages_peaks_and_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(disk, 'STATE', Path(tmp)), patch.object(disk, 'read', return_value='boot-1\n'):
                db = disk.db_open()
                try:
                    for i, (r, w) in enumerate([(10e6, 1e6), (30e6, 2e6), (20e6, 3e6)]):
                        disk.record(db, self.sample(1000 + i * 15, 60 + i, r, w, 10 + i))
                    # A day's range buckets at 360s, so three samples 15s apart
                    # land in one bucket and the averages are testable.
                    h = disk.history(db, 86400, now=1045)
                finally:
                    db.close()
            self.assertEqual(h['count'], 3)
            # Read keeps its per-bucket peak as an envelope; write is the
            # bucket average, so its peak is the highest average bucket.
            self.assertEqual(h['peakRead'], 30e6)
            self.assertEqual(h['peakWrite'], 2e6)
            point = h['points'][0]
            self.assertEqual(len(point), 8)
            self.assertEqual(point[7], 'boot-1')
            self.assertAlmostEqual(point[1], 20e6)
            self.assertAlmostEqual(point[4], 61)
            self.assertAlmostEqual(point[5], 11)

    def test_retention_is_seven_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(disk, 'STATE', Path(tmp)), patch.object(disk, 'read', return_value='b'):
                db = disk.db_open()
                try:
                    disk.record(db, self.sample(1.0, 1, 1, 1, 1))
                    disk.record(db, self.sample(8 * 86400.0, 1, 1, 1, 1))
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 1)
                finally:
                    db.close()


class StateTests(unittest.TestCase):
    def test_prepare_state_repairs_modes_and_refuses_links(self):
        old = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / 'disk-pulse'
                state.mkdir(mode=0o755)
                (state / 'snapshot.json').write_text('{}')
                (state / 'snapshot.json').chmod(0o644)
                (state / 'history.sqlite3').symlink_to('/etc/hostname')
                with patch.object(disk, 'STATE', state):
                    with self.assertRaises(RuntimeError):
                        disk.prepare_state(strict=True)
                    unsafe = disk.prepare_state(strict=False)
                self.assertEqual(unsafe, {'history.sqlite3': 'is a symlink'})
                self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((state / 'snapshot.json').stat().st_mode), 0o600)
        finally:
            os.umask(old)

    def test_atomic_snapshot_is_private_and_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(disk, 'STATE', Path(tmp)):
                disk.atomic('snapshot.json', {'ts': 1, 'free': None})
                path = Path(tmp) / 'snapshot.json'
                self.assertEqual(json.loads(path.read_text()), {'ts': 1, 'free': None})
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual([p.name for p in Path(tmp).iterdir()], ['snapshot.json'])


if __name__ == '__main__':
    unittest.main()
