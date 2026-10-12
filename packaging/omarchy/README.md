# sunshine-mic for Omarchy / Arch

`PKGBUILD` builds this repository's `mic` branch as the package **`sunshine-mic`**. It provides and conflicts with
`sunshine`, installs to the same paths and uses the same systemd user unit, so an existing Sunshine setup (service,
pairing, `~/.config/sunshine`, firewall rules) keeps working unchanged. It is derived from Omarchy's own PKGBUILD
(`omacom/omarchy-pkgs`, `pkgbuilds/sunshine`).

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
- The result is `sunshine-mic-<version>-<rel>-x86_64.pkg.tar.zst` (plus a `-debug` split package).

## Install

```bash
systemctl --user stop sunshine
sudo pacman -U sunshine-mic-*-x86_64.pkg.tar.zst   # answer y to "Remove sunshine?"; do not use --noconfirm
getcap /usr/bin/sunshine                           # cap_sys_admin,cap_sys_nice=p
systemctl --user daemon-reload && systemctl --user start sunshine
```

Then open the microphone port (see `docs/mic_streaming.md` for the UFW rules).

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

## Roll back

```bash
systemctl --user stop sunshine
sudo pacman -S sunshine       # answer y to "Remove sunshine-mic?"
systemctl --user daemon-reload && systemctl --user start sunshine
```
