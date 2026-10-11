<!-- SPDX-License-Identifier: MIT -->
# gpuq

A small cooperative GPU queue for shared Linux research machines.

Submit an experiment once, wait your FIFO turn, run it under your own
account, and keep its history when it finishes. Logs and GPU telemetry can
move to cold storage after 30 days; useful checkpoints follow their own
lifecycle.

gpuq uses Python's standard library, a host-local SQLite database, and
systemd user services. It has no network server or root experiment runner.

## Features

- One FIFO queue per configured GPU resource, shared by cooperating users.
- Atomic claims and at most one active job per resource.
- Persistent runs and events, grouped by experiment ID with unique run IDs.
- systemd process supervision and cleanup before releasing a reservation.
- Cancellation, remaining-time estimates, pinning, and interrupted-run recovery.
- Optional NVIDIA/AMD telemetry through [gputop](https://github.com/mcgrof/gputop).
  Samples append to JSONL with bounded in-memory summaries.
- A MOTD snapshot with username first, description, start/end estimate and run ID.
- Completion-time archive selection, SHA-256 verification, and optional local
  removal. Checkpoints are included only when explicitly requested.

This is an early implementation for a small trusted team. Queue members
can write the shared database directly, so membership is a cooperation
boundary, not a security boundary. The CLI checks ownership of operations;
Linux accounts and systemd keep ordinary job execution under the submitting
user. Use a scheduler such as Slurm when you need centrally enforced policy.

## Quick start

An administrator chooses an installation directory and a private configuration
file, then installs the package and configures shared state. Replace all
`/path/to/...` examples with locations suitable for your deployment:

```sh
GPUQ_INSTALL=/path/to/installation
export GPUQ_CONFIG=/path/to/private/config.json
sudo python3 -m venv "$GPUQ_INSTALL"
sudo "$GPUQ_INSTALL/bin/pip" install .
sudo groupadd --system gpuq
sudo usermod -aG gpuq alice
sudo usermod -aG gpuq bob
sudo mkdir -p "$(dirname "$GPUQ_CONFIG")"
sudo cp examples/nvidia.json "$GPUQ_CONFIG"
# Edit this file's host alias, local database and artifact paths before init.
sudo "$GPUQ_INSTALL/bin/gpuq" --config "$GPUQ_CONFIG" init --group gpuq
sudo loginctl enable-linger alice
sudo loginctl enable-linger bob
```

Review the example configuration for your device and storage layout.
Users should log in again after group membership changes. Lingering keeps
their user manager available after SSH disconnects and at boot.

As each research user, put the chosen installation's `bin` directory on
`PATH` and select the shared configuration using `--config` or `GPUQ_CONFIG`.
Submit with your own environment's interpreter:

```sh
export GPUQ_CONFIG=/path/to/private/config.json
TRAINING_PYTHON=/path/to/your/environment/bin/python
gpuq submit --experiment image-classifier --description "optimizer comparison" \
  --expected-minutes 90 -- "$TRAINING_PYTHON" train.py
gpuq status
gpuq history --experiment image-classifier
gpuq show RUN_ID
gpuq estimate RUN_ID --remaining-minutes 30
gpuq cancel RUN_ID
```

The expected end is an estimate. It does not impose a timeout or cancel an
overrunning experiment. Jobs run in the submission directory, with the
resolved executable path and resource's device-selection environment.
Use explicit interpreter paths. Credentials stay in the submitting user's
environment or normal credential files; gpuq does not copy another user's
credentials or snapshot the entire shell environment.

The job receives:

```text
GPUQ_EXPERIMENT_ID=image-classifier
GPUQ_RUN_ID=run-...
GPUQ_RUN_DIR=<configured-artifact-root>/image-classifier/run-...
```

Write checkpoints beneath `$GPUQ_RUN_DIR/checkpoints/` when you want them
associated with this run. Save exact framework/package snapshots there or
with your project; gpuq records the command and source Git revision at
start, but does not infer every framework version.

## Private deployment configuration

gpuq has no built-in machine names, deployment paths or archive mount.
Keep actual deployment configuration and run artifacts outside the public
source tree. The examples contain placeholders, not a working host setup.

| Setting | Meaning |
| --- | --- |
| `host` | Required deployment alias, used in run metadata and archive directories. The OS hostname is not discovered. |
| `database` | Required absolute path on local storage, in a dedicated shared state directory. |
| `artifact_root` | Required local run root; supports `{user}` and `~/` for per-user storage. |
| `archive_root` | Optional absolute archive root. Omit it or set it to `null` to disable archival. |
| `archive_mount` | Required when archival is enabled: an absolute mount point, or explicit `null` for an ordinary directory. |
| `archive_source` | Optional exact mounted source/export to verify before archival. |
| `resources` | GPU guards, device selection and optional telemetry module paths. |

Use a distinct `host` alias for each host sharing an archive destination.
Actual machine names, usernames, paths, device identities and source revisions
in runtime records are private deployment data. Publishing a research artifact
requires a separate review of those records.

## GPU telemetry

Install gputop separately and configure its module path on each resource:

```json
{
  "gputop_module": "/path/to/gputop/gputop.py",
  "telemetry_index": 0,
  "telemetry_required": true
}
```

These keys are added inside a resource in the configuration, not a separate
top-level resource. NVIDIA monitoring also needs `nvidia-ml-py` in the
Python environment running gpuq:

```sh
sudo "$GPUQ_INSTALL/bin/pip" install nvidia-ml-py
```

For NVIDIA, an optional `gpu_uuid` verifies that the selected monitor
matches the reserved device. AMD samples include the PCI device identity.
Readings are device-wide: other users or display activity can contribute to
power and VRAM measurements.

gpuq calls gputop's monitor API directly. It does not redraw a terminal or
rewrite an ever-growing JSON array. Each run gets `telemetry/gpu.jsonl`
and `telemetry/summary.json`. The JSONL format has run/device context and
is distinct from gputop's legacy plotting JSON. Plot/export adapters are
future work.

## Cold storage

Archival is disabled until you configure an archive destination. For a
mounted archive, add paths chosen by your operator:

```json
{
  "archive_root": "/path/to/archive/experiments",
  "archive_mount": "/path/to/archive"
}
```

Then preview the completion-time policy before removing local copies:

```sh
gpuq archive --older-than-days 30 --dry-run
gpuq archive --older-than-days 30 --prune-local
gpuq pin RUN_ID
gpuq unpin RUN_ID
gpuq archive --run-id RUN_ID --older-than-days 30 \
  --include-checkpoints --prune-local
```

Archive selection uses actual completion time and terminal run state,
not file modification time. Pinned runs are retained locally.

Beneath the configured archive root, artifacts use this layout:

```text
<archive_root>/HOST_ALIAS/USER/EXPERIMENT_ID/RUN_ID/
    manifest.json
    logs/
    telemetry/
    checkpoints/    # only with --include-checkpoints
```

Keep active and recent data on local SSDs. Archive old/superseded
checkpoints once they are no longer needed for resume or comparisons;
there is no automatic 30-day checkpoint deletion rule.

Without `--prune-local`, archival keeps local copies. With it, every
selected file must be copied and checksum-verified before any selected
local file is removed. The small local manifest and queue history remain.
A failed mount or verification keeps the local sources. Configure an exact
`archive_source` if you also want to verify the mounted device/export.

The SQLite database always stays on local storage. To archive on a regular
local directory instead of a mount, explicitly set `archive_mount` to
`null`. No cleanup timer is enabled by installing the package.

The cold manifest includes the saved source provenance, lifecycle events
and file checksums, so the archived artifacts keep their experiment context.

## MOTD and unattended operation

```sh
gpuq motd
sudo gpuq --config "$GPUQ_CONFIG" motd --write /path/to/login-banner
systemctl --user status gpuq-RUN_ID.service
journalctl --user -u gpuq-RUN_ID.service
gpuq reconcile RUN_ID
```

MOTD generation preserves text outside its managed block. Configure the
sample [MOTD timer](docs/operations.md) to refresh the snapshot. The
database is the source of truth; do not edit queue entries in MOTD once
gpuq is in use.

Systemd's stop-post hook records completion after the service's child
processes have been cleaned up. Missing heartbeats do not free a GPU.
After an interrupted finalizer or reboot, `reconcile` requires your unit
to be inactive/failed and the GPU guard to report idle.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
./bin/gpuq --help
```

See [architecture](docs/architecture.md), [operations](docs/operations.md)
and [CONTRIBUTING](CONTRIBUTING). The [initial validation record](docs/validation.md)
describes the unit suite, real user-service checks, GPU collector probes
and their scope.

## Why another queue?

[GPU Task Spooler](https://github.com/justanhduc/task-spooler) provides
GPU-aware scheduling; its GPU implementation uses NVIDIA NVML.
[Pueue](https://github.com/Nukesor/pueue) is a useful shell-command queue,
but explicitly places distributed and multi-user scheduling outside its
scope. [Slurm](https://slurm.schedmd.com/gres.html) supports NVIDIA and AMD
GPU scheduling and accounting and is the established option for a
centrally managed cluster.

gpuq keeps the policy small: cooperative per-host FIFO, experiment
identity, existing systemd execution, vendor-neutral telemetry through
gputop, and verified cold archives.

## License

MIT, with SPDX identifiers and a license text under
[LICENSES/preferred/MIT](LICENSES/preferred/MIT), following gputop's
licensing style. Contributions use the Developer Certificate of Origin.
