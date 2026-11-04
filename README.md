# Jarvis-Forge

Jarvis-Forge is an independent Python package for user-space software environments and command-based acceptance workflows on scientific workstations and HPC systems. Its CLI is `jforge`.

Forge runs local command suites and preserves their evidence. Its `jforge apt` command delegates package recipes, dependency resolution, binary-cache reuse and builds to a pinned, user-local Spack installation. Spack is the only package backend; old source installations remain queryable as archived records. Named environments keep private, versioned shell copies and explicit target Python selections in one shared user directory. Managed services, Slurm submission, scientific comparison policies, and the `Jarvis forge` forwarding command are planned extensions.

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

Text results use standalone headings and record summaries suitable for SSH terminals. Paths and commands are emitted as complete lines; the terminal may visually wrap them without inserted line breaks. `apt update` refreshes the selected backend's recipe metadata. Full structured fields remain available with `--json`.

`--json`, `--home` and `--debug` work before or after the command. `--json` emits one JSON object on stdout, while progress goes to stderr. `compare --check` fails on changed, missing, corrupted or unsuccessful evidence; ordinary comparison only describes differences. Comparison uses exact artifact bytes; scientific equivalence and performance regression policies remain outside the current comparison mode.

## Automatic package environment (Bash and Zsh)

A child process cannot change its parent shell. Enable the shell integration once in each interactive session:

```bash
eval "$(command jforge apt shell-init)"
```

Add that line to your own `~/.bashrc` or `~/.zshrc` to enable it for future shells. The hook sources the selected package environment on startup and refreshes it after successful installs, upgrades, removals and source switches. It uses the selected Forge home, including an explicit `--home`. Forge does not edit your startup files. Existing shells need to enable the hook themselves. Source switching removes previous managed package paths and restores exported values while preserving user PATH additions. After upgrading Forge, reload this hook once in existing shells to obtain its new behavior; other already-open shells keep their current environment until refreshed. The hook is intended for interactive use, not JSON or script automation.

Spack package names are cached during import/update. Listings read that local cache without starting Spack or accessing the network; complete distribution listings are printed as compact lines. `apt list --installed` lists packages in the selected Spack environment. `jforge list` also includes its saved installation instances.

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

## User-local packages with Spack

All package operations use Spack. Existing source installations are retained as read-only history; the first package write prepares the default Spack profile:

```bash
jforge apt import spack
# Or prepare and select a named package environment:
jforge apt switch spack --name gambit --python "$(pyenv which python)"
jforge apt compilers
jforge apt compilers --find
jforge apt list
jforge apt search eigen
jforge apt show libgsl-dev
jforge apt check sqlite3 libgsl-dev libeigen3-dev
jforge apt install sqlite3 libgsl-dev libeigen3-dev --jobs 4
jforge apt list --installed
jforge info libgsl-dev --path
```

`import spack` selects `spack-default`. `switch spack --name NAME` selects a separate store, configuration and saved environments under the shared Forge home. Re-selecting an existing environment retains its configuration and installations. `--python` registers an existing interpreter, including pyenv Python, as an external dependency; Forge does not install or switch Python. The interpreter running Forge controls Spack itself. Select compilers/variants explicitly with native quoted specs when needed:

```bash
jforge apt install 'gsl@2.8 %gcc@13' 'hdf5~mpi' 'root +python'
jforge apt install sqlite3 --cache only
jforge apt sources
jforge apt sources --add site --url https://example.org/spack-cache
jforge apt sources --trust-key /path/to/site-signing-key.asc
jforge apt update
jforge apt upgrade
jforge apt remove sqlite3 --yes
jforge apt switch
```

Common Debian development names are aliases to Spack names, for example `libgsl-dev` → `gsl`, `libeigen3-dev` → `eigen`, and `sqlite3` → `sqlite`. Native Spack specs and `name=version` are accepted. This does not import Ubuntu/Debian binary packages: Spack owns the dependency graph and uses its own recipes and signed build caches. Search/list use cached package names; show fetches detailed metadata from the managed local Spack installation.

Spack 1.2.2 and an engine-compatible recipe commit are bootstrapped from checksum-pinned official archives, without Git checkouts. Import requires HTTPS access, curl/wget, Bash, and a working Python. Source builds require a usable host compiler and relevant platform tools. The solver may bootstrap into the managed directory. Spack normally uses the host glibc; Forge does not build glibc or promise an isolated Ubuntu user space. AlmaLinux users use a compiler module or Spack compiler package as appropriate for their site.

`--cache auto` reuses compatible binaries and permits source builds; `only` refuses source builds for requested packages/dependencies; `never` requires source builds. Spack's own solver/tool bootstrap is separate. Official cache signing keys are installed from the named official mirror. Custom binary mirrors require the user's trusted key; signature checking stays enabled. Cache availability depends on OS, architecture, compiler and the exact dependency graph. Public cache availability is not guaranteed.

Each successful operation saves the concrete `spack.lock`, a configuration snapshot, package hashes and prefixes, stdout/stderr logs, and a new activation revision. Forge commits the selected environment only after Spack reports its packages installed. A failure retains the previous selection and logs; successful dependencies may remain in the store for reuse. Spack emits live installation logs and package status, while Forge reports overall phases. `--json` keeps these on stderr.

`apt check` concretizes and archives a dependency plan without building packages or changing the selected environment; it is not a package integrity audit or a project acceptance test. `--dry-run` is an offline request preview without solver execution. `upgrade` re-solves all selected roots against current recipes; change one root with `install SPEC`. `list --upgradable` is unavailable because upgrades require solving the environment. `reinstall` uses Spack's overwrite semantics for the same hash and can change files used by saved revisions.

`remove` drops explicit roots from the selected environment and retains the store and previous revisions. `autoremove --yes` delegates garbage collection to Spack while retaining all saved named environments and their build dependencies. It therefore does not reclaim packages still needed by historical revisions. Neither command removes external pyenv/system packages.

The Ubuntu/Debian source resolver, source-package importer, custom build adapters and YAML package recipes have been removed. `apt import ubuntu/debian`, `apt switch ubuntu/debian/legacy`, and their source-specific options no longer activate a backend. `apt sources` now manages Spack mirrors, and `apt edit-sources` displays those mirrors.

Existing source prefixes, manifests, shell files and operation logs stay on disk without conversion or automatic deletion. An old source selection routes package commands to `spack-default`; a successful write saves the new selection. Query old records explicitly:

```bash
jforge list --archived
jforge info libgsl-dev --archived --path
jforge apt list --archived
jforge apt switch --archived
```

Archive queries read records without running old shell or recipe commands. Archived records do not satisfy Spack dependencies and cannot be upgraded or removed by the package CLI. Reinstall the packages you need with Spack. Previously named Spack profiles keep working; select an existing root-level Spack store again with `apt switch spack --direct`. Reload `apt shell-init` once after upgrading; subsequent successful package changes automatically refresh the current shell.

See [APT usage](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT.md), [backend design](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT_DEPENDENCIES.md), and [compatibility](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT_COMPATIBILITY.md).

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

