# Install Sunshine Mic on Omarchy

**Independent community preview. Release qualification and package publication are pending.**
Do not treat an earlier development build, a prepared recipe, or a GitHub branch as a qualified release.
No verified `sunshine-mic-bin` AUR entry or official Omarchy package is advertised yet.

## Requirements and safety

- Omarchy/Arch **x86_64**, a graphical user session and PipeWire.
- Microphone-capable **VoidLink on iPad**. Stock Moonlight does not transmit microphone audio.
- Tailscale installed, authenticated on host and iPad, with `tailscale0` present on the host.
- One desktop user and one exact Tailscale IPv4 iPad peer. Other clients/hardware remain unverified.
- A **local keyboard or independent SSH connection before setup, reconfiguration or reset**.
  Stopping Sunshine disconnects the streamed desktop. The management CLI never stops it for you.

The package replaces stock `sunshine`: it provides/conflicts with Sunshine and retains `/usr/bin/sunshine`,
`~/.config/sunshine`, pairing/account data and `app-dev.lizardbyte.app.Sunshine.service`.
It does not configure Voxtype, transcription, models, desktop controls or video tuning.
Never run `omarchy-install-service-sunshine`: that installs/configures stock Sunshine and opens unrelated ports.
Never install a candidate on the production host used to develop this fork.

## Publication status and package discovery

A qualified immutable preview release, complete Corresponding Source, provenance, checksums and successful
AUR publication must exist before these become installation instructions. **They are not verified available now.**
After publication, the intended discovery and installation commands are:

```sh
yay -Ssa sunshine-mic
yay -Siia sunshine-mic-bin
yay -S aur/sunshine-mic-bin
```

Use the interactive installation so you can review the recipe, package checksum and stock-Sunshine conflict.
Omarchy **Install → AUR** searches package names; search `sunshine-mic-bin`, not description keywords.
The inspected Omarchy version also offers `omarchy pkg aur add sunshine-mic-bin`, but its wrapper uses
`--noconfirm`, so it is not recommended for first migration. There is no `omarchy pkg search` command.

**Official Omarchy repository: not submitted yet.** Submission and edge-only acceptance are separate from
signed publication. Do not use **Install → Package** or advertise `omarchy pkg add sunshine-mic` until the
package is actually published in your channel. Do not switch a stable host to edge for this preview.

## Preserve an exact rollback package

Before replacement, identify the exact installed package with `pacman -Q sunshine` (or `sunshine-mic` for an
existing fork installation). Save its exact package archive **and signature** outside this repository in a
private directory, and record both SHA256 hashes. Use literal filenames, never an ambiguous package glob.
If the exact package/signature is unavailable, obtain it and verify it before proceeding with migration.
Keep a private configuration backup; never attach pairing/account material to an Issue.

## Fresh host: account, Desktop and pairing

Keep both microphone options disabled until protected setup succeeds. An inherited development `-1`/`-2`
binary had enabled defaults and is not suitable for this route. A fresh qualified package must leave both
options disabled without installing a profile or writing any user-home activation.

1. Confirm the Sunshine base port you intend to use. The ordinary base is **47989**; an existing host may
   have a different saved `port`. Pinning a base changes **all** Sunshine service ports, not just the mic.
2. For initial account setup only, run Sunshine as your desktop user with explicit disabled options:
   `/usr/bin/sunshine port=<CONFIRMED_CURRENT_BASE> stream_mic=disabled mic_default_source=disabled`.
   Do not run a second copy alongside the service. Preserve an existing account/configuration instead of
   recreating it. Stop this initial process before protected setup.
3. The HTTPS web interface is at **base+1** (ordinary `https://localhost:47990`). Open it locally, or use an
   authenticated private SSH tunnel to that local port. Never open the web UI to the public Internet.
   Verify the private/local endpoint before accepting its self-signed certificate; create the web account.
4. Check the **Desktop** application exists in Sunshine's Applications page. On a fresh configuration,
   create an application named `Desktop` with no launch command if absent; do not replace existing apps.
5. Run `tailscale ip -4` on the host and `tailscale status` to identify the host and exact iPad peer addresses.
   The iPad's Tailscale app also shows its address. An offline peer is a distinct diagnostic state, not
   permission to fall back to LAN. Do not publish tailnet names or addresses in reports.
6. In VoidLink, manually add the host using its **Tailscale IPv4** address. Avoid the automatically discovered
   LAN entry. Pair using the PIN in Sunshine's web interface over the same private/local administrative path.
   Preserve existing pairing when replacing stock Sunshine.

## Protected microphone opt-in

**Have local keyboard/independent SSH access now. The next command ends your stream.**
Close any manually launched Sunshine process, then explicitly stop the canonical service:

```sh
systemctl --user stop app-dev.lizardbyte.app.Sunshine.service
```

Run all management commands as the desktop user, never `sudo sunshine-mic`. Root changes use authenticated
sudo to the fixed helper. Substitute your confirmed values; angle-bracket text below is not executable data.
The preview requires the explicit opt-in and never infers a base port:

```text
sunshine-mic setup --base-port <CONFIRMED_CURRENT_BASE> --client-tailscale-ip <IPAD_TAILSCALE_IPV4> --enable-microphone --dry-run
sunshine-mic setup --base-port <CONFIRMED_CURRENT_BASE> --client-tailscale-ip <IPAD_TAILSCALE_IPV4> --enable-microphone
```

The displayed derived ports are TCP **base−5/base/base+21** and microphone UDP **base+12**.
For the ordinary base these are TCP 47984/47989/48010 and UDP 48001. The CLI validates the exact peer against
the current tailnet and rejects self, unknown peers, ranges, unsupported units and foreign guard assets.
`--dry-run` neither elevates nor mutates. Identical setup is idempotent; changed settings need `reconfigure`.
Setup refuses an activating/deactivating service, a direct Sunshine process or conflicting listeners instead
of stopping anything automatically. Do not remove a foreign/legacy guard to bypass a refusal.

Repeat `--client-lan-ip <KNOWN_ALTERNATE_IP>` for each exact known alternative IPv4/IPv6 client address if
needed. Targeted non-Tailscale TCP rejects discourage those known LAN session paths only. A newly assigned,
unlisted address is not covered: this is **not physical-device authentication**, and a LAN session can cause
receiver source mismatch/silence. Use the manually added Tailscale host entry rather than broadening ingress.

Only after setup reports success, enable startup with the graphical **user session**:

```sh
systemctl --user enable --now app-dev.lizardbyte.app.Sunshine.service
sunshine-mic status
sunshine-mic doctor
```

This is not a root Sunshine daemon and does not enable lingering. Protected setup separately enables the root
oneshot guard at boot. Verify both after a qualification reboot; a oneshot's active state alone is not proof of
kernel protection. Failed guard enable/start means incomplete setup, not successful activation. A user drop-in
or daemon-reload failure leaves the firewall in place and reports **guard installed; activation incomplete**.
Rerun the identical requested operation to recover; never delete protection as error cleanup.

## Use the microphone

Allow VoidLink microphone access in iPadOS privacy settings and enable its microphone option **before
connecting/reconnecting**. Launch Desktop through the manually added Tailscale host entry. Select
**Moonlight Mic** in a harmless input meter/recorder or the application that needs it. No transcription engine
is included and the host's default input is not automatically changed. No node/socket when there is no active
microphone session is normal. Disconnect should remove them; reconnect should restore them.

## Security model

Sunshine's microphone datagrams have no protocol-level authentication or encryption. The supported route uses
Tailscale's private transport plus a dedicated `inet sunshine_mic_managed` nftables input chain at priority −10.
It accepts only the selected IPv4 peer on `tailscale0` for UDP base+12, then drops every other IPv4/IPv6 ingress
path to that port. Another firewall may still deny accepted traffic. No unrelated table is flushed.

The root-owned profile is authoritative; the user receipt is not trusted by root. The managed user drop-in
replaces only ExecStart and preserves the packaged readiness delay. Before exec, the wrapper checks profile,
owner, transition state and guard application; any error aborts without an unguarded fallback. No argument
passthrough is accepted. Saved port changes require explicit reconfiguration rather than silently changing the
protected socket. The wrapper pins `mic_default_source=disabled` on every managed launch.

**Startup gating is not continuous tamper protection.** An administrator firewall flush during a session defeats
the guard until reapplication. `sunshine-mic guard reload` reapplies the typed profile; arbitrary rules cannot be
supplied. Stopping the guard service retains rules. Package install/upgrade does not opt in, write user homes,
or restart Sunshine, PipeWire or Tailscale. Inherited package hooks still set executable capabilities and install
udev/module integration; review these privileged host changes before installation.

## Status and troubleshooting

```sh
sunshine-mic status --json
sunshine-mic doctor --json
```

Stable fields are `state`, `desired_enabled`, `protection_verified`, `running_enabled`, `restart_required`,
`issues`, `next_commands`. Exit 0 means the requested state was verified, 1 unhealthy/drift, 2 usage/conflict,
and 3 verification unavailable. Disabled and unconfigured are legitimate states, not enabled-health claims.
Without noninteractive inspection privilege, protection is **unverified**, never assumed healthy from disk.

Default diagnostics redact addresses, home paths and tailnet names. `--show-addresses` reveals addresses only,
never credentials. Review output before posting it. Diagnostics do not record audio or restart services.
Check client permission/option, exact peer availability/route, effective managed launch, actual kernel guard,
then session address and traffic. No arriving packets, another firewall's denial, and a LAN/Tailscale source
mismatch are different causes; do not infer accepted audio from an open socket or a running service.

## Reconfigure, disable and reset

Reconfiguration requires stopped Sunshine and the same explicit arguments as setup:

```text
sunshine-mic reconfigure --base-port <CONFIRMED_NEW_BASE> --client-tailscale-ip <IPAD_TAILSCALE_IPV4> --enable-microphone
```

`sunshine-mic disable` immediately installs deny-all for the mic port without restarting Sunshine. It retains
managed activation and future launches use disabled microphone arguments. A currently running node/socket may
remain until disconnect/restart; disable does not claim to tear it down.

`sunshine-mic reset` removes only owned profile/delegation/table/activation. It requires stopped Sunshine,
unmodified managed assets, and saved mic options demonstrably disabled/absent on an attested disabled-default
binary. Duplicate, multiline or ambiguous assignments are refused, not rewritten. Fix configuration deliberately
as its owner before retrying. An unresolved transition is recovered conservatively with deny protection first.

## Updates and exact-stock rollback

After AUR publication, update using yay/Omarchy AUR updates. Choose when to stop/restart Sunshine explicitly;
upgrade must preserve the profile without interrupting sessions. Save the exact previous package/signature
privately before replacement and verify the new package hash/provenance. Never replace published asset bytes.

For rollback, obtain local/independent SSH access, stop the canonical user service, run `sunshine-mic disable`
if needed and then `sunshine-mic reset`, resolving every refusal. Only then install the exact preserved stock
package using interactive `sudo pacman -U /absolute/path/to/the-exact-package.pkg.tar.zst`, with its signature
beside it. Verify its installed version and ordinary pairing/configuration. Do not restore an old config backup
blindly and discard newer pairings. Package removal before reset retains protection/activation and warns; it must
not silently restore an unguarded launch.

## Build

The source recipe is `packaging/omarchy/PKGBUILD`, not the unrelated generated upstream Arch recipe.
Public-release builds must use an immutable versioned tag or a literal full candidate commit, public recursive
inputs and the pinned verified FFmpeg asset. The preview supports x86_64 only; CUDA compilation does not prove
NVIDIA runtime compatibility. Build in disposable Arch, never use the production host as a qualification target.

The required candidate command, after provisioning declared dependencies and the isolation/Xvfb tools, is:

```text
_commit=<FULL_CANDIDATE_SHA> _run_unit_tests=true _support_headless_testing=true makepkg --syncdeps --noconfirm
```

A build is not a release. Qualify the **extracted packaged executable**, record package and executable SHA256,
run receiver/guard tests, complete real systemd/pacman/reboot and iPad checks, and supply all Corresponding Source
before publishing. Documentation-only changes after qualification must not relabel binary identity.
