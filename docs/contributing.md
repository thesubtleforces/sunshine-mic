# Contributing
Sunshine Mic is an independent community preview fork focused on iPad microphone forwarding,
protected setup, packaging, and documentation. Qualification and publication are pending; do not
treat a successful build as a qualified release or an available AUR/Omarchy package.

Please use [this fork's issues](https://github.com/thesubtleforces/sunshine-mic/issues) for friendly
troubleshooting, bug reports, and feature proposals, and target pull requests at `mic`.
Small, focused changes with reproduction steps and evidence are welcome. Explain what you tested
and what remains untested; no response or review SLA is guaranteed.
See [Support](../.github/SUPPORT.md) and report suspected vulnerabilities through
[private reporting](https://github.com/thesubtleforces/sunshine-mic/security/advisories/new),
never a public issue. Do not include secrets, private addresses, recordings, or pairing material.

Do not create issues or pull requests in the LizardByte organization for this fork.
An explicitly approved package submission to `omacom/omarchy-pkgs` is a separate permitted track,
not a promise of acceptance. Preserve GPL licensing, LICENSE, NOTICE, and upstream author attribution.
The [upstream contribution guide](https://docs.lizardbyte.dev/latest/developers/contributing.html)
is a technical reference, not this fork's support route or automation contract.

## Change and release boundaries

Follow [AGENTS.md](../AGENTS.md), existing code patterns, `.clang-format`, and Doxygen conventions.
Add or update tests for changed behavior. Keep CMake build directories under `cmake-build-*`.
Do not add dictation engines, speech models, or video tuning to the microphone preview.
Use the [canonical installation guide](../packaging/omarchy/README.md) rather than duplicating
setup instructions.

The fork workflow graph consists of `ci.yml` (pushes/PRs for `mic`), `ci-archlinux.yml`
(reusable Arch candidate build), and `ci-release.yml` (gated maintainer-only publication dispatch).
PRs do not publish releases. The removed upstream publishing, localization, and Codecov integrations
are not part of this fork's CI contract.

Management, firewall, package, and reboot checks require disposable isolation; never run the root
helper against the production filesystem or install/restart a production service to test a candidate.
Real graphical-host and iPad/VoidLink qualification must exercise the exact candidate package bytes.
Report missing qualification honestly; tags, releases, AUR publication, and the Omarchy package PR
remain held until that qualification is complete. Never replace already published artifact bytes.

## Recommended Tools

| Tool                                                                                                                                                                           | Description                                                             |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------|
| <a href="https://www.jetbrains.com/clion/"><img src="https://resources.jetbrains.com/storage/products/company/brand/logos/CLion_icon.svg" width="30" height="30"></a><br>CLion | Recommended IDE for C and C++ development. Free for non-commercial use. |

## Project Patterns

### Web UI
* The Web UI uses [Vite](https://vitejs.dev) as its build system.
* The HTML pages used by the Web UI are found in `./src_assets/common/assets/web`.
* [EJS](https://www.npmjs.com/package/vite-plugin-ejs) is used as a templating system for the pages
  (check `template_header.html` and `template_header_main.html`).
* The Style System is provided by [Bootstrap](https://getbootstrap.com).
* Icons are provided by [Lucide](https://lucide.dev) and [Simple Icons](https://simpleicons.org).
* The JS framework used by the more interactive pages is [Vue.js](https://vuejs.org).

#### Building

@tabs{
  @tab{CMake | ```bash
    cmake -B cmake-build-web -G Ninja -S .
    cmake --build cmake-build-web --target web-ui
    ```}
  @tab{Manual | ```bash
    npm run dev
    ```}
}

### Localization
The inherited project supports localization; the default language is `en` (English).
For fork changes, update only English source strings, not `en-US` or other language variants.
This fork does not run the inherited Crowdin synchronization workflow, so merging a change does
not automatically translate it or open localization pull requests.

Brand names such as *Sunshine Mic*, *Sunshine*, *LizardByte*, *AMD*, *Intel*, and *NVIDIA*
should not be translated.

#### Extraction

##### Web UI
Sunshine uses [Vue I18n](https://vue-i18n.intlify.dev) for localizing the UI.
The following is a simple example of how to use it.

* Add the string to the `./src_assets/common/assets/web/public/assets/locale/en.json` file, in English.
  ```json
  {
   "index": {
     "welcome": "Hello, Sunshine!"
   }
  }
  ```

  > [!NOTE]
  > The JSON keys should be sorted alphabetically. You can use [jsonabc](https://novicelab.org/jsonabc)
  > to sort the keys.

  > [!IMPORTANT]
  > Only add source strings to *en.json*. Do not modify other language files in a feature PR.
  > Translation updates require a separately reviewed change; no automatic Crowdin PR is promised.

* Use the string in the Vue component.
  ```html
  <template>
    <div>
      <p>{{ $t('index.welcome') }}</p>
    </div>
  </template>
  ```

  > [!TIP]
  > More formatting examples can be found in the
  > [Vue I18n guide](https://kazupon.github.io/vue-i18n/guide/formatting.html).

##### C++

There should be minimal cases where strings need to be extracted from C++ source code; however it may be necessary in
some situations. For example the system tray icon could be localized as it is user interfacing.

* Wrap the string to be extracted in a function as shown.
  ```cpp
  #include <boost/locale.hpp>
  #include <string>

  std::string msg = boost::locale::translate("Hello world!");
  ```

> [!TIP]
> More examples can be found in the documentation for
> [boost locale](https://www.boost.org/doc/libs/1_70_0/libs/locale/doc/html/messages_formatting.html).

> [!WARNING]
> The below is for information only. Contributors should never include manually updated template files, or
> manually compiled language files in Pull Requests.

Local extraction can generate the `locale/sunshine.po` template for inspection.
There is no `localize.yml` workflow in this fork; extracted templates and compiled language files
are not automatically updated on pushes.

When testing locally, it may be desirable to manually extract, initialize, update, and compile strings. Python and
uv are required for this, along with the Python dependencies in the Sunshine `pyproject.toml`. From the repository
root, install these with the following command.

```bash
uv sync --locked
```

Additionally, [xgettext](https://www.gnu.org/software/gettext) must be installed.

* Extract, initialize, and update
  ```bash
  uv run --locked --no-sync lb-localize --root-dir . --extract --init --update
  ```

* Compile
  ```bash
  uv run --locked --no-sync lb-localize --root-dir . --compile
  ```

> [!IMPORTANT]
> Keep extracted templates and compiled language files out of feature pull requests.
> Local generation is for inspection and testing, not evidence of a translation service integration.

### Testing

#### Clang Format
Follow the repository's `.clang-format` settings when changing C/C++ source.

From the repository root, apply clang-format locally with the installed lizardbyte-common script. This will modify
files in place.

```bash
uv sync --locked
uv run --locked --no-sync lb-update-clang-format
```

#### Unit Testing
Sunshine uses [Google Test](https://github.com/google/googletest) for unit testing. Google Test is included in the
repo as a submodule. The test sources are located in the `./tests` directory.

The tests need to be compiled into an executable, and then run. The tests are built using the normal build process, but
can be disabled by setting the `BUILD_TESTS` CMake option to `OFF`.

To run the tests, execute the following command.

```bash
./cmake-build-debug/tests/test_sunshine
```

To see all available options, run the tests with the `--help` flag.

```bash
./cmake-build-debug/tests/test_sunshine --help
```

> [!TIP]
> See the googletest [FAQ](https://google.github.io/googletest/faq.html) for more information on how to use Google Test.

The build supports [gcovr](https://www.gcovr.com) coverage reports; this fork does not use the
removed Codecov integration as a PR gate. Add tests for changed behavior and report coverage gaps,
especially GPU-dependent cases that cannot run on ordinary CI runners. Do not suppress coverage
merely to make a report look healthy. See the
[gcovr documentation](https://gcovr.com/en/stable/guide/exclusion-markers.html) for justified exclusions.

Even if your changes cannot be covered in the CI, we still encourage you to write the tests for them. This will allow
maintainers to run the tests locally.

The real-binary receiver and management suites live in `tests/e2e/mic_e2e.py` and
`tests/e2e/mic_guard_e2e.py`. Run them only with their required disposable isolation and candidate
binary inputs. A namespace test is not proof of package lifecycle, graphical login, reboot
persistence, or a working real iPad session.

<div class="section_buttons">

| Previous                |                                                         Next |
|:------------------------|-------------------------------------------------------------:|
| [Building](building.md) | [Source Code](../third-party/doxyconfig/docs/source_code.md) |

</div>

<details style="display: none;">
  <summary></summary>
  [TOC]
</details>
