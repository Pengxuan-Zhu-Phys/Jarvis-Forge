# Jarvis-Forge

Jarvis-Forge is an independent Python package for user-space software environments and command-based acceptance workflows on scientific workstations and HPC systems. Its CLI is `jforge`.

Forge runs local command suites and preserves their evidence. Its `jforge apt` command downloads, verifies and builds recipe-defined software into private user prefixes. Named environments keep private, versioned shell copies and explicit target Python selections in one shared user directory. Managed services, Slurm submission, scientific comparison policies, and the `Jarvis forge` forwarding command are planned extensions.

## Install

Python 3.10 or newer and a POSIX system with Bash are required for execution. Use WSL on Windows. Forge does not depend on Jarvis-HEP.

```bash
python3 -m pip install Jarvis-Forge
jforge --version
jforge --help
```

The distribution name is `Jarvis-Forge`. For development from a checkout:

```bash
python3 -m pip install -e '.[dev]'
```

Build source and wheel distributions with:

```bash
python3 -m build
```

## Try the acceptance workflow

These commands create a private Forge user directory under the current directory and write a smoke-suite YAML. The demo's `init` also remembers that directory as your default. The demo uses Python only; it does not download software or start services.

```bash
jforge --home .forge-home init
jforge example --output smoke.yaml
jforge validate smoke.yaml
jforge plan smoke.yaml
jforge --home .forge-home run smoke.yaml
jforge --home .forge-home run smoke.yaml
jforge --home .forge-home history --suite forge-smoke
```

Copy the run IDs printed by `run` or listed by `history`:

```bash
jforge --home .forge-home show <run-id>
jforge --home .forge-home logs <run-id> --step calculate
jforge --home .forge-home compare <older-run-id> <newer-run-id>
```

Text results use standalone headings and record summaries suitable for SSH terminals. Paths and commands are emitted as complete lines; the terminal may visually wrap them without inserted line breaks. `apt update` highlights pending imports and their reasons. Full structured fields remain available with `--json`.

`--json`, `--home` and `--debug` work before or after the command. `--json` emits one JSON object on stdout, while progress goes to stderr. `compare --check` fails on changed, missing, corrupted or unsuccessful evidence; ordinary comparison only describes differences. Comparison uses exact artifact bytes; scientific equivalence and performance regression policies remain outside the current comparison mode.

## Automatic package environment (Bash and Zsh)

A child process cannot change its parent shell. Enable the shell integration once in each interactive session:

```bash
eval "$(command jforge apt shell-init)"
```

Add that line to your own `~/.bashrc` or `~/.zshrc` to enable it for future shells. The hook sources the selected package environment on startup and refreshes it after successful installs, upgrades, removals and source switches. It uses the selected Forge home, including an explicit `--home`. Forge does not edit your startup files. Existing shells need to enable the hook themselves. Source switching removes previous managed package paths and restores exported values while preserving user PATH additions. After upgrading Forge, reload this hook once in existing shells to obtain its new behavior; other already-open shells keep their current environment until refreshed. The hook is intended for interactive use, not JSON or script automation.

The first `apt list` after a repository refresh builds a local derived index. Subsequent listings reuse it; complete distribution listings are printed as compact lines. `apt list --installed` only lists installed packages.

## User directories and package queries

Forge uses `~/.jarvis-forge` until you choose a default with `init`. `jforge init --home /data/my-forge` initializes that directory and remembers it for later commands, including commands started in other terminals and Python environments. Home selection follows `--home`, `JFORGE_HOME`, the remembered default, then `~/.jarvis-forge`, in that order. Using `--home` with another command only overrides that invocation.

The remembered location is stored in `~/.config/jarvis-forge/default-home.json`, or under an absolute `XDG_CONFIG_HOME` when configured. `jforge paths` shows both the selected data directory and this preference file. Initialization does not move existing software or histories. `paths`, `doctor`, `list`, `info`, `validate`, `plan`, `history`, `show` and `logs` do not initialize a user directory or install software.

```bash
jforge init --home /data/my-forge
jforge env list
jforge apt list
```

Both queries above use `/data/my-forge` without repeating `--home`, provided no `JFORGE_HOME` override is set. Repeating `init --home` with another directory changes the remembered default. A missing remembered directory remains selected; Forge does not silently switch to a different store. To repair a corrupt preference, explicitly initialize the desired directory again.

```bash
jforge paths
jforge doctor
jforge doctor --shell /path/to/cbs-env.sh
jforge doctor --shell /path/to/cbs-env.sh --json
jforge list
jforge info eigen
jforge info eigen --package-version 3.4.0 --instance <install-id> --path
```

`info` queries JSON installation manifests under the selected package environment's `packages/<name>/<version>/<install-id>/manifest.json`. `jforge apt install` creates those records after installation checks pass. Multiple instances are listed explicitly; `--path` requires a unique match. `--package-version` selects installed software; `--version` remains Forge's version query.

You select and prepare Python environments with pyenv or your existing tools. `doctor --shell` sources your trusted Bash script and reports the resulting CC/CXX/FC selections, compiler and build-tool paths and versions, Python interpreter paths and versions, virtual environment prefixes, installed Python packages, and selected module/pyenv variables. It does not choose an environment or switch to a fallback. Without `--shell`, it inspects the inherited environment. Missing tools and failed version probes are reported individually; activation errors fail the command. `--timeout` limits the complete inspection, including activation. Shell output is kept separate from JSON results. Inspection creates no Forge user state.

## Named environments and target Python

All Forge installations for the same user share the remembered data directory, independently of the Python version running the CLI; the initial fallback is `~/.jarvis-forge`. `JFORGE_HOME` and `--home` can explicitly override it. Target Python environments do not need Forge installed.

```bash
jforge env add cbs --shell /path/to/cbs-env.sh
jforge env list
jforge env show cbs
jforge env show cbs --path
jforge env set-python cbs --python /path/to/existing/venv/bin/python
# Alternatively, select an existing pyenv environment:
jforge env set-python cbs --pyenv cbs-python
jforge doctor --env cbs
jforge run acceptance.yaml --env cbs
```

`env add` copies the script into `environments/<name>/revisions/<revision>/source.sh` and generates `activate.sh`. It checks Bash syntax without sourcing the script. Registration and Python configuration do not activate an environment in your current shell or set a default for later runs. Choose a profile explicitly with `--env`, `environment.name` in YAML, or `source "$(jforge env show cbs --path)"` in your own shell.

`set-python` changes the target Python for that named environment only. Use `--python` for an existing executable, or `--pyenv` for one existing pyenv version/environment; `--pyenv-root` locates non-default version storage. Forge records the actual pyenv program separately from its storage root, supporting package-manager installations. It does not install Python, change pyenv global/local settings, or switch the interpreter running Forge. `--inherit` removes the Python override and uses the script's PATH again. When overridden, both `python` and `python3` use the chosen interpreter; the configured environment's bin directory is also put on PATH.

Every `env add ... --replace` or `set-python` saves a new revision and preserves previous scripts. Use `env show cbs --revision <revision>` to inspect an older revision. `env list/show` do not execute scripts. Missing or modified managed files are reported and cannot be selected for execution. Runs archive the chosen revision, original script copy and generated wrappers. The reserved `apt` environment appears in the list after package activation and remains managed by `jforge apt`.

Scripts are copied, not relocated together with their dependencies: use stable absolute paths for external files. If the original script changes, register the update with `env add cbs --shell /path/to/cbs-env.sh --replace`. For reproducibility, files in published revisions are not intended for direct editing. Full usage and storage details are in [the environment guide](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/ENVIRONMENTS.md).

## User-local package manager

Dependency installation resolves the complete graph first, installs dependencies before their consumers, and verifies reused instances on every operation. A dependency must be ready before a consumer can build. Failures leave the previous active environment selected; completed dependencies can remain as inactive installations for later reuse.

Recipe schema 2 adds `build_depends`, `run_depends`, `probe`, `dependency_check` and `artifacts`. Existing schema 1 `depends` recipes remain supported. A `dependency_check` runs after source preparation and before the consumer build in the same selected dependency environment. `probe` is a lightweight post-install and reuse check; legacy recipes fall back to `check`. Declared artifact and environment-script hashes detect changes to the recorded files.

Official dependency discovery uses both Sources and native-architecture Packages metadata, preserves Debian version comparisons and alternatives, and maps binary aliases to source providers. It currently targets native Linux amd64, arm64, ppc64el, s390x and riscv64. Ambiguous virtual providers, cross builds, source bootstrap cycles, exact binary equality without an explicit mapping, and unsupported build layouts stop with a diagnostic. System/compiler requirements are probed in your selected environment. Generic library probes establish file availability; use a recipe's consumer checks to verify a particular CMake, pkg-config or link interface.

After upgrading, refresh the imported source:

```bash
jforge apt update
jforge apt install libgsl-dev
```

Dependency discovery, external probes and readiness records are saved under the printed operation directory. New dependency fingerprints can cause a one-time rebuild. Full design and recipe examples: [dependency installation](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT_DEPENDENCIES.md).

`jforge apt` follows familiar apt command names and operates on Forge recipes and user prefixes. It does not invoke system apt, request sudo, or edit shell startup files. Import an entire official source index with `apt import` without a package name; source archives are fetched on demand during installation.

```bash
jforge apt --help
jforge apt sources
jforge apt import debian
jforge apt update
jforge apt search neofetch
jforge apt install neofetch
jforge apt search eigen
jforge apt show eigen
jforge apt install eigen redis --jobs 2
jforge apt list --installed
jforge info eigen
source "$(jforge apt env --path)"
redis-server --version
```

Bundled recipes pin Eigen 3.4.0 and Redis 7.2.17 to official source archives and SHA-256 checksums. Eigen needs CMake and C/C++ compilers; Redis needs make and a C compiler. Use your site's existing modules for build tools. Installs do not start a Redis server.

```bash
jforge apt install eigen=3.4.0 --dry-run
jforge apt satisfy 'eigen>=3.4' --dry-run
jforge apt reinstall eigen
jforge apt upgrade --dry-run
jforge apt full-upgrade --dry-run
jforge apt remove eigen=3.4.0 --dry-run
jforge apt remove eigen=3.4.0 --yes
jforge apt autoremove --dry-run
```

Package installation reports package and command step counts and streams build output to stderr as it runs; exact command stdout and stderr are also saved separately in the operation record. Counts describe discrete steps, not elapsed-time percentages. JSON results remain on stdout.

Every successful instance gets an immutable receipt and activation script; source downloads, recipe snapshots, command logs and checks live in the operation record. Reinstall and upgrade create separate prefixes and preserve old instances. Build or check failure leaves the previous active environment selected. Valid dependencies completed before a later package fails can remain as inactive installations.

The catalog combines configured recipes and the imported official source index. Missing recipes are looked up in the selected repository during install; standard and nested CMake/configure, Meson and prefix-aware Makefile layouts receive generated build steps, and Neofetch has a Bash installation adapter. Complex layouts report the required adaptation. You can also add `recipes/*.yaml`, or register a local recipe directory/file or HTTPS YAML index:

```bash
jforge apt edit-sources --add /path/to/recipes
jforge apt update
```

Bundled recipes change with Forge releases; `update` refreshes the selected official source index, configured recipe sources and tracked official recipes. Use `update --recipes-only` or `install --no-official` for recipe-only operation. `upgrade` resolves newer compatible versions from that catalog. `full-upgrade` uses the same source-recipe resolver and retains old instances; it performs no system-wide removals. Recipe commands are trusted build instructions, not sandboxed code. See the [package manager design and recipe format](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT.md).

### Switch Debian and Ubuntu environments

```bash
# Select Ubuntu 24.04 LTS; metadata is imported on first use:
jforge apt switch ubuntu
# Equivalent explicit selection:
jforge apt switch ubuntu --suite noble --component main,universe
jforge apt update
jforge apt install libeigen3-dev libgsl-dev
jforge apt switch                         # List saved environments
jforge apt switch debian --suite bookworm
jforge apt switch legacy                  # Return to the original store
jforge paths
```

The selected source persists across CLI invocations and Python versions using the same Forge home. Package prefixes, recipe settings, caches, operation history and apt shell revisions are isolated under `<home>/apt/profiles/<source-id>/`. Previous installations remain available when you switch back. `apt list/search/show`, top-level `list/info`, `env show apt` and `paths` automatically follow the selection. The shared home, named Python environments and acceptance-run histories remain in place. Switching selects a package environment; install the packages you need in that environment.

Ubuntu defaults to `noble` with `main,universe`; Debian defaults to `bookworm/main`. Their base, updates and security pockets are refreshed together. Existing imported components remain selected on upgrade. `apt import` refreshes metadata within an environment; use `apt switch` when changing its distribution, suite or components. An explicitly configured signature keyring is retained on later switches to that environment.

With shell integration enabled, a successful switch immediately refreshes the current Bash/Zsh environment without manual path edits or per-source `source` commands. Without the hook, source `"$(jforge apt env --path)"` explicitly. Source-specific scope selection is shared on disk; switching from another SSH session does not modify an already-running shell.

### Import a complete official index

```bash
jforge apt import debian
jforge apt search neofetch
jforge apt show neofetch
jforge apt install neofetch
source "$(jforge apt env --path)"
neofetch
```

This imports the selected distribution's source and native-architecture binary package metadata, without downloading every source archive. New Ubuntu imports default to `noble/main,universe`, Debian to `bookworm/main`; `--component` accepts a comma-separated list. Use `apt sources` for available distributions and `apt switch` for isolated selections. An existing populated environment cannot be repurposed to another source family through import. `apt update` refreshes its indexes and tracked recipes; search and show use cached metadata without downloading. `apt edit-sources` displays official and custom recipe configuration.

`apt install` queries the official source after you explicitly import one. Bundled and user-provided recipes remain available without an official import. It verifies source archives, selects a compatible installation adapter and saves the generated recipe and provenance. Dry-run does not fetch missing metadata: it reports the pending official lookup instead. Official index availability does not imply that every package can be built on every host; missing native tools/libraries and unsupported layouts are reported with operation logs. Supported Debian build and runtime dependencies are resolved before compilation; original metadata and explicit adapter decisions are retained.

### Track official Debian and Ubuntu source packages

```bash
jforge apt import libeigen3-dev --origin debian --suite bookworm --name eigen
jforge apt update
jforge apt list --upgradable
jforge apt upgrade --dry-run
jforge apt upgrade
```

Import maps a binary or source package name through the official `Sources` index, records source versions and SHA-256, and preserves Debian quilt patches. This advanced per-package import can reuse an explicit reviewed template for custom options and checks. Built-in reviewed templates cover Eigen and Redis; `--template NAME` reuses a custom Forge recipe. Ubuntu sources use `--origin ubuntu --suite <codename>`.

`update` refreshes these tracked imports without compiling. `upgrade` detects both upstream changes and Debian revision/patch changes, creates new instances, and keeps old ones. Identical official metadata does not force a rebuild. Per-package metadata imports without a reviewed template retain draft/pending information. During explicit installation, automatic adapters can generate supported standard layouts; unsupported layouts remain pending. Drafts themselves do not silently enter the install catalog. Automatic adapters resolve supported Debian build and runtime relationships, retain their original fields and record every upstream-build policy decision; unresolved requirements stop before compilation.

Named original-source components are verified and mounted before quilt patches; native single archives are supported too. Internal source symlinks and hardlinks are allowed only within the extraction tree. Full official revisions use Debian version ordering.

Default metadata trust is official HTTPS plus Release/index/archive checksums, reported as `apt_signature_verified: false`. To require archive-signature verification, supply `--keyring /path/to/trusted-archive-keyring.gpg`; this requires `gpgv` and fails if verification cannot be completed. The importer builds upstream projects with quilt patches; it does not execute `debian/rules` or reproduce the full Debian binary-package build.

## Suite format

Suites use `schema_version: 1`, a stable `id`, and ordered command steps. Each step chooses either `argv` or an explicit Bash `script`, with optional `needs`, `cwd`, `timeout_seconds`, `expect_exit_code` and required `artifacts`. Commands and scripts are trusted user code.

The default working directory is the run's shared `workspace/`. Declared artifacts are relative file paths within the step's working directory; each is copied into the run archive. Supported placeholders are `${python}`, `${suite_dir}`, `${run_dir}`, `${step_dir}` and `${input:name}`. Inputs are declared as named file paths relative to the suite file and copied before execution.

Use `environment.name: cbs` to select a registered environment, optionally pinned with `environment.revision`. A CLI `--env` explicitly overrides the suite selection and uses the named environment's current revision. Alternatively, `environment.shell` points to a Bash script, relative to the suite file. These two YAML selections are mutually exclusive. It is snapshotted and sourced before each command, in the same shell. `environment.variables` supplies string-valued variables. The recorded plan and raw logs may include values supplied by the user; keep credentials out of suite files and command arguments.

The script must initialize your selected environment in a noninteractive Bash; login shell startup files are not loaded. Different suites may select different scripts. For example:

```yaml
schema_version: 1
id: cbs-environment
environment:
  shell: ./cbs-env.sh
steps:
  - id: python
    argv: [python, --version]
```

Use `python` or `python3` for the interpreter selected by that script. `${python}` always refers to the interpreter running Forge itself, which can be different. Each executed step saves `activation.json` and includes its environment report in `show --step`; reports contain actual tool versions and target Python package inventories. Comparisons include these activated environments, so changing a compiler or Python environment is visible even when commands and script contents are unchanged. Missing activation records make the affected comparison not comparable. Version probes do not compile code or establish that dependencies meet a particular project's requirements.

Every run has a new ID and saves configuration, environment observations, actual commands, activation details, logs, exit status, elapsed time, and declared output files. Source code and arbitrary undeclared outputs are not archived automatically. These records support comparison; they are not a complete machine image or automatic source backup. Failed prerequisites block dependent steps; independent steps continue. Foreground execution does not promise survival after SSH disconnection.

## Development and integration

```bash
python3 -m pytest
```

The host integration point is `jarvis_forge.cli.main(argv, prog="Jarvis forge")`; it returns an exit code and owns the single parser. The standalone console script is operational. Jarvis-HEP forwarding is not yet integrated.

The [APT comparison and scientific-library support matrix](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT_COMPATIBILITY.md) describes GAMBIT/ROOT prerequisite adapters and remaining build limitations. Source-layout recognition is separate from a successful build on the target HPC.

Project design and implementation scope live in [Jarvis-Books](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/tree/main/Docs/Jarvis-Forge). CLI behavior follows the [Jarvis ecosystem CLI specification](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Ecosystem/JARVIS_CLI_DESIGN.md).

