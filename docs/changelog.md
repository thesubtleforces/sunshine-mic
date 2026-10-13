# Sunshine Mic changelog

This is the local history of the independent Sunshine Mic fork, not the upstream Sunshine release feed.

## Unreleased — community preview preparation

**Publication is on hold.** The prepared version is `2026.1013.180134-1`, based on
Sunshine `2026.914.233613`. A version in a recipe is not a published or qualified release.
Tags, GitHub releases, AUR publication and the official Omarchy package request remain held
until the exact candidate passes disposable graphical-host and real iPad qualification.

- Provide iPad microphone forwarding through microphone-capable VoidLink to the **Moonlight Mic**
  PipeWire input. Stock Moonlight does not send microphone audio; no transcription engine is included.
- Keep microphone forwarding and default-input changes disabled by default. Add explicit desktop-user
  setup, status/doctor, reconfiguration, guard reload, disable and reset commands.
- Add a dedicated nftables guard and fail-closed managed launch for one desktop user and one exact
  Tailscale IPv4 peer. Setup does not adopt foreign production guards or automatically stop a stream.
- Prepare an x86_64 Arch/Omarchy source recipe with public pinned inputs, explicit CUDA build dependency,
  packaged management tools, preserved Sunshine replacement identities and no installation-time opt-in.
- Add candidate-only Arch automation, isolated receiver/guard coverage, Corresponding Source assembly,
  provenance/checksums and a separately approved, immutable preview publication workflow.
- Verify CI checkout identity as its unprivileged builder owner after the ownership handoff, avoiding
  container-root access to a runner-owned bind mount without weakening Git's ownership checks.
- Provision narrow Arch UID/GID mapper capabilities and permit nested namespaces on the disposable
  hosted CI runner. Real namespace/mount/packet verification remains mandatory; never apply these
  CI-only privilege settings to a production machine or fall back to an unisolated run.
- Identify AppStream metadata as a development build with its actual version/build date, retaining both
  metadata validators without implying a qualified release or package-manager publication.
- Supply staged application assets to uninstalled binary version checks and isolate the recipe's
  configuration home. Declare the observed direct runtime libraries and disable unused debug packaging.
- Remove inherited Codecov bundle analysis and its dependency; diagnostic reports containing absolute
  build paths do not belong in the installed web assets.
- Resolve the disposable builder's diagnostic directory through passwd rather than embedding a home
  path in exported workflow source; preserve the strict source privacy scan without a new exception.
- Pin the exact GCC source archive on its public GitHub mirror after the Sourceware archive endpoint
  returned 404. Verify all source contents and normalized modes against the earlier audited export;
  retain the same compiler revision, runtime hashes and source/privacy gates.
- Introduce fork-specific product/support surfaces and protected installation, security and rollback guidance.

These entries describe source changes, **not successful release qualification**. Diagnostic artifacts,
historical development-package results and compilation alone do not prove package lifecycle, reboot,
iPad audio, video or GPU compatibility. The candidate's static dependency source audit and all release
gates must succeed before publication. No AUR or official Omarchy availability is claimed.

See the [canonical installation guide](../packaging/omarchy/README.md) and
[maintainer release procedure](../packaging/omarchy/RELEASING.md).

## Upstream reference (not this fork's changelog)

For inherited Sunshine history, consult the
[upstream Sunshine changelog](https://github.com/LizardByte/Sunshine/blob/changelog/CHANGELOG.md).
Its releases, compatibility claims and support routes do not qualify Sunshine Mic.
Upstream authorship, GPL licensing and existing NOTICE/history are retained.
