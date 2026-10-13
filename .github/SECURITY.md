# Security policy

Sunshine Mic is an independent Sunshine fork preparing a community preview. Qualification and publication are pending; no preview release is currently claimed as qualified. There is no stable-support or response-time guarantee. Report concerns about the fork's current `mic` branch or any future preview version, including the exact version/source SHA when known.

## Report privately

Use [GitHub private vulnerability reporting](https://github.com/thesubtleforces/sunshine-mic/security/advisories/new). Private vulnerability reporting is enabled for this repository. Do not open public issues or pull requests containing exploit details or sensitive diagnostics. If the form is unavailable, do not fall back to posting the vulnerability publicly; retry the private route later.

Describe the affected version, security impact, prerequisites, and minimal reproduction using synthetic addresses and data. Even in a private report, do not send passwords, access tokens, SSH keys, pairing certificates, private recordings, or complete personal configuration files. Share only the minimum redacted evidence needed. Coordinate disclosure with the maintainer; there is no guaranteed response SLA or bounty.

## Security boundaries

The protected setup is explicit opt-in, for one desktop user and one IPv4 Tailscale peer on an x86_64 host. Tailscale and the managed firewall are required protections, not proof that microphone packets themselves authenticate a physical device. Known alternate LAN addresses can be blocked, but a newly assigned LAN address is not automatically known. Keep the Sunshine web UI local or behind an authenticated private access path, never exposed to the public Internet.

The startup gate fails closed when it cannot apply protection. It is not continuous firewall tamper protection: administrator changes or firewall flushes during a session can defeat the guard until it is reapplied. Do not bypass a failed gate or remove firewall rules to troubleshoot microphone access.

See the [security model and troubleshooting](../docs/mic_streaming.md) and the canonical [protected installation guide](../packaging/omarchy/README.md). Ordinary redacted troubleshooting belongs in this fork's [Issues](https://github.com/thesubtleforces/sunshine-mic/issues), not in LizardByte's issue tracker. Preserve upstream attribution; this project does not speak for upstream Sunshine, VoidLink, Tailscale, or Omarchy.
