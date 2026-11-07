# libvirt-backup.spec - RPM packaging (noarch)
#
# The version comes from debian/changelog; the release workflow builds
# the source tarball libvirt-backup-<version>.tar.gz first, then:
#
#   rpmbuild -bb -D "lb_version <version>" -D "_sourcedir <tarball dir>" \
#            libvirt-backup.spec
#
# Without lb_version the build uses 0.0.0 (local smoke test only).

Name:           libvirt-backup
Version:        %{?lb_version}%{!?lb_version:0.0.0}
Release:        1
Summary:        Push backup and restore tool for libvirt/QEMU virtual machines
License:        MIT OR Apache-2.0
URL:            https://github.com/tk7r/libvirt-backup
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3

# Fedora/RHEL define _unitdir via systemd-rpm-macros; distros without
# that package (e.g. Ubuntu runners) get the canonical location
%{!?_unitdir:%define _unitdir %{_prefix}/lib/systemd/system}

# same runtime model as the .deb: python3 >= 3.11 (tomllib), libvirt
# bindings, qemu-img/qemu-storage-daemon (Fedora: qemu-img package)
Requires:       python3 >= 3.11
Requires:       python3-libvirt
Requires:       qemu-img
# repository mode (weak deps, as in the .deb Recommends)
Recommends:     rustic
Recommends:     python3-libnbd

%description
libvirt-backup backs up every running domain of a libvirt host entirely
through the libvirt API (domfsfreeze, backupBegin, blockjob monitoring,
dumpxml).  Images are written as standalone qcow2/raw files into a backup
directory, or flattened to raw and streamed into a deduplicated,
encrypted rustic/restic repository (e.g. S3).
.
The restore subcommand places one VM's images and XMLs into a destination
directory and runs on a rescue system without libvirt; forget and check
handle repository retention and integrity.  The included systemd timers
stay inert until /etc/libvirt-backup/libvirt-backup.toml exists.

%prep
%autosetup

%build
# nothing to compile - single Python file

%install
install -D -m 0755 libvirt-backup.py %{buildroot}%{_bindir}/libvirt-backup
install -D -m 0644 debian/libvirt-backup.1 \
    %{buildroot}%{_mandir}/man1/libvirt-backup.1
gzip -9nf %{buildroot}%{_mandir}/man1/libvirt-backup.1
install -D -m 0644 examples/libvirt-backup-directory.toml \
    %{buildroot}%{_sysconfdir}/libvirt-backup/libvirt-backup.toml.directory-mode
install -D -m 0644 examples/libvirt-backup-rustic.toml \
    %{buildroot}%{_sysconfdir}/libvirt-backup/libvirt-backup.toml.repository-mode
for u in libvirt-backup libvirt-backup-check; do
    install -D -m 0644 systemd/$u.service \
        %{buildroot}%{_unitdir}/$u.service
    install -D -m 0644 systemd/$u.timer \
        %{buildroot}%{_unitdir}/$u.timer
done

%check
# the installed file is the tool itself - --help must work without
# libvirt, config or root
%{buildroot}%{_bindir}/libvirt-backup --help > /dev/null

# plain systemctl scriptlets (no systemd-rpm-macros, so the spec also
# builds on non-Fedora rpmbuild); the services are oneshot units driven
# by the timers, which enable on install (as in the .deb) and stay
# inert until /etc/libvirt-backup/libvirt-backup.toml exists
%post
systemctl --system enable libvirt-backup.timer libvirt-backup-check.timer \
    > /dev/null 2>&1 || :

%preun
# $1 == 0: removal (an upgrade keeps the timers enabled)
if [ "$1" -eq 0 ]; then
    systemctl --system disable --now libvirt-backup.timer \
        libvirt-backup-check.timer > /dev/null 2>&1 || :
fi

%postun
systemctl --system daemon-reload > /dev/null 2>&1 || :

%files
%license LICENSE-MIT LICENSE-APACHE
%doc README.md
%{_bindir}/libvirt-backup
%{_mandir}/man1/libvirt-backup.1*
%dir %{_sysconfdir}/libvirt-backup
%{_sysconfdir}/libvirt-backup/libvirt-backup.toml.*
%{_unitdir}/libvirt-backup.service
%{_unitdir}/libvirt-backup.timer
%{_unitdir}/libvirt-backup-check.service
%{_unitdir}/libvirt-backup-check.timer

%changelog
* Sat Nov 07 2026 Thomas Kotzian <thomas.kotzian@lausa.at> - %{version}-%{release}
- initial RPM packaging, mirrors the Debian package
