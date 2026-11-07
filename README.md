# libvirt-backup

Push backup and restore tool for libvirt/QEMU virtual machines, written
against the **python3-libvirt API** (no `virsh` subprocesses).  One
self-contained Python file with five subcommands: `backup`, `restore`,
`forget`, `check`, `init`.

For every running domain the tool performs, entirely through libvirt:

1. `domfsfreeze` — freeze the guest filesystems via the guest agent
2. `backupBegin` — start a checkpoint-based backup job (dirty bitmaps)
3. `domfsthaw` — thaw the filesystems (the copy runs on afterwards)
4. blockjob polling — wait until QEMU finished copying every disk
5. `dumpxml` — save the live domain XML next to the images

## Modes and artefacts

**Directory mode** (default) writes per VM into `backup_dir`
(example VM `vm1`, disk `sdb`):

| file | content |
|------|---------|
| `vm1-backup-sdb.qcow2` | standalone (flattened) backup image per disk |
| `vm1-backup.xml` | `<domainbackup>` job description |
| `vm1-config.xml` | live domain XML (`virsh dumpxml` equivalent) |
| `vm1-backup.log` | log of this VM's run |
| `vm1-state.json` | selection state: last success, retries, last error |
| `vm1-incr-TS-sdb.qcow2` | incremental images (with `incremental`) |
| `vm1-chain.json` | incremental chain state (with `incremental`) |

**Repository mode** (`[repo]` configured): no image files on the host.
Each backup overlay is flattened to **raw** through a temporary
read-only NBD export (`qemu-storage-daemon` + `python3-libnbd`) and
streamed into a **rustic**/restic repository — deduplicated,
zstd-compressed, encrypted, append-only (a broken stream never touches
previous data). One snapshot set per VM and run, tagged
`vm:<name>,run:<timestamp>`. Works with local paths and all
rustic/restic backends, e.g. S3/MinIO.

**Incremental backups** (directory mode, qcow2 at the active layer):
every `backupBegin` rotates a libvirt checkpoint with dirty bitmaps;
subsequent runs write only changed blocks and `restore` merges the
chain back into the full image automatically.

## Requirements

- Debian/Ubuntu host with libvirt + KVM, Python ≥ 3.11 (`tomllib`)
- `python3-libvirt`, `qemu-utils` (`qemu-img`, `qemu-storage-daemon`)
- repository mode additionally: `rustic` (preferred) or `restic`,
  `python3-libnbd`
- root (qemu creates the backup overlays `root:root 0600`)
- `restore`/`forget`/`check` need **no libvirt** — they also run on a
  rescue system

## Install

### apt repository (Debian/Ubuntu, recommended)

The release workflow publishes a GPG-signed apt repository on GitHub
Pages; every released version stays available:

    sudo install -d -m 0755 /etc/apt/keyrings
    sudo curl -fsSLo /etc/apt/keyrings/libvirt-backup.asc \
        https://tk7r.github.io/libvirt-backup/repo-pub.asc
    echo 'deb [signed-by=/etc/apt/keyrings/libvirt-backup.asc] https://tk7r.github.io/libvirt-backup stable main' \
        | sudo tee /etc/apt/sources.list.d/libvirt-backup.list
    sudo apt-get update && sudo apt-get install libvirt-backup

### dnf repository (Fedora/RHEL, recommended)

    sudo dnf config-manager --add-repo \
        https://tk7r.github.io/libvirt-backup/libvirt-backup.repo
    sudo dnf install libvirt-backup

### Manual .deb

Download the `.deb` from the [Releases](../../releases) page, or build
it yourself with [`build-package.sh`](build-package.sh) (see
[Building](#building)); then:

    sudo apt install ./libvirt-backup_0.1.5_all.deb

The package installs:

- `/usr/bin/libvirt-backup` — the tool
- `/etc/libvirt-backup/libvirt-backup.toml.directory-mode` and
  `.repository-mode` — ready-made config templates
- systemd units + timers (timers enabled at install; the services stay
  **skipped** until a config file exists — see [systemd](#systemd))

Activate a config:

    cp /etc/libvirt-backup/libvirt-backup.toml.repository-mode \
       /etc/libvirt-backup/libvirt-backup.toml
    $EDITOR /etc/libvirt-backup/libvirt-backup.toml
    libvirt-backup backup --dry-run

### Manual install

    install -m 755 libvirt-backup.py /usr/bin/libvirt-backup
    mkdir -p /etc/libvirt-backup
    cp examples/libvirt-backup-*.toml /etc/libvirt-backup/

Or just run the file directly: `./libvirt-backup.py backup --dry-run`.

## Configuration

TOML file, searched in this order (first hit wins):

1. `--config FILE`
2. `/etc/libvirt-backup/libvirt-backup.toml`
3. `/etc/libvirt-backup.toml` (legacy)
4. `~/.config/libvirt-backup.toml`

Precedence: **command line > config file > built-in defaults**.

- [`libvirt-backup.toml`](libvirt-backup.toml) — fully commented
  template documenting every key
- [`examples/libvirt-backup-directory.toml`](examples/libvirt-backup-directory.toml) —
  directory mode, standalone compressed qcow2
- [`examples/libvirt-backup-rustic.toml`](examples/libvirt-backup-rustic.toml) —
  rustic/S3 mode, raw streamed

Sections:

- `[backup]` — global defaults: `uri`, `backup_dir`, `timeout`,
  `freeze` (`try`/`no`/`require`), `standalone`, `dest_format`,
  `compress`, `verify`, `incremental`, `poll_interval`, `overwrite`
- `[domains.<name-or-glob>]` — per-VM overrides of the above plus
  `exclude = true` (glob patterns must be quoted:
  `[domains."test-*"]`)
- `[repo]` — repository mode: `repository` URL, `password_file`,
  `access_key`/`secret_key`, `region` (**required for `s3:` URLs**),
  `bin` (`auto`/`rustic`/`restic`).  Secrets belong in this file
  (mode 0600!) or the environment — never on the command line.
- `[retention]` — policy for `forget`: `keep_last`, `keep_daily`,
  `keep_weekly`, `keep_monthly`, `keep_yearly`, `keep_within`, `prune`

## Usage

    # see what is there / what would be backed up (changes nothing)
    libvirt-backup backup --list
    libvirt-backup backup --dry-run

    # back up all running VMs into the configured directory
    libvirt-backup backup

    # selected VMs, custom destination, verify the images afterwards
    libvirt-backup backup -D 'web*,db1' -d /mnt/backup --verify

    # incremental chain (first run full, then only changed blocks)
    libvirt-backup backup --incremental -D web1
    libvirt-backup backup --full -D web1      # restart the chain

    # stream into a rustic/restic repository (config: [repo])
    libvirt-backup backup

    # honour a per-VM minimum gap between backups (config: backoff)
    libvirt-backup backup --backoff 7d
    libvirt-backup backup -D web1        # -D always backs up now

    # restore: newest run by default; --rewrite makes the XML
    # directly 'virsh define'-able against the restored images
    libvirt-backup restore --list
    libvirt-backup restore -D web1 -d /restore/web1 --verify --rewrite

    # retention (repository mode): always keeps the newest run per VM
    libvirt-backup forget --dry-run
    libvirt-backup forget

    # integrity: qemu-img check (directory) / rustic check (repository)
    libvirt-backup check

    # create an empty repository once, before the first backup
    libvirt-backup init

Run `libvirt-backup <subcommand> --help` for the full option list.

**Summary and exit codes:** every run ends with a per-VM summary line
(`OK`/`WARN`/`FAIL`/`EXCL`/`SKIP`); the process exits 1 only if at
least one VM **FAIL**ed — warnings (e.g. no guest agent, so no freeze)
do not fail the run.

**Selection order and backoff:** before every VM the candidate list is
recomputed and the running VM whose **last successful backup lies
furthest back** is picked next (never-backed-up VMs first). A long run
that gets interrupted therefore continues where it stopped instead of
restarting alphabetically and starving the last VMs; a VM that
live-migrated in is seen with its real backup history, not as a new
VM. The last success is read from the repository itself in repository
mode (shared across all hosts writing to it) and from
`<vm>-state.json` in directory mode. `backoff` (e.g. `"7d"`, or
seconds) is the minimum time between two backups of the same VM;
`-D` selections bypass it. Failures are counted per VM: after more
than 2 consecutive failures the administrator is notified on stderr
and the VM is only retried once the backoff period has passed since
the last error; any success resets the counter.

## systemd

Four units ship with the package (sources in [`systemd/`](systemd/)):

- `libvirt-backup.timer` — nightly 02:30 (`Persistent=true` catches up
  missed runs) → `libvirt-backup.service` runs `backup` and, on
  success only, `forget` (retention + prune)
- `libvirt-backup-check.timer` — weekly repository integrity check

Both services carry
`ConditionPathExists=/etc/libvirt-backup/libvirt-backup.toml`: without
a config file the timer runs are *skipped*, not failed.  Watch the
journal:

    journalctl -u libvirt-backup -n 50
    systemctl list-timers 'libvirt-backup*'
    systemctl start libvirt-backup.service    # manual run

## Tests

    ./tests/live-tests.sh       # end-to-end, self-cleaning

Defines a throwaway 64 MB domain on `qemu:///session` (no root
needed) plus a local rustic repository and runs the selection /
backoff / retry-tracking, repository round-trip and SIGTERM-abort
scenarios against them.  All scratch data lives in a private
`mktemp -d /tmp/libvirt-backup.XXXXXX` directory that is destroyed on
exit (`-k` keeps it for debugging).  Exit code 0 = all checks passed.

## Building

    ./build.py                  # quality checks + .deb
    ./build-package.sh          # .deb only, directly
    ./build-package.sh -c       # clean build output

`build.py` runs the quality checks (py_compile, ruff when present),
then the .deb via `build-package.sh` (lintian when present).  The .deb
(plus `.changes`/`.buildinfo`) lands **next to the checkout**.  Requires
`dpkg-dev`, `debhelper`, `fakeroot`; optionally `ruff` and `lintian`.

## CI and releases

[`.github/workflows/release.yml`](.github/workflows/release.yml) runs
when a **release is published**: it verifies that the tag matches the
version in `debian/changelog`, builds the .deb with
`build-package.sh` and the .rpm from `libvirt-backup.spec`, and
attaches the .deb, the .rpm and a versioned source tarball
(`libvirt-backup-<version>.tar.gz`) to the release assets.

It then adds both packages to signed apt + dnf repositories served
from the `gh-pages` branch via GitHub Pages
(`https://tk7r.github.io/libvirt-backup`), using
[`packaging/publish-repo.sh`](packaging/publish-repo.sh)
(`apt-ftparchive` + `createrepo_c`, everything GPG-signed; the script
also runs locally against a scratch directory).

One-time maintainer setup:

1. Create (or reuse) a GPG signing key and export both halves:

       gpg --armor --export-secret-keys KEYID > secret.asc
       gpg --armor --export KEYID > packaging/repo-pub.asc

2. Store `secret.asc` as repository secret `REPO_SIGNING_KEY` (plus
   `REPO_SIGNING_KEY_PASSPHRASE` if the key has a passphrase); commit
   `packaging/repo-pub.asc` — clients fetch it to verify the
   repositories.
3. Enable Pages (Settings → Pages) for the `gh-pages` branch, root
   folder; the first release creates the branch.

Every release needs a fresh `debian/changelog` version — bump it
before tagging.

The live tests (`tests/live-tests.sh`) are not part of CI — they need
qemu:///session + rustic and are run locally with
`./tests/live-tests.sh`.

## Repository layout

    libvirt-backup.py    the tool (single file)
    libvirt-backup.toml  fully commented config template
    libvirt-backup.spec  RPM spec (noarch, built by the release CI)
    examples/            ready-to-use configs (directory / rustic-S3)
    systemd/             units, timers and install README
    debian/              Debian packaging
    packaging/           publish-repo.sh + repo-pub.asc (Pages repos)
    docs/                background notes (disk source types, ...)
    tests/live-tests.sh  end-to-end test suite (self-cleaning)
    .github/workflows/   release CI: deb+rpm build, assets, Pages repos
    build-package.sh     .deb build script
    build.py             local build (quality + deb)
    LICENSE-MIT          license (MIT OR Apache-2.0, at your option)
    LICENSE-APACHE       license (MIT OR Apache-2.0, at your option)

## Notes & caveats

- Disks without a libvirt-backupable source (empty cdroms, `hostdev`
  LUN passthrough) are reported and skipped; VMs on iSCSI/`block`
  sources back up fine but always as full images.
- `s3:` repositories need a region (rustic requirement) — set
  `[repo] region` or export `AWS_REGION`.  It must be the region the
  server signs for: MinIO/AWS-like `eu-central-1`, Garage servers
  usually `garage`; a wrong region shows up as HTTP 400
  `unexpected scope`.
- rustic ≥ 0.8 has no built-in S3 backend; the tool routes `s3:` URLs
  through rustic's opendal backend (`opendal:s3` + `OPENDAL_*`
  environment variables) automatically.  restic keeps the URL as-is.
- Create an empty repository once with `libvirt-backup init` before the
  first backup.
- Pre-flight before the first VM is touched: repository mode probes the
  repository (`snapshots` — checks reachability, initialisation and the
  key/password/region in one go), so a missing `init` or a wrong `[repo]`
  setting aborts the run immediately instead of after the first 100 GiB
  block job has copied; `--dry-run` shows the same probe as a warning.
  Directory mode verifies the backup directory is writable.
- Before each VM is backed up (and in `--dry-run`), the tool lists the
  VM's libvirt checkpoints (`virsh checkpoint-list` equivalent); any
  checkpoint other than the tool's own incremental-chain checkpoint is
  reported as **WARN** — leftovers usually mean an aborted run or
  foreign tooling.  Clean up: `virsh checkpoint-delete <vm> <name>`.
- Locking is per host and per VM, so cluster hosts sharing one backup
  directory back up concurrently.  The run lock
  (`.libvirt-backup-<host>.lock`, holder `host pid` inside) lets only
  one run per host: a second `backup` prints a warning and skips
  (exit 0, nothing touched).  Each VM's backup additionally holds that
  VM's lock (`.libvirt-backup-vm-<vm>.lock`) for its duration, and a
  directory-mode `restore` of a VM takes the same lock (refuses with
  an error while it is held).  The VM lock spans hosts on a shared
  filesystem: a live migration can hand a VM to another host while a
  backup of it is still running there, and the new host then skips the
  VM (SKIP in the summary, naming the holder) instead of racing for
  the same `<vm>-backup-*` artefacts, chain file and snapshots.  The
  kernel releases the locks when the holding process dies, so a
  crashed run never blocks the next one.  A lock that lingers anyway
  (hung holder, or a child that inherited the lock fd) is cleanable:
  the warning names the holder and how to find the real lock holder
  (`fuser -v`), and `--force` on `backup`/`restore` proceeds despite
  it.  Stale lock files are harmless and never need removing.
- Repository-side locking differs by client.  **rustic is lock-free**
  by design: it neither takes nor honours repository locks, so there
  is no `rustic unlock`/break-lock and nothing to clean after a kill —
  concurrent safety comes from two-phase deletion (`forget --prune`
  only *marks* packs and deletes them after `--keep-delete`, default
  23 h).  Never pass `--instant-delete`, and never run `restic prune`
  or `restic forget --prune` against a repository rustic may be
  writing to (restic relies on a lock rustic ignores).  **restic**
  coordinates multi-host access itself: `backup` takes a non-exclusive
  lock, so several hosts may back up into one repository concurrently,
  while `prune`/`check` take an exclusive lock and wait or fail while
  another host is backing up — schedule `forget`/`check` outside the
  backup window.  When a killed run leaves a stale lock, the tool
  automatically runs `restic unlock` (removes locks older than 30 min
  only, never a live run's) and retries the command once.
- `.qcow2.job` files are pure intermediates, never artefacts: a failed
  or aborted backup removes them, and stale ones left by a killed run
  are discarded automatically when the next backup starts (with a
  warning; `--dry-run` reports them in advance).  A backup job still
  active on a VM (e.g. a killed run whose block job is still copying)
  fails that VM with a `virsh job-abort` hint instead of touching the
  live overlay.
- The tool never modifies or defines domains; restore only writes into
  the destination directory you choose.

## License

Licensed under either of, at your option:

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or
  <https://www.apache.org/licenses/LICENSE-2.0>)
- MIT license ([LICENSE-MIT](LICENSE-MIT) or
  <https://opensource.org/licenses/MIT>)
