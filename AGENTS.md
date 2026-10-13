Sunshine Mic is an independent community preview fork. Qualification and publication are pending;
do not claim a build, release, AUR entry, or Omarchy package is qualified or published without evidence.
Keep work focused on microphone forwarding, protected setup, packaging, and documentation.
Support and security routes are in `.github/SUPPORT.md` and `.github/SECURITY.md`.
Never put secrets or unredacted private diagnostics into public issues.

On Windows we use msys2 and ucrt64 to compile.
You need to prefix commands with `C:\msys64\msys2_shell.cmd -defterm -here -no-start -ucrt64 -c`.

Prefix build directories with `cmake-build-`.

The test executable is named `test_sunshine` and will be located inside the `tests` directory within
the build directory.

The project uses gtest as a test framework.

When adding localization do not update any language other than `en`. This also means to exclude en-US or other variants.

Always add or update doxygen documentation.

The project requires that everything be documented in doxygen or the build will fail.

Primary doxygen comments should be done like so:

```cpp
  /**
   * @brief Describe the function, structure, etc.
   *
   * @param my_param Describe the parameter.
   * @return Describe the return.
   */
```

Inline doxygen comments should use `///< ...` instead of `/**< ... */`.

Always follow the style guidelines defined in .clang-format for c/c++ code.

Use `thesubtleforces/sunshine-mic` for fork support and contribution issues/pull requests when requested.
Never create an issue or pull request in the LizardByte GitHub organization.
An explicitly approved package submission to `omacom/omarchy-pkgs` is permitted; do not treat
that exception as permission to post elsewhere or as evidence of package acceptance.

Add or update tests for new or modified methods and code. Target 100% coverage on changed code.

The fork retains only `ci.yml`, `ci-archlinux.yml`, and `ci-release.yml` as its workflow graph.
Changes target `mic`; release publication is a gated maintainer dispatch, not a pull-request side effect.
Do not promise inherited Crowdin, Codecov, upstream publishing, or other removed automation.
Preserve LICENSE, NOTICE, upstream attribution, and existing technical identities used for replacement compatibility.
Use disposable isolation for management/firewall/package tests; never install, restart, or reconfigure
the production host merely to test a candidate. Builds and CI alone do not prove graphical-host,
reboot, or real-iPad qualification.
