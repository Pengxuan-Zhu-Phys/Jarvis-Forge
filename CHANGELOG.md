# Changelog

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

