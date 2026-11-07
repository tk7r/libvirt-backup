#!/bin/sh
# publish-repo.sh - add the built .deb and .rpm to a GitHub-Pages
# checkout as signed apt and dnf repositories.
#
# Called by .github/workflows/release.yml after the packages are built;
# the caller owns all git operations on PAGES_DIR.  Runnable locally
# against a scratch directory for testing.
#
# Environment (all required unless marked):
#   PAGES_DIR              pages checkout the packages join
#   DEB_FILE               built .deb (copied to pool/)
#   RPM_FILE               built .rpm (signed here, copied to rpm/)
#   PAGES_URL              public root URL PAGES_DIR is served from
#   PUBKEY_FILE            armored public key, published as repo-pub.asc
#   SIGNING_KEY_FILE       armored secret key used for all signing
#   SIGNING_KEY_PASSPHRASE (optional) passphrase of the secret key
#   DIST                   apt distribution name (default: stable)

set -eu

DIST=${DIST:-stable}
COMPONENT=main

for var in PAGES_DIR DEB_FILE RPM_FILE PAGES_URL PUBKEY_FILE \
           SIGNING_KEY_FILE; do
    eval "val=\${$var:-}"
    [ -n "$val" ] || { echo "error: $var is not set" >&2; exit 1; }
done
for file in "$DEB_FILE" "$RPM_FILE" "$PUBKEY_FILE" "$SIGNING_KEY_FILE"; do
    [ -f "$file" ] || { echo "error: no such file: $file" >&2; exit 1; }
done
for tool in apt-ftparchive createrepo_c rpmsign gpg gzip; do
    command -v "$tool" >/dev/null \
        || { echo "error: '$tool' not installed" >&2; exit 1; }
done

# throwaway keyring plus scratch space: the secret key never touches a
# persistent keyring
WORKDIR=$(mktemp -d)
export GNUPGHOME="$WORKDIR/gnupg"
mkdir -m 700 "$GNUPGHOME"
trap 'rm -rf "$WORKDIR"' EXIT INT TERM
# rpmsign calls gpg internally; loopback lets one passphrase priming
# of the agent cover every later signing call
printf 'allow-loopback-pinentry\n' > "$GNUPGHOME/gpg-agent.conf"
printf 'pinentry-mode loopback\n'  > "$GNUPGHOME/gpg.conf"
gpg --batch --quiet --import "$SIGNING_KEY_FILE"
FPR=$(gpg --batch --with-colons --list-secret-keys \
      | awk -F: '/^fpr/{print $10; exit}')
[ -n "$FPR" ] || { echo "error: no secret key in $SIGNING_KEY_FILE" >&2; exit 1; }

sign() {
    if [ -n "${SIGNING_KEY_PASSPHRASE:-}" ]; then
        gpg --batch --yes --pinentry-mode loopback \
            --passphrase "$SIGNING_KEY_PASSPHRASE" -u "$FPR" "$@"
    else
        gpg --batch --yes -u "$FPR" "$@"
    fi
}

# prime the agent cache once so rpmsign's internal gpg stays headless
sign --armor --output "$GNUPGHOME/probe.asc" --sign "$PUBKEY_FILE"
rm -f "$GNUPGHOME/probe.asc"

rpmsign --define "_gpg_name $FPR" --addsign "$RPM_FILE"

mkdir -p "$PAGES_DIR/pool" "$PAGES_DIR/rpm" \
         "$PAGES_DIR/dists/$DIST/main/binary-all"
: > "$PAGES_DIR/.nojekyll"
cp -f "$DEB_FILE" "$PAGES_DIR/pool/"
cp -f "$RPM_FILE" "$PAGES_DIR/rpm/"
cp -f "$PUBKEY_FILE" "$PAGES_DIR/repo-pub.asc"

# apt metadata: pool/ keeps every released version, apt picks newest
(
    cd "$PAGES_DIR"
    apt-ftparchive packages pool \
        > "dists/$DIST/main/binary-all/Packages"
    # -k is essential: apt only recognises index targets whose plain
    # "Packages" key is listed in Release (it then picks a compressed
    # variant itself); listing only Packages.gz makes apt skip the
    # repository silently as "optional target not provided"
    gzip -9nkf "dists/$DIST/main/binary-all/Packages"
    # generate outside dists/ so the Release file cannot hash a
    # partially written copy of itself, then move it into place
    apt-ftparchive \
        -o APT::FTPArchive::Release::Origin=libvirt-backup \
        -o APT::FTPArchive::Release::Label=libvirt-backup \
        -o APT::FTPArchive::Release::Suite="$DIST" \
        -o APT::FTPArchive::Release::Codename="$DIST" \
        -o APT::FTPArchive::Release::Components="$COMPONENT" \
        -o APT::FTPArchive::Release::Architectures=all \
        release "dists/$DIST" > "$WORKDIR/Release.tmp"
    mv "$WORKDIR/Release.tmp" "dists/$DIST/Release"
    # detached armored signature: apt's verifier rejects clearsigned
    # Release.gpg ("lines not belonging to a signature")
    sign --armor --output "dists/$DIST/Release.gpg" \
         --detach-sign "dists/$DIST/Release"
)

# dnf metadata: repodata over rpm/, repomd signature for repo_gpgcheck
createrepo_c "$PAGES_DIR/rpm"
sign --armor --output "$PAGES_DIR/rpm/repodata/repomd.xml.asc" \
     --detach-sign "$PAGES_DIR/rpm/repodata/repomd.xml"

cat > "$PAGES_DIR/libvirt-backup.repo" <<EOF
[libvirt-backup]
name=libvirt-backup
baseurl=$PAGES_URL/rpm
gpgcheck=1
gpgkey=$PAGES_URL/repo-pub.asc
repo_gpgcheck=1
enabled=1
EOF

echo "published $DEB_FILE and $RPM_FILE into $PAGES_DIR" \
     "(apt dist '$DIST', dnf repodata, all signed by $FPR)"
