#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
libvirt-backup.py -- back up and restore domains on a libvirt host.

Subcommands:

    backup    back up every running domain (the default: flag-only
              invocations like 'libvirt-backup.py -D vm1' still work)
    restore   restore one VM's images and XMLs into a destination
              directory, from a backup directory or a rustic/restic
              repository; needs only rustic/restic - no libvirt, so it
              also runs on a rescue system
    forget    apply a retention policy to the repository (per VM the
              newest run is always kept); needs only rustic/restic
    check     verify standalone images (directory mode) or repository
              integrity (repo mode); needs no libvirt
    init      create a new empty repository; needs only rustic/restic

For each running domain the backup performs, entirely through the
libvirt API (python3-libvirt / libvirt.py), the equivalent of:

    virsh domfsfreeze  <vm>
    virsh backup-begin <vm> <vm>-backup.xml
    virsh domfsthaw    <vm>
    virsh blockjob     <vm> <device> --info     (polled until finished)
    virsh dumpxml      <vm>

Artefacts written to the backup directory, all sharing the VM name as
filename prefix (example for a VM named "vm1" with disk "sdb"):

    vm1-backup.xml          <domainbackup> description passed to backupBegin()
    vm1-backup-sdb.qcow2    standalone backup image of block device sdb
    vm1-incr-TS-sdb.qcow2   incremental image (only with 'incremental')
    vm1-chain.json          incremental chain state (only with 'incremental')
    vm1-state.json          selection state: last success, retries, last error
    vm1-config.xml          live domain XML (virsh dumpxml equivalent)
    vm1-backup.log          log of this VM's backup run

Participating disks are those backed by a file, block device or network
source (type='file'/'block'/'network', with a defined <source>; empty
cdrom/floppy drives and source-less disks are skipped).  LUN passthrough
disks (type='hostdev', displayed by virsh domblklist with sources like
'unit:0:0:12') cannot take part in libvirt backup jobs - QEMU has no
block backend for them - and are reported and skipped.  fsFreeze/fsThaw
require qemu-guest-agent inside the guest.  By default (same as
--try-freeze) a failed freeze is logged very prominently, the backup
still runs (crash-consistent only) and the VM is reported as WARN;
--no-freeze does not even attempt the freeze; only --require-freeze
turns a failed freeze into a failed backup (FAIL).

Every domain appears in the final summary: OK/WARN/FAIL for the ones
that were backed up, EXCL for -x exclusions, SKIP for domains not
selected by -D, not running, blocked by the backoff period, or without
backupable disks.  Only FAIL makes the exit code non-zero.

Backup order: before every VM the candidate list is recomputed (VMs
may have been created, shut down or live-migrated in or out) and the
running VM whose last successful backup lies furthest back is picked
next - never-backed-up VMs first.  The last success comes from the
repository itself in repo mode (shared, so a VM that migrated in is
not mistaken for a new one) and from <vm>-state.json in directory
mode.  A long run that gets interrupted therefore continues where it
stopped instead of starving the alphabetically last VMs.  'backoff'
(e.g. "7d", or seconds) sets the minimum time between two backups of
the same VM; -D selections bypass it ('back up now').  Failures are
counted per VM in <vm>-state.json: after more than 2 consecutive
failures the administrator is notified on stderr and the VM is only
retried once the backoff period has passed since the last error; any
success resets the counter.

Most options can also be set in a TOML config file (see --config and
the Config file section below); precedence is CLI > config > defaults.

NOTE on backup images: libvirt's push backup itself always creates a
qcow2 overlay whose backing file is the VM's current disk.  By default
this tool flattens that overlay with 'qemu-img convert' into a
self-contained standalone image (no external dependencies) and removes
the overlay; the final format is qcow2 (--dest-format raw for raw) and
--compress stores it zlib-compressed.  Use --no-standalone to keep the
overlay as-is (saves time and space, but the image then depends on the
VM's original disk).

Incremental backups (--incremental / 'incremental = true', directory
mode only): every backup also creates a libvirt checkpoint whose dirty
bitmap tracks subsequent writes; after the first full backup, each run
writes only the clusters changed since the previous backup to
<vm>-incr-<TS>-<dev>.qcow2 and rotates the checkpoint.  The chain
(base + incrementals + checkpoint name) lives in <vm>-chain.json.
'restore' copies the whole chain and folds the incrementals into the
base image with 'qemu-img rebase -u' + 'qemu-img commit', yielding the
merged image of that backup point.  Needs qcow2 at the active layer
(iSCSI/raw-block VMs always get full backups); --full forces a new
full backup and restarts the chain.

Requires: python3-libvirt, libvirt >= 9.0 (QEMU driver) for the
<domainbackup> XML format used by virDomainBackupBegin().  The restore
subcommand needs none of these - only rustic/restic (repository) or
plain file copy (directory).
"""

import argparse
import copy
import errno
import fcntl
import getpass
import glob
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from fnmatch import fnmatch

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None
import xml.etree.ElementTree as ET

libvirt = None  # imported on demand; see require_libvirt()


def require_libvirt():
    """Import python3-libvirt on first use.  The restore subcommand works
    without it, so the import must not happen at module level."""
    global libvirt
    if libvirt is None:
        try:
            import libvirt as _mod
        except ImportError:  # pragma: no cover
            sys.exit(
                "error: the backup subcommand needs the 'libvirt' "
                "Python module (install with e.g. sudo apt install "
                "python3-libvirt)"
            )
        libvirt = _mod
    return libvirt


class BackupError(Exception):
    """A backup for one domain failed."""


class BackupTimeout(BackupError):
    """The backup for one domain exceeded the configured timeout."""


class NoBackupableDisks(BackupError):
    """The domain has no disks usable for the libvirt backup API."""


class BackupAbort(BackupError):
    """Interrupted by SIGINT/SIGHUP/SIGTERM.  Unwinding out of
    backup_domain runs its finally block (fsThaw the guest, abort the
    backup job, discard the overlay); the run then stops instead of
    continuing with the next VM."""


EPILOG = """\
Artefacts written per VM into the backup directory (prefix = VM name):
  <vm>-backup.xml          backup job description (domainbackup XML)
  <vm>-backup-<dev>.qcow2  standalone backup image per block device (e.g. sdb)
  <vm>-state.json          selection state: last success, retries, last error
  <vm>-config.xml          live domain XML (virsh dumpxml equivalent)
  <vm>-backup.log          log of this VM's backup run

Config file (TOML), searched at --config FILE,
/etc/libvirt-backup/libvirt-backup.toml, /etc/libvirt-backup.toml,
~/.config/libvirt-backup.toml; precedence is CLI > config > defaults.
A fully commented template (libvirt-backup.toml) and working examples
for directory and rustic/S3 mode (examples/) ship in the repository:

  [backup]
  uri = "qemu:///system"
  backup_dir = "/var/lib/libvirt/backups"
  timeout = 14400
  freeze = "try"              # try | no | require
  standalone = true
  compress = true
  verify = true
  incremental = false         # checkpoint-based incrementals
  backoff = "7d"              # min. time between two backups of
                              # one VM; 0 = always eligible

  [domains.vm1]           # name or glob pattern
  exclude = true

  [domains.vm2]
  timeout = 28800
  freeze = "no"

  [retention]                  # used by the 'forget' subcommand
  keep_daily = 7
  keep_weekly = 4
  keep_monthly = 6
  keep_yearly = 2
  prune = true                 # false: only drop indexes, keep packs

Repository mode (rustic/restic): with --repo or [repo], images are
flattened through a temporary read-only NBD export and streamed as raw
into a deduplicated, encrypted repository ('rustic backup -') - no
final image file on disk.  Snapshots are append-only: a broken stream
never touches the previous generation.  Per VM one snapshot set tagged
vm:<name>,run:<timestamp>.  Needs qemu-storage-daemon and
python3-libnbd.

  [repo]
  repository    = "s3:https://s3.example.lan/bucket/libvirt"
  password_file = "/etc/libvirt-backup/libvirt-backup.repo-pass"
  access_key    = "..."        # or AWS_ACCESS_KEY_ID in env
  secret_key    = "..."        # or AWS_SECRET_ACCESS_KEY in env
  region        = "eu-central-1"   # or AWS_REGION in env
  bin           = "auto"       # auto | rustic | restic

With rustic, an 's3:' URL is passed on through rustic's opendal S3
backend (rustic >= 0.8 has no built-in S3 anymore); 'region' must be
the region the server signs for - Garage answers with its own region,
usually "garage".  Create an empty repository once with
'libvirt-backup init'.

--verify checks the artefacts after the backup: directory mode runs
'qemu-img check' on every final image, repository mode confirms that
the snapshots just committed under this run's tags contain every
expected file.

Restore (libvirt-backup.py restore): places one VM's disk image(s) and
<vm>-config.xml/<vm>-backup.xml into a destination directory, from a
backup directory (--from, default: [backup] backup_dir) or from a
repository (same [repo] config section and search order as backup).
An incremental chain (<vm>-chain.json + <vm>-incr-* images) is
restored and merged into the full image automatically.
Generations: newest run by default, --run TS or --snap ID to pick.
--verify runs qemu-img check on restored qcow2 images; --rewrite
points the disk sources in the restored <vm>-config.xml at the
restored images so it can be 'virsh define'd directly.  The tool never
touches a running libvirt setup.

Examples:
  %(prog)s backup                     back up all running VMs
  %(prog)s backup --list              show VMs and block devices only
  %(prog)s backup --dry-run -D 'vm1,db*'
                                      show what would happen, touch nothing
  %(prog)s backup -D vm1 -D vm2       back up vm1 and vm2 only
  %(prog)s backup -x 'k8s-*,test-*'
                                      exclude all VMs matching those patterns
  %(prog)s backup -d /mnt/backup -t 3600
                                      destination dir, 1h timeout per VM
  %(prog)s backup --incremental -D vm1
                                      start an incremental chain for vm1
                                      (first run full, then only changes)
  %(prog)s backup --full -D vm1       force a new full backup, restart chain
  %(prog)s backup --repo s3:https://s3.example.lan/bucket/libvirt
                                      stream images into a rustic/restic S3
                                      repository (password + keys via
                                      [repo] config section or environment)
  %(prog)s restore --list             list restorable VMs in the backup dir
  %(prog)s restore -D vm1 -d /restore/vm1 --verify --rewrite
  %(prog)s restore -D vm1 -d /restore/vm1 \
          --repo s3:https://s3.example.lan/bucket/libvirt
  %(prog)s forget --dry-run       show which runs the retention policy
                                  would forget ([retention] config)
  %(prog)s check                  verify images (directory mode) or
                                  repository integrity (repo mode)
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def human(nbytes):
    """Format a byte count for humans."""
    value = float(nbytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024.0 or unit == "PiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0


def xml_to_str(root):
    """Serialise an ElementTree element to a nicely indented XML string."""
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def write_text(path, content, log):
    """Write `content` to `path` as UTF-8 and log the write."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    log.info("wrote %s", path)


def cred_callback(credentials, _cbdata):
    """Minimal auth callback for libvirt.openAuth() (remote URIs)."""
    for credential in credentials:
        if credential[0] in (
            libvirt.VIR_CRED_AUTHNAME,
            libvirt.VIR_CRED_CNONCE,
        ):
            credential[4] = getpass.getuser()
        elif credential[0] == libvirt.VIR_CRED_PASSPHRASE:
            credential[4] = getpass.getpass("Enter password: ")
    return 0


def open_connection(uri):
    """Open a libvirt connection to `uri`, prompting for credentials if
    needed; exits with an error when the connection fails."""
    auth = [
        [libvirt.VIR_CRED_AUTHNAME, libvirt.VIR_CRED_PASSPHRASE],
        cred_callback,
        None,
    ]
    try:
        conn = libvirt.openAuth(uri, auth, 0)
    except libvirt.libvirtError as err:
        sys.exit(f"error: cannot connect to '{uri}': {err}")
    if conn is None:
        sys.exit(f"error: cannot connect to '{uri}'")
    return conn


def name_patterns(values):
    """Flatten a list of (possibly comma-separated) domain names or glob
    patterns into a plain list of patterns."""
    if not values:
        return []
    return [p.strip() for v in values for p in v.split(",") if p.strip()]


def name_matches(name, patterns):
    """True if the domain name matches any of the glob patterns."""
    return any(fnmatch(name, p) for p in patterns)


class RunLock:
    """Advisory whole-run lock (flock) inside the backup directory.

    Two lock kinds, both scoped so hosts of a cluster sharing one
    backup directory can back up concurrently:

    run lock (vm=None), ``.libvirt-backup-<host>.lock``: one run per
    host.  A second run on the same host would race for the same
    libvirt backup jobs and log/state files, so it is refused instead.

    VM lock (vm=<name>), ``.libvirt-backup-vm-<vm>.lock``: taken around
    each VM's backup, and by a directory-mode restore of that VM.  A VM
    normally runs on exactly one host, but a live migration can hand it
    to another host while a backup is still running there; this lock
    makes the new host skip the VM instead of racing for the same
    ``<vm>-backup-*`` artefacts, chain file and snapshots.  flock works
    across hosts on a shared filesystem - the same assumption the run
    lock always made when the backup directory was shared.

    flock is released by the kernel when the holding process dies, so
    a crashed run never blocks later ones; lock files stay behind and
    are harmless and never need removing.

    A lock can nevertheless linger when the recorded process is gone
    but one of its children (rustic, qemu-storage-daemon) inherited the
    lock file descriptor, or when the holder hangs.  acquire(force=True)
    then proceeds without the lock; the stale file is overwritten by
    the next normal run.  The holder is recorded as ``<host> <pid>``; a
    holder on another host cannot be liveness-checked from here.
    """

    def __init__(self, backup_dir, vm=None):
        """Create the lock for `backup_dir`; acquire() takes it.

        vm names the per-VM lock instead of the per-host run lock.
        """
        if vm is None:
            name = f".libvirt-backup-{os.uname().nodename}.lock"
        else:
            name = f".libvirt-backup-vm-{vm}.lock"
        self.path = os.path.join(backup_dir, name)
        self.fh = None

    def holder(self):
        """The '<host> <pid>' recorded in the lock file, or ''."""
        try:
            with open(self.path) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    @staticmethod
    def holder_alive(holder):
        """True/False when the holder is on this host, None when it
        names a different host (not checkable from here)."""
        parts = holder.split()
        if len(parts) == 2 and parts[0] != os.uname().nodename:
            return None
        try:
            os.kill(int(parts[-1]), 0)
        except (TypeError, ValueError):
            return False
        except PermissionError:
            return True  # exists, only not ours
        except OSError:
            return False
        return True

    def acquire(self, force=False):
        """True when the lock was taken (or forced); on False a warning
        was printed and the caller must not touch this VM/directory.
        Raises OSError when the lock file itself cannot be opened
        (e.g. read-only directory) - that is an error, not a skip."""
        try:
            self.fh = open(self.path, "a+")
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            if self.fh is not None:
                self.fh.close()
                self.fh = None
            if err.errno != errno.EAGAIN:
                raise  # not a lock conflict: cannot even use the file
            holder = self.holder()
            alive = self.holder_alive(holder) if holder else False
            pid = holder.split()[-1] if holder else "?"
            if alive is None:
                detail = f"{holder} on another host"
            elif holder and alive is False:
                detail = (
                    f"pid {pid} is gone, but the lock is still "
                    f"held by one of its child processes"
                )
            else:
                detail = f"pid {pid}"
            print(
                f"warning: another libvirt-backup run is active on "
                f"{os.path.abspath(self.path)} ({detail}): {err}",
                file=sys.stderr,
            )
            if force:
                print(
                    "warning: --force given: continuing without the "
                    "lock; only use this when the holder is known to "
                    "be stuck",
                    file=sys.stderr,
                )
                return True
            if alive is None:
                print(
                    "hint: the holder runs on another host sharing "
                    "this directory: wait for its run to finish, or "
                    "rerun with --force",
                    file=sys.stderr,
                )
            elif holder and not alive:
                print(
                    f"hint: find the process still holding the lock "
                    f"with: fuser -v {os.path.abspath(self.path)}, "
                    f"kill it, or rerun with --force",
                    file=sys.stderr,
                )
            else:
                print(
                    f"hint: wait for the other run to finish, kill it "
                    f"(kill {pid}), or rerun with --force",
                    file=sys.stderr,
                )
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"{os.uname().nodename} {os.getpid()}\n")
        self.fh.flush()
        return True

    def release(self):
        """Drop the lock (closing the fd releases the flock); the lock
        file itself stays behind, harmless."""
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def make_vm_logger(name, backup_dir):
    """Logger writing both to the console and to <vm>-backup.log."""
    log = logging.getLogger(f"libvirt-backup.{name}")
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    file_handler = logging.FileHandler(
        os.path.join(backup_dir, f"{name}-backup.log"), mode="w"
    )
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    log.handlers[:] = [file_handler, console_handler]
    return log


# ---------------------------------------------------------------------------
# domain / disk inspection
# ---------------------------------------------------------------------------


def describe_hostdev_source(source):
    """Human readable description of a hostdev disk source address."""
    addr = source.find("address") if source is not None else None
    if addr is None:
        return "hostdev"
    attrs = dict(addr.attrib)
    atype = attrs.pop("type", "hostdev")
    parts = [
        attrs[key]
        for key in (
            "domain",
            "bus",
            "slot",
            "function",
            "controller",
            "target",
            "unit",
            "address",
        )
        if key in attrs
    ]
    return f"{atype} {' '.join(parts)}".strip()


# Reasons a disk cannot take part in a virDomainBackupBegin() job, keyed
# by the 'kind' marker returned from list_disks().
NOT_BACKUPABLE = {
    "empty": "disk has no source defined (nothing to back up)",
    "hostdev": "host device passthrough (type='hostdev'): the libvirt "
    "backup API cannot back up LUN passthrough because QEMU "
    "has no block backend for it; back this LUN up on the "
    "storage side or re-attach it as a type='block' or "
    "type='network' (iSCSI) disk",
}


def resolve_volume_source(conn, source):
    """Resolve <source pool='..' volume='..'/> to the host path behind it.

    This is the same lookup virt-manager does to display a block device
    for type='volume' disks (virStoragePoolLookupByName /
    virStorageVolLookupByName / virStorageVolGetPath).
    """
    pool_name, vol_name = source.get("pool"), source.get("volume")
    if conn is None or not (pool_name and vol_name):
        return None
    try:
        pool = conn.storagePoolLookupByName(pool_name)
        return pool.storageVolLookupByName(vol_name).path()
    except libvirt.libvirtError:
        return None


def list_disks(xml_desc, conn=None):
    """Inspect all block devices (disk/lun) in a domain XML.

    Returns a list of dicts with keys:
      target  device name as shown by virsh domblklist (e.g. 'sdb')
      src     source string ('-' when none), e.g. file, block device,
              network source or hostdev address; type='volume' disks
              show 'pool/volume -> resolved host path'
      fmt     disk format
      kind    'backupable' (file/block/network/volume) | 'empty' | 'hostdev'
      note    why the disk is not backupable ('' for backupable ones)
    cdrom/floppy and other non-disk devices are ignored entirely.
    """
    root = ET.fromstring(xml_desc)
    disks = []
    for disk in root.findall("./devices/disk"):
        if disk.get("device") not in ("disk", "lun"):
            continue
        target = disk.find("target")
        if target is None or not target.get("dev"):
            continue
        driver = disk.find("driver")
        entry = {
            "target": target.get("dev"),
            "fmt": (driver.get("type") if driver is not None else None)
            or "unknown",
            "src": "-",
            "kind": "empty",
            "note": NOT_BACKUPABLE["empty"],
        }
        source = disk.find("source")
        if disk.get("type") == "hostdev":
            entry["kind"] = "hostdev"
            entry["src"] = describe_hostdev_source(source)
            entry["note"] = NOT_BACKUPABLE["hostdev"]
        elif disk.get("type") == "volume" and source is not None:
            ref = (
                f"{source.get('pool')}/{source.get('volume')}"
                if source.get("pool") and source.get("volume")
                else "volume"
            )
            resolved = resolve_volume_source(conn, source)
            entry.update(
                kind="backupable",
                src=f"{ref} -> {resolved}" if resolved else ref,
                note="",
            )
        elif source is not None:
            src = (
                source.get("file")
                or source.get("dev")
                or source.get("name")
                or source.get("dir")
            )
            if src:
                entry.update(kind="backupable", src=src, note="")
        disks.append(entry)
    disks.sort(key=lambda d: (len(d["target"]), d["target"]))
    return disks


def build_backup_xml(entries, mode="push", incremental=None):
    """Build the <domainbackup> document for virDomainBackupBegin().

    `entries` is a list of dicts with 'target' (guest device name) and
    'dest' (destination file in the backup directory).  `incremental`
    is the name of an existing checkpoint to back up only the changes
    since (None = full backup).
    """
    root = ET.Element("domainbackup", {"mode": mode})
    if incremental:
        ET.SubElement(root, "incremental").text = incremental
    disks = ET.SubElement(root, "disks")
    for entry in entries:
        disk = ET.SubElement(
            disks, "disk", {"name": entry["target"], "type": "file"}
        )
        ET.SubElement(disk, "target", {"file": entry["jobfile"]})
        ET.SubElement(disk, "driver", {"type": "qcow2"})
    return xml_to_str(root)


def build_checkpoint_xml(name, targets):
    """<domaincheckpoint> document creating checkpoint `name` with a
    dirty bitmap on every disk in `targets`, passed as the second
    document to virDomainBackupBegin()."""
    root = ET.Element("domaincheckpoint")
    ET.SubElement(root, "name").text = name
    disks = ET.SubElement(root, "disks")
    for target in targets:
        ET.SubElement(disks, "disk", {"name": target, "checkpoint": "bitmap"})
    return xml_to_str(root)


def active_job_type(dom):
    """The domain's current job type (VIR_DOMAIN_JOB_*), or None when
    no job is active."""
    try:
        info = dom.jobInfo()
    except libvirt.libvirtError:
        return None
    return None if info[0] == libvirt.VIR_DOMAIN_JOB_NONE else info[0]


def list_checkpoints(dom):
    """Names of all checkpoints currently defined on the domain; []
    when the driver does not support checkpoints."""
    try:
        return sorted(cp.name() for cp in dom.listAllCheckpoints(0))
    except libvirt.libvirtError:
        return []


def find_checkpoint(dom, name):
    """The domain checkpoint `name`, or None."""
    try:
        return dom.checkpointLookupByName(name)
    except libvirt.libvirtError:
        return None


def delete_checkpoint(dom, name, log):
    """Remove a checkpoint that the chain no longer needs; silent when
    it is already gone."""
    cp = find_checkpoint(dom, name)
    if cp is not None:
        try:
            cp.delete(0)
            log.info("removed old checkpoint '%s'", name)
        except libvirt.libvirtError as err:
            log.warning("could not delete checkpoint '%s': %s", name, err)


# ---------------------------------------------------------------------------
# incremental backup chains
# ---------------------------------------------------------------------------


def chain_path(backup_dir, vm):
    """Path of the per-VM incremental chain state file."""
    return os.path.join(backup_dir, f"{vm}-chain.json")


def load_chain(backup_dir, vm):
    """The per-VM chain state ({checkpoint, base_ts, disks}), or None.
    A corrupt file is treated like no chain: the next backup starts a
    new full backup."""
    try:
        with open(chain_path(backup_dir, vm)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_chain(backup_dir, vm, chain):
    """Atomically write the incremental chain state `chain` for `vm`."""
    path = chain_path(backup_dir, vm)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(chain, fh, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# per-VM backup state (selection bookkeeping)
# ---------------------------------------------------------------------------

# consecutive failures after which the administrator is notified and
# the backoff period is enforced before the next attempt
MAX_RETRIES = 2


def state_path(backup_dir, vm):
    """Path of the per-VM backup state file."""
    return os.path.join(backup_dir, f"{vm}-state.json")


def load_state(backup_dir, vm):
    """The per-VM backup state ({last_success, retries, last_error,
    last_error_ts}), or {} when missing or corrupt."""
    try:
        with open(state_path(backup_dir, vm)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(backup_dir, vm, state):
    """Atomically write the per-VM backup state."""
    path = state_path(backup_dir, vm)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, path)


def state_epoch(value):
    """Epoch seconds of an ISO timestamp from a state file; 0 when
    missing or unparseable."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def human_age(epoch):
    """Age of an epoch timestamp for humans: 'never', '42min', '5h',
    '3.2d'."""
    if not epoch:
        return "never"
    secs = time.time() - epoch
    if secs < 3600:
        return f"{secs / 60:.0f}min"
    if secs < 172800:
        return f"{secs / 3600:.0f}h"
    return f"{secs / 86400:.1f}d"


def record_backup_result(backup_dir, vm, ok, message=""):
    """Store the outcome of one backup attempt in the per-VM state
    file.  Success records the timestamp and resets the failure
    counters; failure bumps the retry counter, stores the message and
    past MAX_RETRIES consecutive failures prints the administrator
    notification."""
    state = load_state(backup_dir, vm)
    now = datetime.now().isoformat(timespec="seconds")
    if ok:
        state = {"last_success": now, "retries": 0}
    else:
        retries = int(state.get("retries") or 0) + 1
        state["retries"] = retries
        state["last_error"] = (message or "see log")[:200]
        state["last_error_ts"] = now
        if retries > MAX_RETRIES:
            print(
                f"error: '{vm}': {retries} consecutive backup failures "
                f"(last: {state['last_error']}); investigate - next "
                "attempt only after the backoff period has passed",
                file=sys.stderr,
            )
    try:
        save_state(backup_dir, vm, state)
    except OSError as err:
        # bookkeeping must never be the reason a backup run dies
        print(
            f"warning: cannot write backup state for '{vm}': {err}",
            file=sys.stderr,
        )


def backup_mode(dom, name, disks, opts, log):
    """Decide 'incr' vs 'full' for one domain; returns (mode, chain)."""
    chain = load_chain(opts.backup_dir, name)
    if opts.force_full:
        return "full", chain
    if not opts.incremental:
        return "full", chain
    if opts.repo:
        log.info(
            "incremental is not used in repository mode "
            "(the repository deduplicates full backups)"
        )
        return "full", chain
    if (
        not chain
        or not chain.get("checkpoint")
        or not isinstance(chain.get("disks"), dict)
    ):
        log.info(
            "no (usable) incremental chain yet: starting a new full "
            "backup with checkpoint"
        )
        return "full", chain
    if any(d["fmt"] != "qcow2" for d in disks):
        log.warning(
            "incremental requested but not every disk is qcow2; "
            "doing a full backup"
        )
        return "full", chain
    if find_checkpoint(dom, chain["checkpoint"]) is None:
        log.warning(
            "checkpoint '%s' of the chain no longer exists "
            "(VM redefined?); starting a new full backup",
            chain["checkpoint"],
        )
        return "full", chain
    return "incr", chain


def merge_chain(base, incrs, log):
    """Fold the incremental images into the full backup image, in
    order: rebase -u points each incremental at the base, commit then
    copies its allocated (dirty) clusters in; holes keep the base
    data.  The incremental files are removed afterwards."""
    for path in incrs:
        proc = subprocess.run(
            ["qemu-img", "rebase", "-u", "-F", "qcow2", "-b", base, path],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise BackupError(
                f"qemu-img rebase of '{path}' failed: "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        proc = subprocess.run(
            ["qemu-img", "commit", "-f", "qcow2", path],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise BackupError(
                f"qemu-img commit of '{path}' failed: "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        os.unlink(path)
        log.info(
            "merged incremental %s into %s",
            os.path.basename(path),
            os.path.basename(base),
        )


# ---------------------------------------------------------------------------
# backup progress monitoring (the API equivalent of:
#   virsh blockjob <vm> <device> --info )
# ---------------------------------------------------------------------------


def monitor_backup(dom, targets, poll_interval, timeout, log):
    """Poll the backup block jobs of `targets` until all have finished.

    Raises BackupError when a block job reports an error and
    BackupTimeout when `timeout` seconds (0 = wait forever) pass.
    """
    pending = dict.fromkeys(targets)  # target -> last logged percentage
    start = time.monotonic()
    log.info(
        "waiting for backup block job(s) to finish: %s", ", ".join(targets)
    )
    while pending:
        for target, last_pct in list(pending.items()):
            try:
                info = dom.blockJobInfo(target, 0) or {}
            except libvirt.libvirtError as err:
                raise BackupError(
                    f"cannot query block job on '{target}' "
                    f"(domain gone?): {err}"
                ) from err
            job_type = info.get(
                "type", libvirt.VIR_DOMAIN_BLOCK_JOB_TYPE_UNKNOWN
            )
            error = info.get("error", 0)
            if error:
                raise BackupError(
                    f"block job on device '{target}' failed "
                    f"(error code {error})"
                )
            if job_type == libvirt.VIR_DOMAIN_BLOCK_JOB_TYPE_BACKUP:
                cur = info.get("cur", 0)
                end = info.get("end", 0)
                pct = (100.0 * cur / end) if end else 0.0
                if int(pct) != (int(last_pct) if last_pct is not None else -1):
                    log.info(
                        "device %s: backup progress %.1f%% (%s of %s)",
                        target,
                        pct,
                        human(cur),
                        human(end),
                    )
                    pending[target] = pct
            elif job_type == libvirt.VIR_DOMAIN_BLOCK_JOB_TYPE_UNKNOWN:
                log.info("device %s: block job finished", target)
                del pending[target]
            else:
                log.info(
                    "device %s: active block job type %s", target, job_type
                )
        if pending:
            if timeout and (time.monotonic() - start) > timeout:
                raise BackupTimeout(
                    f"backup did not finish within {timeout:.0f}s"
                )
            time.sleep(poll_interval)


def log_completed_job_stats(dom, log):
    """Log the per-backup counters of the finished job, if available."""
    try:
        stats = dom.jobStats(libvirt.VIR_DOMAIN_JOB_STATS_COMPLETED)
    except libvirt.libvirtError:
        return
    if stats:
        relevant = {
            k: human(v)
            if k.startswith("backup.") and isinstance(v, int)
            else v
            for k, v in stats.items()
            if k == "type" or k.startswith("backup.")
        }
        log.info("completed job stats: %s", relevant)


def flatten_to_standalone(jobfile, dest, fmt, compress, log):
    """Convert the backup overlay (with backing chain) into a standalone
    image that no longer depends on the VM's original disk.

    qemu-img convert reads through the whole backing chain and writes a
    self-contained image; the overlay is removed afterwards.
    """
    cmd = ["qemu-img", "convert"]
    if compress:
        cmd.append("-c")
    cmd += ["-O", fmt, jobfile, dest]
    log.info(
        "flattening backup image to standalone %s: %s", fmt, " ".join(cmd)
    )
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as err:
        os.rename(jobfile, dest)
        raise BackupError(
            f"cannot run qemu-img ({err}); overlay kept "
            f"as {dest} but is NOT standalone"
        ) from err
    if proc.returncode != 0:
        os.rename(jobfile, dest)
        log.warning(
            "keeping the un-flattened overlay as %s - it is NOT "
            "standalone (depends on the VM's disk as backing file)",
            dest,
        )
        raise BackupError(
            f"qemu-img convert failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    os.unlink(jobfile)
    log.info(
        "standalone image written: %s (%s), temporary overlay removed",
        dest,
        human(os.path.getsize(dest)),
    )


# ---------------------------------------------------------------------------
# rustic/restic repository mode
# ---------------------------------------------------------------------------

REPO_KEYS = (
    "repository",
    "password_file",
    "access_key",
    "secret_key",
    "region",
    "bin",
)


def resolve_repo_bin(opts, streaming=False):
    """Validate repository mode and pick the rustic/restic binary.
    streaming=True additionally requires the image-flattening helpers
    (qemu-storage-daemon + python3-libnbd) used by the backup."""
    if opts.repo_bin in ("rustic", "restic"):
        if shutil.which(opts.repo_bin) is None:
            sys.exit(
                f"error: repository client '{opts.repo_bin}' not found in PATH"
            )
        opts.repo_bin_name = opts.repo_bin
        opts.repo_bin_resolved = shutil.which(opts.repo_bin)
    else:
        for cand in ("rustic", "restic"):
            if shutil.which(cand):
                opts.repo_bin_name = cand
                opts.repo_bin_resolved = shutil.which(cand)
                break
        else:
            sys.exit(
                "error: repository mode needs 'rustic' or 'restic' in PATH"
            )
    if not (
        opts.repo_password_file
        or os.environ.get("RUSTIC_PASSWORD")
        or os.environ.get("RESTIC_PASSWORD")
    ):
        sys.exit(
            "error: repository mode needs the repository password: "
            "set [repo] password_file in the config file or export "
            "RUSTIC_PASSWORD / RESTIC_PASSWORD"
        )
    if opts.repo_password_file:
        warn_if_world_readable(opts.repo_password_file, "password file")
    m = re.match(r"^([a-z0-9]+):(.+)$", opts.repo)
    if m and m.group(2).startswith(m.group(1) + ":"):
        sys.exit(
            f"error: repository '{opts.repo}' repeats the backend "
            f"prefix; use "
            f"'{m.group(1)}:{m.group(2)[len(m.group(1)) + 1 :]}'"
        )
    if opts.repo.startswith("s3:") and not opts.repo_region:
        sys.exit(
            "error: S3 repositories need a region: set [repo] region "
            "in the config file or export AWS_REGION"
        )
    # rustic >= 0.8 dropped its native s3 backend; S3 is served there by
    # the generic 'opendal' backend.  Translate the s3: URL (which restic
    # still understands natively) into 'opendal:s3' plus the OPENDAL_*
    # environment variables the opendal s3 service reads.
    opts.repo_rustic_repo = None
    opts.repo_opendal_env = {}
    if opts.repo_bin_name == "rustic" and opts.repo.startswith("s3:"):
        m = re.match(
            r"^s3:(?:(https?)://)?([^/]+)(?:/([^/]+))?(?:/(.*))?$", opts.repo
        )
        if not m or not m.group(3):
            sys.exit(
                f"error: cannot parse S3 repository '{opts.repo}'; "
                "expected s3:[scheme://]host/bucket[/prefix]"
            )
        scheme, host, bucket, prefix = m.groups()
        opts.repo_rustic_repo = "opendal:s3"
        opts.repo_opendal_env = {
            "OPENDAL_ENDPOINT": f"{scheme or 'https'}://{host}",
            "OPENDAL_BUCKET": bucket,
            "OPENDAL_ROOT": "/" + (prefix or ""),
            "OPENDAL_REGION": opts.repo_region,
        }
        if opts.repo_access_key:
            opts.repo_opendal_env["OPENDAL_ACCESS_KEY_ID"] = (
                opts.repo_access_key
            )
        if opts.repo_secret_key:
            opts.repo_opendal_env["OPENDAL_SECRET_ACCESS_KEY"] = (
                opts.repo_secret_key
            )
    if streaming:
        if shutil.which("qemu-storage-daemon") is None:
            sys.exit(
                "error: repository mode needs 'qemu-storage-daemon' "
                "in PATH (to flatten images into the repository)"
            )
        if importlib.util.find_spec("nbd") is None:
            sys.exit(
                "error: repository mode needs the python module 'nbd' "
                "(package python3-libnbd)"
            )
    if opts.repo.startswith("s3:") and not (
        (opts.repo_access_key and opts.repo_secret_key)
        or (
            os.environ.get("AWS_ACCESS_KEY_ID")
            and os.environ.get("AWS_SECRET_ACCESS_KEY")
        )
    ):
        print(
            "warning: s3 repository but no access_key/secret_key "
            "configured ([repo] section) and no AWS_ACCESS_KEY_ID/"
            "AWS_SECRET_ACCESS_KEY in the environment",
            file=sys.stderr,
        )


def repo_env(opts):
    """Environment for rustic/restic: repository URL, password and S3
    credentials travel via env only, never on the command line."""
    prefix = "RUSTIC" if opts.repo_bin_name == "rustic" else "RESTIC"
    env = os.environ.copy()
    env[f"{prefix}_REPOSITORY"] = (
        getattr(opts, "repo_rustic_repo", None) or opts.repo
    )
    env.update(getattr(opts, "repo_opendal_env", None) or {})
    if opts.repo_password_file:
        env[f"{prefix}_PASSWORD_FILE"] = opts.repo_password_file
    if opts.repo_access_key:
        env["AWS_ACCESS_KEY_ID"] = opts.repo_access_key
    if opts.repo_secret_key:
        env["AWS_SECRET_ACCESS_KEY"] = opts.repo_secret_key
    if opts.repo_region:
        env["AWS_REGION"] = opts.repo_region
    return env


def read_text(path):
    """The file contents decoded lossily; '' when unreadable."""
    try:
        with open(path, "rb") as fh:
            return fh.read().decode(errors="replace").strip()
    except OSError:
        return ""


def repo_stream(opts, env, log, jobfile, stdin_name, tags):
    """Flatten the qcow2 overlay and stream it as raw into the repository.

    qemu-img convert cannot write raw output to a pipe (it insists on a
    seekable file), so the flattening goes through NBD instead:
    qemu-storage-daemon exposes the overlay (with its backing chain) as
    a read-only raw NBD export on a unix socket, this process reads it
    back with libnbd and feeds it into 'rustic backup -'.  The snapshot
    is only committed when the whole stream succeeded, so a broken pipe
    can never destroy the previous generation.  Raw (not qcow2) because
    qemu-img's qcow2 output is not content-stable and would dedup
    badly; the repository compresses anyway."""
    try:
        import nbd
    except ImportError:
        raise BackupError(
            "repository mode needs the 'nbd' python "
            "module (package python3-libnbd) to stream "
            "images"
        ) from None
    binname = opts.repo_bin_name
    # --force is essential: without it rustic can treat a stdin file as
    # 'unchanged' when the previous snapshot has the same stdin-filename
    # and skip reading the stream entirely, silently storing an empty
    # snapshot (rustic bug #1793); restic always reads stdin anyway
    if binname == "rustic":
        backup_args = [
            "backup",
            "--force",
            "-",
            "--stdin-filename",
            stdin_name,
        ]
    else:
        backup_args = [
            "backup",
            "--force",
            "--stdin",
            "--stdin-filename",
            stdin_name,
        ]
    log.info(
        "streaming %s to repository as %s (raw, deduplicated)",
        jobfile,
        stdin_name,
    )
    tmpdir = tempfile.mkdtemp(prefix="libvirt-backup-")
    sock = os.path.join(tmpdir, "nbd.sock")
    # both subprocesses log to files in tmpdir: their stderr must never
    # sit in an unread pipe while this process blocks writing to their
    # stdin (a full pipe buffer would deadlock the whole stream)
    daemon_log = os.path.join(tmpdir, "storage-daemon.log")
    client_log = os.path.join(tmpdir, f"{binname}.log")
    with open(daemon_log, "wb") as dlog:
        daemon = subprocess.Popen(
            [
                "qemu-storage-daemon",
                "--blockdev",
                f"driver=file,filename={jobfile},node-name=base0",
                "--blockdev",
                "driver=qcow2,file=base0,node-name=disk0,read-only=on",
                "--nbd-server",
                f"addr.type=unix,addr.path={sock}",
                "--export",
                "nbd,id=exp0,node-name=disk0,writable=off",
            ],
            stdout=subprocess.DEVNULL,
            stderr=dlog,
        )
    try:
        for _ in range(300):  # the daemon creates the socket when ready
            if os.path.exists(sock):
                break
            if daemon.poll() is not None:
                raise BackupError(
                    f"qemu-storage-daemon exited ({daemon.returncode}): "
                    f"{read_text(daemon_log)}"
                )
            time.sleep(0.1)
        else:
            raise BackupError(
                f"qemu-storage-daemon did not create its NBD socket {sock}"
            )
        try:
            with open(client_log, "wb") as clog:
                proc = subprocess.Popen(
                    [opts.repo_bin_resolved] + backup_args + tags,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=clog,
                    env=env,
                )
        except OSError as err:
            raise BackupError(f"cannot run {binname}: {err}") from err
        h = None
        try:
            h = nbd.NBD()
            h.connect_uri(f"nbd+unix:///disk0?socket={sock}")
            size = h.get_size()
            done = 0
            next_report = 10 << 30  # log progress every 10 GiB
            while done < size:
                count = min(1 << 20, size - done)
                proc.stdin.write(h.pread(count, done))
                done += count
                if done >= next_report:
                    log.info(
                        "streamed %s of %s (%s%%)",
                        human(done),
                        human(size),
                        done * 100 // size,
                    )
                    next_report += 10 << 30
            proc.stdin.close()
        except BrokenPipeError as err:
            raise BackupError(
                f"{binname} died while reading '{stdin_name}': "
                f"{read_text(client_log)}"
            ) from err
        except nbd.Error as err:
            raise BackupError(
                f"NBD read of '{jobfile}' failed: {err}"
            ) from err
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            if h is not None:
                try:
                    h.close()
                except nbd.Error:
                    pass
        proc.wait()
        if proc.returncode != 0:
            detail = read_text(client_log)
            if repo_lock_error(opts, detail):
                repo_unlock_stale(opts, env)
                detail += (
                    "; the repository lock was stale and has been "
                    "removed via 'unlock' - rerun this backup"
                )
            raise BackupError(
                f"{binname} backup of '{stdin_name}' failed "
                f"(exit {proc.returncode}): {detail}"
            )
        log.info("repository snapshot committed for %s", stdin_name)
    finally:
        daemon.terminate()
        try:
            daemon.wait(10)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()
        shutil.rmtree(tmpdir, ignore_errors=True)


def repo_backup_files(opts, env, log, paths, tags):
    """Back up small files (domain XML, backup XML) to the repository."""
    binname = opts.repo_bin_name
    proc = subprocess.run(
        [opts.repo_bin_resolved, "backup"] + paths + tags,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0 and repo_lock_error(
        opts, (proc.stderr or "") + (proc.stdout or "")
    ):
        repo_unlock_stale(opts, env)
        proc = subprocess.run(
            [opts.repo_bin_resolved, "backup"] + paths + tags,
            capture_output=True,
            text=True,
            env=env,
        )
    if proc.returncode != 0:
        raise BackupError(
            f"{binname} backup of config files failed "
            f"(exit {proc.returncode}): {proc.stderr.strip()}"
        )
    log.info(
        "uploaded to repository: %s",
        ", ".join(os.path.basename(p) for p in paths),
    )


def verify_image(path, log):
    """qemu-img check a finished image; a broken image fails the backup."""
    proc = subprocess.run(
        ["qemu-img", "check", path], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise BackupError(
            f"verification of '{path}' failed: "
            f"{(proc.stdout + proc.stderr).strip()}"
        )
    lines = (proc.stdout or "").strip().splitlines()
    log.info("verified image %s: %s", path, lines[-1] if lines else "ok")


def repo_lock_error(opts, text):
    """Whether failed repository-client output complains about a
    repository lock.  Only restic stores exclusive locks inside the
    repository (borg break-lock style); rustic is lock-free by design
    and neither takes nor honours such locks, so it never hits this."""
    if getattr(opts, "repo_bin_name", None) != "restic":
        return False
    return "already locked" in text or "lock already" in text


def repo_unlock_stale(opts, env):
    """restic's break-lock equivalent: 'restic unlock' removes stale
    locks (older than 30 minutes) left behind by killed runs; a fresh
    lock of a live run is never touched."""
    subprocess.run(
        [opts.repo_bin_resolved, "unlock"],
        env=env,
        capture_output=True,
        text=True,
    )


def repo_run(opts, env, args, capture_json=False):
    """Run the repository client; raise BackupError on failure.

    restic can be blocked by a stale repository lock from a killed run;
    the tool then runs 'restic unlock' (stale locks only) once and
    retries.  rustic is lock-free and needs no such handling.
    """
    proc = subprocess.run(
        [opts.repo_bin_resolved] + args,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0 and repo_lock_error(
        opts, (proc.stderr or "") + (proc.stdout or "")
    ):
        repo_unlock_stale(opts, env)
        proc = subprocess.run(
            [opts.repo_bin_resolved] + args,
            capture_output=True,
            text=True,
            env=env,
        )
    if proc.returncode != 0:
        raise BackupError(
            f"{opts.repo_bin_name} {' '.join(args[:1])} "
            f"failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout if capture_json else None


def json_parse(text, what):
    """json.loads that reports malformed repository-client output as
    BackupError (with a snippet) instead of a raw traceback."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise BackupError(
            f"{what}: repository client returned invalid JSON ({err}): "
            f"{text[:200]!r}"
        ) from err


def repo_snapshots(opts, env):
    """All snapshots, normalised to [{id, time, tags, paths}]."""
    data = json_parse(
        repo_run(opts, env, ["snapshots", "--json"], capture_json=True),
        "snapshots --json",
    )
    snaps = []
    if data and isinstance(data[0], dict) and "group_key" in data[0]:
        for group in data:  # rustic: grouped output
            for s in group["snapshots"]:
                snaps.append(
                    {
                        "id": s["id"],
                        "time": s.get("time", ""),
                        "tags": s.get("tags") or [],
                        "paths": s.get("paths")
                        or group["group_key"].get("paths", []),
                    }
                )
    else:
        for s in data:  # restic: flat list
            snaps.append(
                {
                    "id": s["id"],
                    "time": s.get("time", ""),
                    "tags": s.get("tags") or [],
                    "paths": s.get("paths", []),
                }
            )
    return snaps


def repo_files(opts, env, snap_id):
    """Artefact file paths inside one snapshot (no leading slash).

    rustic's 'ls --json' lists directories too and offers no type
    field, so only the names libvirt-backup.py writes are kept."""
    out = repo_run(opts, env, ["ls", "--json", snap_id], capture_json=True)
    if opts.repo_bin_name == "rustic":
        paths = [p.lstrip("/") for p in json_parse(out, "ls --json")]
    else:  # restic: JSONL, one node object per line
        paths = []
        for line in out.splitlines():
            try:
                node = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                node.get("struct_type") == "node"
                and node.get("type") == "file"
            ):
                paths.append(node["path"].lstrip("/"))
    return [
        p
        for p in paths
        if p.endswith(".raw")
        or p.endswith("-config.xml")
        or p.endswith("-backup.xml")
    ]


def repo_dump(opts, env, snap_id, path, dest_path):
    """Extract one file from the repository to dest_path."""
    if opts.repo_bin_name == "rustic":
        args = ["dump", f"{snap_id}:{path}"]
    else:
        args = ["dump", snap_id, path]
    with open(dest_path, "wb") as out:
        proc = subprocess.run(
            [opts.repo_bin_resolved] + args,
            stdout=out,
            stderr=subprocess.PIPE,
            env=env,
        )
    if proc.returncode != 0:
        os.unlink(dest_path)
        raise BackupError(
            f"could not dump '{path}' from snapshot "
            f"{snap_id[:8]}: {proc.stderr.decode(errors='replace').strip()}"
        )


def repo_verify(opts, env, log, tagvals, expected):
    """Confirm that the snapshots committed under these tags contain all
    expected files; a missing file means the data did not land."""
    present = set()
    for s in repo_snapshots(opts, env):
        if all(t in s["tags"] for t in tagvals):
            present.update(s["paths"])
    missing = [e for e in expected if e not in present]
    if missing:
        raise BackupError(
            "verification failed, missing from repository "
            "snapshots: " + ", ".join(missing)
        )
    log.info("verified repository snapshots: %s", ", ".join(expected))


# ---------------------------------------------------------------------------
# restore mode
# ---------------------------------------------------------------------------


def select_snapshots(opts, snapshots):
    """The snapshots of one VM run: --snap, --run, or the newest run."""
    vm_snaps = [s for s in snapshots if f"vm:{opts.vm}" in s["tags"]]
    if not vm_snaps:
        vms = sorted(
            {
                t[3:]
                for s in snapshots
                for t in s["tags"]
                if t.startswith("vm:")
            }
        )
        raise BackupError(
            f"no snapshots with tag 'vm:{opts.vm}' in the repository"
            + ("; available: " + ", ".join(vms) if vms else "")
        )
    if opts.snap:
        chosen = [s for s in vm_snaps if s["id"].startswith(opts.snap)]
        if not chosen:
            raise BackupError(
                f"no snapshot of '{opts.vm}' with id prefix '{opts.snap}'"
            )
        return chosen
    runs = {}
    for s in vm_snaps:
        run = next(
            (t[4:] for t in s["tags"] if t.startswith("run:")), s["time"]
        )
        runs.setdefault(run, []).append(s)
    if opts.run:
        if opts.run not in runs:
            raise BackupError(
                f"no run '{opts.run}' for '{opts.vm}'; "
                f"available: {', '.join(sorted(runs))}"
            )
        return runs[opts.run]
    newest = max(runs)
    return runs[newest]


def repo_dest_name(path, vm):
    """Map a repository path to the restored filename."""
    base = os.path.basename(path)
    if path.endswith(".raw") and "/" in path:
        return f"{vm}-backup-{base[:-4]}.raw"
    return base


def list_directory(opts):
    """Print the restorable VMs and generations in opts.src_dir.

    Raises BackupError when the directory does not exist.
    """
    src = opts.src_dir
    if not os.path.isdir(src):
        raise BackupError(f"backup directory '{src}' does not exist")
    vms = sorted(
        f[: -len("-config.xml")]
        for f in os.listdir(src)
        if f.endswith("-config.xml")
    )
    if not vms:
        print(f"no VM artefacts (<vm>-config.xml) found in {src}")
        return
    for vm in vms:
        images = sorted(
            f
            for f in os.listdir(src)
            if (f.startswith(f"{vm}-backup-") or f.startswith(f"{vm}-incr-"))
            and not f.endswith(".job")
        )
        print(f"{vm}:")
        for img in images:
            size = human(os.path.getsize(os.path.join(src, img)))
            print(f"    {img}  ({size})")
        if not images:
            print("    (no images)")
        if os.path.exists(os.path.join(src, f"{vm}-chain.json")):
            print("    (incremental chain: restore merges it automatically)")


def list_repo(opts, env):
    """Print the restorable VMs, runs and snapshots of the repository."""
    snaps = repo_snapshots(opts, env)
    runs = {}
    for s in snaps:
        vm = next((t[3:] for t in s["tags"] if t.startswith("vm:")), "?")
        run = next(
            (t[4:] for t in s["tags"] if t.startswith("run:")), s["time"]
        )
        runs.setdefault((vm, run), []).append(s)
    if not runs:
        print("no tagged snapshots in the repository")
        return
    for (vm, run), group in sorted(runs.items()):
        ids = ", ".join(s["id"][:8] for s in group)
        paths = sorted(
            p for s in group for p in repo_files(opts, env, s["id"])
        )
        print(f"{vm}  run {run}  [{ids}]")
        for path in paths:
            print(f"    {path}")


def prepare_dest(opts, log):
    """Create opts.dest; raise BackupError when it is not a usable
    directory, or is not empty and --overwrite was not given."""
    if os.path.exists(opts.dest):
        try:
            existing = os.listdir(opts.dest)
        except OSError as err:
            raise BackupError(f"destination '{opts.dest}': {err}") from err
        if existing and not opts.overwrite:
            raise BackupError(
                f"destination '{opts.dest}' is not empty (use --overwrite)"
            )
    os.makedirs(opts.dest, exist_ok=True)


def restore_directory(opts, log):
    """Copy the artefacts of VM opts.vm (base image, incremental chain,
    XML files) from opts.src_dir into opts.dest."""
    src = opts.src_dir
    if not os.path.isdir(src):
        raise BackupError(f"backup directory '{src}' does not exist")
    names = os.listdir(src)
    prefix = f"{opts.vm}-"
    artefacts = [
        f
        for f in names
        if f == f"{opts.vm}-config.xml"
        or f == f"{opts.vm}-backup.xml"
        or (f.startswith(prefix + "backup-") and not f.endswith(".job"))
        or f.startswith(prefix + "incr-")
        or f == f"{opts.vm}-chain.json"
    ]
    if not [f for f in artefacts if not f.endswith("-chain.json")]:
        raise BackupError(f"no artefacts for '{opts.vm}' in '{src}'")
    prepare_dest(opts, log)
    for name in sorted(artefacts):
        source = os.path.join(src, name)
        target = os.path.join(opts.dest, name)
        log.info("copying %s (%s)", name, human(os.path.getsize(source)))
        with open(source, "rb") as fin, open(target, "wb") as fout:
            while chunk := fin.read(1 << 20):
                fout.write(chunk)
    merge_restored_chain(opts, log)


def merge_restored_chain(opts, log):
    """Fold the restored incremental images into the restored full
    backup images, then drop the now-merged chain file."""
    path = os.path.join(opts.dest, f"{opts.vm}-chain.json")
    if not os.path.exists(path):
        return
    try:
        with open(path) as fh:
            chain = json.load(fh)
    except (OSError, json.JSONDecodeError) as err:
        log.warning("ignoring unreadable chain file %s: %s", path, err)
        return
    merged = failed = False
    for dev, info in sorted((chain.get("disks") or {}).items()):
        base = os.path.join(opts.dest, info.get("base") or "")
        incrs = [
            os.path.join(opts.dest, f)
            for f in info.get("incr", [])
            if os.path.exists(os.path.join(opts.dest, f))
        ]
        if not incrs:
            continue
        if not info.get("base") or not os.path.exists(base):
            log.warning(
                "chain: base image '%s' not restored, cannot "
                "merge %d incremental(s) for '%s'",
                info.get("base"),
                len(incrs),
                dev,
            )
            failed = True
            continue
        log.info(
            "merging %d incremental image(s) into %s",
            len(incrs),
            os.path.basename(base),
        )
        merge_chain(base, incrs, log)
        merged = True
    if merged and not failed:
        os.unlink(path)
        log.info(
            "incremental chain merged, %s removed", os.path.basename(path)
        )


def restore_repo(opts, env, log):
    """Restore the snapshots of VM opts.vm from the repository into
    opts.dest."""
    snaps = select_snapshots(opts, repo_snapshots(opts, env))
    log.info(
        "restoring %d snapshot(s) of '%s' from %s",
        len(snaps),
        opts.vm,
        opts.repo,
    )
    prepare_dest(opts, log)
    for snap in snaps:
        for path in repo_files(opts, env, snap["id"]):
            name = repo_dest_name(path, opts.vm)
            target = os.path.join(opts.dest, name)
            log.info("extracting %s -> %s", path, name)
            repo_dump(opts, env, snap["id"], path, target)
            log.info("    %s (%s)", name, human(os.path.getsize(target)))


def verify_dest(opts, log):
    """qemu-img check on qcow2 images; report raw image sizes."""
    if shutil.which("qemu-img") is None:
        log.warning("--verify: qemu-img not found, skipping image checks")
        return
    for name in sorted(os.listdir(opts.dest)):
        path = os.path.join(opts.dest, name)
        if name.endswith(".qcow2"):
            verify_image(path, log)
        elif name.endswith(".raw"):
            log.info(
                "restored raw image %s (%s)",
                name,
                human(os.path.getsize(path)),
            )


def rewrite_sources(opts, log):
    """Point the disk sources in the restored <vm>-config.xml at the
    restored image files (matched by target device name)."""
    cfg = os.path.join(opts.dest, f"{opts.vm}-config.xml")
    if not os.path.exists(cfg):
        log.warning("--rewrite: %s not found, nothing rewritten", cfg)
        return
    tree = ET.parse(cfg)
    root = tree.getroot()
    images = {
        f[len(opts.vm) + len("-backup-") :].rsplit(".", 1)[0]: f
        for f in os.listdir(opts.dest)
        if f.startswith(f"{opts.vm}-backup-") and not f.endswith(".xml")
    }
    changed = 0
    for disk in root.iter("disk"):
        target = disk.find("target")
        if target is None:
            continue
        dev = target.get("dev")
        if dev not in images:
            continue
        source = disk.find("source")
        if source is None:
            source = ET.SubElement(disk, "source")
        for attr in ("file", "dev", "name", "protocol"):
            if attr in source.attrib:
                del source.attrib[attr]
        disk.set("type", "file")
        source.set(
            "file", os.path.join(os.path.abspath(opts.dest), images[dev])
        )
        changed += 1
        log.info("rewrote disk '%s' source -> %s", dev, images[dev])
    if not changed:
        log.warning("--rewrite: no disk source matched a restored image")
        return
    ET.indent(tree, space="  ")
    with open(cfg, "w") as out:
        out.write(ET.tostring(root, encoding="unicode") + "\n")
    log.info("updated %s (%d disk source(s) rewritten)", cfg, changed)


# ---------------------------------------------------------------------------
# retention (forget) and repository check
# ---------------------------------------------------------------------------

RETENTION_KEYS = (
    "keep_last",
    "keep_daily",
    "keep_weekly",
    "keep_monthly",
    "keep_yearly",
    "keep_within",
    "prune",
)


def apply_retention_config(opts, cfg_path, ret_cfg):
    """Apply the [retention] section (CLI options win)."""
    if not isinstance(ret_cfg, dict):
        sys.exit(f"error: {cfg_path}: [retention] must be a table")
    for key in ret_cfg:
        if key not in RETENTION_KEYS:
            print(
                f"warning: {cfg_path}: [retention] unknown option "
                f"'{key}' ignored",
                file=sys.stderr,
            )
    for key in (
        "keep_last",
        "keep_daily",
        "keep_weekly",
        "keep_monthly",
        "keep_yearly",
    ):
        if getattr(opts, key) is None and key in ret_cfg:
            value = ret_cfg[key]
            if not isinstance(value, int) or value < 0:
                sys.exit(
                    f"error: {cfg_path}: [retention] {key} must be "
                    f"a non-negative integer"
                )
            setattr(opts, key, value)
    if opts.keep_within is None:
        opts.keep_within = ret_cfg.get("keep_within")
    if opts.prune is None:
        opts.prune = bool(ret_cfg.get("prune", True))


def parse_duration(text):
    """'30d' -> seconds; suffixes s/m/h/d/w, a plain number is seconds."""
    m = re.fullmatch(r"(\d+)([smhdw])?", str(text).strip())
    if not m:
        raise BackupError(f"invalid duration '{text}' (use e.g. 48h, 30d, 4w)")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}.get(
        m.group(2), 1
    )
    return int(m.group(1)) * mult


def run_epoch(run_key, snap_time):
    """Unix time of a run: parse the run:<YYYYMMDDTHHMMSS> tag, else the
    snapshot's ISO timestamp; 0 when neither parses."""
    try:
        return datetime.strptime(run_key, "%Y%m%dT%H%M%S").timestamp()
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(snap_time).timestamp()
    except (ValueError, TypeError):
        return 0.0


def repo_last_success(snapshots):
    """{vm: epoch of the newest run} over our own tagged snapshots.
    Repository mode derives each VM's last successful backup from the
    shared repository, so the answer is identical on every host that
    writes to it - a VM that just live-migrated over is not 'new'."""
    last = {}
    for s in snapshots:
        vm = next((t[3:] for t in s["tags"] if t.startswith("vm:")), None)
        if vm is None:
            continue
        run = next((t[4:] for t in s["tags"] if t.startswith("run:")), "")
        epoch = run_epoch(run, s["time"])
        if epoch > last.get(vm, 0.0):
            last[vm] = epoch
    return last


def retention_groups(snapshots):
    """{vm: {run: {"epoch": float, "ids": [...]}}} over our own tagged
    snapshots; untagged (foreign) snapshots are never touched."""
    groups = {}
    for s in snapshots:
        vm = next((t[3:] for t in s["tags"] if t.startswith("vm:")), None)
        if vm is None:
            continue
        run = next(
            (t[4:] for t in s["tags"] if t.startswith("run:")), s["time"]
        )
        entry = groups.setdefault(vm, {}).setdefault(
            run, {"epoch": run_epoch(run, s["time"]), "ids": []}
        )
        entry["ids"].append(s["id"])
    return groups


def kept_runs(runs, opts):
    """Which runs of one VM survive the policy: union of keep-last,
    keep-within and one run per day/week/month/year bucket; the newest
    run is always kept."""
    order = sorted(runs, key=lambda k: runs[k]["epoch"], reverse=True)
    keep = set(order[: opts.keep_last])
    if opts.keep_within:
        limit = runs[order[0]]["epoch"] - parse_duration(opts.keep_within)
        keep.update(k for k in order if runs[k]["epoch"] >= limit)
    buckets = {
        "keep_daily": lambda d: d.date(),
        "keep_weekly": lambda d: d.isocalendar()[:2],
        "keep_monthly": lambda d: (d.year, d.month),
        "keep_yearly": lambda d: d.year,
    }
    for opt, bucket in buckets.items():
        n = getattr(opts, opt)
        if not n:
            continue
        seen = []
        for k in order:
            stamp = datetime.fromtimestamp(runs[k]["epoch"])
            if bucket(stamp) not in seen:
                seen.append(bucket(stamp))
            if seen.index(bucket(stamp)) < n:
                keep.add(k)
    keep.add(order[0])
    return keep


def main_forget(opts):
    """'forget' subcommand: apply the retention policy to the
    repository; returns the process exit code."""
    logging.basicConfig(
        level=logging.WARNING if opts.quiet else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(message)s",
    )
    log = logging.getLogger("libvirt-forget")
    cfg_path, cfg = load_config(opts.config)
    if cfg_path:
        log.info("using config file: %s", cfg_path)
    apply_repo_config(opts, cfg_path or "config", cfg.get("repo", {}))
    apply_retention_config(
        opts, cfg_path or "config", cfg.get("retention", {})
    )
    for key in (
        "keep_last",
        "keep_daily",
        "keep_weekly",
        "keep_monthly",
        "keep_yearly",
    ):
        setattr(opts, key, getattr(opts, key) or 0)
    if not opts.repo:
        log.warning(
            "retention applies to repository mode only "
            "(--repo / [repo]); nothing to do"
        )
        return 0
    resolve_repo_bin(opts)
    env = repo_env(opts)
    if not (
        opts.keep_last
        or opts.keep_daily
        or opts.keep_weekly
        or opts.keep_monthly
        or opts.keep_yearly
        or opts.keep_within
    ):
        sys.exit(
            "error: no retention policy: set [retention] keep_daily "
            "etc. in the config file or pass --keep-* options"
        )
    try:
        groups = retention_groups(repo_snapshots(opts, env))
        forget_ids = []
        for vm in sorted(groups):
            runs = groups[vm]
            keep = kept_runs(runs, opts)
            for run in sorted(
                runs, key=lambda k: runs[k]["epoch"], reverse=True
            ):
                mark = "keep" if run in keep else "forget"
                log.info(
                    "%s  %s  run %s  (%d snapshot(s))",
                    mark,
                    vm,
                    run,
                    len(runs[run]["ids"]),
                )
                if run not in keep:
                    forget_ids.extend(runs[run]["ids"])
        if not forget_ids:
            log.info("nothing to forget")
            return 0
        args = ["forget"]
        if opts.dry_run:
            args.append("--dry-run")
        if opts.prune:
            args.append("--prune")
        out = repo_run(opts, env, args + forget_ids, capture_json=True)
        tail = [ln for ln in out.splitlines() if ln.strip()][-3:]
        for ln in tail:
            log.info("repository client: %s", ln)
        log.info(
            "%s %d snapshot(s) %s",
            "would forget" if opts.dry_run else "forgotten",
            len(forget_ids),
            "" if opts.prune else "(no prune)",
        )
    except BackupError as err:
        log.error("%s", err)
        return 1
    return 0


def image_backing(path):
    """The backing file recorded in a qcow2 image, or None when it is
    standalone; '?' when even the metadata is unreadable."""
    proc = subprocess.run(
        ["qemu-img", "info", "--output=json", path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return "?"
    try:
        info = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return "?"
    return info.get("full-backing-filename") or info.get("backing-filename")


def check_directory_images(log, backup_dir):
    """'check' in directory mode: qemu-img check every qcow2 image.
    Images with a recorded backing file (rebased chain members) are
    reported and skipped - only 'restore' merging the chain can
    verify those; the tool's own overlays do not record their backing
    file (the dependency on the VM's disk is implicit) and are checked
    as standalone; raw images have no structure to check."""
    if shutil.which("qemu-img") is None:
        log.error("check needs qemu-img (package qemu-utils)")
        return 1
    images = sorted(
        f for f in os.listdir(backup_dir) if f.endswith((".qcow2", ".raw"))
    )
    if not images:
        log.warning("no images found in %s", backup_dir)
        return 0
    failed = checked = 0
    for name in images:
        path = os.path.join(backup_dir, name)
        if name.endswith(".raw"):
            log.info(
                "%s: raw image (%s), nothing to check",
                name,
                human(os.path.getsize(path)),
            )
            continue
        backing = image_backing(path)
        if backing:
            log.info(
                "%s: skipped, has backing file '%s' (chain member; "
                "verified after restore merges the chain)",
                name,
                backing,
            )
            continue
        proc = subprocess.run(
            ["qemu-img", "check", path], capture_output=True, text=True
        )
        if proc.returncode != 0:
            failed += 1
            log.error(
                "FAILED %s: %s", name, (proc.stdout + proc.stderr).strip()
            )
        else:
            checked += 1
            log.info("OK %s", name)
    if failed:
        log.error("%d of %d image(s) failed", failed, len(images))
        return 1
    log.info(
        "checked %d of %d image(s) in %s", checked, len(images), backup_dir
    )
    return 0


def main_check(opts):
    """'check' subcommand: verify repository integrity; returns the
    process exit code."""
    logging.basicConfig(
        level=logging.WARNING if opts.quiet else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(message)s",
    )
    log = logging.getLogger("libvirt-check")
    cfg_path, cfg = load_config(opts.config)
    if cfg_path:
        log.info("using config file: %s", cfg_path)
    apply_repo_config(opts, cfg_path or "config", cfg.get("repo", {}))
    if not opts.repo:
        backup_dir = cfg.get("backup", {}).get(
            "backup_dir", "/var/lib/libvirt/backups"
        )
        if not isinstance(backup_dir, str) or not os.path.isdir(backup_dir):
            log.error(
                "directory mode check needs an existing [backup] "
                "backup_dir: %r",
                backup_dir,
            )
            return 1
        log.info("checking images in %s (directory mode)", backup_dir)
        return check_directory_images(log, backup_dir)
    resolve_repo_bin(opts)
    env = repo_env(opts)
    log.info("running '%s check' on %s", opts.repo_bin_name, opts.repo)
    proc = subprocess.run(
        [opts.repo_bin_resolved, "check"],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 and repo_lock_error(
        opts, (proc.stderr or "") + (proc.stdout or "")
    ):
        repo_unlock_stale(opts, env)
        proc = subprocess.run(
            [opts.repo_bin_resolved, "check"],
            env=env,
            capture_output=True,
            text=True,
        )
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            log.info("%s", line)
    if proc.returncode != 0:
        log.error(
            "repository check FAILED: %s", (proc.stderr or proc.stdout).strip()
        )
        return 1
    log.info("repository check passed")
    return 0


def main_init(opts):
    """'init' subcommand: create an empty repository; returns the
    process exit code."""
    logging.basicConfig(
        level=logging.WARNING if opts.quiet else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(message)s",
    )
    log = logging.getLogger("libvirt-init")
    cfg_path, cfg = load_config(opts.config)
    if cfg_path:
        log.info("using config file: %s", cfg_path)
    apply_repo_config(opts, cfg_path or "config", cfg.get("repo", {}))
    if not opts.repo:
        sys.exit("error: init needs a repository (--repo / [repo])")
    resolve_repo_bin(opts)
    env = repo_env(opts)
    log.info("running '%s init' on %s", opts.repo_bin_name, opts.repo)
    proc = subprocess.run(
        [opts.repo_bin_resolved, "init"],
        env=env,
        capture_output=True,
        text=True,
    )
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            log.info("%s", line)
    if proc.returncode != 0:
        log.error(
            "repository init FAILED: %s", (proc.stderr or proc.stdout).strip()
        )
        return 1
    log.info("repository initialized")
    return 0


# ---------------------------------------------------------------------------
# per-domain backup
# ---------------------------------------------------------------------------


def backup_domain(conn, dom, opts):
    """Run a full push backup for one domain.

    Returns a (summary, status) tuple where status is 'ok' or 'warn'
    (backup done, but e.g. the file systems could not be frozen or
    leftover checkpoints were seen).
    Raises BackupError/libvirtError for a failed backup ('fail').
    """
    name = dom.name()
    log = make_vm_logger(name, opts.backup_dir)
    freeze_mode = opts.freeze_mode
    started = False
    completed = False
    frozen = False
    warned = False
    entries = []
    try:
        # 1./2. live XML and the block devices that take part
        xml_desc = dom.XMLDesc(0)
        all_disks = list_disks(xml_desc, conn)
        for disk in all_disks:
            if disk["kind"] != "backupable":
                log.warning(
                    "device %s (%s) skipped: %s",
                    disk["target"],
                    disk["src"],
                    disk["note"],
                )
        disks = [d for d in all_disks if d["kind"] == "backupable"]
        if not disks:
            raise NoBackupableDisks(
                "no block devices usable for the libvirt backup API "
                "(see warnings above)"
            )
        log.info(
            "domain '%s' (id %d): %d block device(s): %s",
            name,
            dom.ID(),
            len(disks),
            ", ".join(f"{d['target']} -> {d['src']}" for d in disks),
        )

        # 3. backup image destinations + <domainbackup> XML file
        #    The backup job always writes a qcow2 overlay whose backing
        #    file is the VM's current disk.  In standalone mode (the
        #    default) that overlay goes to a '.job' temp file and is
        #    flattened with qemu-img convert into a self-contained final
        #    image afterwards; --no-standalone keeps the overlay instead.
        standalone = not opts.no_standalone
        repo_mode = bool(opts.repo)
        ts = time.strftime("%Y%m%dT%H%M%S")
        mode, chain = backup_mode(dom, name, disks, opts, log)
        # pre-check: checkpoints left on the VM are a dirty-state signal
        # (aborted backup run, foreign tooling); only this tool's own
        # chain checkpoint is expected, everything else gets reported
        expected_cp = (chain or {}).get("checkpoint")
        stale_cps = [c for c in list_checkpoints(dom) if c != expected_cp]
        for c in stale_cps:
            log.warning(
                "leftover checkpoint '%s' on this VM: usually the "
                "remnant of an aborted backup run or of foreign "
                "tooling; clean up with: virsh checkpoint-delete %s",
                c,
                c,
            )
        # a backup job still active on this VM (killed run whose block
        # job keeps copying, foreign tooling) blocks backupBegin, and
        # its overlay must not be touched; report it instead
        job = active_job_type(dom)
        if job is not None:
            raise BackupError(
                f"a backup job is still active on this VM (type {job}); "
                f"wait for it to finish or abort it with: "
                f"virsh job-abort {name}"
            )
        # A checkpoint is only useful for incremental chains (directory
        # mode); raw/block/iSCSI disks reject them, so only attempt one
        # when incrementals are enabled for this VM.
        want_cp = bool(opts.incremental) and not repo_mode
        cp_name = f"libvirt-backup-{ts}" if want_cp else None
        entries = []
        for disk in disks:
            if repo_mode:
                # no local final image: the overlay is streamed to the
                # repository as raw and removed afterwards
                dest = None
                jobfile = os.path.join(
                    opts.backup_dir,
                    f"{name}-backup-{disk['target']}.qcow2.job",
                )
            elif mode == "incr":
                # the overlay itself is the incremental backup: it only
                # holds the clusters written since the checkpoint, which
                # 'restore' folds into the full image (merge_chain)
                dest = os.path.join(
                    opts.backup_dir, f"{name}-incr-{ts}-{disk['target']}.qcow2"
                )
                jobfile = dest
            else:
                dest = os.path.join(
                    opts.backup_dir,
                    f"{name}-backup-{disk['target']}.{opts.dest_format}",
                )
                jobfile = dest + ".job" if standalone else dest
            for path in dict.fromkeys(p for p in (dest, jobfile) if p):
                if not os.path.exists(path):
                    continue
                if path.endswith(".job"):
                    # intermediate of an aborted run; no backup job is
                    # active (checked above), so it is dead weight and
                    # gets discarded instead of failing the whole VM
                    os.unlink(path)
                    log.warning(
                        "removed stale backup job file %s left "
                        "behind by an aborted run",
                        path,
                    )
                elif opts.overwrite:
                    os.unlink(path)
                    log.info("removed old backup image %s", path)
                else:
                    raise BackupError(
                        f"backup image '{path}' already exists "
                        f"(use --overwrite to replace it)"
                    )
            entries.append(
                {"target": disk["target"], "dest": dest, "jobfile": jobfile}
            )

        backup_xml = build_backup_xml(
            entries,
            incremental=(chain["checkpoint"] if mode == "incr" else None),
        )
        backup_xml_path = os.path.join(opts.backup_dir, f"{name}-backup.xml")
        write_text(backup_xml_path, backup_xml, log)

        # 4. fsfreeze -> backup-begin -> fsthaw
        #    freeze_mode: 'try' (default) = try and continue with a loud
        #    warning + WARN status; 'no' = never try; 'require' = fail.
        if freeze_mode != "no":
            try:
                dom.fsFreeze([])
                frozen = True
                log.info("guest file systems frozen")
            except libvirt.libvirtError as err:
                if freeze_mode == "require":
                    raise BackupError(f"domfsfreeze failed: {err}") from err
                warned = True
                log.warning("!" * 72)
                log.warning(
                    "fsFreeze FAILED (%s): the guest file systems "
                    "could NOT be frozen!",
                    err,
                )
                log.warning(
                    "This backup is only CRASH-CONSISTENT, i.e. it "
                    "is like the power was pulled; file systems and "
                    "databases may need recovery after a restore."
                )
                log.warning(
                    "Expected for guests without qemu-guest-agent "
                    "or without fsfreeze support (e.g. FreeBSD) or "
                    "with a misconfigured agent."
                )
                log.warning("!" * 72)
        checkpoint_xml = (
            build_checkpoint_xml(cp_name, [e["target"] for e in entries])
            if want_cp
            else None
        )
        try:
            try:
                dom.backupBegin(backup_xml, checkpoint_xml, 0)
            except libvirt.libvirtError as err:
                if mode == "incr" or checkpoint_xml is None:
                    raise
                log.warning(
                    "backupBegin with checkpoint failed (%s); "
                    "retrying without checkpoint - this VM will "
                    "get full backups only",
                    err,
                )
                cp_name = None
                dom.backupBegin(backup_xml, None, 0)
            started = True
            if mode == "incr":
                log.info(
                    "incremental backup job started (changes since "
                    "checkpoint '%s')",
                    chain["checkpoint"],
                )
            else:
                log.info(
                    "full backup job started (virsh backup-begin equivalent)"
                )
        finally:
            if frozen:
                try:
                    dom.fsThaw([])
                    log.info("guest file systems thawed")
                except libvirt.libvirtError as err:
                    warned = True
                    log.warning(
                        "fsThaw failed: %s - the guest file systems "
                        "may still be frozen, check the guest!",
                        err,
                    )

        # 5. monitor progress until the backup job is gone
        monitor_backup(
            dom,
            [e["target"] for e in entries],
            opts.poll_interval,
            opts.timeout,
            log,
        )
        completed = True
        log_completed_job_stats(dom, log)

        # 5b. flatten the overlay into a standalone image (default), or
        #     stream it into the rustic/restic repository (repo mode)
        if repo_mode:
            env = repo_env(opts)
            run_tag = "run:" + time.strftime("%Y%m%dT%H%M%S")
            tags = ["--tag", f"vm:{name}", "--tag", run_tag]
            for entry in entries:
                repo_stream(
                    opts,
                    env,
                    log,
                    entry["jobfile"],
                    f"{name}/{entry['target']}.raw",
                    tags,
                )
                os.unlink(entry["jobfile"])
                log.info("overlay %s removed after upload", entry["jobfile"])
            config_xml_path = os.path.join(
                opts.backup_dir, f"{name}-config.xml"
            )
            write_text(config_xml_path, dom.XMLDesc(0), log)
            repo_backup_files(
                opts, env, log, [backup_xml_path, config_xml_path], tags
            )
            if opts.verify:
                repo_verify(
                    opts,
                    env,
                    log,
                    [f"vm:{name}", run_tag],
                    [f"{name}/{e['target']}.raw" for e in entries]
                    + [backup_xml_path, config_xml_path],
                )
            log.info(
                "streamed to repository %s: %s",
                opts.repo,
                ", ".join(f"{name}/{e['target']}.raw" for e in entries),
            )
            return (
                f"backed up {len(entries)} block device(s) to repo"
                + (
                    ", file systems NOT frozen (crash-consistent only)"
                    if warned
                    else ""
                ),
                "warn" if warned else "ok",
            )
        if standalone and mode != "incr":
            for entry in entries:
                flatten_to_standalone(
                    entry["jobfile"],
                    entry["dest"],
                    opts.dest_format,
                    opts.compress,
                    log,
                )
        if opts.verify:
            for entry in entries:
                verify_image(
                    entry["dest"] if standalone else entry["jobfile"], log
                )

        # 5c. checkpoint / chain bookkeeping (only after success)
        old_cp = (chain or {}).get("checkpoint")
        if cp_name:
            if mode == "incr":
                for entry in entries:
                    dchain = chain["disks"].setdefault(
                        entry["target"], {"base": None, "incr": []}
                    )
                    dchain["incr"].append(os.path.basename(entry["dest"]))
                chain["checkpoint"] = cp_name
            else:
                chain = {
                    "checkpoint": cp_name,
                    "base_ts": ts,
                    "disks": {
                        e["target"]: {
                            "base": os.path.basename(e["dest"]),
                            "incr": [],
                        }
                        for e in entries
                    },
                }
            save_chain(opts.backup_dir, name, chain)
            log.info(
                "chain updated: checkpoint '%s' (%s)",
                cp_name,
                "incremental added" if mode == "incr" else "new full base",
            )
        elif old_cp:
            # full backup without a checkpoint: the old chain no longer
            # matches the freshly overwritten base image
            log.warning(
                "no checkpoint available: dropping the chain of "
                "'%s', next backup starts a new full backup",
                name,
            )
            try:
                os.unlink(chain_path(opts.backup_dir, name))
            except OSError:
                pass
        if old_cp and old_cp != cp_name:
            delete_checkpoint(dom, old_cp, log)

        log.info("backup image(s):")
        for entry in entries:
            size = (
                os.path.getsize(entry["dest"])
                if os.path.exists(entry["dest"])
                else 0
            )
            log.info("  %s (%s)", entry["dest"], human(size))
        notes = []
        if warned:
            notes.append("file systems NOT frozen (crash-consistent only)")
        if stale_cps:
            notes.append(f"{len(stale_cps)} leftover checkpoint(s) - see log")
        return (
            f"{'incremental' if mode == 'incr' else 'full'} backup of "
            f"{len(entries)} block device(s)"
            + (", " + "; ".join(notes) if notes else ""),
            "warn" if notes else "ok",
        )

    except Exception as err:
        # anything unexpected is still this VM's failure, not the
        # whole run's; logged and re-raised, main_backup isolates it
        log.error("backup of '%s' failed: %s", name, err)
        raise

    finally:
        # abort an unfinished backup job so the host is left clean
        if started and not completed:
            log.warning("aborting unfinished backup job")
            try:
                dom.abortJobFlags(0)
            except libvirt.libvirtError as err:
                log.error("could not abort backup job: %s", err)

        # discard intermediate overlays of a failed run (a successful
        # run removes or flattens them itself); they cannot be resumed
        # and would only litter the backup directory until the next
        # run has to clear them away
        for entry in entries:
            jf = entry.get("jobfile") or ""
            if jf.endswith(".job") and os.path.exists(jf):
                try:
                    os.unlink(jf)
                    log.warning("removed incomplete intermediate %s", jf)
                except OSError as err:
                    log.error("could not remove intermediate %s: %s", jf, err)

        # 6. dumpxml alongside the backup image and backup config
        try:
            write_text(
                os.path.join(opts.backup_dir, f"{name}-config.xml"),
                dom.XMLDesc(0),
                log,
            )
        except libvirt.libvirtError as err:
            log.error("could not save domain XML: %s", err)

        for handler in log.handlers[:]:
            handler.close()
            log.removeHandler(handler)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

CONFIG_SEARCH_PATHS = (
    "/etc/libvirt-backup/libvirt-backup.toml",
    "/etc/libvirt-backup.toml",
    os.path.join(os.path.expanduser("~"), ".config", "libvirt-backup.toml"),
)

FREEZE_MODES = ("try", "no", "require")

# keys accepted in the [backup] section
BACKUP_KEYS = (
    "uri",
    "backup_dir",
    "poll_interval",
    "timeout",
    "freeze",
    "overwrite",
    "standalone",
    "dest_format",
    "compress",
    "verify",
    "incremental",
    "backoff",
)

# keys accepted in a [domains.<pattern>] section
DOMAIN_KEYS = (
    "exclude",
    "timeout",
    "poll_interval",
    "freeze",
    "overwrite",
    "standalone",
    "dest_format",
    "compress",
    "verify",
    "incremental",
)


def load_config(path):
    """Load the TOML config file (explicit --config, else the default
    search paths).  Returns (path or None, config dict)."""
    if path is None:
        path = next(
            (c for c in CONFIG_SEARCH_PATHS if os.path.isfile(c)), None
        )
        if path is None:
            return None, {}
    elif not os.path.isfile(path):
        sys.exit(f"error: config file '{path}' not found")
    if tomllib is None:
        sys.exit(
            "error: reading a config file requires Python >= 3.11 "
            "(tomllib); pass the options on the command line instead"
        )
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as err:
        sys.exit(f"error: cannot read config file '{path}': {err}")
    warn_if_world_readable(path, "config file")
    for section in cfg:
        if section not in ("backup", "domains", "repo", "retention"):
            print(
                f"warning: {path}: unknown section '[{section}]' ignored",
                file=sys.stderr,
            )
    return path, cfg


def check_freeze(value, where):
    """Validate a freeze mode read from config or CLI; exits on an
    invalid value."""
    if value not in FREEZE_MODES:
        sys.exit(
            f"error: {where}: freeze must be one of "
            f"'try', 'no', 'require', got {value!r}"
        )
    return value


def check_number(value, where, minimum):
    """Validate a numeric config value (timeout, poll_interval);
    returns it as float, exits with a clear message otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        sys.exit(f"error: {where}: must be a number, got {value!r}")
    if value < minimum:
        sys.exit(f"error: {where}: must be >= {minimum}")
    return float(value)


def check_duration(value, where):
    """Validate a duration config value: a string like '7d' or a plain
    number of seconds; returns whole seconds, exits otherwise."""
    if isinstance(value, bool):
        sys.exit(
            f"error: {where}: must be a duration like '7d' or a number "
            f"of seconds, got {value!r}"
        )
    if isinstance(value, (int, float)):
        if value < 0:
            sys.exit(f"error: {where}: must be >= 0")
        return int(value)
    try:
        return parse_duration(value)
    except BackupError as err:
        sys.exit(f"error: {where}: {err}")


def check_bool(value, where):
    """Validate a boolean config value; TOML has native booleans, so
    strings like 'no' exit instead of silently coercing to True."""
    if not isinstance(value, bool):
        sys.exit(f"error: {where}: must be true or false, got {value!r}")
    return value


def check_str(value, where):
    """Validate a string config value (paths, URIs, keys); exits on
    anything else - a TOML number is never a valid path."""
    if not isinstance(value, str):
        sys.exit(f"error: {where}: must be a string, got {value!r}")
    return value


def warn_if_world_readable(path, what):
    """Warn when a file that may hold secrets is accessible beyond the
    owner; never fails - reading it is the repository client's job."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & 0o077:
        print(
            f"warning: {what} '{path}' is accessible to group/others "
            f"(mode {mode & 0o777:03o}); it may contain credentials, "
            "chmod 600 is recommended",
            file=sys.stderr,
        )


def apply_config(opts, cfg_path, cfg):
    """Merge CLI (None = not given) > [backup] section > built-in
    defaults into opts, and collect the [domains.*] override sections."""
    backup = cfg.get("backup", {})
    if not isinstance(backup, dict):
        sys.exit(f"error: {cfg_path}: [backup] must be a table")
    for key in backup:
        if key not in BACKUP_KEYS:
            print(
                f"warning: {cfg_path}: [backup] unknown option "
                f"'{key}' ignored",
                file=sys.stderr,
            )

    def pick(cli, key, default):
        """CLI value wins over the config value, which wins over the
        built-in default."""
        if cli is not None:
            return cli
        if key in backup:
            return backup[key]
        return default

    opts.connect = check_str(
        pick(opts.connect, "uri", "qemu:///system"), f"{cfg_path}: uri"
    )
    opts.backup_dir = check_str(
        pick(opts.backup_dir, "backup_dir", "/var/lib/libvirt/backups"),
        f"{cfg_path}: backup_dir",
    )
    opts.poll_interval = check_number(
        pick(opts.poll_interval, "poll_interval", 2.0),
        f"{cfg_path}: poll_interval",
        0.01,
    )
    opts.timeout = check_number(
        pick(opts.timeout, "timeout", 0.0), f"{cfg_path}: timeout", 0.0
    )
    if opts.require_freeze:
        opts.freeze_mode = "require"
    elif opts.no_freeze:
        opts.freeze_mode = "no"
    elif opts.try_freeze:
        opts.freeze_mode = "try"
    else:
        opts.freeze_mode = check_freeze(
            backup.get("freeze", "try"), str(cfg_path)
        )
    standalone = check_bool(
        backup.get("standalone", True), f"{cfg_path}: standalone"
    )
    if opts.no_standalone:
        standalone = False
    opts.no_standalone = not standalone
    opts.overwrite = check_bool(
        pick(opts.overwrite, "overwrite", False), f"{cfg_path}: overwrite"
    )
    opts.dest_format = pick(opts.dest_format, "dest_format", "qcow2")
    if opts.dest_format not in ("qcow2", "raw"):
        sys.exit(f"error: {cfg_path}: dest_format must be 'qcow2' or 'raw'")
    opts.compress = check_bool(
        pick(opts.compress, "compress", False), f"{cfg_path}: compress"
    )
    opts.verify = check_bool(
        pick(opts.verify, "verify", False), f"{cfg_path}: verify"
    )
    opts.incremental = check_bool(
        pick(opts.incremental, "incremental", False),
        f"{cfg_path}: incremental",
    )
    opts.backoff = check_duration(
        pick(opts.backoff, "backoff", 0), f"{cfg_path}: backoff"
    )
    opts.force_full = bool(opts.full)
    if opts.compress and opts.dest_format != "qcow2":
        sys.exit("error: --compress only works with dest-format qcow2")
    if opts.compress and opts.no_standalone:
        sys.exit(
            "error: --compress requires standalone images "
            "(drop --no-standalone or compress=false)"
        )

    opts.domain_cfgs = []
    domains = cfg.get("domains", {})
    if not isinstance(domains, dict):
        sys.exit(f"error: {cfg_path}: [domains] must be a table")
    for pat, dcfg in domains.items():
        if not isinstance(dcfg, dict):
            sys.exit(f"error: {cfg_path}: [domains.{pat}] must be a table")
        for key in dcfg:
            if key not in DOMAIN_KEYS:
                print(
                    f"warning: {cfg_path}: [domains.{pat}] unknown "
                    f"option '{key}' ignored",
                    file=sys.stderr,
                )
        where = f"{cfg_path}: [domains.{pat}]"
        if "freeze" in dcfg:
            check_freeze(dcfg["freeze"], where)
        if "timeout" in dcfg:
            check_number(dcfg["timeout"], f"{where} timeout", 0.0)
        if "poll_interval" in dcfg:
            check_number(dcfg["poll_interval"], f"{where} poll_interval", 0.01)
        if "dest_format" in dcfg and dcfg["dest_format"] not in (
            "qcow2",
            "raw",
        ):
            sys.exit(f"error: {where}: dest_format must be 'qcow2' or 'raw'")
        for key in (
            "exclude",
            "overwrite",
            "standalone",
            "compress",
            "verify",
            "incremental",
        ):
            if key in dcfg:
                check_bool(dcfg[key], f"{where} {key}")
        opts.domain_cfgs.append((pat, dcfg))

    apply_repo_config(opts, cfg_path, cfg.get("repo", {}))


def apply_repo_config(opts, cfg_path, repo_cfg):
    """Apply the [repo] section (shared by backup and restore)."""
    if not isinstance(repo_cfg, dict):
        sys.exit(f"error: {cfg_path}: [repo] must be a table")
    for key in repo_cfg:
        if key not in REPO_KEYS:
            print(
                f"warning: {cfg_path}: [repo] unknown option '{key}' ignored",
                file=sys.stderr,
            )
    opts.repo = opts.repo or repo_cfg.get("repository")
    if opts.repo is not None:
        check_str(opts.repo, f"{cfg_path}: [repo] repository")
    opts.repo_password_file = opts.repo_password_file or repo_cfg.get(
        "password_file"
    )
    if opts.repo_password_file is not None:
        check_str(opts.repo_password_file, f"{cfg_path}: [repo] password_file")
    for key in ("access_key", "secret_key", "region"):
        if repo_cfg.get(key) is not None:
            check_str(repo_cfg[key], f"{cfg_path}: [repo] {key}")
    opts.repo_access_key = repo_cfg.get("access_key") or os.environ.get(
        "AWS_ACCESS_KEY_ID"
    )
    opts.repo_secret_key = repo_cfg.get("secret_key") or os.environ.get(
        "AWS_SECRET_ACCESS_KEY"
    )
    opts.repo_region = repo_cfg.get("region") or os.environ.get("AWS_REGION")
    opts.repo_bin = opts.repo_bin or repo_cfg.get("bin", "auto")
    if opts.repo_bin not in ("auto", "rustic", "restic"):
        sys.exit(
            f"error: {cfg_path}: [repo] bin must be "
            f"'auto', 'rustic' or 'restic'"
        )


def apply_domain_config(opts, name):
    """Return (per-domain copy of opts, excluded_by_config) applying all
    matching [domains.<pattern>] sections in file order.  Values were
    already validated when the config file was loaded."""
    dopts = copy.copy(opts)
    excluded = False
    for pat, dcfg in opts.domain_cfgs:
        if not fnmatch(name, pat):
            continue
        if dcfg.get("exclude"):
            excluded = True
        if "timeout" in dcfg:
            dopts.timeout = float(dcfg["timeout"])
        if "poll_interval" in dcfg:
            dopts.poll_interval = float(dcfg["poll_interval"])
        if "freeze" in dcfg:
            dopts.freeze_mode = dcfg["freeze"]
        if "overwrite" in dcfg:
            dopts.overwrite = dcfg["overwrite"]
        if "standalone" in dcfg:
            dopts.no_standalone = not dcfg["standalone"]
        if "dest_format" in dcfg:
            dopts.dest_format = dcfg["dest_format"]
        if "compress" in dcfg:
            dopts.compress = dcfg["compress"]
        if "verify" in dcfg:
            dopts.verify = dcfg["verify"]
        if "incremental" in dcfg:
            dopts.incremental = dcfg["incremental"]
    if not dopts.repo:
        if dopts.compress and dopts.dest_format != "qcow2":
            sys.exit(f"error: domain '{name}': compress needs qcow2")
        if dopts.compress and dopts.no_standalone:
            sys.exit(
                f"error: domain '{name}': compress needs standalone "
                "images (drop standalone = false)"
            )
    return dopts, excluded


class HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Help output showing defaults, except config-driven (None) ones."""

    def _get_help_string(self, action):
        """Omit the '(default: ...)' suffix for config-driven defaults."""
        # None means "not given on the CLI"; the real default comes
        # from the config file or the built-in defaults
        if action.default is None:
            return action.help
        return super()._get_help_string(action)


def build_parser():
    """Build the top-level parser with the backup/restore/forget/check/
    init subcommands."""
    top = argparse.ArgumentParser(
        description=__doc__.splitlines()[1],
        epilog=EPILOG,
        formatter_class=HelpFormatter,
    )
    sub = top.add_subparsers(dest="command", metavar="COMMAND")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="TOML config file (default: /etc/libvirt-"
        "backup/libvirt-backup.toml, /etc/libvirt-"
        "backup.toml or ~/.config/libvirt-backup.toml "
        "if present); CLI options win",
    )
    rg = common.add_argument_group("repository options")
    rg.add_argument(
        "--repo",
        metavar="REPO",
        default=None,
        help="rustic/restic repository (local path or e.g. "
        "s3:https://host/bucket/prefix); backup streams "
        "images into it, restore reads from it; see the "
        "[repo] config section",
    )
    rg.add_argument(
        "--repo-password-file",
        metavar="FILE",
        default=None,
        help="file containing the repository password "
        "(or export RUSTIC_PASSWORD/RESTIC_PASSWORD)",
    )
    rg.add_argument(
        "--repo-bin",
        choices=("auto", "rustic", "restic"),
        default=None,
        help="repository client (default: auto: rustic if "
        "available, else restic)",
    )
    parser = sub.add_parser(
        "backup",
        parents=[common],
        formatter_class=HelpFormatter,
        help="back up running domains (default subcommand)",
    )
    parser.add_argument(
        "-c",
        "--connect",
        metavar="URI",
        default=None,
        help="libvirt connection URI (default: qemu:///system)",
    )
    parser.add_argument(
        "-d",
        "--dest",
        "--backup-dir",
        metavar="DIR",
        default=None,
        dest="backup_dir",
        help="destination directory for all backup "
        "artefacts (default: /var/lib/libvirt/backups)",
    )
    parser.add_argument(
        "-D",
        "--vm",
        "--domain",
        metavar="NAME",
        action="append",
        dest="domain",
        help="only back up VMs matching NAME or glob "
        "pattern, comma-separated, repeatable; "
        "default: all running VMs "
        "(e.g. -D 'vm1,db*')",
    )
    parser.add_argument(
        "-x",
        "--exclude",
        metavar="NAME",
        action="append",
        help="exclude domains matching NAME or glob "
        "pattern, comma-separated, repeatable "
        "(e.g. -x 'prod-*')",
    )
    parser.add_argument(
        "-p",
        "--poll-interval",
        metavar="SEC",
        type=float,
        default=None,
        help="blockjob progress polling interval (default: 2.0)",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        metavar="SEC",
        type=float,
        default=None,
        help="abort a backup after this many seconds "
        "(0 = wait forever; default: 0)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--try-freeze",
        action="store_true",
        default=None,
        help="try domfsfreeze, but back up anyway with a "
        "prominent warning if it fails; such VMs are "
        "reported as WARN (default behaviour)",
    )
    group.add_argument(
        "--no-freeze",
        action="store_true",
        default=None,
        help="do not even try domfsfreeze/domfsthaw",
    )
    group.add_argument(
        "--require-freeze",
        action="store_true",
        default=None,
        help="fail a domain if domfsfreeze does not work",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=None,
        help="replace artefacts of previous runs",
    )
    parser.add_argument(
        "--no-standalone",
        action="store_true",
        default=None,
        help="keep the backup job's qcow2 overlay (which "
        "depends on the VM's original disk as backing "
        "file) instead of flattening to a standalone "
        "image",
    )
    parser.add_argument(
        "--dest-format",
        choices=("qcow2", "raw"),
        default=None,
        help="format of the standalone backup image (default: qcow2)",
    )
    parser.add_argument(
        "--compress",
        action="store_true",
        default=None,
        help="compress the standalone qcow2 image "
        "(qemu-img convert -c); slower, but much "
        "smaller for compressible data",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        default=None,
        help="after the backup: qemu-img check the final "
        "images (directory mode) or confirm that the "
        "repository snapshots contain every expected "
        "file (repo mode)",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        default=None,
        help="checkpoint-based incremental backups: after "
        "the first full backup only changed blocks "
        "are written (qcow2 disks, directory mode; "
        "'restore' merges the chain)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        default=None,
        help="force a new full backup and restart the incremental chain",
    )
    parser.add_argument(
        "--backoff",
        metavar="DUR",
        default=None,
        help="minimum time between two backups of the same "
        "VM (e.g. 12h, 7d; plain number = seconds; 0 = "
        "no wait, the default); VMs explicitly chosen "
        "with -D bypass it",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list domains and their block devices, then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate the -D/-x selection and report what "
        "would be backed up (with the summary), but "
        "start no backup job and write no files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="continue even when another run holds the "
        "lock on the backup directory; last resort "
        "for a stuck or zombie holder - prefer "
        "killing the holder shown in the warning",
    )

    restore = sub.add_parser(
        "restore",
        parents=[common],
        formatter_class=HelpFormatter,
        help="restore one VM into a destination directory",
    )
    restore.add_argument(
        "--from",
        dest="src_dir",
        metavar="DIR",
        default=None,
        help="backup directory to restore from "
        "(default: [backup] backup_dir, else "
        "/var/lib/libvirt/backups)",
    )
    restore.add_argument(
        "--list",
        action="store_true",
        help="list restorable VMs and generations, then exit",
    )
    restore.add_argument(
        "-D", "--vm", metavar="NAME", help="name of the VM to restore"
    )
    restore.add_argument(
        "-d",
        "--dest",
        metavar="DIR",
        help="destination directory for the artefacts",
    )
    restore.add_argument(
        "--run",
        metavar="TS",
        help="repository mode: restore this run "
        "(run:<TS> tag), default: newest run",
    )
    restore.add_argument(
        "--snap",
        metavar="ID",
        help="repository mode: restore only the snapshot with this id prefix",
    )
    restore.add_argument(
        "--overwrite",
        action="store_true",
        help="allow a non-empty destination directory",
    )
    restore.add_argument(
        "--force",
        action="store_true",
        help="restore even while another run holds the "
        "lock on the source directory; last resort "
        "for a stuck holder - artefacts may be "
        "mid-write",
    )
    restore.add_argument(
        "--verify",
        action="store_true",
        help="after restoring: qemu-img check on qcow2 "
        "images, report raw image sizes",
    )
    restore.add_argument(
        "--rewrite",
        action="store_true",
        help="rewrite the disk sources in the restored "
        "<vm>-config.xml to the restored image "
        "files (matched by target device name)",
    )
    restore.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only log warnings and errors",
    )

    forget = sub.add_parser(
        "forget",
        parents=[common],
        formatter_class=HelpFormatter,
        help="apply the retention policy to the repository (needs [repo])",
    )
    forget.add_argument(
        "--keep-last",
        metavar="N",
        type=int,
        default=None,
        help="keep the newest N runs per VM",
    )
    forget.add_argument(
        "--keep-daily",
        metavar="N",
        type=int,
        default=None,
        help="keep the newest run of N days per VM",
    )
    forget.add_argument(
        "--keep-weekly",
        metavar="N",
        type=int,
        default=None,
        help="keep the newest run of N ISO weeks per VM",
    )
    forget.add_argument(
        "--keep-monthly",
        metavar="N",
        type=int,
        default=None,
        help="keep the newest run of N months per VM",
    )
    forget.add_argument(
        "--keep-yearly",
        metavar="N",
        type=int,
        default=None,
        help="keep the newest run of N years per VM",
    )
    forget.add_argument(
        "--keep-within",
        metavar="DUR",
        default=None,
        help="keep all runs newer than DUR relative to "
        "the newest run (e.g. 48h, 30d, 4w)",
    )
    forget.add_argument(
        "--no-prune",
        dest="prune",
        action="store_false",
        default=None,
        help="only remove snapshot indexes, keep the "
        "pack files (default: also prune)",
    )
    forget.add_argument(
        "--dry-run",
        action="store_true",
        help="show which runs would be forgotten, change nothing",
    )
    forget.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only log warnings and errors",
    )

    check = sub.add_parser(
        "check",
        parents=[common],
        formatter_class=HelpFormatter,
        help="verify images (directory mode) or repository "
        "integrity (repo mode)",
    )
    check.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only log warnings and errors",
    )

    init = sub.add_parser(
        "init",
        parents=[common],
        formatter_class=HelpFormatter,
        help="create a new (empty) repository (rustic/restic init)",
    )
    init.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only log warnings and errors",
    )
    return top


def parse_args(argv=None):
    """Parse the command line; a flag-only invocation is treated as
    'backup' for backward compatibility."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (
        argv[0] not in ("backup", "restore", "forget", "check", "init")
        and not argv[0].startswith("-h")
        and argv[0] != "--help"
    ):
        argv.insert(0, "backup")
    return build_parser().parse_args(argv)


def _abort_handler(signum, _frame):
    """Turn a termination signal into BackupAbort so the cleanup
    finally blocks run on the way out; a default-handled SIGTERM would
    kill the process between fsFreeze and fsThaw and leave the guest
    file systems frozen forever."""
    raise BackupAbort(f"terminated by signal {signum}")


def next_backup_candidate(
    domains, opts, sel, excl, attempted, results, repo_last
):
    """Pick the VM to back up next: among the domains not yet handled
    this run, the eligible one whose last successful backup lies
    furthest in the past (VMs that never succeeded come first).  The
    last success comes from the repository in repo mode (shared, so
    live migration cannot fool it) and from the per-VM state file in
    directory mode.  Domains that are excluded, not selected, not
    running or blocked by the backoff period get their final summary
    entry in `results` and are never picked.  A VM with more than
    MAX_RETRIES consecutive failures is only retried once `backoff`
    seconds have passed since the last error.  An explicit -D
    selection means 'back up now' and bypasses both backoff gates.
    Returns (dom, dopts, last_epoch) or None when no eligible VM is
    left."""
    now = time.time()
    candidates = []
    for dom in domains:
        name = dom.name()
        if name in attempted or name in results:
            continue
        dopts, cfg_excluded = apply_domain_config(opts, name)
        if cfg_excluded or name_matches(name, excl):
            results[name] = ("excl", "excluded")
            continue
        if sel and not name_matches(name, sel):
            results[name] = ("skip", "not selected")
            continue
        try:
            state = dom.info()[0]
        except libvirt.libvirtError as err:
            results[name] = ("skip", f"domain vanished: {err}")
            continue
        if state != libvirt.VIR_DOMAIN_RUNNING:
            results[name] = ("skip", "not running")
            continue
        st = load_state(dopts.backup_dir, name)
        if repo_last is not None:
            last_ts = repo_last.get(name, 0.0)
        else:
            last_ts = state_epoch(st.get("last_success"))
        if not sel and opts.backoff:
            retries = int(st.get("retries") or 0)
            err_ts = state_epoch(st.get("last_error_ts"))
            if retries > MAX_RETRIES and now - err_ts < opts.backoff:
                results[name] = (
                    "skip",
                    f"backoff: {retries} consecutive failures, "
                    f"last error {human_age(err_ts)} ago",
                )
                continue
            if last_ts and now - last_ts < opts.backoff:
                results[name] = (
                    "skip",
                    f"backoff: last success {human_age(last_ts)} ago",
                )
                continue
        candidates.append((last_ts, name, dom, dopts))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    last_ts, _name, dom, dopts = candidates[0]
    return dom, dopts, last_ts


def main_backup(opts):
    """'backup' subcommand: push-back up the selected VMs; returns the
    process exit code."""
    require_libvirt()
    cfg_path, cfg = load_config(opts.config)
    apply_config(opts, cfg_path or "config", cfg)
    if cfg_path:
        print(f"using config file: {cfg_path}", file=sys.stderr)
    conn = open_connection(opts.connect)

    # 1. get a list of all domains
    try:
        domains = sorted(conn.listAllDomains(0), key=lambda d: d.name())
    except libvirt.libvirtError as err:
        sys.exit(f"error: cannot list domains: {err}")

    if opts.list:
        for dom in domains:
            state = dom.info()[0]
            running = state == libvirt.VIR_DOMAIN_RUNNING
            print(f"{dom.name():<24} {'running' if running else 'inactive'}")
            if running:
                for disk in list_disks(dom.XMLDesc(0), conn):
                    line = (
                        f"    {disk['target']:<8} {disk['fmt']:<8} "
                        f"{disk['src']}"
                    )
                    if disk["kind"] != "backupable":
                        line += f"   [skipped: {disk['kind']}]"
                    print(line)
        conn.close()
        return 0

    if not domains:
        print("no domains found on this host")
        conn.close()
        return 0

    # repository mode: streamed raw images, no local final images
    if opts.repo:
        if opts.no_standalone:
            sys.exit(
                "error: --repo and --no-standalone are mutually "
                "exclusive (the repository gets the standalone data)"
            )
        if opts.compress or opts.dest_format != "qcow2":
            print(
                "warning: --compress/--dest-format do not apply in repo "
                "mode (images are streamed as raw, the repository "
                "compresses)",
                file=sys.stderr,
            )
        resolve_repo_bin(opts, streaming=True)

    # standalone images are produced with qemu-img convert
    if not opts.no_standalone:
        if shutil.which("qemu-img") is None:
            if opts.dry_run:
                print(
                    "warning: qemu-img not found; a real standalone "
                    "run would fail",
                    file=sys.stderr,
                )
            else:
                sys.exit(
                    "error: qemu-img not found; it is required for "
                    "standalone backups (install qemu-utils or use "
                    "--no-standalone)"
                )
        if os.geteuid() != 0:
            if opts.dry_run:
                print(
                    "warning: not root; a real standalone run would "
                    "fail (use --no-standalone or run as root)",
                    file=sys.stderr,
                )
            else:
                sys.exit(
                    "error: standalone backups must run as root: "
                    "qemu creates the backup overlay as root:root "
                    "0600 and qemu-img convert must read it (plus "
                    "the VM's disk).\n"
                    "       Re-run with sudo, or use --no-standalone "
                    "to keep the overlay instead."
                )

    # make sure the backup directory exists before the first artefact
    if not opts.dry_run and not os.path.isdir(opts.backup_dir):
        try:
            os.makedirs(opts.backup_dir, exist_ok=True)
        except OSError as err:
            sys.exit(
                f"error: cannot create backup directory "
                f"'{opts.backup_dir}': {err}"
            )

    # one run per host at a time: a second concurrent run on this host
    # would race for the same libvirt backup jobs and log/state files;
    # the newcomer skips (exit 0, warning) instead of damaging the
    # active one.  Cross-host protection is per VM (see the VM lock in
    # the backup loop)
    if not opts.dry_run and not opts.list:
        run_lock = RunLock(opts.backup_dir)
        try:
            locked = run_lock.acquire(opts.force)
        except OSError as err:
            sys.exit(
                f"error: cannot lock backup directory "
                f"'{opts.backup_dir}': {err}"
            )
        if not locked:
            return 0

    # pre-flight: validate the destination before the first block job,
    # so a missing 'libvirt-backup init', a wrong password/region/keys
    # or a non-writable backup_dir surface before hundreds of GiB have
    # been copied.  'snapshots' is the cheapest read-only probe that
    # checks reachability, initialisation and the repository key at once
    repo_snaps = []
    if opts.repo:
        env = repo_env(opts)
        try:
            repo_snaps = repo_snapshots(opts, env)
        except BackupError as err:
            if opts.dry_run:
                print(
                    f"warning: repository not usable: {err}", file=sys.stderr
                )
            else:
                sys.exit(
                    f"error: repository not usable: {err}\n"
                    "       create it with 'libvirt-backup init' or "
                    "check the [repo] settings"
                )
    elif not opts.dry_run:
        probe = os.path.join(opts.backup_dir, ".libvirt-backup.probe")
        try:
            with open(probe, "w") as fh:
                fh.write("x")
            os.unlink(probe)
        except OSError as err:
            sys.exit(
                f"error: backup directory '{opts.backup_dir}' is not "
                f"writable: {err}"
            )

    sel = name_patterns(opts.domain)
    excl = name_patterns(opts.exclude)
    all_names = [dom.name() for dom in domains]
    for pat in sel + excl:
        if not any(name_matches(n, [pat]) for n in all_names):
            print(
                f"warning: pattern '{pat}' matches no domain", file=sys.stderr
            )

    results = {}
    attempted = set()
    repo_last = repo_last_success(repo_snaps) if opts.repo else None
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, _abort_handler)
    try:
        while True:
            # re-evaluate before every cycle: VMs may have been created,
            # shut down or live-migrated in or out since the last pick;
            # the next candidate is the running VM whose last success
            # lies furthest back (see next_backup_candidate)
            try:
                domains = sorted(
                    conn.listAllDomains(0), key=lambda d: d.name()
                )
            except libvirt.libvirtError as err:
                results["run"] = ("fail", f"cannot list domains: {err}")
                break
            pick = next_backup_candidate(
                domains, opts, sel, excl, attempted, results, repo_last
            )
            if pick is None:
                break
            dom, dopts, last_ts = pick
            name = dom.name()
            attempted.add(name)
            if opts.dry_run:
                try:
                    disks = [
                        d
                        for d in list_disks(dom.XMLDesc(0), conn)
                        if d["kind"] == "backupable"
                    ]
                    chain = load_chain(dopts.backup_dir, name)
                    expected_cp = (chain or {}).get("checkpoint")
                    stale_cps = [
                        c for c in list_checkpoints(dom) if c != expected_cp
                    ]
                    warns = []
                    if stale_cps:
                        warns.append(
                            f"leftover checkpoint(s) {', '.join(stale_cps)}"
                        )
                    if active_job_type(dom) is not None:
                        warns.append("backup job still active")
                    stale_jobs = sorted(
                        glob.glob(
                            os.path.join(
                                dopts.backup_dir, f"{name}-backup-*.job"
                            )
                        )
                    )
                    if stale_jobs:
                        warns.append(
                            "stale job file(s): "
                            + ", ".join(
                                os.path.basename(p) for p in stale_jobs
                            )
                        )
                    if disks:
                        mode, _ = backup_mode(
                            dom,
                            name,
                            disks,
                            dopts,
                            logging.getLogger("libvirt-backup"),
                        )
                        results[name] = (
                            "dry",
                            f"would back up ({mode}): "
                            + ", ".join(d["target"] for d in disks)
                            + (
                                f"; last success {human_age(last_ts)} ago"
                                if last_ts
                                else "; never backed up"
                            )
                            + (
                                f"; WARNING: {'; '.join(warns)}"
                                if warns
                                else ""
                            ),
                        )
                    else:
                        results[name] = ("skip", "no backupable disks")
                except libvirt.libvirtError as err:
                    results[name] = ("fail", str(err))
                except BackupAbort as err:
                    results[name] = ("fail", f"{err}; run stopped")
                    break
                except Exception as err:
                    results[name] = ("fail", f"unexpected error: {err}")
                continue
            # per-VM lock: a live migration can hand this VM to another
            # host while a backup of it is still running there; skip
            # the VM instead of racing for the same artefacts, chain
            # file and snapshots (flock spans hosts on a shared dir)
            vm_lock = RunLock(dopts.backup_dir, vm=name)
            try:
                vm_locked = vm_lock.acquire(opts.force)
            except OSError as err:
                results[name] = ("fail", f"cannot lock VM: {err}")
                continue
            if not vm_locked:
                results[name] = (
                    "skip",
                    f"locked by {vm_lock.holder() or '?'}",
                )
                continue
            print()
            try:
                summary, status = backup_domain(conn, dom, dopts)
                results[name] = (status, summary)
                record_backup_result(dopts.backup_dir, name, True)
            except NoBackupableDisks:
                results[name] = ("skip", "no backupable disks")
            except BackupAbort as err:
                # backup_domain's finally already thawed the guest,
                # aborted the job and discarded the overlay; the run
                # stops, the interruption itself is not a VM failure
                results[name] = ("fail", f"{err}; run stopped")
                break
            except BackupError:
                results[name] = ("fail", "see log")
                record_backup_result(dopts.backup_dir, name, False, "see log")
            except libvirt.libvirtError as err:
                results[name] = ("fail", str(err))
                record_backup_result(dopts.backup_dir, name, False, str(err))
            except Exception as err:
                # one VM's unexpected error must not skip the rest
                results[name] = ("fail", f"unexpected error: {err}")
                record_backup_result(
                    dopts.backup_dir,
                    name,
                    False,
                    f"unexpected error: {err}",
                )
            finally:
                vm_lock.release()
    except BackupAbort as err:
        results["run"] = ("fail", f"{err}; run stopped")

    for dom in domains:
        if dom.name() not in results and dom.name() not in attempted:
            results[dom.name()] = ("skip", "not attempted (run stopped)")

    conn.close()

    # 7. summary after all domains were processed
    marks = {
        "ok": "OK  ",
        "warn": "WARN",
        "skip": "SKIP",
        "excl": "EXCL",
        "dry": "DRY ",
        "fail": "FAIL",
    }
    print("\n" + "=" * 62)
    print(
        "backup summary"
        + (" (dry run - nothing was backed up)" if opts.dry_run else "")
    )
    print("=" * 62)
    counts = dict.fromkeys(marks, 0)
    for name, (status, msg) in sorted(
        results.items(), key=lambda kv: (kv[0] == "run", kv[0])
    ):
        print(f"  {marks[status]}  {name:<24} {msg}")
        counts[status] += 1
    print(
        "\n"
        + ", ".join(
            f"{counts[s]} {marks[s].strip().lower()}"
            for s in ("ok", "dry", "warn", "skip", "excl", "fail")
        )
    )
    print(f"backup directory: {os.path.abspath(opts.backup_dir)}")
    return 1 if counts["fail"] else 0


def main_restore(opts):
    """'restore' subcommand: restore one VM into a destination
    directory; returns the process exit code."""
    logging.basicConfig(
        level=logging.WARNING if opts.quiet else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(message)s",
    )
    log = logging.getLogger("libvirt-restore")
    cfg_path, cfg = load_config(opts.config)
    if cfg_path:
        log.info("using config file: %s", cfg_path)
    apply_repo_config(opts, cfg_path or "config", cfg.get("repo", {}))
    if opts.src_dir is None:
        opts.src_dir = cfg.get("backup", {}).get(
            "backup_dir", "/var/lib/libvirt/backups"
        )
    env = None
    if opts.repo:
        resolve_repo_bin(opts)
        env = repo_env(opts)
    try:
        if opts.list:
            if opts.repo:
                list_repo(opts, env)
            else:
                list_directory(opts)
            return 0
        if not opts.vm or not opts.dest:
            sys.exit("error: --vm and --dest are required (or use --list)")
        # a restore reading the source directory while a backup is
        # rewriting exactly those files would read torn artefacts; the
        # per-VM lock of the restored VM is exactly that protection
        if not opts.repo and os.path.isdir(opts.src_dir):
            run_lock = RunLock(opts.src_dir, vm=opts.vm)
            try:
                locked = run_lock.acquire(opts.force)
            except OSError as err:
                sys.exit(
                    f"error: cannot lock source directory "
                    f"'{opts.src_dir}' for VM '{opts.vm}': {err}"
                )
            if not locked:
                return 1
        if opts.repo:
            restore_repo(opts, env, log)
        else:
            restore_directory(opts, log)
        if opts.verify:
            verify_dest(opts, log)
        if opts.rewrite:
            rewrite_sources(opts, log)
    except BackupError as err:
        log.error("%s", err)
        return 1
    log.info(
        "restore of '%s' to %s finished", opts.vm, os.path.abspath(opts.dest)
    )
    return 0


def main(argv=None):
    """Dispatch to the selected subcommand; returns the process exit
    code."""
    # backup artefacts are VM disk images and domain XML (which can
    # contain VNC passwords); never let the caller's umask make them
    # group/world-readable
    os.umask(0o077)
    opts = parse_args(argv)
    if opts.command == "restore":
        return main_restore(opts)
    if opts.command == "forget":
        return main_forget(opts)
    if opts.command == "check":
        return main_check(opts)
    if opts.command == "init":
        return main_init(opts)
    return main_backup(opts)


if __name__ == "__main__":
    sys.exit(main())
