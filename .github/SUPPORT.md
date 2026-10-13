# Sunshine Mic support

Sunshine Mic is an independent Sunshine fork for iPad microphone forwarding to Omarchy/Arch. This is community preview preparation: disposable graphical-host and real iPad qualification are still required. No qualified preview release, AUR publication, or official Omarchy package availability is claimed here.

## Start here

- [Install on Omarchy / Arch](../packaging/omarchy/README.md): the canonical guide for protected setup, first-run pairing, updates, disable/reset, and rollback.
- [Microphone security and troubleshooting](../docs/mic_streaming.md).
- [Bug reports and troubleshooting](https://github.com/thesubtleforces/sunshine-mic/issues/new?template=bug-report.yml).
- [Feature requests](https://github.com/thesubtleforces/sunshine-mic/issues/new?template=feature-request.yml).
- [Private security reports](https://github.com/thesubtleforces/sunshine-mic/security/advisories/new); see the [security policy](SECURITY.md).
- [Contributing](../docs/contributing.md).

The preview targets an x86_64 Omarchy/Arch host with PipeWire and authenticated Tailscale, one desktop user, and one iPad Tailscale IPv4 peer using microphone-capable VoidLink. Stock Moonlight does not send microphone audio. Other clients, architectures, and hardware are unverified unless an explicit qualification record says otherwise. The host input is named **Moonlight Mic**; select it in the consuming app. This does not install a transcription engine or promise automatic default-input switching.

## Ask safely

Search existing fork issues, then use the bug/troubleshooting form even if you do not yet know the cause. Include package version/source SHA and package hash when available, installation path, Omarchy/Arch version, iPadOS/VoidLink versions, symptoms, and reproduction steps. Unknown values are okay if identified as unknown.

Run `sunshine-mic doctor` without `--show-addresses`, review its output, and redact any remaining private addresses, tailnet/account names, home paths, or secrets before posting. Do not upload audio, pairing material, credentials, full logs, or entire configuration files. Suspected vulnerabilities belong only in the private reporting route.

Do not stop a production stream, install a candidate, or flush firewall rules merely to gather a report. Setup/reconfigure/reset require a local keyboard or independent SSH session because stopping Sunshine disconnects the streamed desktop; follow the installation guide rather than improvising live changes.

## Community scope

Support is best effort, with **no guaranteed response SLA**, hardware compatibility promise, or commercial support commitment. Microphone forwarding, protected setup, packaging, and clear documentation are in scope. Dictation engines, speech models, desktop dictation controls, and video tuning are not part of this preview. Useful, respectful reports and small focused contributions are welcome.

Use this fork's support routes, not LizardByte's issues or pull requests. This community project is not endorsed or supported by upstream Sunshine, Omarchy, VoidLink, or Tailscale. An eventual AUR entry or Omarchy package request is not official acceptance or publication; consult the installation guide for actual availability.
