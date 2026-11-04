# Changelog

## 1.0.11

- Activate successfully installed binary packages by verifying the requested roots and their transitive link and run dependencies. Build-only tools remain in the full plan and lockfile.

## 1.0.10

### Fixed

- Prevent Spack installs launched from interactive SSH terminals from failing with `Inappropriate ioctl for device`. Detached package commands now receive closed input instead of inheriting a terminal owned by another session; live stdout/stderr logs and process-group timeout cleanup remain enabled.
- Record the noninteractive input mode in command evidence. Existing package stores, cached archives, profiles and Python selections remain usable; retry failed installs without resetting the Forge home.

## 1.0.9

### Added

- Use managed Spack as the only package backend, with checksum-pinned engine/recipe archives, native dependency solving, compiler discovery, signed binary-cache reuse and live installation logs.
- Add `apt import spack`, named `apt switch spack` environments, `apt compilers`, `apt check`, and binary mirror/key management through `apt sources`. Accept native Spack specs and common Debian development-package aliases.
- Save concrete dependency lockfiles, configuration snapshots, package hashes, installation logs and reversible shell activation for each successful environment revision. Read package lists directly from a local name cache.
- Query retired source installations with `list --archived`, `info --archived`, `apt list --archived`, and `apt switch --archived` without executing old recipes or shell scripts.

### Removed

- Remove the Ubuntu/Debian source backend, custom dependency resolver, source-recipe generation and build adapters, bundled package YAML recipes and recipe schema. Remove the `python-debian` and `packaging` runtime dependencies.
- Remove source-specific import/switch/edit options. `apt import` and `apt switch` accept Spack; `apt sources` manages Spack mirrors and `apt edit-sources` displays them. Old source backend modules are no longer importable.

### Compatibility and upgrade notes

- Existing source prefixes, shell files, manifests and operation logs stay on disk as read-only history. Package commands resolve an old source selection to the default Spack profile and save the new selection after successful preparation. Packages must be installed through Spack before they can satisfy its dependencies; old source environments cannot be activated, upgraded or removed through the package CLI.
- Existing Spack profiles and direct Spack homes retain their data. Explicitly run `jforge apt import spack` to prepare/select the default profile. User-selected pyenv Python remains an external dependency.
- Spack normally uses the host glibc and requires host build tools when compatible binaries are unavailable. This backend does not create an Ubuntu user space. `--cache only` requires binary packages; `auto` may build from source.
- Package names/versions follow Spack recipes. `upgrade` updates the entire locked environment; use `install SPEC` to change a root. Offline dry runs preview requests, while `apt check` concretizes dependencies without running project acceptance tests. `list --upgradable` directs users to dependency planning.
- Removing Spack roots retains prior environments and store entries; garbage collection preserves saved environment revisions. Reinstall uses native Spack overwrite semantics and can modify prefixes used by historical revisions.
- Reload existing Bash/Zsh integration once with `eval "$(command jforge apt shell-init)"`. The hook refreshes environments after successful imports, switches and package changes; Forge does not edit startup files.

## 1.0.8

### Fixed

- Keep Forge package names, exact versions and Debian constraints in a Forge-owned requirement record. Parse Python constraints through public packaging APIs without adding or changing attributes on third-party Requirement objects.
- Send real line breaks to Tcl during generated source-tool and timezone checks; avoid double-escaped input that prevents valid Tcl installations from passing verification.
- Let explicit native contracts provide their documented CLI, library and header aliases together. Reuse an existing Tcl development provider for SQLite's Tcl interpreter requirement without claiming unsupported library ABIs.
- Remove rejected automatic recipes from available candidates when metadata refresh determines that their output needs an adapter.
- Retain obsolete automatic discovery branches as inactive metadata once all known user roots resolve. Preserve explicit requests, installed packages and history; reactivate a saved branch on later explicit installation using the current source keyring.

### Compatibility and upgrade notes

- Existing Ubuntu source selections and user directories remain active. After upgrading, run `jforge apt update`, then retry `jforge apt install sqlite3`.
- Automatic adapter revisions refresh cached schema 2 recipes and can rebuild affected packages. Historical unrelated pending entries can become inactive once the complete root graph resolves; unresolved explicit requests remain reported.

## 1.0.7

### Fixed

- Read unified-diff structure without decoding patch bodies as UTF-8. Preserve original patch bytes, including legacy translations, and retain path-escape checks. Invalid quilt-series filenames produce a contextual error.
- Give SQLite and its Readline, ncurses, Tcl 8.6 and zlib prerequisites explicit native build contracts. Keep actual library and source-generation dependencies while excluding distribution-only documentation, packaging and multilib steps; retain SQLite CLI Readline support.
- Build narrow and wide ncurses libraries, install Tcl's bundled timezone data, and check SQLite with a real SQL command after installation. Record every dependency adaptation; unknown prerequisites remain required.
- Regenerate cached automatic recipes after adapter-policy changes, including existing schema 2 recipes. Accept structured official version constraints during dependency discovery and tolerate non-UTF-8 tool-version output.

### Compatibility and upgrade notes

- Run `jforge apt update` after upgrading, then retry `jforge apt install sqlite3`. Existing installation history and source environments remain available. Automatic adapter changes can cause affected packages to rebuild.
- Native contracts build user-prefix upstream outputs. They do not reproduce Debian packaging, documentation, extra architectures or SQLite's Tcl extension; Tcl remains a required source-generation tool. Availability on a particular HPC still depends on its selected compiler and build tools.

## 1.0.6

### Added

- Add `apt switch` for persistent, source-isolated Debian and Ubuntu environments, with separate package prefixes, recipe catalogs, caches, installation histories and activation scripts. List saved environments with `apt switch`; return to the original store with `apt switch legacy`.
- Default new Ubuntu environments to Ubuntu 24.04 LTS (`noble`), main and universe. Refresh base, updates and security pockets together and select the highest full Debian package version.
- Support native source archives and named original-source components, including SQLite's separate `www` archive, mounted before quilt patches.
- Recognize nested CMake/configure layouts and Meson builds; add OpenSSL, dependency-metapackage and Boost development-header adapters, including modular Boost source trees.

### Fixed

- Restore the top-level installed-package list and keep package queries, `paths` and the reserved apt environment aligned with the selected source.
- Remove previous managed package paths and restore exported values when switching, upgrading or removing packages, while preserving user PATH additions. Refresh the Bash/Zsh hook after successful package changes and source switches.
- Fetch repository indexes by content hash when available to avoid mirror synchronization races; retain checksum verification and detect modified cached indexes without repeatedly hashing unchanged distribution indexes on list/search.
- Preserve full Debian revisions and epochs during dependency matching and upgrade selection, avoid downgrades, reuse validated installed recipes after catalog removal, and retain unrelated installed dependencies during ordinary installs.
- Roll back catalog metadata when updates fail; retain the previous source selection when a switch fails and preserve configured signature keyrings when returning to an environment.
- Accept safe internal archive links and valid source filenames while rejecting extraction escapes. Distinguish native source-build runtime requirements from the ABI minimums of distribution-built binaries.
- Show readable update summaries and pending reasons; preserve complete paths, commands and headings in narrow SSH terminals.

### Compatibility and upgrade notes

- Existing user directories remain the `legacy` environment and are not moved or deleted. New source environments live under the same user directory's `apt/profiles/`; named Python environments and acceptance histories stay shared. Custom recipes and recipe-source settings belong to each package environment.
- After upgrading, reload shell integration once with `eval "$(command jforge apt shell-init)"`, or start a new shell if that line is already in its startup file. Subsequent installs and switches activate automatically in that shell. Other already-open shells retain their environment until refreshed.
- Run `apt update` in each environment to refresh tracked recipes and dependency metadata. Source layouts and dependency translation do not establish that every repository package builds on every HPC host; compiled Boost components, system services, bootstrap cycles and unsupported provider mappings still need explicit adapters.
- Metadata trust remains official HTTPS plus checksums by default; pass a trusted `--keyring` to require archive-signature verification. Forge builds upstream sources and does not execute dpkg maintainer scripts or replace the host libc.

## 1.0.5

### Added

- Resolve dependency graphs before compilation, verify installed or reused providers, and block consumers until their dependencies are ready.
- Add recipe schema 2 with build/run dependencies, lightweight probes, consumer dependency checks and integrity checks for declared artifacts.
- Import native Linux binary dependency metadata alongside official source indexes; retain Debian version constraints and alternative dependencies, and map related binary outputs to source providers.
- Record dependency discovery, external provider probes, per-package readiness and blocked states in operation history.

### Fixed

- Skip missing documentation when building repacked Debian GSL sources and verify the installed development files.

### Changed

- Show package and command step progress with live build output on stderr while preserving separate stdout and stderr logs.

### Compatibility and upgrade notes

- Run `jforge apt update` after upgrading to refresh tracked recipes and download binary dependency metadata. Schema 1 recipes remain supported; schema 2 requires this release or newer.
- Dependency fingerprints now include resolution and provider evidence. Existing packages may rebuild once; reused instances run their probe, or their legacy check when no probe is declared.
- Official dependency discovery supports native Linux on amd64, arm64, ppc64el, s390x and riscv64. Cross-architecture requests, ambiguous virtual providers, source bootstrap cycles and unsupported package layouts require explicit adapters and stop before compilation.
- Native tool probes use documented upstream-version mappings; exact Debian binary equality requires a dedicated provider. Generic library probes check installed files; consumers can declare stronger compile/link checks.

## 1.0.4

### Added

- Add an opt-in Bash/Zsh hook to activate the package environment after installation.

### Changed

- Cache parsed official source metadata and print large package lists compactly.

## 1.0.3

### Fixed

- Accept Neofetch 7.1.0's expected version output when its `--version` command exits with status 1, while rejecting unexpected output or other exit statuses.
- Refresh tracked official recipes with `jforge apt update` after upgrading Forge to pick up the corrected Neofetch check.


## 1.0.2

### Added

- List supported Debian and Ubuntu sources with `jforge apt sources`, then import the chosen source with `jforge apt import debian` or `jforge apt import ubuntu`. Refresh the complete source index with `apt update` and query package names through `apt list/search/show`.
- Look up missing recipes in the configured official repository during installation, download verified source archives on demand and track generated recipes for future updates and upgrades.
- Generate user-prefix build recipes for standard CMake, configure and prefix-aware Makefile layouts, and install Neofetch through its Bash command layout.
- Accept Debian-style package names containing `+` or dots, including compiler and versioned Python package names.
- Provide `apt update --recipes-only` and `apt install --no-official` for recipe-only workflows. Dry-run reports pending official lookups without downloading files.

### Compatibility and upgrade notes

- Official repositories require explicit import. Debian defaults to bookworm/main and Ubuntu to noble/main; existing imported configurations are retained. Repository import downloads metadata; it does not install every package or fetch every source archive.
- Official index availability does not guarantee that every package has a supported source layout or satisfied build dependencies. Unsupported packages retain their metadata and adapter report; Debian Build-Depends is recorded without blindly installing system packages.


## 1.0.1

### Added

- Remember the directory selected by `jforge init --home PATH` as the user's default across terminals and Python environments.
- Show the default-location configuration file in `init` and `paths` output.

### Compatibility and upgrade notes

- Home selection now follows `--home`, `JFORGE_HOME`, the remembered default, then `~/.jarvis-forge`, in that order. An explicit `init` saves its successfully initialized directory; other commands do not change the remembered default.
- Default-location preferences are stored in the user configuration directory, independently of the selected data directory. Existing data is not moved. After upgrading from 1.0.0, run `jforge init --home /path/to/existing-store` once to remember your chosen store.

## 1.0.0

### Added

- Provide the independent `jforge` CLI for user-space package installation, environment inspection and command-based acceptance workflows, with structured JSON output.
- Keep software receipts, named environments and run history in one shared user directory, independent of the Python installation running Forge.
- Install, reinstall, remove and upgrade source packages under private user prefixes with `jforge apt`; include pinned Eigen and Redis recipes, version constraints, dependency resolution and activation scripts.
- Import Debian and Ubuntu source metadata and quilt patches through reviewed build adapters, then track upstream versions and packaging revisions with `apt update` and `apt upgrade`.
- Register, list and inspect named environments with `env add/list/show`; preserve private shell copies and immutable revisions, with explicit executable or pyenv selections through `env set-python`.
- Inspect compiler paths and versions, Python interpreters and packages, and selected pyenv/module variables with `doctor`; select a registered environment explicitly using `--env` or acceptance YAML.
- Run versioned YAML command suites with step dependencies, environment scripts, explicit working directories, timeouts, process cleanup and declared output checks.
- Archive suite configurations, inputs, commands, activated environments, stdout/stderr, exit status and output files for each run; query history and compare recorded evidence with `compare` and `compare --check`.

### Compatibility and upgrade notes

- Forge requires Python 3.10 or newer and a POSIX system with Bash. Use existing compiler tools, site modules and pyenv environments; target Python environments do not need Forge installed.
- Environment selection is explicit. `${python}` refers to Forge's own interpreter; use `python` or `python3` for the interpreter selected by an environment script or named environment.
- Existing prototype user directories remain in use. Comparisons include activated tool and Python versions; missing activation evidence makes a comparison not comparable.
- Package installation uses recipe-defined source builds. Bundled official-source adapters cover Eigen and Redis; other imports may require a reviewed recipe. Official metadata uses HTTPS and checksum verification by default; provide `--keyring` to require archive-signature verification.
- Commands run locally in the foreground. Slurm submission, SSH-disconnect recovery, automatic service management and scientific comparison policies are not included. Installing Redis does not start a server.

## 0.1.2

- Import and track source metadata from official Debian and Ubuntu repositories.
- Refresh tracked recipes with `apt update` and detect Debian patch/revision changes with `apt upgrade`.
- Preserve and apply quilt patch series before building source packages into user prefixes.
- Record repository origin, suite, source versions, index/archive checksums and signature-verification status.
- Keep unsupported imports pending and save standard CMake/configure drafts without activating them.

## 0.1.1

- Add the user-local `jforge apt` command group with catalog search, package details, installation, reinstall, removal, autoremove, source updates, upgrades and version constraints.
- Bundle pinned Eigen and Redis source recipes and support local and HTTPS recipe indexes.
- Verify archive checksums, reject unsafe archive entries, resolve recipe dependencies and record real installations under user prefixes.
- Generate versioned activation scripts and preserve working instances across failed builds and upgrades.
- Record source recipes, commands, stdout/stderr, checks and removed receipts for package operations.

## 0.1.0

- Create the independent Jarvis-Forge distribution and `jforge` CLI.
- Add private user-directory initialization, tool discovery and package-manifest queries.
- Validate versioned YAML command suites and preview execution without running commands.
- Execute local acceptance steps with dependency ordering, environment scripts, timeouts and process-group cleanup.
- Preserve configuration, commands, input snapshots, stdout/stderr, exit status and output artifacts for every run.
- Query run history and compare archived evidence without repeating tests.
- Bundle a Python-only smoke suite and a host CLI integration entry point.

