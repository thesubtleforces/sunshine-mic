# Maintaining a Sunshine Mic community preview

## Current hold and scope

**No tag, release, AUR publication or Omarchy package PR is authorized before disposable graphical-host and real iPad qualification.** This procedure records future gated operations, not completed evidence. Do not install candidates, restart services, alter firewall/configuration or replace the foreign guard on the production development host. An unavailable disposable graphical target or real iPad is a release blocker.

The newcomer instructions live only in [README.md](README.md). Preview scope is Arch/Omarchy x86_64, PipeWire, Tailscale and microphone-capable VoidLink on iPad. Stock Moonlight cannot transmit microphone audio. Keep the fork, GPL/NOTICE/authorship and Sunshine executable/config/service identities; do not add dictation or video-tuning scope or file issues/PRs with LizardByte.

## 1. Review and freeze source identity

1. Review upstream Sunshine security changes and relevant dependency advisories. Record incorporated fixes and unresolved risks; a build passing is not a security review.
2. Keep this prepared version frozen: `pkgver=2026.1013.180134`, `pkgrel=1`, `_basever=2026.914.233613`, planned tag `mic-v2026.1013.180134`, title **Sunshine Mic — Community preview 1**. In disposable Arch, require `vercmp 2026.1013.180134-1 2026.914.233613-3` to return a positive number. Do not create the tag yet.
3. Finish source, packaging, source inventory, workflows, docs and changelog before the final candidate commit. Record its full 40-character SHA. The public recipe defaults to the exact versioned tag; pre-tag CI passes `_commit=<full SHA>`. Never substitute a moving branch, local source URL or latest dependency asset.
4. Preserve pinned recursive gitlinks and package-lock integrity. Recheck public anonymous retrieval and regenerate public-default `.SRCINFO` with `makepkg --printsrcinfo` from `packaging/omarchy` without an inherited `_commit` override.
5. Future code releases get a newly frozen UTC `YYYY.MDD.HHMMSS`; packaging-only revisions increment `pkgrel`. Update all coupled workflow/package identity checks deliberately. The current assembler explicitly requires `pkgrel=1`, so a future packaging revision also needs reviewed tooling support; merely changing the recipe is insufficient. Never reuse published filenames or move a published tag.

## 2. Complete the static-source audit before accepting CI

Audit the **actual candidate link command**, not a guessed dependency list. `release-artifacts.py` consumes `CMakeFiles/sunshine.dir/link.txt`, the build directory, the pinned FFmpeg asset, recursive source tree and `dependency-sources.json`. All static archives must map to exact redistributable source and notices: FFmpeg, codecs, recursively linked dependencies and compiler runtimes. In particular `-static-libgcc` requires the candidate's real `libgcc.a` and `libgcc_eh.a`, matching source records and recorded compiler version; record other linked runtimes when present. An exception in a runtime license is not proof that the inventory is complete.

Require exact public source URLs/revisions/digests, applicable notices and build scripts/patches. Verify the FFmpeg `v2026.910.121303` asset SHA256 `496d2bbb674d01e6033e31b9dfc15cbc9dc1494e882a4505f6ab1e03f75b385c`. Missing or unresolvable sources, unknown static inputs, checksum mismatch or missing notices block binary publication. Correct the inventory in source and produce a new final candidate; never patch provenance after qualification.

The build-side assembler runs unprivileged without tokens, SSH agents or publishing credentials. The authoritative invocation is in `../../.github/workflows/ci-archlinux.yml`; its CLI is:

```sh
python -I "$SOURCE/packaging/omarchy/release-artifacts.py" \
  --package "$PACKAGE" --checkout "$SOURCE" \
  --dependency-inventory "$SOURCE/packaging/omarchy/dependency-sources.json" \
  --ffmpeg-asset "$FFMPEG_ASSET" --link-command "$BUILD/CMakeFiles/sunshine.dir/link.txt" \
  --build-dir "$BUILD" --runtime-archive-dir "$RUNTIME_ARCHIVES" \
  --commit "$SOURCE_COMMIT" --version 2026.1013.180134 \
  --tag mic-v2026.1013.180134 --ci-run "$CI_RUN" \
  --toolchain "c++=$CXX_VERSION" --toolchain "cmake=$CMAKE_VERSION" \
  --toolchain "makepkg=$MAKEPKG_VERSION" --output "$NEW_OUTPUT"
```

Set these variables from the same candidate build, not the maintainer workstation: clean exact source checkout, original package, build tree, verified asset, captured runtime archive bytes and captured version strings. `CI_RUN` is the exact public fork Actions run URL. `NEW_OUTPUT` must not exist and its parent must exist. The workflow supplies these values automatically; this command documents the interface, not a way to replace the workflow candidate with a local rebuild.

Optional `--source-cache` entries are rehashed; `--scan-allowlist` permits only exact reviewed path/hash/reason exceptions. Repeated `--deny-string` can reject private identifiers. Inspect scan findings rather than broadly allowlisting them. `--check-inputs` produces **inputs-verified-not-source-complete**, performs no complete source assembly and is never a release gate substitute. GitHub's automatic source ZIP alone omits required recursive/dependency sources. No offline, hermetic or reproducible-build claim is made.

## 3. Build the final exact candidate once

Push the final reviewed commit to `mic` through the repository's required-check policy. `ci.yml` (**Fork CI**) calls `ci-archlinux.yml` at the same SHA. A PR/merge-SHA run, manual local build or failed push run cannot be the publishing candidate.

The disposable privileged Arch build provisions isolation, an unprivileged builder and Xvfb. It uses the public recipe with `_commit="$SOURCE_COMMIT" _run_unit_tests=true _support_headless_testing=true` and `makepkg --syncdeps --noconfirm --log`. Require real AppStream/desktop validation, GoogleTest XML, coverage, binary version/full SHA, namcap review, all 23 receiver scenarios on the extracted package and the source/packaged guard namespace/firewall tests. Isolation failure is a failure, not a skipped pass. Resolve namcap errors and explicitly assess warnings. Some inherited tests contact httpbin; do not claim hermetic execution or NVIDIA runtime qualification from CUDA compilation.

Full source assembly must succeed before the workflow uploads **`sunshine-mic-candidate-<full SHA>`** containing exactly:

- `sunshine-mic-2026.1013.180134-1-x86_64.pkg.tar.zst`
- `sunshine-mic-2026.1013.180134-corresponding-source.tar.zst`
- `sunshine-mic-2026.1013.180134-downstream-source.tar.gz`
- `provenance.json`
- `SHA256SUMS`

**`sunshine-mic-diagnostics-<SHA>-<attempt>` is not a candidate**, even if it contains version/hash records or passing partial test logs. A failed source-assembly run does not qualify. Candidate retention is 30 days: qualification and publication need an unexpired artifact. Rebuilding after expiry creates a new candidate identity/hash and requires fresh qualification.

In a clean maintainer/download workspace, set the observed run ID and full commit SHA, then download only that run's exact candidate:

```sh
REPO=thesubtleforces/sunshine-mic
read -r -p 'Successful push-to-mic CI run ID: ' RUN_ID
read -r -p 'Full candidate source SHA: ' SOURCE_COMMIT
gh run view "$RUN_ID" --repo "$REPO" --json event,headBranch,headSha,status,conclusion,url
gh run download "$RUN_ID" --repo "$REPO" \
  --name "sunshine-mic-candidate-$SOURCE_COMMIT" --dir candidate
(cd candidate && sha256sum -c SHA256SUMS)
```

Require completed/success, event `push`, branch `mic`, exact SHA and exact reusable-workflow SHA. Check `provenance.json` binds the run URL, package, executable hash, recursive gitlinks, asset/static-source inventory and toolchains. Preserve these downloaded bytes unchanged through qualification and publication.

## 4. Qualify those bytes on disposable infrastructure

Use a disposable Arch/Omarchy graphical VM/host with real system and user systemd, sudoers, pacman, PipeWire and the actual iPad. Keep exact package/executable hashes and the source/run identity in the evidence record. Install the literal package filename, never an ambiguous glob. Follow the canonical guide as a newcomer, with a local keyboard or independent SSH connection before stopping Sunshine.

Record actual observations for every surface:

- **Receiver/guard/firewall:** retain complete CI logs for the extracted binary and all receiver/isolated guard scenarios. Supplement these with real VM observations for partial activation recovery, launch/reconfigure races, reset, disabled future launch and redacted diagnostics: injected service failures do not qualify systemd. Private packet topology must prove selected IPv4 on `tailscale0` succeeds, wrong peer/interface and IPv6 fail, alternate-address TCP exclusions work, non-default base works, unrelated ports/tables survive, repeated apply is idempotent and invalid reload preserves protection. Matching generated text is not packet-delivery proof.
- **Package lifecycle:** fresh installation leaves microphone options disabled, no root profile/user activation and no automatic service restarts. Exercise setup, reconfigure, disable, reset, foreign-asset refusal, partial activation failure, guard-failure startup denial and upgrade without restarting. Review actual capability/udev/module hooks. Reset before exact-stock rollback must preserve ordinary accounts/pairing/video configuration and remove only owned assets. Save the previous literal package/signature privately.
- **Reboot:** actually reboot. Verify root guard boot enablement, user-service graphical-session enablement, real nft kernel protection and protected startup. A oneshot active state or namespace/systemd-analyze check is not boot evidence. Do not enable root Sunshine or user lingering.
- **Real iPad:** record host/Omarchy/Arch, iPadOS, VoidLink, encoder, package and source versions. Grant microphone permission and enable the client option before connecting via manually entered Tailscale host. Observe live **Moonlight Mic** input, unchanged default-source policy, disconnect cleanup and reconnect recovery. Repeat after reboot. Record useful video during the session; do not extend compatibility claims to untested hardware.
- **Product surface:** open the actual fork README, package guide, issue forms, private security route and branding on desktop/mobile. Observe modified desktop/web naming on the disposable host. Verify support links without implying upstream endorsement.
- **Demo:** capture a real opt-in qualification session showing the iPad microphone option and responding host input. Obtain consent, redact private addresses/tailnet/account information and publish the reviewed demonstration at a public HTTPS URL. No simulated UI, generated product proof, invented performance figures or placeholder demo. Without a real demo, publication stays blocked.

No qualification operation may mutate the production host. If code or package bytes change, start again with the final CI candidate; do not reuse historical evidence for new bytes.

## 5. Review protection and prepare the qualification report

Before creating any tag, verify active GitHub controls (do not merely document intended settings):

- `mic` disallows force-push/deletion and requires the actual **Arch candidate** check.
- An active tag ruleset covers `mic-v*` creation, update and deletion. Only individually verified maintainers or repository Admin may bypass. Review Settings and record actual bypass identities/control scope; the workflow token may not see them.
- `community-preview` requires maintainer review/approval, disallows admin bypass and permits deployment only from tags matching `mic-v*`, not branches. Record the real review, including settings the API omits. If the account/plan cannot enforce the protections, publication is blocked; do not weaken the workflow.
- Public metadata/support/private security reporting and links are verified. Fork status remains; ordinary GitHub search excludes forks by default, so discovery checks use `fork:true` without promising indexing.

Write a real `qualification.json` outside tracked source. The dispatch takes this JSON as data, not a report URL. Exactly these top-level keys are required by `ci-release.yml`:

| Key | Required content |
| --- | --- |
| `redacted` | Boolean `true`, only after reviewing the complete report. |
| `source_commit` | Full candidate/tag SHA. |
| `package_sha256` | Exact qualified package SHA256. |
| `executable_sha256` | Extracted `usr/bin/sunshine` SHA256 matching provenance. |
| `candidate_run_id` | Decimal run ID as a string. |
| `versions` | Nonempty observed strings `host`, `ipad_os`, `voidlink`, `encoder`. |
| `evidence` | Actual observations/evidence references for `receiver`, `guard`, `firewall`, `package_lifecycle`, `reboot`, `ipad_voidlink`, `video`, `repository_controls`; each string must contain at least 20 characters, not an unexecuted checklist. |
| `demo` | Exactly `url`, `verified`, `consented`, `redacted`; the three flags are boolean `true` based on actual review. |

The demo URL must be public HTTPS without credentials, query or fragment. Report size must be 100–60000 bytes. Never include home paths, private IPs, tailnet names, credentials, pairing material or secrets. Machine schema/hash checks do not establish truth: the approving maintainer must assess the underlying observations.

## 6. Tag and dispatch only after all holds are lifted

This section is **not permission to publish now**. After complete qualification and controls review, use the candidate SHA (never whichever commit happens to be HEAD) and record the package hash from the verified report:

```sh
TAG=mic-v2026.1013.180134
PACKAGE=sunshine-mic-2026.1013.180134-1-x86_64.pkg.tar.zst
PACKAGE_SHA256=$(sha256sum "candidate/$PACKAGE" | cut -d ' ' -f 1)
git tag -a "$TAG" "$SOURCE_COMMIT" -m 'Sunshine Mic — Community preview 1'
git push origin "refs/tags/$TAG"
gh workflow run ci-release.yml --repo "$REPO" --ref "$TAG" \
  -f "tag=$TAG" -f "candidate_run_id=$RUN_ID" \
  -f "package_sha256=$PACKAGE_SHA256" -F qualification_report=@qualification.json
```

Verify `origin` is this fork before pushing. Never force/update an existing tag. The dispatch **ref and input tag must be identical**, resolve to the exact successful push-to-mic SHA and match the recipe version. An authorized maintain/admin actor must dispatch and approve the `community-preview` deployment; rerun actors must also be maintainers. Inspect the run waiting for approval and approve only after reviewing this exact report and artifact hash.

The publisher downloads only the trusted run artifact, validates the five-file identity/provenance/checksums, binds qualification to package/executable hashes and rechecks the tag. It never rebuilds or executes downloaded packages, hooks or repository scripts. It attests package/source/provenance/qualification, retains original `SHA256SUMS`, adds `qualification.json` and `PUBLICATION-SHA256SUMS`, and creates a prerelease with `--verify-tag --prerelease --latest=false`. An existing release, including a draft, is refused; there is no clobber path.

**The reusable launch announcement is generated by the publisher as release notes**, using the real verified demo URL and installation/support links. Review those notes after publication; do not maintain fake demo placeholder text or claim AUR/official availability in the announcement. No unsolicited external community posts or implied endorsement.

## 7. Verify public artifacts before distribution

Use a new directory, separate from the qualified download, after the release actually exists:

```sh
gh release view "$TAG" --repo "$REPO" --json url,isPrerelease,assets
gh release download "$TAG" --repo "$REPO" --dir public-release
(cd public-release && sha256sum -c SHA256SUMS && sha256sum -c PUBLICATION-SHA256SUMS)
gh attestation verify "public-release/$PACKAGE" --repo "$REPO"
gh attestation verify "public-release/sunshine-mic-2026.1013.180134-corresponding-source.tar.zst" --repo "$REPO"
```

Check the actual public release is a prerelease and not latest/stable. Compare package and extracted executable hashes to qualification, and source/provenance hashes to the original candidate. Inspect the Corresponding Source contents/notices and exported recipe for completeness, exact full commit, public URLs and absence of private deployment files. Download without private repository access as a public-access check. Attestations identify the publication workflow, not independent proof of successful manual tests. Record the real release/source/provenance and qualification URLs only after observing them.

## 8. AUR binary package — only after the immutable release exists

Do not push a speculative AUR recipe. Recheck `sunshine-mic-bin` availability/history and authenticate the user's authorized AUR account over verified SSH host identity. Never expose private keys, reuse unrelated keys or generate a publishing key without authorization. Missing authorization or an occupied name is an external gate, not a successful submission.

Prepare `packaging/aur/sunshine-mic-bin/{PKGBUILD,.SRCINFO,sunshine.install}` only against the actual immutable public release. Require:

- Literal versioned package URL and independently verified literal SHA256, x86_64, matching version, explicit runtime/optional dependencies.
- `provides=('sunshine=2026.914.233613' 'sunshine-mic=2026.1013.180134')`, conflicts with `sunshine` and `sunshine-mic`, no `replaces`.
- Repackage ordinary payload files preserving modes and symlinks, but exclude upstream `.PKGINFO`, `.BUILDINFO`, `.MTREE`, `.INSTALL`; let makepkg generate its own metadata. Derive `sunshine.install` from the canonical `packaging/omarchy/sunshine.install` at the qualified source commit, preserving license/provenance; do not fork hook behavior.
- The community-preview description and accurate compatibility/security scope, not stable or official endorsement.

In a disposable Arch directory containing that recipe, run `makepkg --printsrcinfo > .SRCINFO`, then `makepkg --syncdeps` and install the resulting exact package interactively. Extract/hash its `usr/bin/sunshine` and require equality with the qualified GitHub executable; exercise protected setup and lifecycle. Review the recipe and conflict prompt. AUR git contains only recipe/helper metadata, never binary archives. Push deliberately with public commit identity to `ssh://aur@aur.archlinux.org/sunshine-mic-bin.git` on `master` only after these gates.

Verify AUR RPC, `yay -Ssa sunshine-mic`, `yay -Siia sunshine-mic-bin` and a clean Omarchy **Install → AUR** picker actually find it before advertising `yay -S aur/sunshine-mic-bin`. Publish the observed AUR URL and update the canonical guide's status. No invented `omarchy pkg search`; first migration should be interactive rather than the wrapper's `--noconfirm` route.

## 9. Separate edge-only Omarchy source request

Follow [omarchy-pkgs contribution instructions](https://github.com/omacom/omarchy-pkgs#contribute-a-package) and [upstream watches](https://github.com/omacom/omarchy-pkgs/blob/master/docs/upstream-sources.md) in a separate fork/workspace branched from `master`. Use the verified downstream source archive, not the AUR binary recipe: literal full release `_commit`, public inputs and makepkg-generated source checksums. Add only `pkgbuilds/sunshine-mic/PKGBUILD`, necessary hook/helper files and package-local `.omarchy/package.json`:

```json
{
  "source": "local",
  "channels": ["edge"],
  "upstream": {
    "watch": {
      "github": "thesubtleforces/sunshine-mic",
      "pattern": "mic-v(?P<version>[0-9]+(?:\\.[0-9]+)*)",
      "allow_prerelease": true,
      "variables": {"_commit": "{commit}"}
    }
  }
}
```

Validate watch discovery against the **actual published prerelease**, using that repository's current documented watch tooling. Run its real `bin/build --package sunshine-mic` in disposable build infrastructure; `--dry-run` is not validation. Do not modify its tooling/workflows. Submit one package PR to `omacom/omarchy-pkgs:master` documenting edge-only scope, security boundaries, source/license/provenance, replacement behavior and exact observed tests.

Record the real PR URL and statuses: new-contributor `build-approved`/workflow approval, required `result`, `self-tests`, `build-isolation`, review, merge and signed package/database publication. Address actionable review feedback. AUR import is a one-time copy, not synchronization. Submission is not acceptance; acceptance is not publication; edge is not stable. Never switch the production host's channel. Advertise **Install → Package** / `omarchy pkg add sunshine-mic` only after observing signed availability in the user's channel. Until then link the actual submission as pending, not as an installation source.

## 10. Ongoing support and withdrawal

Update the local changelog and canonical guide only with observed release/distribution state. Publish a pinned fork welcome/support issue with real installation links, verified client requirements, limitations and contribution route. Verify the private vulnerability-report form before directing sensitive reports there; do not invite secrets into public Issues. Community preview support has no guaranteed response SLA.

For each update repeat security review, frozen version, complete source audit, final CI candidate, exact-byte qualification, protected publication, public verification, AUR recipe/checksum/`.SRCINFO` update and Omarchy watch/PR review. Choose service restarts explicitly; package upgrades do not automatically restart sessions.

If a preview is known bad, mark it **withdrawn** prominently in its release notes and support/install surfaces, describe the risk and safe rollback, and stop recommending it. Preserve existing asset bytes and tags for auditability. Ship the fix under a new version with fresh evidence; never silently replace or clobber assets, retag, or weaken failing gates. Keep implemented, publicly visible and externally blocked work separate in the release record.
