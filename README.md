# Jarvis-Forge

Jarvis-Forge is an independent Python package for user-space software environments and command-based acceptance workflows on scientific workstations and HPC systems. Its CLI is `jforge`.

This initial prototype runs local command suites and preserves their evidence. Package downloading/building, managed environments and services, Slurm submission, scientific comparison policies, and the `Jarvis forge` forwarding command are planned extensions.

## Install from this checkout

Python 3.10 or newer and a POSIX system with Bash are required for execution. Use WSL on Windows. Forge does not depend on Jarvis-HEP.

```bash
python3 -m pip install -e '.[dev]'
jforge --version
jforge --help
```

The distribution name is `Jarvis-Forge`; no PyPI release is published by this scaffold. Build installable archives with:

```bash
python3 -m build
```

## Try the acceptance workflow

These commands create a private Forge user directory under the current directory and write a smoke-suite YAML. The demo uses Python only; it does not download software or start services.

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

`--json`, `--home` and `--debug` work before or after the command. `--json` emits one JSON object on stdout, while progress goes to stderr. `compare --check` fails on changed, missing, corrupted or unsuccessful evidence; ordinary comparison only describes differences. This prototype compares artifact bytes, not scientific equivalence or performance regressions.

## User directories and package queries

Forge uses `~/.jarvis-forge` by default. `--home` overrides `JFORGE_HOME`, which overrides that default. `paths`, `doctor`, `list`, `info`, `validate`, `plan`, `history`, `show` and `logs` do not initialize a user directory or install software.

```bash
jforge paths
jforge doctor
jforge list
jforge info eigen
jforge info eigen --package-version 3.4.0 --instance <install-id> --path
```

`info` queries JSON installation manifests under `packages/<name>/<version>/<install-id>/manifest.json`. At this stage no installer creates those records. There are no preinstalled packages or fabricated Eigen/Redis entries. Multiple instances are listed explicitly; `--path` requires a unique match. `--package-version` selects installed software; `--version` remains Forge's version query.

## Suite format

Suites use `schema_version: 1`, a stable `id`, and ordered command steps. Each step chooses either `argv` or an explicit Bash `script`, with optional `needs`, `cwd`, `timeout_seconds`, `expect_exit_code` and required `artifacts`. Commands and scripts are trusted user code.

The default working directory is the run's shared `workspace/`. Declared artifacts are relative file paths within the step's working directory; each is copied into the run archive. Supported placeholders are `${python}`, `${suite_dir}`, `${run_dir}`, `${step_dir}` and `${input:name}`. Inputs are declared as named file paths relative to the suite file and copied before execution.

An optional `environment.shell` points to a Bash script, relative to the suite file. It is snapshotted and sourced before each command, in the same shell. `environment.variables` supplies string-valued variables. The recorded plan and raw logs may include values supplied by the user; keep credentials out of suite files and command arguments.

Every run has a new ID and saves configuration, environment observations, actual commands, activation details, logs, exit status, elapsed time, and declared output files. Source code and arbitrary undeclared outputs are not archived automatically. These records support comparison; they are not a complete machine image or automatic source backup. Failed prerequisites block dependent steps; independent steps continue. Foreground execution does not promise survival after SSH disconnection.

## Development and integration

```bash
python3 -m pytest
```

The host integration point is `jarvis_forge.cli.main(argv, prog="Jarvis forge")`; it returns an exit code and owns the single parser. The standalone console script is operational. Jarvis-HEP forwarding is not changed in this prototype.

Project design and implementation scope live in [Jarvis-Books](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/tree/main/Docs/Jarvis-Forge). CLI behavior follows the [Jarvis ecosystem CLI specification](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Ecosystem/JARVIS_CLI_DESIGN.md).

