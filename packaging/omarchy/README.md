# sunshine-mic for Omarchy / Arch

`PKGBUILD` builds this repository's `mic` branch as the package **`sunshine-mic`**. It provides and conflicts with
`sunshine`, installs to the same paths and uses the same systemd user unit, so an existing Sunshine setup (service,
pairing, `~/.config/sunshine`, firewall rules) keeps working unchanged. It is derived from Omarchy's own PKGBUILD
(`omacom/omarchy-pkgs`, `pkgbuilds/sunshine`).

> Read `docs/mic_streaming.md` first. The microphone stream is not authenticated or encrypted; install it only for
> one known client over Tailscale/WireGuard, and verify the network restriction **before** enabling `stream_mic`.

## Build

The package is built from the **committed** `mic` branch of this repository (not the working tree).

```bash
cd packaging/omarchy
makepkg -s --nocheck          # -s installs missing makedepends with sudo: appstream-glib, python-jinja
```

- Pin an exact commit: `_commit=$(git rev-parse HEAD) makepkg -s --nocheck -f`
- Build from a hosted fork: `_repo_url=https://github.com/<you>/Sunshine.git makepkg -s --nocheck -f`. The web UI's
  website and issue links follow `_repo_url` (override with `_project_url`), so reports never go to upstream's tracker.
- Bump `pkgrel` for every rebuild of the same upstream base so the installed package can be told apart.
- Run the real validators (`appstreamcli`, `appstream-util`, `desktop-file-validate`); do not stub them.
- The result is `sunshine-mic-<pkgver>-<pkgrel>-x86_64.pkg.tar.zst` (plus a `-debug` split package).

### Branch HEAD versus package commit

A package is a build of **one commit**. The packaged executable prints it:

```bash
sunshine --version          # ... Sunshine version: <pkgver> commit: <40-hex>
```

The branch `HEAD` can be ahead of that commit, for example by documentation-only commits that were made after the
package was built and validated. That is expected and is not a reason to rebuild a validated binary, but it must be
visible: record the package commit and the artifact's SHA256 together when you validate an artifact, and compare the
commit printed by `--version` with the one you recorded, not with `HEAD`.

## Choose one artifact

Builds of the same version with different `pkgrel` can sit next to each other, so **never install with a glob**
(`sunshine-mic-*.pkg.tar.zst` matches every revision). Name the one artifact you validated:

```bash
cd packaging/omarchy
PKG="$PWD/sunshine-mic-<pkgver>-<pkgrel>-x86_64.pkg.tar.zst"     # fill in the one you chose
test -f "$PKG" && echo "artifact present"
pacman -Qip "$PKG" | grep -E '^(Name|Version|Provides|Conflicts With)'
sha256sum "$PKG"                                                  # must equal the checksum you recorded for it
```

## Keep the exact stock package before installing

Rolling back with `pacman -S sunshine` can fetch a different version than the one you ran. Preserve the installed
stock package (and its signature) first, and verify the copy:

```bash
STOCK_SRC="/var/cache/pacman/pkg/sunshine-<version>-x86_64.pkg.tar.zst"   # the version `pacman -Q sunshine` prints
SAFE="$HOME/<private-backup-dir>"                                          # outside the repository, mode 0700
mkdir -m 0700 -p "$SAFE"
install -m 0600 "$STOCK_SRC" "$STOCK_SRC.sig" "$SAFE/"
( cd "$SAFE" && sha256sum sunshine-<version>-x86_64.pkg.tar.zst* )        # note both checksums
```

## Install

Stop the service (this ends any running stream), then install the chosen artifact:

```bash
systemctl --user stop sunshine
sudo pacman -U "$PKG"       # answer y to "Remove sunshine?"; do not use --noconfirm
```

Then check and start it:

```bash
getcap /usr/bin/sunshine                          # cap_sys_admin,cap_sys_nice=p
systemctl --user daemon-reload && systemctl --user start sunshine
```

For a first trial keep `stream_mic = disabled` in `sunshine.conf` until the network restriction for the microphone
port is in place and verified (see `docs/mic_streaming.md`).

## Updates

`sunshine-mic` is a foreign package: `pacman -Syu` and `omarchy-update` do not replace it, even when Omarchy's
repository publishes a newer `sunshine`. When Omarchy moves to a newer upstream Sunshine tag, rebase the `mic`
branch onto that tag, set `_basever` and `pkgrel` in `PKGBUILD`, rebuild and reinstall:

```bash
git fetch upstream --tags
git rebase v<new tag>
git submodule update --init --recursive --depth 1
# edit PKGBUILD: _basever=<new>, pkgrel=1; run the unit tests; commit
makepkg -s --nocheck -f
```

Do not re-run `omarchy-install-service-sunshine` while `sunshine-mic` is installed: it tries to install the
conflicting `sunshine` package and aborts.

## Roll back to the exact stock package

Use the preserved file from "Keep the exact stock package"; its `.sig` must sit next to it under its exact name.

```bash
STOCK="$SAFE/sunshine-<version>-x86_64.pkg.tar.zst"
test -f "$STOCK" && test -f "$STOCK.sig" && echo "stock package and signature present"
( cd "$SAFE" && sha256sum -c <<< "<recorded package sha256>  sunshine-<version>-x86_64.pkg.tar.zst" )
```

```bash
systemctl --user stop sunshine
sudo pacman -U "$STOCK"     # answer y to "Remove sunshine-mic?"; never `pacman -S sunshine`, never --noconfirm
```

```bash
pacman -Q sunshine          # must print exactly the version you preserved
pacman -Q sunshine-mic      # must fail: the package is gone
systemctl --user daemon-reload && systemctl --user start sunshine
```

Configuration is left alone by a package rollback. The fork's two options in `sunshine.conf` are harmless to stock
Sunshine (unknown keys are reported and ignored); restoring `~/.config/sunshine` from a backup is a separate, optional
step that also removes any client paired in the meantime.
