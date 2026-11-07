#!/bin/sh
# build-package.sh - build the libvirt-backup Debian package
#
# Requires: dpkg-dev, debhelper, fakeroot, lintian (lintian optional).
# Run from anywhere; the package lands next to the repository checkout
# (dpkg-buildpackage default), e.g. ../libvirt-backup_0.1.5_all.deb.
#
# Usage:  ./build-package.sh [-c]
#   -c    "clean": remove build output (debian/ build artifacts and
#         the .deb/.changes/.buildinfo files next to the checkout)

set -eu

cd "$(dirname "$0")"

if [ "${1:-}" = "-c" ]; then
	dh_clean
	rm -f ../libvirt-backup_*.deb ../libvirt-backup_*.changes \
	      ../libvirt-backup_*.buildinfo
	echo "clean."
	exit 0
fi

for tool in dpkg-buildpackage fakeroot; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		echo "error: '$tool' not found (apt install dpkg-dev fakeroot)" \
			>&2
		exit 1
	fi
done

dpkg-buildpackage -us -uc -b

# locate the freshly built .deb (printed by dpkg-deb, but glob is fine)
deb=$(ls -t ../libvirt-backup_*.deb | head -1)

if command -v lintian >/dev/null 2>&1; then
	echo
	echo "=== lintian ==="
	lintian "$deb" && echo "lintian: no findings"
fi

echo
echo "built: $deb"
echo "install:  sudo apt install ../$(basename "$deb")"
