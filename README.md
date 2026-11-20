# Jarvis-Forge

Jarvis-Forge is an independent Python package for user-space software environments and command-based acceptance workflows on scientific workstations and HPC systems. Its CLI is `jforge`.

Forge runs local command suites, test cards, and benchmark cards, and preserves their evidence. Its `jforge apt` command uses a pinned, user-local Spack installation on Linux and an existing Homebrew installation on macOS by default. Old source installations remain queryable as archived records. Named environments keep private, versioned shell copies in one shared user directory. Benchmark cards can submit Slurm array jobs through `jforge bench`. Managed services, suite-level scientific comparison policies, and the `Jarvis forge` forwarding command are planned extensions.

The root commands are `apt`, `env`, `build`, `test`, `bench`, `suite`, `playlist`, `dashboard`, `history`, `show`, `logs`, `init`, and `doctor`. Use `playlist` to organize and run cases across projects and `dashboard start` to open the local UI. See the [playlist library guide](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/PLAYLIST.md).

Configure the local dashboard once, then manage it from any directory:

```bash
jforge dashboard config --library /absolute/path/to/library --port 8787
jforge dashboard start
jforge dashboard status
jforge dashboard restart
jforge dashboard stop
```

`jforge dashboard config` shows the settings stored in `<Forge home>/dashboard/config.json`. Open the session link printed by `start` or `status`. The dashboard uses the same Forge home selection as other commands.


## Install

Python 3.10 or newer and a POSIX system with Bash are required for execution. Use WSL on Windows. Forge does not depend on Jarvis-HEP.

```bash
python3 -m pip install Jarvis-Forge
jforge init
jforge apt profile
```

The pyenv setup script runs `jforge init` automatically. Initialization selects `spack-default` on Linux and `homebrew-mac` on macOS, and preserves a profile already selected on this system. Spack is prepared on first use. On macOS, Forge registers an existing Homebrew. If Homebrew is absent, the profile stays pending until it is available.

Run `tools/jforge-pyenv-setup.sh` from a dedicated installation directory. It installs its own pyenv, Python versions, JForge launcher and caches there, initializes Forge data in `./jforge`, and creates `./activate.sh` using absolute paths. Source that script from your shared startup file. Startup-file changes require `--shell-config`; the default prints the activation command. Repeat the setup from the same directory to reuse its installed Python, and add `--upgrade` to upgrade JForge.

Jarvis-Forge is installed from PyPI directly into that pyenv version's base Python environment, without a separate client virtual environment. The stable launcher uses this same interpreter, so `python -m pip install --upgrade Jarvis-Forge` in that pyenv version updates the running client. Rerun the updated setup script in an existing installation directory to replace the older virtual-environment launcher; existing profiles, packages and data stay in place. Previous client virtual environments are left on disk and are no longer used by the launcher.

On Linux, the setup script checks ncurses (including panels), Readline and SQLite development libraries before building Python. Missing libraries are built from checksum-pinned sources in `./python-deps/`, with runtime search paths embedded in the linked binaries. Readline links its terminal library explicitly. Each locally built shared library must load independently with immediate symbol resolution, without `LD_LIBRARY_PATH` or `LD_PRELOAD`; damaged libraries are rebuilt on retry. This needs a host C compiler and GNU Make, uses no sudo, and leaves shell startup files unchanged by default. Dependency build logs are saved in `./jforge-bootstrap/logs/`. Python validation includes `curses`, `curses.panel`, `readline` and `sqlite3`; rerunning the script rebuilds an incomplete interpreter with pyenv.

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
jforge suite example --output smoke.yaml
jforge suite validate smoke.yaml
jforge suite plan smoke.yaml
jforge --home .forge-home suite run smoke.yaml
jforge --home .forge-home suite run smoke.yaml
jforge --home .forge-home history --suite forge-smoke
```

Copy the run IDs printed by `suite run` or listed by `history`:

```bash
jforge --home .forge-home show <run-id>
jforge --home .forge-home logs <run-id> --step calculate
jforge --home .forge-home suite compare <older-run-id> <newer-run-id>
```

Text results use standalone headings and record summaries suitable for SSH terminals. Paths and commands are emitted as complete lines; the terminal may visually wrap them without inserted line breaks. `apt update` refreshes the selected backend's recipe metadata. Full structured fields remain available with `--json`.

`--json`, `--home` and `--debug` work before or after the command. `--json` emits one JSON object on stdout, while progress goes to stderr. `suite compare --check` fails on changed, missing, corrupted or unsuccessful evidence; ordinary comparison only describes differences. Comparison uses exact artifact bytes; scientific equivalence and performance regression policies remain outside the current comparison mode.

## Automatic shell activation (Bash and Zsh)

A child process cannot change its parent shell. Enable the shell integration once in each interactive session:

```bash
eval "$(command jforge apt shell-init)"
```

To register the hook for future shells, run `jforge env repair --hook` or use the bootstrap installer. Regular `jforge env repair` restores registered hook blocks and the bootstrap loader when needed. It also refreshes the hook in the calling interactive shell when that shell already runs the Forge hook. An open shell running an older hook can reload it with the command above.

The hook loads the selected environment at startup and refreshes it after successful package changes. It uses the selected Forge home, including an explicit `--home`. Source switching removes previous managed package paths and restores exported values while preserving user PATH additions. The hook is intended for interactive use.

The shell entry for each environment is a short, stable script next to its metadata file. The package environment is `<home>/env/apt/activate.sh`, described by `<home>/env/apt/env.json`. `jforge apt env --path` prints that script. Its text sources the current package revision, so the command above keeps working after later installs. A successful `root` adds that build's activation script to this same file. A ready root already on disk is added the next time the entry is published. A failed build leaves the entry unchanged. `source "$(jforge apt env --path)"` in a shell startup file loads that entry in each new shell. It does not install the `jforge` command wrapper, so later package commands in that shell do not refresh it and a finished build does not activate itself. Use the hook above when you want both.

Successful package changes publish the stable entry even without a shell hook, and show that path with `source "$(jforge apt env --path)"`. Unified profiles use `<home>/apt/profiles/ubuntu24-U1/activate.sh`, with the actual semantic instance name and suffix recorded in JSON. Transaction JSON retains the backend revision in `activation` and adds the stable path as `environment_activation`, with the copyable `source_command`. Historical revision paths remain in metadata and operation records.

`jforge env switch <name>` selects `apt` or a named environment for new shells. With the Forge hook loaded, the current shell loads it when the command returns. Without the hook, the current shell stays unchanged. `jforge env detach` clears that selection. The environment files remain. `jforge env current` shows the selection. `jforge env drop root` removes that build from the apt environment and leaves the installation on disk; the next successful `root` adds it again. `jforge env list` shows shell environments and package profiles separately: switch an environment with `env switch`, and select a package profile with `apt profile`. `jforge env metadata [name]` prints the metadata file for that environment. With no name, it prints the selected environment.

Spack package names are cached during import/update. `apt list` shows installed packages in the selected profile, with version, requested/dependency kind and state. Requested packages appear first, followed by dependencies, sorted by name. Use `apt list --available` or `apt search` for the cached package catalog. Complete catalog listings are printed as compact lines. Use `apt show <package>` for installation paths and instance metadata, or `--json` for complete records. `jforge apt instances` lists saved installation instances, and `jforge apt prefix` prints one prefix.

JForge allocates instance directories with semantic names and short suffixes such as `ubuntu24-U1`, `root-A1` and `build-A1`. Full names, versions, host/CPU/Python identities, timestamps and checksums live in atomic JSON indexes and object records. Two-character suffixes are preferred; the decimal part grows only after the semantic name's two-character suffixes are exhausted. Completed packages remain as permanent terminal rows above the running package display, preserving their versions, final status and timing.

## User directories and package queries

Forge uses `~/.jarvis-forge` until you choose a default with `init`. `jforge init --home /data/my-forge` initializes that directory and remembers it for later commands, including commands started in other terminals and Python environments. Home selection follows `--home`, `JFORGE_HOME`, the remembered default, then `~/.jarvis-forge`, in that order. Using `--home` with another command only overrides that invocation.

The remembered location is stored in `~/.config/jarvis-forge/default-home.json`, or under an absolute `XDG_CONFIG_HOME` when configured. `jforge doctor --paths` shows both the selected data directory and this preference file. Initialization does not move existing software or histories. `doctor`, `doctor --paths`, `apt instances`, `apt prefix`, `suite validate`, `suite plan`, `history`, `show` and `logs` do not initialize a user directory or install software.

```bash
jforge init --home /data/my-forge
jforge env list
jforge apt list
```

Both queries above use `/data/my-forge` without repeating `--home`, provided no `JFORGE_HOME` override is set. Repeating `init --home` with another directory changes the remembered default. A missing remembered directory remains selected; Forge does not silently switch to a different store. To repair a corrupt preference, explicitly initialize the desired directory again.

```bash
jforge doctor --paths
jforge doctor
jforge doctor --shell /path/to/cbs-env.sh
jforge doctor --shell /path/to/cbs-env.sh --json
jforge apt instances
jforge apt prefix eigen
jforge apt prefix eigen --package-version 3.4.0 --instance <install-id> --path
```

`apt prefix` queries JSON installation manifests under the selected package environment's `packages/<name>/<version>/<install-id>/manifest.json`. `jforge apt install` creates those records after installation checks pass. Multiple instances are listed explicitly; `--path` requires a unique match. `--package-version` selects installed software; `--version` remains Forge's version query.

New Spack environments install each package into `<home>/apps/<profile>/<package>`, for example `apps/root-env/eigen`. These are real installation directories. Versions, compiler/variant/dependency identities and full hashes remain in Spack's own `.spack/spec.json` and database. There is no extra link layer or Forge-specific package metadata format.

Each new environment permits one locally required build per package name. Forge uses Spack's installer dependency traversal. Installed packages omit their historical build tools, and binary-only installs require link and run dependencies. With `--cache auto`, Forge first attempts a validated binary-only install. It then validates the remaining dependencies, installs binaries listed in the cache index, and builds packages without a binary. If the remaining binary install fails, Forge solves again against the selected environment and local installations, validates the new plan, and installs the remaining packages from source. `--cache only` requires binaries. `--cache never` validates source dependencies directly. Conflicting builds and occupied package directories stop installation. External packages do not compete for managed store directories. Spack validates native metadata, and `reinstall` includes the build tools for the requested same-spec overwrite.

Operations save the complete concrete graph in `dag.json` and the initial required dependency plan in `plan.json`, including its `cache_policy`. A source fallback saves its refreshed plan in `source-plan.json`. When the remaining binary install fails, Forge saves the previous candidate lock in `spack-before-cache-retry.lock` and the new source plan in `source-retry-plan.json`. `apt check` uses the same initial plan without installing anything. With automatic caching in a package-name environment, its note and JSON `source_fallback_requires_validation` field identify the conditional source fallback.

Existing environments retain their original layout and multi-instance policy, including when regenerating configuration or selecting another Python. To adopt package-name directories with an existing Forge home, choose a previously unused environment name:

```bash
jforge apt profile
jforge apt profile root-env --create
jforge apt install libeigen3-dev libgsl-dev sqlite3
jforge apt prefix eigen --path
```

`jforge doctor --paths` reports the selected native store. Old prefixes and installed ROOT references are preserved. Each `build` dependency profile follows the same one-build-per-name rule when newly created. Saved Spack environment revisions are installation history. `jforge env list` shows both named shell environments and configured Spack profiles.

You select and prepare Python environments with pyenv or your existing tools. `doctor --shell` sources your trusted Bash script and reports the resulting CC/CXX/FC selections, compiler and build-tool paths and versions, Python interpreter paths and versions, virtual environment prefixes, installed Python packages, and selected module/pyenv variables. It does not choose an environment or switch to a fallback. Without `--shell`, it inspects the inherited environment. Missing tools and failed version probes are reported individually; activation errors fail the command. `--timeout` limits the complete inspection, including activation. Shell output is kept separate from JSON results. Inspection creates no Forge user state.

## Named shell environments

All Forge installations for the same user share the remembered data directory, independently of the Python version running the CLI; the initial fallback is `~/.jarvis-forge`. `JFORGE_HOME` and `--home` can explicitly override it. Target Python environments do not need Forge installed.

```bash
jforge env add cbs --shell /path/to/cbs-env.sh
jforge env list
jforge env show cbs
jforge env show cbs --path
jforge doctor --env cbs
jforge suite run acceptance.yaml --env cbs
```

`env add` copies the script into `environments/<name>/revisions/<revision>/source.sh` and generates `activate.sh`. It checks Bash syntax without sourcing the script. Select Python in your shell or in the registered script using pyenv or your existing tools. Choose a profile explicitly with `--env`, `environment.name` in YAML, or `source "$(jforge env show cbs --path)"` in your own shell.

Each `env add ... --replace` saves a new revision. Use `env show cbs --revision <revision>` to inspect a retained revision. If a retained revision contains a Forge Python override, select Python in your shell and re-register the script with `--replace`. Runs archive the selected revision and its script. The reserved `apt` environment appears in the list after package activation and remains managed by `jforge apt`.

Scripts are copied, not relocated together with their dependencies: use stable absolute paths for external files. If the original script changes, register the update with `env add cbs --shell /path/to/cbs-env.sh --replace`. For reproducibility, files in published revisions are not intended for direct editing. Full usage and storage details are in [the environment guide](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/ENVIRONMENTS.md).

Run `jforge env repair` from any directory to discover and repair Forge-managed environments. It checks named environments, published package profiles, build workspaces and independent build exports, and the setup script's bootstrap activation when installed. Use `--dry-run` to preview or `--json` for per-setting results. The command saves a report for each changed target and keeps going if an individual target fails.

Literal references to a known build workspace's `activate.sh` are skipped while it is missing and used again when the workspace is rebuilt. Use `--shell PATH`, a registered environment name, or `apt` to target a single file, named environment, or active package profile. Repair checks literal paths without executing activation scripts.

## User-local packages with Spack

Spack is the default package backend on Linux. Native Spack extension recipes are downloaded from a dedicated recipe Release and refreshed with apt update, independently of Python package releases. Existing source installations are retained as read-only history. The first Spack write prepares the default profile:

```bash
jforge apt import spack
# Or prepare and select a named package environment:
jforge apt profile gambit --create
jforge apt compilers
jforge apt compilers --find
jforge apt list --available
jforge apt search eigen
jforge apt show libgsl-dev
jforge apt check sqlite3 libgsl-dev libeigen3-dev
jforge apt install sqlite3 libgsl-dev libeigen3-dev --jobs 4
jforge apt list
jforge apt prefix libgsl-dev --path
```

Maintainers publish all package recipes, project build YAMLs, library cards and companion scripts with `./jfrecipes <recipe-revision>`. Use `--dry` to prepare and check both catalogs without network access. The fixed `recipes/recipes.json` and `builds/builds.json` entries keep their independent release cycles. `jforge apt update` refreshes the selected package backend and the build recipes supported by this OS. Use `jforge build update` to refresh just the build catalog. The current build catalog requires Jarvis-Forge 1.1.11 or newer; upgrade the client before refreshing the catalog. See the [recipe publishing guide](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/RELEASING.md#%E7%8B%AC%E7%AB%8B%E5%8F%91%E5%B8%83%E6%89%A9%E5%B1%95%E9%85%8D%E6%96%B9).

`apt import spack` prepares and selects `spack-default`. `apt profile NAME --create` creates and selects a separate store and configuration. Use `apt profile` to list configured profiles and `apt profile NAME` to select an existing one. Select Python before creating a profile. Forge registers the active interpreter as an external Spack dependency and keeps it for the life of the profile. A package plan that requires a Spack-managed interpreter or virtual environment is rejected. The interpreter running Forge executes Spack itself. Select compilers and variants with native quoted specs when needed:

```bash
jforge apt install 'gsl@2.8 %gcc@13' 'hdf5~mpi' 'root +python'
jforge apt install sqlite3 --cache only
jforge apt sources
jforge apt sources --add site --url https://example.org/spack-cache
jforge apt sources --trust-key /path/to/site-signing-key.asc
jforge apt update
jforge apt upgrade
jforge apt remove sqlite3 --yes
jforge apt profile default
```

Common Debian development names are aliases to Spack names, for example `libgsl-dev` → `gsl`, `libeigen3-dev` → `eigen`, and `sqlite3` → `sqlite`. Native Spack specs and `name=version` are accepted. This does not import Ubuntu/Debian binary packages: Spack owns the dependency graph and uses its own recipes and signed build caches. Search/list use cached package names; show fetches detailed metadata from the managed local Spack installation.

On Linux, install fonts in the selected Spack profile with `jforge apt install font-fira-code font-liberation-sans font-inter font-noto-sans-sc`. Forge discovers TrueType and OpenType fonts under installed packages' `share/fonts` directories and configures Fontconfig when the profile is activated. The generated `fonts.conf` stays with the activation revision and Fontconfig's cache lives at `cache/fontconfig` under the profile. The standard Linux system font directories are still available. A profile on shared storage makes its fonts available to activated jobs on each node without requiring root access or user font directories. Verify with `fc-list 'Fira Code' family file` in the active shell.

Spack 1.2.2 and an engine-compatible recipe commit are bootstrapped from checksum-pinned official archives, without Git checkouts. Import requires HTTPS access, curl/wget, Bash, and a working Python. Source builds require a usable host compiler and relevant platform tools. The solver may bootstrap into the managed directory. Spack normally uses the host glibc; Forge does not build glibc or promise an isolated Ubuntu user space. AlmaLinux users use a compiler module or Spack compiler package as appropriate for their site.

When a Linux Spack installation allows source builds, Forge uses `patch` from `PATH` when available. Otherwise, it verifies and builds GNU patch using the host C compiler and `make`. The binary lives under the Spack profile's `spack/bootstrap-tools` directory and is added to `PATH` for the relevant Spack subprocesses. A profile on shared storage can reuse it across compatible nodes. Profile checks and binary-only installs do not download or build GNU patch.

`--cache auto` reuses compatible binaries and permits source builds; `only` refuses source builds for requested packages/dependencies; `never` requires source builds. Spack's own solver/tool bootstrap is separate. Official cache signing keys are installed from the named official mirror. Custom binary mirrors require the user's trusted key; signature checking stays enabled. Cache availability depends on OS, architecture, compiler and the exact dependency graph. Public cache availability is not guaranteed.

If a plan conflicts with an installed `gcc-runtime` in a one-build-per-package store, Forge retries resolution against that runtime and checks the new plan before installation. Compatible binaries remain eligible for installation.

Each successful operation saves the concrete `spack.lock`, a configuration snapshot, package hashes and prefixes, stdout/stderr logs, and a new activation revision. Forge commits the selected environment only after Spack reports its packages installed. A failure retains the previous selection and logs; successful dependencies may remain in the store for reuse. Spack commands run with noninteractive input so detached processes do not access the calling SSH terminal.

APT shows concise progress and results by default, including when a project build installs dependencies. In an interactive terminal, the package summary shows the completed count and the number in flight. Each active package has an animated bar with its current Spack phase and elapsed time. Pipes, `--json`, dumb terminals and `NO_COLOR` use plain phase lines. Use `jforge apt install eigen --verbose` to stream the complete dependency tree, native Spack commands and raw output. `--debug` also enables detail. These options work before or after commands. Original output is always archived, including the complete tree in `command-dependency-tree.stdout.log`. Errors retain complete messages and copyable paths. `--json` keeps stdout to one result object and sends progress and detail to stderr.

`apt check` concretizes and archives a dependency plan without building packages or changing the selected environment; it is not a package integrity audit or a project acceptance test. `--dry-run` is an offline request preview without solver execution. `upgrade` re-solves all selected roots against current recipes; change one root with `install SPEC`. `list --upgradable` is unavailable because upgrades require solving the environment. `reinstall` uses Spack's overwrite semantics for the same hash and can change files used by saved revisions.

`remove` drops explicit roots from the selected environment and retains the store and previous revisions. `autoremove --yes` delegates garbage collection to Spack while retaining all saved named environments and their build dependencies. It therefore does not reclaim packages still needed by historical revisions. Neither command removes external pyenv/system packages.

The Ubuntu/Debian source resolver, source-package importer, custom build adapters and YAML package recipes have been removed. `apt import ubuntu/debian`, `apt switch ubuntu/debian/legacy`, and their source-specific options no longer activate a backend. `apt sources` now manages Spack mirrors, and `apt edit-sources` displays those mirrors.

Existing source prefixes, manifests, shell files and operation logs stay on disk without conversion or automatic deletion. An old source selection routes package commands to `spack-default`; a successful write saves the new selection. Query old records explicitly:

```bash
jforge apt instances --archived
jforge apt prefix libgsl-dev --archived --path
jforge apt list --archived
jforge apt profile --archived
```

Archive queries read records without running old shell or recipe commands. Archived records do not satisfy Spack dependencies and cannot be upgraded or removed by the package CLI. Reinstall the packages you need with Spack. Use `apt profile direct` to select an existing root-level Spack store that uses external Python. Reload `apt shell-init` once after upgrading. Subsequent successful package changes automatically refresh the current shell.

See [APT usage](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT.md), [backend design](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT_DEPENDENCIES.md), and [compatibility](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT_COMPATIBILITY.md).

## Native packages with Homebrew on macOS

With Homebrew installed, initialize Forge to register it and select the native formulas you need.

```bash
jforge init
jforge apt profile
jforge apt install eigen gsl --dry-run
jforge apt install eigen gsl
jforge apt list
jforge apt prefix eigen --path
jforge apt env --path
```

Each Homebrew profile records its selected formulas and shares the existing Cellar. `apt list` includes shared installations, and `apt remove eigen --yes` drops the profile's selection while keeping the keg. Forge reuses installed kegs when possible. Installing missing formulas runs Homebrew as your user and requires a writable prefix. `apt update` refreshes Homebrew metadata.

Use pyenv for Python. Forge rejects formulas that require Homebrew Python, including the official ROOT formula. Activation adds the selected kegs' CMake and pkg-config paths while preserving the current PATH and Python. `jforge env repair` refreshes these paths after external Homebrew changes.

Current build recipes with `apt.install` use Spack. See the [Homebrew profile design](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/HOMEBREW_PROFILE_DESIGN.md).

## Build profiles

```bash
jforge apt profile
jforge apt profile check
jforge apt profile build ubuntu-24.04 --jobs 8
jforge apt profile global ubuntu-24.04
jforge apt profile apps
jforge apt profile apps rivet --off
jforge apt profile apps rivet --on
```

Built-in Linux profiles prepare GCC, G++, GFortran, CMake, GNU Make, Autoconf and Automake with Spack. The `macos` profile uses Apple Clang and Homebrew tools. Select your Python with pyenv before building or activating a profile. The Forge shell hook loads the selected default and its build outputs. On Linux, each new Spack profile keeps its store, toolchain, metadata, and activation scripts inside its own instance directory. Apt checks the selected Python and CPU before operating, fails if a selected backend is damaged, and enforces the CPU target on the full package build graph. See the [unified profile design](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/PROFILE_DESIGN.md).

Fixed tool suites may use their exact pinned versions even when Spack marks those versions deprecated. Forge records the permitted versions in toolchain metadata and each operation's `deprecation-policy.json`, applies the setting only to that operation's configuration, and rejects other deprecated source packages before installation, including after cache fallback. Bound profiles retain the same exact permissions. Ordinary package operations without a bound tool suite keep deprecated versions disabled. This does not change preset versions or require clearing a failed profile directory before retrying.

`apt profile apps` lists installed build outputs in the selected profile, including libraries. Use an app name or an exact output ID with `--on` or `--off`. Turning an app off preserves its installation and selected version. Turn it back on by name, or select a different version by exact output ID. If another enabled app needs that output, turn the dependent app off first. The shell hook refreshes the currently loaded profile if it is the one you changed; a named or detached shell stays as it is. `cbs` prepares its required ROOT build and temporarily leaves Rivet out of the build environment.

Existing standalone GCC installations and bindings remain accessible with `apt toolchain`. See the [HPC toolchain workflow](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT.md#hpc-%E7%94%A8%E6%88%B7%E7%9B%AE%E5%BD%95%E4%B8%AD%E7%9A%84-gcc).

## Project builds and regression checker

`jforge build` prepares, configures, compiles, installs and verifies software projects using CMake and reusable installation outputs. The `workflow` command namespace is reserved for future orchestration design. It is parallel to `apt`. Recipe dependencies install into the selected Spack profile, normally `spack-default` until another profile is selected. Build YAML sources are distributed independently from PyPI through GitHub Release assets. From a development checkout:

```bash
jforge build validate builds/root.yaml
jforge build plan builds/root.yaml --env hep
jforge build run builds/root.yaml --env hep --jobs 8
jforge build resume <run-id> --jobs 8
jforge build list
source "$(jforge build env <artifact-id> --path)"
```

For a published app, start in the directory where you want to keep the workspace:

```bash
jforge build list --available
jforge build fetch root        # Creates ./root/build.yaml and its companion scripts
jforge build plan root         # Shows dependencies and steps without running them
jforge build run root --jobs 8 # Uses ./root, including any edits you made
jforge build list             # Shows installations in the current profile
```

Use `build show root` to read the recipe and `build plan root --verbose` for the CMake commands. From inside the workspace, `build run .` also works. To find one installed app's activation script, use `build env root --path`; if several outputs match, pass an exact output ID from `build list`.

Use `--directory` to choose another workspace path, or fetch and build it in one command:

```bash
jforge build run root --directory "$HOME/workshops/root-forge" --jobs 8
source "$HOME/workshops/root-forge/activate.sh"
```

To fetch without building into a custom workspace:

```bash
jforge build fetch root --directory "$HOME/workshops/root-forge"
jforge build run "$HOME/workshops/root-forge" --jobs 8
```

The directory contains `build.yaml`, companion scripts and `instances/<name>/{downloads,sources,build,install}`. A build with one exported package uses its package and version as the instance name, for example `ROOT-6.40.04`. Other builds use the recipe ID. Distinct build identities with the same name get a numbered suffix. Each new instance records its full identity in `.jforge-instance.json`. Resume and reuse also support existing directories named by their full hash.

Package dependencies live in Forge's shared Spack profiles. `runs/<run-id>` links to the retained Forge history. A successful acceptance run updates the workspace's `activate.sh`, and a failure leaves the previous successful activation available. Absolute installation paths may be embedded in binaries and build files. Building in a new workspace gives an existing project a new installation path.

With the Forge shell hook loaded, successful `jforge build run` and `jforge build resume` commands activate the installation in the current interactive Bash or Zsh session. The bootstrap installer loads the hook at startup. For a plain pip installation or an already open terminal after upgrading, load it with `eval "$(command jforge apt shell-init)"`. Use `--no-activate` to keep a build from activating. JSON and noninteractive commands keep their existing behavior.

In an interactive terminal, `build run` and `build resume` show a progress bar while downloading a source archive. The bar shows the bytes transferred and a percentage when the downloader knows the total size. Raw downloader output stays in the run logs. `--verbose` prints the download log path.

An uncached app recipe is downloaded on explicit `build fetch/run`; cached IDs and existing workspaces remain usable offline. `build update` refreshes the catalog explicitly and never overwrites an editable workspace. Use `fetch --output file.yaml` for just the recipe and its scripts. The old names such as `root-build` still resolve to the same app. Existing workspaces, installed outputs and saved runs retain their records. `validate/plan` stay read-only and do not implicitly download missing recipes.

Build cards use native Forge project sections: `dependencies`, `build` and `checks`. The ROOT card explicitly lists its source URL/checksum, packages, CMake options, extra dependency search paths and acceptance commands. A shortened excerpt is:

```yaml
name: root
dependencies: [gsl, openblas+shared, openssl, libbsd, libmd]  # Full list in the actual card.
build:
  name: ROOT
  version: "6.40.04"
  # The actual card declares source URL, SHA-256 and archive subdirectory here.
  cmake:
    options:
      CMAKE_CXX_STANDARD: 17
      all: true
      fcgi: false
      cuda: false
    includes: [libbsd, mesa-glu, fontconfig]
    libraries: [libbsd, libmd, mesa-glu]
    runtime: [openssl, libbsd, libmd]
checks:
  before:
    python:
      version: ">=3.10"
      development: true
      modules: [numpy]
    tools: [cmake, cc, c++, gfortran]
  after:
    python:
      modules: [ROOT]
    scripts:
      - path: scripts/root_acceptance.py
        args: ["${path}", "${build.version}", "${parameters.cxx_standard}", "${build.features}"]
```

Only engine policy is implicit: Forge home selection, work/build/install/log directories, phase defaults, ordinary build type and timeouts. These defaults live in `card/build_default.yaml`; process-log layout lives in `card/logging.yaml`. Dependency path lists resolve from selected apt package prefixes. Includes/libraries feed both CMake discovery and compiler/linker search paths, including nested builds. Runtime paths apply during builds and after installation, with CMake RPATH defaults. Each CMake step saves resolved paths in `dependency-paths.json`. The independently released `builds/root.yaml` declares the project requirements; its companion `builds/scripts/root_acceptance.py` implements ROOT-specific acceptance. `build show` summarizes the recipe. `--expanded` adds the resolved steps, and `--json` prints the card data.

`build.source` accepts a local source directory or URL/SHA-256/subdir mapping. Use `build.cmake.preset` for a project's native preset, and `requires` to consume another build's installation output. Standard `checks.before/after` declare Python versions/development files, imported modules and executable requirements. Module/tool mappings add constraints, for example `modules: {numpy: ">=1.26"}`. Checks run with the selected Python, which does not need Forge installed. Reports preserve actual versions, paths and failures in `checks.json`. Custom Python belongs in relative companion scripts, with optional argument lists; shell strings, cmd/cwd mappings and literal argv lists remain supported. Previous LibDeps/Calculators spellings remain compatible aliases; new cards do not require HEP-specific keys. Similar log presentation does not add a dependency on Jarvis-HEP.

`hep` is a user-registered environment; omit `--env` to inherit the current compiler/Python selection. The ROOT recipe currently targets Linux, needs an existing CMake/C/C++/Fortran toolchain and Python with NumPy/development headers, and defaults to C++17. Select the interpreter with pyenv or your shell before running the recipe. ROOT uses the interpreter, headers and library verified in preflight. It configures ROOT with `-Dall=ON` and turns off fcgi, dcache, R, Qt6Web, CUDA, and the GPU TMVA options. Acceptance requires the declared features, PyROOT, histogram and file I/O, and RooFit. `-Dall=ON` can build bundled XRootD, VDT, Clad, and RooFit MultiProcess when those packages are not already installed. It does not promise GPU support. ROOT's full real build requires separate target-machine validation of this new automation.

User cards declare prerequisite modules with `required_modules`; the expanded execution schema represents external build outputs using `requires` entries. Compatible installed artifacts are reused; missing ones are built first. Recipes, local source digests, parameters, dependency instances and tool observations are locked. Multiple compatible instances require an explicit artifact binding; incompatible compiler/Python/C++ choices fail before consumer configuration. `cmake.project` consumes pinned prefixes and verifies the selected package-config path. History/show remain available; logs support qualified step names such as `root.ROOT.install`.

`build run` and `build resume` show numbered stages, dependency and download progress, and a concise result. Failures show the stage, diagnostic, log paths and a resume command. Use `--verbose` or `--debug` to stream command lines and raw output. The attempt keeps the timestamped `build.log`, individual command logs and check reports in every mode.

`build list` lists installed outputs with their artifact IDs and prefixes. `build list --available` lists cached recipes and libraries. `build artifacts` lists all recorded outputs, including historical installations.

Independent recipe distribution (once the maintainer publishes the assets):

```bash
jforge build update
jforge build list --available
jforge build fetch root --output root.yaml
PYTHONPATH=src python3 tools/build_project_assets.py --version r1 --output /tmp/forge-build-release
```

The asset builder prepares versioned YAML, referenced Python scripts and a checksum index. `update` validates the whole bundle; `fetch` copies the YAML and its scripts together, refusing conflicting local files. Script changes alter recipe identity, and resume uses the original script snapshots. Companion scripts should be self-contained. The builder never uploads or modifies `jfrel`. An unavailable Release does not prevent executing local or previously cached YAML.

`cepc` is the Linux parton-level recipe for 240 GeV WW generation with WHIZARD 3.2.0 and MadGraph 3.8.1. After fetching the recipe into a workspace, copy the CEPC project's `server_setup/` handoff into that workspace. The build checks its checksums, installs the pinned generators, and runs the handoff's WHIZARD, MG5 Born and standalone C++ export checks in a new acceptance directory. On success, source the workspace's `env.sh` to load the tools. For detector simulation, compose a separate build with an explicit dependency on `root` and a verified CEPC detector card.

`higgstools` installs HiggsTools 1.1.3 for GMFit. It uses the vendored source archive, applies the assignment-trace bridge v2 and the HS measurement-breakdown bridge v1, and builds the Python module from the bundled third-party sources. The wheel is installed into the `python3` on `PATH` with `pip --no-index --no-deps`. It also installs pinned HBDataSet v1.7 and HSDataSet v1.1 under the prefix and sets `HIGGSTOOLS_HBDATASET`, `HIGGSTOOLS_HSDATASET`, and `JARVIS_OPERAS_HTS_DATASET_CACHE_ROOT` for that interpreter. The legacy `HIGGSTOOLS_HB_DATASET` and `HIGGSTOOLS_HS_DATASET` names receive the same paths. It runs on Linux and macOS. Preflight requires CMake, a C++17 compiler, `make`, `patch`, Python development headers, and `setuptools`, `wheel`, and `pip` in that interpreter. Confirm with `python3 -m pip show HiggsTools` and `python3 -c 'import Higgs; import Higgs.predictions'`.

`jforge test` also runs schema 1 checker cards: commands, builds, or the same structured standard checks. Standard checks use `check: {python: {modules: [numpy]}}` and run every time by default. Default selection for command/build checks compares declared input globs (including additions/deletions), check configuration, prerequisite signatures and the observed environment with the last successful evidence. It does not infer all software dependencies from Git changes. Checks with external/untracked inputs should use `cache: false` or be run with `--full`. Schema 2 cards use `test plan` instead of `test list`.

```bash
jforge test validate tests/checker.yaml
jforge test plan tests/checker.yaml --profile acceptance
jforge test run tests/checker.yaml --full
jforge test run tests/checker.yaml
jforge test run tests/checker.yaml --group build-engine
jforge test history --suite forge-development
jforge test report <checker-run-id>
```

The development checker covers local test modules plus ROOT YAML validation. Real Spack integration and ROOT/CBS/MadGraph builds run separately. `--full` reruns checks but does not erase CMake incremental builds or force successful build artifacts to rebuild. Byte/scientific comparison policies for these run types are not yet part of `suite compare`.

`jforge test` runs schema 2 test cards and test catalogs. A card selects pytest files by globs and markers, names profiles such as `daily` and `acceptance`, and can require that every test file belongs to an acceptance group. `validate` and `plan` read the YAML and file tree. `run` executes selected groups, records their results and reuses matching evidence. Run `jforge test accept` in a project directory to run its complete gate. `jforge test status` exits 0 when the latest complete result accepts the current Git source or pinned release archive with its current dependencies and benchmark evidence.

Use `jforge bench init ./`, `jforge bench fetch cbs-test-atlas`, and `jforge bench run smoke.yaml` in a benchmark workspace. After the smoke test, `jforge bench run` selects the physics card. See the [CBS setup and run commands](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/PROJECT_ACCEPTANCE_DESIGN.md#%E5%9C%A8-nas-%E4%B8%8A%E8%BF%90%E8%A1%8C).

```bash
jforge test validate tests/jforge-tests.yaml
jforge test plan tests/jforge-tests.yaml --profile acceptance --batch 2/4
jforge test accept
jforge test run tests/jforge-tests.yaml --profile acceptance --batch 2/4 --acceptance-id ID
jforge test summarize --acceptance-id ID
jforge test status
jforge test history --case test_resume
jforge test report RUN_ID
```

Both executors run in the foreground and clean up their child processes on interruption; use tmux for long SSH builds. See the [build implementation and design](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/BUILDS.md), [checker guide](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/CHECKER.md) and [ecosystem acceptance contract](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Ecosystem/JARVIS_ACCEPTANCE.md).

## Acceptance suite format

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

Use `${project_python}` to run `python3` from the activated suite environment. `${controller_python}` selects the interpreter running Forge. Existing suites can continue to use `${python}` for the controller. In build and checker recipes, `${python}` retains its project interpreter meaning and the two explicit names are also available. Each executed step saves `activation.json` and includes its environment report in `show --step`. Reports contain actual tool versions and target Python package inventories. Comparisons include these activated environments, so changing a compiler or Python environment is visible even when commands and script contents are unchanged. Missing activation records make the affected comparison not comparable. Version probes inspect the environment without compiling a project.

Every run has a new ID and saves configuration, environment observations, actual commands, activation details, logs, exit status, elapsed time, and declared output files. Source code and arbitrary undeclared outputs are not archived automatically. These records support comparison; they are not a complete machine image or automatic source backup. Failed prerequisites block dependent steps; independent steps continue. Foreground execution does not promise survival after SSH disconnection.

## Development and integration

```bash
python3 -m pytest
```

The host integration point is `jarvis_forge.cli.main(argv, prog="Jarvis forge")`; it returns an exit code and owns the single parser. The standalone console script is operational. Jarvis-HEP forwarding is not yet integrated.

The [APT comparison and scientific-library support matrix](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT_COMPATIBILITY.md) describes GAMBIT/ROOT prerequisite adapters and remaining build limitations. Source-layout recognition is separate from a successful build on the target HPC.

Project design and implementation scope live in [Jarvis-Books](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/tree/main/Docs/Jarvis-Forge). CLI behavior follows the [Jarvis ecosystem CLI specification](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Ecosystem/JARVIS_CLI_DESIGN.md).


Recipe sources live in recipes/, separately from the Python distribution. Maintainers build versioned assets with tools/build_recipe_assets.py and publish them to the dedicated recipes Release. See the recipe publishing workflow in Jarvis-Books/Docs/Jarvis-Forge/RELEASING.md.
