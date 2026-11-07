# systemd units for libvirt-backup

## Debian package

Installing the `libvirt-backup` .deb provides everything:

- `/usr/bin/libvirt-backup` — the tool
- `/etc/libvirt-backup/libvirt-backup.toml.directory-mode` and
  `.repository-mode` — ready-made config templates (see also the
  fully commented `libvirt-backup.toml` template in the source repo)
- the four units below, with the timers enabled and started

The units are inert until a config file exists
(`ConditionPathExists=/etc/libvirt-backup/libvirt-backup.toml`), so
after installation pick a mode:

    cp /etc/libvirt-backup/libvirt-backup.toml.repository-mode \
       /etc/libvirt-backup/libvirt-backup.toml
    $EDITOR /etc/libvirt-backup/libvirt-backup.toml   # [repo] + [retention]!
    libvirt-backup backup --dry-run                  # verify the config

From then on the nightly/weekly timers do their work; no unit restart
is needed.

## Manual install (no package)

    install -m 755 libvirt-backup.py /usr/bin/libvirt-backup
    mkdir -p /etc/libvirt-backup
    cp examples/libvirt-backup-*.toml /etc/libvirt-backup/   # as templates
    cp systemd/libvirt-backup.{service,timer} \
       systemd/libvirt-backup-check.{service,timer} /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now libvirt-backup.timer libvirt-backup-check.timer

## How it works

- `libvirt-backup.timer` starts `libvirt-backup.service` nightly at
  02:30 (`Persistent=true` catches up a missed run the same morning).
  The service runs `libvirt-backup backup` and, only if it succeeded,
  `libvirt-backup forget` (retention + prune from the `[retention]`
  config section; a no-op without `[repo]`).
- `libvirt-backup-check.timer` runs `libvirt-backup check` weekly for
  repository integrity.
- Both services carry `ConditionPathExists=/etc/libvirt-backup/libvirt-backup.toml`:
  without a config the run is *skipped*, not failed.
- A oneshot service cannot start twice, so a new timer trigger is
  skipped while the previous run is still active.  Additionally the
  tool takes its own flock locks inside the backup directory: a
  per-host run lock (`.libvirt-backup-<host>.lock`) — a second run on
  the same host prints a warning and exits 0 without touching
  anything — and a per-VM lock (`.libvirt-backup-vm-<vm>.lock`) around
  each VM's backup and directory-mode restore, which spans hosts of a
  cluster sharing one backup directory.  A stuck run holding a lock
  can be cleared with `--force` after killing it.
- `TimeoutStartSec=infinity` — backups legitimately run for hours.
- Output goes to the journal:

      journalctl -u libvirt-backup -n 50
      systemctl list-timers libvirt-backup*

- Config search order: `--config FILE`,
  `/etc/libvirt-backup/libvirt-backup.toml`, `/etc/libvirt-backup.toml`
  (legacy), `~/.config/libvirt-backup.toml`; sections `[backup]`,
  `[domains.*]`, `[repo]`, `[retention]` (see the tool's `--help`).
  If the backup destination is a separate mount, uncomment
  `ConditionPathIsMountPoint` in `libvirt-backup.service`.
- Manual runs: `systemctl start libvirt-backup.service`, or directly
  `libvirt-backup backup --dry-run`.
