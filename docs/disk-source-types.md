# Libvirt disk source types — comparison and notes

Context: the backup tool in this repository (`libvirt-backup.py`) drives
`virDomainBackupBegin()` and the other libvirt block APIs. Which disk
source types can take part in those APIs — and which syntax is the most
portable for a given backend — is documented here.

Sources: libvirt domain XML format (formatdomain.html, "Hard drives,
floppy disks, CDROMs"), storage volume format (formatstorage.html) and
backup XML format (formatbackup.html) on <https://libvirt.org>.

## Overview

| `type=` | XML points at | QEMU block backend → block jobs¹ | Host dependency | Portability | Typical use |
|---------|---------------|----------------------------------|-----------------|-------------|-------------|
| `file` | image file (`<source file=…/>`) | ✅ full | path must exist on host (shared FS, or data copied on migration) | good with shared storage; XML itself is host-local | qcow2/raw images, most common |
| `block` | host block device (`<source dev=…/>`), e.g. LVM, dm-multipath, iSCSI `/dev/disk/by-path/…` | ✅ full | host must bring the device up *before* VM start (iscsid login, `vgchange`, …) | only as stable as the symlink used; migration target needs the identical setup | maximum I/O-path performance, kernel multipath |
| `network` | protocol + name: `iscsi`, `rbd`, `nbd`, `gluster`, `https`, `ssh`, … | ✅ full | libvirt/QEMU logs in itself (storage-pool autostart, secrets/TLS); no host kernel session | best self-contained XML: portal + IQN travel with the domain | managed iSCSI / RBD |
| `volume` | **indirection**: `pool=` + `volume=` of a libvirt storage pool | ✅ full — per libvirt docs an iSCSI LUN from a pool "provides the same features as a disk configured using type 'block' or 'network'" (`mode='host'` vs `'direct'`) | none in the XML; each host's pool definition maps the name to a local path | the same domain XML works on every host that defines an identically named pool | central provisioning; moving storage without editing domain XML |
| `hostdev` | host SCSI/USB device address | ❌ none — no block layer, so no snapshots, backups, blockcopy or IO throttling | host SCSI addressing (`adapter='hostN' …`) | worst | only when the *guest* must own the raw device (persistent reservations, SPC/vendor commands) |
| `nvme` | NVMe namespace of a PCI controller (managed or not) | ❌ (passthrough, like hostdev) | NVMe controller detach/attach | poor | NVMe namespace passthrough |
| `dir` | directory exposed via virtiofs / emulated FAT | n/a (filesystem, not block) | path on host | — | filesystem sharing |

¹ block jobs = `backup-begin`, external snapshots, `blockcommit`,
`blockpull/copy`, `blkdeviotune` — all require a QEMU block backend.

## `type='hostdev'` example as seen in `virsh domblklist`

A hostdev disk shows a *source address* instead of a path:

```
 Target   Source
 sdb      unit:0:0:12
```

The XML carries `<source><address type='scsi'|'drive' …/></source>`.
Such disks are invisible to the libvirt backup API; back the LUN up on
the storage side or re-attach it as `type='block'`, `type='network'` or
`type='volume'` (keep `<target dev>` and the guest `<address>` identical
so the guest sees the same device).

## Recommendation summary

- Managed fleets / multiple hosts → `volume` (storage layer) or `network`.
- Direct iSCSI with kernel multipath → `block` on `/dev/disk/by-id/dm-uuid-mpath-…`
  or `/dev/disk/by-path/…` (never bare `/dev/sdX`).
- Plain images → `file`.
- `hostdev`/`nvme` only for genuine raw-device requirements — they give
  up every libvirt block feature, including the backups this tool does.

## Backup output format (this tool)

libvirt's push backup itself always writes a qcow2 **overlay** whose
backing file is the VM's current active image — by itself that image is
*not* standalone.  `libvirt-backup.py` therefore defaults to producing
standalone images:

1. the backup job writes its overlay to `<vm>-backup-<dev>.<fmt>.job`,
2. after the job finishes, `qemu-img convert` reads through the whole
   backing chain and writes a self-contained final image
   (`<vm>-backup-<dev>.qcow2`, or `.raw` with `--dest-format raw`),
3. the overlay is removed.

Options:

| option | effect |
|--------|--------|
| (default) | standalone qcow2, uncompressed, sparse |
| `--compress` | standalone qcow2 written zlib-compressed (`qemu-img convert -c`) — much smaller for compressible data, slower |
| `--dest-format raw` | standalone raw image (full virtual size, sparse on disk) |
| `--no-standalone` | skip the flatten step, keep the qcow2 overlay (fast, small, but depends on the VM's original disk as backing file) |

`qemu-img convert` needs read access to the overlay *and* the VM's live
disk, so standalone mode effectively requires running the tool as root
(the overlay is created by qemu and is `root:root 0600`).

## Resolving `type='volume'` to a host path via the Python API

The domain XML never contains the resolved path — virt-manager looks it
up live through the storage layer. The same chain works in
python3-libvirt (`libvirt-backup.py` calls it in `list_disks()`):

```python
source = ...  # <source pool='fast' volume='db-data'/>
pool = conn.storagePoolLookupByName(source.get("pool"))
vol  = pool.storageVolLookupByName(source.get("volume"))
path = vol.path()          # C API: virStorageVolGetPath
```

What `vol.path()` returns depends on the pool type:

| pool type | example `vol.path()` |
|-----------|----------------------|
| `dir` | `/var/lib/libvirt/images/vm.qcow2` |
| `logical` | `/dev/vg0/lv-vm` |
| `iscsi` | `/dev/disk/by-path/ip-…-lun-N` (or a `/dev/iscsi/…` symlink) |
| `disk` | `/dev/sdb1` |

The specific `/dev/disk/by-id/…` name is a udev property, one symlink
resolution away:

```python
import glob, os

def to_by_id(path):
    real = os.path.realpath(path)
    return next((l for l in glob.glob("/dev/disk/by-id/*")
                 if os.path.realpath(l) == real), None)
```

With device-mapper multipath the result is a `dm-uuid-mpath-…` id,
which is normally the most stable choice.

Other handy storage APIs: `vol.key()`, reverse lookup
`vol.storagePoolLookupByVolume()`, `conn.storagePoolLookupByTargetPath()`.
