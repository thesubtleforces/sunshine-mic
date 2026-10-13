# Community preview qualification record

**NOT RELEASE QUALIFIED. Publication is on hold by maintainer choice.**

No package is release-qualified. There is no immutable preview tag, published release, verified AUR entry,
or official Omarchy package submission for this release. CI artifacts, if successfully assembled, are only
build candidates. Do not use this preparation record as the release workflow's qualification approval.
Earlier development packages are not the candidate; final run/hash and live qualification belong in a
separate exact-candidate report as described in [RELEASING.md](RELEASING.md).

## Observed during implementation

- Product templates rendered with real CMake `SUNSHINE_CONFIGURE_ONLY=ON` in an isolated staging directory.
  `desktop-file-validate` passed; `appstreamcli validate --no-net` passed with two pedantic notices.
- README rendered through GitHub's Markdown API and observed in Chromium at 390 px width without page
  overflow. Original 1280×640 social PNG was opened and visually inspected. This local rendering
  did not qualify an installed Sunshine web UI.
- No configured fal executable/client/environment-variable names were found by the artwork implementation;
  original SVG artwork was used. No product demonstration was generated or simulated.
- `python3 tests/e2e/mic_guard_e2e.py` passed in private user/network/mount/PID namespaces:
  profile type/range/peer validation; conservative reset scalars including parser ambiguity refusal;
  trusted profile modes and symlink/hardlink refusal; shared/exclusive lock exclusion and close-on-exec
  release; root-helper privilege and exact-command boundaries; actual UDP selected-peer/interface
  acceptance; wrong/spoofed/LAN/IPv6 denial; alternate-address TCP exclusions; non-default base;
  unrelated ports/tables preserved; idempotence; invalid atomic reload retaining protection;
  structural kernel inspection/drift refusal; disabled deny-all with alternate TCP exclusions removed.
  Injected activation failure also retained real deny rules/durable intent, blocked apply/conflicting
  recovery, permitted identical recovery and then disabled delivery. These transaction tests inject
  service, stopped-unit and peer boundaries; they are not real systemd qualification.
- Public CLI `--help` executed. Read-only `status --json` against the development host exited **3** and
  correctly reported protection unverified and legacy/unmanaged activation conflict. It did not apply
  the helper, alter the production firewall, stop/restart a service or write user activation.
- `systemd-analyze verify` on the uninstalled guard unit failed because its fixed
  `/usr/lib/sunshine-mic/guard` executable is not installed. This is **not** lifecycle qualification.
- The FFmpeg asset was downloaded anonymously from
  `https://github.com/LizardByte/build-deps/releases/download/v2026.910.121303/Linux-x86_64-ffmpeg.tar.gz`.
  Independent SHA256 matched the API-reported expected value:
  `496d2bbb674d01e6033e31b9dfc15cbc9dc1494e882a4505f6ab1e03f75b385c`.
- Frozen candidate version: `2026.1013.180134-1`, upstream base `2026.914.233613`.
  `vercmp 2026.1013.180134-1 2026.914.233613-3` returned **1**.
- Public recipe `.SRCINFO` was generated with `makepkg --printsrcinfo`; non-SHA `_commit` was rejected.
  Anonymous `makepkg --nobuild --nodeps --noconfirm` source retrieval/preparation completed with all
  recursive gitlinks at existing published commit `198b14f36351df0e4b84a0a005541e59454c6837`.
  This proves the declared public inputs can be prepared, not a final candidate build.
- Seventeen actual dependency source archives and notices were downloaded, hash-checked and assembled
  through the source-export scanner. This includes GCC commit `d564253eb6c859e266d3cae18e82fb4db9a88316`
  and immutable Arch GCC recipe/patch commit `13b07516f792b0b11ef818300f9d7a9341f31b5b`.
  Runtime hashes bind the observed `g++ (GCC) 16.2.1 20260810`; final CI must match the actual compiler
  and linked archives or fail closed. This is not a reproduced compiler build or a final static-link audit.
- `python3 -B -m unittest discover -s tests/e2e -p test_release_artifacts.py` passed 25 behavioral
  source/provenance rejection tests. `actionlint` passed the three fork workflows; embedded Python compiled.
  Publication validation was executed without credentials and refused wrong repository, invalid tag,
  branch dispatch and unprotected tag before any API request. No publication workflow was dispatched.
- After the workflow cutover commit, GitHub's default branch was changed to `mic`; fork status remained
  true. About/homepage/topics, Issues and private vulnerability reporting were enabled and verified
  through GitHub APIs. The [welcome/support issue](https://github.com/thesubtleforces/sunshine-mic/issues/1)
  was created and pinned.
- Actual public README and canonical guide were opened in Chromium at 390 px: no page-level horizontal
  overflow; the original banner loaded. The public security policy and pinned support issue were visible.
  Issue creation and private security submission routes correctly reached GitHub sign-in; authenticated
  form rendering is not claimed. Issue YAML parsed locally.
- GitHub repository search `sunshine omarchy microphone fork:true` returned this fork. This does not
  imply ordinary fork-excluding search inclusion or future ranking.
- Custom social-preview upload remains incomplete: no authenticated browser was available, and GitHub's
  supported repository-update GraphQL input exposes no social-image field/mutation. The original PNG
  is committed; `usesCustomOpenGraphImage` was observed false. Do not confuse the README banner with
  the repository social-preview setting.
- The three fork workflows are registered active. Active `mic` deletion/force-push restrictions and
  maintainer-only `mic-v*` tag rules were configured; `community-preview` requires maintainer approval,
  disallows admin bypass and allows only `mic-v*` tags. Required CI context is finalized after observing
  the actual first check; no release approval or publication was exercised.

Static reviews found and prompted corrections for incomplete activation recovery, user/root transaction races,
direct-process diagnostic blind spots, socket verification exits, reset parser differentials, reset recovery
following loss of runtime rules and effective configuration-directory overrides. Static fixes are not proof of
real systemd lifecycle behavior. The namespace harness covers only the boundaries listed above.

## Unfulfilled release gates

1. Anonymous fresh candidate build with explicit CUDA, actual validators, full GoogleTest XML/coverage,
   namcap review, package/executable identity and complete audited Corresponding Source.
2. All 23 receiver scenarios against the extracted **new candidate** executable, not an old package.
3. Real root/user systemd and sudoers: setup, incomplete transition recovery, startup/reconfigure races,
   disable future launch, foreign-asset refusal, upgrade without restart, reset and exact-stock rollback.
4. Actual reboot proving root guard boot enablement and graphical user-session Sunshine enablement.
5. Disposable graphical host and real iPad/VoidLink permission/connect/input-meter/disconnect/reconnect
   check; usable video; exact host/iPadOS/client/encoder versions; candidate package hash.
6. Consented redacted live demonstration. No screenshots/audio recordings of private sessions may be
   repurposed without consent. No invented latency or hardware compatibility claims.
7. Protected tag/environment enforcement, immutable prerelease publication, public checksum/attestation
   verification and source/provenance hash comparison.
8. Authorized AUR account/name availability and actual package discovery, recipe install and executable
   identity verification; separate real Omarchy build/watch/edge PR and maintainer approval gates.
9. Authenticated issue/security form rendering, custom social-preview upload and installed desktop/web
   surfaces. Public README/guide/security/support and fork-inclusive search have been observed above.

The maintainer chose **Keep publication blocked** when offered a disposable target/VM qualification route.
Continue implementation and isolated verification, but do not tag, publish binary/AUR packages or submit an
unqualified official package request. No production stream, guard, dictation, video or channel changes are
permitted to satisfy these gates.
