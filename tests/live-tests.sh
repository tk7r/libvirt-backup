#!/bin/bash
#
# live-tests.sh - end-to-end tests for libvirt-backup
#
# Defines a throwaway 64 MB domain on qemu:///session (no root needed,
# no guest agent -> backups are WARN/crash-consistent) and a local
# rustic repository, runs the selection/backoff/retry and repository
# round-trip scenarios against it and prints PASS/FAIL per check.
#
# All scratch data (VM image, repositories, configs, fake rustic)
# lives in a private mktemp directory and is destroyed on exit.
#
#   usage: tests/live-tests.sh [-k]
#     -k   keep the scratch directory (printed at the end) for debugging

set -u

here="$(cd "$(dirname "$0")" && pwd)"
tool="$here/../libvirt-backup.py"
work="$(mktemp -d /tmp/libvirt-backup.XXXXXX)"
vm="lbtest-$$"
keep=0
[ "${1:-}" = "-k" ] && keep=1
sigpid=""

cleanup() {
    local rc=$?
    [ -n "$sigpid" ] && kill -9 "$sigpid" 2>/dev/null
    virsh -c qemu:///session destroy "$vm" >/dev/null 2>&1
    virsh -c qemu:///session undefine "$vm" >/dev/null 2>&1
    if [ "$keep" = 1 ]; then
        echo "scratch dir kept: $work"
    else
        rm -rf "$work"
    fi
    exit "$rc"
}
trap cleanup EXIT INT TERM

for prereq in virsh qemu-img rustic python3 flock; do
    command -v "$prereq" >/dev/null 2>&1 || {
        echo "error: missing prerequisite: $prereq"
        exit 1
    }
done

n_pass=0 n_fail=0
check() {
    local desc="$1" expect="$2" actual="$3"
    if [[ "$actual" == *"$expect"* ]]; then
        printf 'PASS  %s\n' "$desc"
        n_pass=$((n_pass + 1))
    else
        printf 'FAIL  %s\n      expected substring: %s\n      got: %s\n' \
            "$desc" "$expect" "$(printf '%s' "$actual" | tr '\n' '|')"
        n_fail=$((n_fail + 1))
    fi
}

# the tool insists on root for standalone/repo runs; emulate it for the
# session-VM tests (qemu:///session runs qemu as the user anyway)
cat > "$work/run_lb.py" <<'PYEOF'
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("lb", sys.argv[1])
lb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lb)
os.geteuid = lambda: 0
sys.argv = ["libvirt-backup"] + sys.argv[2:]
try:
    rc = lb.main()
except SystemExit as e:
    if isinstance(e.code, str):
        # sys.exit("error: ...") - print what the tool wanted to say
        print(e.code, file=sys.stderr)
        rc = 1
    else:
        rc = e.code if e.code is not None else 0
sys.exit(rc if isinstance(rc, int) else 0)
PYEOF
lb() { python3 "$work/run_lb.py" "$tool" "$@"; }

# fake rustic: controllable upload failures (marker file) and a
# blocking upload (sleep) so SIGTERM can hit mid-stream
mkdir -p "$work/fakebin"
cat > "$work/fakebin/rustic" <<'FKEOF'
#!/bin/sh
case "$1" in
  snapshots) echo "[]" ;;
  backup)
    [ -n "${FAKE_RUSTIC_SLEEP:-}" ] && sleep "$FAKE_RUSTIC_SLEEP"
    cat >/dev/null
    [ -f "${FAKE_RUSTIC_FAIL:-}" ] && { echo "fake rustic: boom" >&2; exit 1; }
    exit 0 ;;
  *) exit 0 ;;
esac
FKEOF
chmod +x "$work/fakebin/rustic"

# throwaway VM
qemu-img create -f qcow2 "$work/$vm.qcow2" 64M >/dev/null || exit 1
cat > "$work/$vm.xml" <<XMLEOF
<domain type='kvm'>
  <name>$vm</name>
  <memory unit='MiB'>64</memory>
  <os><type>hvm</type><boot dev='hd'/></os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file="$work/$vm.qcow2"/>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>
XMLEOF
virsh -c qemu:///session define "$work/$vm.xml" >/dev/null || exit 1
virsh -c qemu:///session start "$vm" >/dev/null || exit 1

# real rustic repository + password
printf 'testpass\n' > "$work/repo.pass"
chmod 600 "$work/repo.pass"
rustic -r "$work/repo" --password-file "$work/repo.pass" init \
    >/dev/null 2>&1 || exit 1

# one backup_dir per scenario so the per-VM state files stay isolated
cat > "$work/dir.toml" <<CFGEOF
[backup]
uri = "qemu:///session"
backup_dir = "$work/bdir"
standalone = false
backoff = "1h"
CFGEOF
cat > "$work/repo.toml" <<CFGEOF
[backup]
uri = "qemu:///session"
backup_dir = "$work/rdir"
backoff = "1d"

[repo]
repository = "$work/repo"
password_file = "$work/repo.pass"
bin = "rustic"
CFGEOF
cat > "$work/failrepo.toml" <<CFGEOF
[backup]
uri = "qemu:///session"
backup_dir = "$work/fdir"
backoff = "1h"

[repo]
repository = "$work/failrepo"
password_file = "$work/repo.pass"
bin = "rustic"
CFGEOF

echo "== selection and backoff (directory mode)"
out=$(lb backup --config "$work/dir.toml" --dry-run 2>&1)
check "dry-run: fresh VM is top candidate" "never backed up" "$out"

out=$(lb backup --config "$work/dir.toml" 2>&1)
check "first backup succeeds (WARN: no agent)" "$vm " "$out"
check "first backup: no FAIL" "0 fail" "$out"
st=$(cat "$work/bdir/$vm-state.json" 2>/dev/null)
check "state: success recorded, retries 0" '"retries": 0' "$st"

out=$(lb backup --config "$work/dir.toml" 2>&1)
check "second run skipped by backoff" "backoff: last success" "$out"

out=$(lb backup --config "$work/dir.toml" -D "$vm" --dry-run 2>&1)
check "-D bypasses backoff (dry-run)" "would back up" "$out"

printf '[backup]\nbackoff = "7x"\n' > "$work/bad.toml"
out=$(lb backup --config "$work/bad.toml" --dry-run 2>&1)
check "invalid backoff value rejected" "invalid duration" "$out"

out=$(lb check --config "$work/dir.toml" 2>&1)
check "directory-mode check verifies overlay" "OK $vm-backup-vda.qcow2" "$out"
qemu-img create -f qcow2 "$work/base.qcow2" 8M >/dev/null
qemu-img create -f qcow2 -b "$work/base.qcow2" -F qcow2 \
    "$work/bdir/chainmember.qcow2" 8M >/dev/null
out=$(lb check --config "$work/dir.toml" 2>&1)
check "directory-mode check skips chain members" "has backing file" "$out"

echo "== repository mode"
out=$(lb backup --config "$work/repo.toml" 2>&1)
check "repo backup succeeds" "block device(s) to repo" "$out"

out=$(lb restore --config "$work/repo.toml" --list 2>&1)
check "config.xml versioned in repository" "$vm-config.xml" "$out"

lb restore --config "$work/repo.toml" -D "$vm" -d "$work/restore" \
    --verify >/dev/null 2>&1
[ -f "$work/restore/$vm-config.xml" ] && r=ok || r=missing
check "restore roundtrip brings config.xml back" "ok" "$r"

out=$(lb backup --config "$work/repo.toml" --dry-run 2>&1)
check "repo backoff from run tags" "backoff: last success" "$out"

echo "== failure tracking (fake rustic)"
touch "$work/fail-marker"
out3=""
for _ in 1 2 3; do
    out3=$(FAKE_RUSTIC_FAIL="$work/fail-marker" \
        PATH="$work/fakebin:$PATH" \
        lb backup --config "$work/failrepo.toml" 2>&1)
done
check "3rd failure notifies administrator" "consecutive backup failures" "$out3"
out=$(FAKE_RUSTIC_FAIL="$work/fail-marker" PATH="$work/fakebin:$PATH" \
    lb backup --config "$work/failrepo.toml" 2>&1)
check "4th run gated by backoff" "backoff: 3 consecutive failures" "$out"
rm -f "$work/fail-marker"
out=$(PATH="$work/fakebin:$PATH" lb backup --config "$work/failrepo.toml" \
    -D "$vm" 2>&1)
check "-D bypasses failure gate, backup succeeds" "block device(s) to repo" "$out"
st=$(cat "$work/fdir/$vm-state.json" 2>/dev/null)
check "success resets retry counter" '"retries": 0' "$st"

echo "== clean SIGTERM abort mid-upload"
# fresh state: the -D success above would otherwise backoff-gate this run
rm -f "$work/fdir/$vm-state.json"
FAKE_RUSTIC_SLEEP=30 PATH="$work/fakebin:$PATH" \
    python3 "$work/run_lb.py" "$tool" backup \
    --config "$work/failrepo.toml" > "$work/sig.out" 2>&1 &
sigpid=$!
sleep 6
kill -TERM "$sigpid" 2>/dev/null
wait "$sigpid" 2>/dev/null
sigpid=""
out=$(cat "$work/sig.out")
check "SIGTERM reported, run stopped" "terminated by signal 15" "$out"
if ls "$work/fdir"/*.job >/dev/null 2>&1; then
    leftover=yes
else
    leftover=none
fi
check "SIGTERM removed the overlay" "none" "$leftover"

echo "== locking"
# run lock is per host and every backed-up VM has its own lock file,
# in both modes; the old single global lock file is gone
if [ -f "$work/rdir/.libvirt-backup-$(uname -n).lock" ] \
   && [ -f "$work/rdir/.libvirt-backup-vm-$vm.lock" ] \
   && [ -f "$work/bdir/.libvirt-backup-$(uname -n).lock" ] \
   && [ ! -f "$work/rdir/.libvirt-backup.lock" ] \
   && [ ! -f "$work/bdir/.libvirt-backup.lock" ]; then
    r=per-host-and-vm
else
    r=wrong
fi
check "run lock per host, VM lock per VM" "per-host-and-vm" "$r"

# a VM lock held under another host's name (the live-migration race)
# makes this host skip that VM
printf 'otherhost 99999\n' > "$work/rdir/.libvirt-backup-vm-$vm.lock"
flock -x "$work/rdir/.libvirt-backup-vm-$vm.lock" -c 'sleep 15' &
otherpid=$!
sleep 1
out=$(lb backup --config "$work/repo.toml" -D "$vm" 2>&1)
kill "$otherpid" 2>/dev/null
wait "$otherpid" 2>/dev/null
check "VM locked by another host is skipped" \
    "locked by otherhost 99999" "$out"

# a directory-mode restore of a locked VM is refused
flock -x "$work/bdir/.libvirt-backup-vm-$vm.lock" -c 'sleep 15' &
otherpid=$!
sleep 1
out=$(lb restore --config "$work/dir.toml" -D "$vm" \
    -d "$work/restore-locked" 2>&1)
kill "$otherpid" 2>/dev/null
wait "$otherpid" 2>/dev/null
check "restore of a locked VM is refused" \
    "another libvirt-backup run is active" "$out"

# a run lock held under another host's name must not block this host
flock -x "$work/rdir/.libvirt-backup-otherhost.lock" -c 'sleep 15' &
otherpid=$!
sleep 1
out=$(lb backup --config "$work/repo.toml" -D "$vm" 2>&1)
kill "$otherpid" 2>/dev/null
wait "$otherpid" 2>/dev/null
check "foreign-host run lock does not block" "0 fail" "$out"

# a concurrent run on the same host is still refused
FAKE_RUSTIC_SLEEP=15 PATH="$work/fakebin:$PATH" \
    python3 "$work/run_lb.py" "$tool" backup \
    --config "$work/failrepo.toml" >/dev/null 2>&1 &
samepid=$!
sleep 3
out=$(PATH="$work/fakebin:$PATH" \
    lb backup --config "$work/failrepo.toml" -D "$vm" 2>&1)
kill "$samepid" 2>/dev/null
wait "$samepid" 2>/dev/null
check "same-host concurrent run is locked out" \
    "another libvirt-backup run is active" "$out"

echo
echo "$n_pass passed, $n_fail failed"
[ "$n_fail" -eq 0 ]
