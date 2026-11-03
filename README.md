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

`--json`, `--home` and `--debug` work before or after the command. `--json` emits one JSON object on stdout, while progress goes to stderr. `compare --check` fails on changed, missing, corrupted or unsuccessful evidence; ordinary comparison only describes differences. Comparison uses exact artifact bytes; scientific equivalence and performance regression policies remain outside the current comparison mode.

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

`info` queries JSON installation manifests under `packages/<name>/<version>/<install-id>/manifest.json`. `jforge apt install` creates those records after installation checks pass. Multiple instances are listed explicitly; `--path` requires a unique match. `--package-version` selects installed software; `--version` remains Forge's version query.

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

`jforge apt` follows familiar apt command names and operates on Forge recipes and user prefixes. It does not invoke system apt, request sudo, or edit shell startup files. Official source-package metadata can be tracked through `apt import`.

```bash
jforge apt --help
jforge apt update
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

Every successful instance gets an immutable receipt and activation script; source downloads, recipe snapshots, command logs and checks live in the operation record. Reinstall and upgrade create separate prefixes and preserve old instances. Build or check failure leaves the previous active environment selected. Valid dependencies completed before a later package fails can remain as inactive installations.

The catalog contains only configured recipes. Add your own `recipes/*.yaml`, or register a local recipe directory/file or HTTPS YAML index, then refresh it:

```bash
jforge apt edit-sources --add /path/to/recipes
jforge apt update
```

Bundled recipes change with Forge releases; `update` refreshes configured external sources. `upgrade` resolves newer compatible versions from that catalog. `full-upgrade` uses the same source-recipe resolver and retains old instances; it performs no system-wide removals. Recipe commands are trusted build instructions, not sandboxed code. See the [package manager design and recipe format](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/APT.md).

### Track official Debian and Ubuntu source packages

```bash
jforge apt import libeigen3-dev --origin debian --suite bookworm --name eigen
jforge apt update
jforge apt list --upgradable
jforge apt upgrade --dry-run
jforge apt upgrade
```

Import maps a binary or source package name through the official `Sources` index, records source versions and SHA-256, and preserves Debian quilt patches. A reviewed Forge template supplies user-prefix build options and checks. Built-in source adapters cover Eigen and Redis; `--template NAME` reuses a custom Forge recipe. Ubuntu sources use `--origin ubuntu --suite <codename>`.

`update` refreshes these tracked imports without compiling. `upgrade` detects both upstream changes and Debian revision/patch changes, creates new instances, and keeps old ones. Identical official metadata does not force a rebuild. Packages without an adapter are reported as pending; standard CMake/configure layouts receive a draft YAML for review. Drafts do not enter the install catalog or silently become valid updates. Debian Build-Depends fields are retained in the report; they are not blindly translated into Forge dependencies.

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

Project design and implementation scope live in [Jarvis-Books](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/tree/main/Docs/Jarvis-Forge). CLI behavior follows the [Jarvis ecosystem CLI specification](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Ecosystem/JARVIS_CLI_DESIGN.md).

