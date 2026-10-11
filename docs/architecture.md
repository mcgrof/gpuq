<!-- SPDX-License-Identifier: MIT -->
# Architecture

Each host owns its queue. A job claims one configured resource; separate
resources can progress independently. There is no cross-host dispatcher.

Host identity is an operator-supplied alias, not an automatically discovered
OS hostname. Database, artifact and archive paths are supplied by private
deployment configuration. Archival is disabled unless explicitly configured.

## State and ordering

SQLite stores two tables:

- **jobs:** current state and durable run metadata.
- **events:** an append-only lifecycle record keyed by run ID.

Submission assigns an increasing FIFO sequence. A claim uses BEGIN
IMMEDIATE, checks the first waiting row, and transitions it to running.
A partial unique index enforces at most one running/review row per
resource. Estimates and wall-clock timestamps do not determine order.

The database lives on local disk. Queue participants share a setgid state
directory through a Unix group or explicit ACLs. WAL and busy timeouts
coordinate concurrent clients. Group members can write the file directly:
this is a trusted-team protocol, not authorization against hostile members.

## Execution and cleanup

Submission creates a transient service in the submitting user's systemd
manager. Its worker waits for the queue head and a successful GPU-idle
guard, then launches the saved command under that same account.

The service uses KillMode=control-group. Its ExecStopPost finalizer records
completion after systemd cleans up remaining child processes. The worker
itself never releases the reservation. This avoids handing the device
to the next job while a child of the old job is still alive.

The process guard detects existing unregistered GPU jobs. NVIDIA uses
nvidia-smi; the AMD example uses fuser with sufficient process visibility.
Missing tools or errors fail the guard. No processes belonging to other
experiments are killed.

An unconfirmed service launch is retained for review unless the unit is
provably absent. Cancellation marks the request and stops the owner's
unit; the finalizer records the terminal state.

## Recovery

Running rows become completed, failed or cancelled and stay in history.
Needs-review rows continue to occupy the resource.

A crash, reboot or interrupted finalizer can leave a row behind. Neither
an elapsed estimate nor a missing heartbeat frees the slot. The owning
user can reconcile an inactive/failed service only after the guard reports
the device idle. Administrator intervention uses the owner's account and
normal systemd inspection.

## Experiment identity and provenance

An experiment ID groups related research runs. Each invocation has a
unique run ID; retries can record a parent run ID.

The run ID ties together its reservation, user-service unit, artifacts and
archive. A manifest records the command, working directory, Git revision
observed at start, resource environment, timestamps, status and lifecycle
events. It is not a filesystem snapshot: queued code can change before
start. Use immutable checkouts and save framework/package pins when exact
reproduction matters.

## Telemetry

The optional collector loads a separately installed gputop module and
calls GPUMonitor. Samples include UTC time, elapsed monotonic time,
experiment/run IDs and device identity. JSONL appends and constant-size
aggregates keep collection memory bounded.

Stats are device-wide. gputop may report zero for unavailable metrics; an
API-provided zero must not be treated as proof that a sensor exists or a
device consumed no energy. Summary energy integrates available power
samples and is an estimate.

Raw JSONL and summaries are run artifacts. Plotting and conversion to
gputop's legacy graph format are outside the initial implementation.

## Archives

Only terminal, unpinned runs past the completion-time cutoff are selected.
Default categories are logs and telemetry. Checkpoints need an explicit
option. Small local manifests and database rows stay local.

Before local removal, all selected files are copied, fsynced, hashed and
verified. Source signatures and hashes are checked again before any
removal. A failed verification keeps local sources. Existing archive
content with a different checksum is refused.

The archive is a mounted storage destination, not live SQLite state.
Mount verification prevents a missing NAS from becoming an ordinary local
directory that fills the system disk. Archive files and runtime host
configuration are outside this source repository.
