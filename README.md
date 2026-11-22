# Jarvis-Forge

Forge prepares native packages and source-built scientific applications in isolated project profiles. Python installation and version selection belong to pyenv.

The development documentation lives in [Jarvis-Books](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/tree/main/Docs/Jarvis-Forge): [CLI](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/CLI_DESIGN.md), [built-in profiles](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/PROFILE_DESIGN.md), [directory rules](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/PATH_DESIGN.md), and [remaining work](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/PROFILE_LEDGER.md).

## Install and select a profile

Select an existing pyenv Python first, then install the client into that interpreter:

```bash
python3 -m pip install --upgrade Jarvis-Forge
jforge --home /shared/path/forge init
jforge profile check ubuntu24
jforge profile create ubuntu24
jforge profile use ubuntu24
```

The built-in profiles are `ubuntu22`, `ubuntu24`, `ubuntu26` and `macos`. Each owns its compiler tool suite, packages and applications. Linux compiler builds can take a long time; repeating `create NAME` resumes an interrupted setup. macOS uses Apple Clang and audited existing Homebrew formulae, without sudo or Homebrew Python.

The five commands are `profile check [NAME]`, `create NAME`, `use NAME`, `show [NAME]` and `remove NAME`. No platform selection or custom profile names are required. Native operations display their plan and run directly; use `--dry-run` to preview changes.

The standalone `tools/jforge-pyenv-setup.sh` prepares pyenv and installs Forge from PyPI into its base interpreter. Forge itself never creates a Python environment. Select Python before creating a profile; use a separate Forge home when a preset needs another Python identity.

## Packages and applications

The package and application commands share these verbs: `list`, `search`, `show`, `install`, `remove`, `autoremove`, `update`, and `upgrade`. `list` defaults to installed items. `--available` lists recipes. `update` refreshes recipes without upgrading installed software.

```bash
jforge apt search gsl
jforge apt install gsl --yes
jforge apt list
jforge apt show gsl --path
jforge apt upgrade --dry-run

jforge build update
jforge build fetch root
jforge build install root --yes
jforge build list
jforge build show root --path
```

`build fetch APP DIR` creates an external editable workspace. `build install DIR` builds there; the profile records its location but does not load or delete its files. Without DIR, sources, builds and installations belong to the profile. The install command prepares required applications, validates the host and Python, builds and checks outputs, and enables successful installations. Unchanged failed inputs resume automatically.

```bash
jforge build fetch cbs /shared/work/cbs
jforge build install /shared/work/cbs --yes
jforge build install root@6.40.04
jforge build install root --rebuild --yes
jforge build remove root --yes
jforge build autoremove --yes
```

Reinstalling an intact installed version enables it without rebuilding. Changing a version leaves the old installation until autoremove. Same-version rebuilding keeps the original absolute prefix and restores it on failure. Enabled dependent applications prevent conflicting version switches and deletion. External in-place source builds cannot restore modified source binaries.

`apt install --dry-run` performs native dependency resolution and saves a history record. It changes no installed software or profile selection. `apt install --from 0001 --yes` replays that exact lock. Preview operations may create planning evidence, download verified recipe assets and write logs. Homebrew previews use native dependency and dry-run commands.


## Activation and storage

```bash
eval "$(jforge shell-init)"
jforge profile use ubuntu24
source "$(jforge profile show ubuntu24 --path)"
```

Add the hook to your own Bash/Zsh startup configuration to activate successful changes in the calling terminal. Without the hook, source the stable profile entry explicitly. Activation preserves pyenv's interpreter and shell options. Named user scripts remain under `jforge env`; use `jforge env repair` to inspect and repair managed entries without executing arbitrary scripts.

Each profile has `metadata.json`, `profile.yaml`, `activate.sh`, one current `spack/` environment, and `pkg/name-version`, `src/app-version`, `build/app-version`, `apps/app-version`. Each profile owns its compiler bootstrap and tools in `tools/`; only the Spack engine in `spack/spack-version` and verified download caches are shared. Runs and histories use ordered directories such as `0001-install`. Hashes, host identities and timestamps belong in JSON, never allocated directory suffixes.

Compilation defaults to node-local `SLURM_TMPDIR`, `TMPDIR` or the system temporary directory. `profile create --stage DIR` chooses another writable compilation root. Prefixes, downloads, logs and persistent metadata remain under the Forge home. Forge does not write packages into compute-node home directories.

Old commands and old layouts are unsupported. They report errors; there are no compatibility aliases or automatic migrations. Use a fresh Forge home for this development layout.

Native apt/build/profile commands choose parallelism automatically; no `--jobs` option is available. Signed-cache trust failures stop immediately; missing binary archives can fall back to validated source builds. Full raw output stays in operation logs; `--verbose` also streams it to the terminal.

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


## Development

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest
jforge test accept tests/checker.yaml --profile acceptance
```

The opt-in Spack integration tests require an explicitly disposable native store. Local mocked-backend and CMake tests do not install a compiler or contact remote nodes. The UI and per-test reports are separate components described below.

## Per-test reports

The dashboard serves the synchronized Playlists interface and test-report API v1. It requires the new capability contract; no adapter for older dashboard services is provided. Existing execution-step fields retain their meaning and are counted separately from actual pytest items and final-only command checks.

```sh
jforge test discover tests/checker.yaml --profile acceptance --json
jforge test metadata validate tests/metadata/index.yaml --json
jforge test metadata validate tests/metadata/index.yaml --strict --inventory /path/to/inventory.json --json
jforge test metadata scaffold . --output /path/to/new-drafts --json
jforge test report RUN_ID --case INSTANCE_ID --format html
jforge test report RUN_ID --case INSTANCE_ID --rebuild --format json
```

Discovery imports tests only when explicitly requested. Static plan, catalog and metadata-format validation do not collect pytest items. Cached discoveries track selected inputs, metadata, interpreter and installed-distribution identities; incomplete environment observations remain unknown. Reads never rerun collection. Runs use their actual selected inventory. Metadata documents are frozen at run preparation and included in group inputs and reuse identity. Missing metadata is reported explicitly; drafts are never automatically marked reviewed.

Pytest captures exact final per-case call output, phase failures, current-item progress, parameter values, and original reuse references. Fixture setup/teardown captures are marked as shared step evidence. The optional `jforge_evidence` pytest fixture registers a step-owned file or directory bundle with a case and optional criterion/expected-evidence ID. `jforge_observe` records an explicitly instrumented automated criterion; it cannot complete manual review. Existing unregistered outputs remain step artifacts.

Reports and events live under each run step's `test-report-v1/`. A renderer failure leaves the raw outcome unchanged; rebuild preserves the previous presentation revision and execution reuse. Progress reads a persisted snapshot; lost indexes can be rebuilt, and event pagination resumes by byte cursor rather than reloading the complete history. HTTP views are authenticated, script-free reports can be embedded by the same-origin app, and registered relative resources remain restricted to their report capability. Downloading HTML does not bundle linked evidence. CLI and Web read the same persisted facts.

The versioned contract is in `src/jarvis_forge/schemas/test-report.schema.json` and `test-report.openapi.json`; canonical fixtures are in `tests/fixtures/test_reports/v1/`. CTest/CI examples demonstrate data representation, not implemented execution adapters. An automatic retry engine and complete JarvisPLOT metadata authoring are not included. The implementation and acceptance scope are tracked in [Jarvis-Books](https://github.com/Pengxuan-Zhu-Phys/Jarvis-Books/blob/main/Docs/Jarvis-Forge/TEST_REPORT_REQUIREMENTS.md).
