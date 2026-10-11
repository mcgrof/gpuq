<!-- SPDX-License-Identifier: MIT -->
# Operations

## Configure the host

Keep the configuration outside the public source tree. Supply it with
`--config` or `GPUQ_CONFIG`; there is no built-in config file location.
Set a deployment alias in `host`, plus explicit local `database` and
`artifact_root` paths. Actual OS hostnames are not automatically recorded.
Each alias must be unique among hosts sharing an archive destination.

The archive is disabled unless `archive_root` is configured. Enabling it
also requires an explicit `archive_mount`: use a mounted destination's
absolute mount point, or explicit `null` for an ordinary local directory.
Optionally set `archive_source` to verify the precise mounted source.

Choose the resource guard and device-selection environment from the
NVIDIA or AMD example. Verify device indices/UUIDs on the host. Different
resource names must refer to distinct devices; declaring the same GPU
twice creates independent reservations for one physical device.

For NVIDIA, select the same GPU with nvidia-smi --id,
CUDA_VISIBLE_DEVICES, telemetry_index, and optionally gpu_uuid.
For AMD, verify the render node and HIP device ordinal. The fuser guard
must see all relevant users' device file descriptors; the example uses
an administrator-approved passwordless invocation of that read-only tool.
If visibility fails, fix the guard rather than treating the device as idle.

The configuration file must not contain secrets. Job command arguments
and descriptions are visible to queue participants; use normal credential
files or your account's credential environment.

Initialize a dedicated state directory. Avoid pointing the database at
your home directory or another directory containing unrelated files:
initialization sets shared-directory permissions. Root, home and data
volumes need not have the same layout across hosts.

Lingering is necessary for unattended user services. After initial setup:

~~~sh
loginctl show-user "$USER" -p Linger
systemctl --user is-system-running
gpuq status
~~~

A degraded manager can mean an unrelated user unit failed; inspect the
specific gpuq service and journal when diagnosing a run.

## Update MOTD

Install the following optional system units, replacing the placeholder
executable, config and banner paths with private deployment locations.

The system's gpuq-motd.service:

~~~ini
[Unit]
Description=Refresh the GPU reservation login banner

[Service]
Type=oneshot
ExecStart=/path/to/installation/bin/gpuq --config /path/to/private/config.json motd --write /path/to/login-banner
~~~

The system's gpuq-motd.timer:

~~~ini
[Unit]
Description=Refresh the GPU reservation login banner periodically

[Timer]
OnBootSec=15s
OnUnitActiveSec=10s

[Install]
WantedBy=timers.target
~~~

Save them in the system unit directory selected by your administrator, then:

~~~sh
sudo systemctl daemon-reload
sudo systemctl enable --now gpuq-motd.timer
~~~

The static banner is a snapshot. The status command reads authoritative
current state. Avoid concurrent manual edits inside gpuq's managed MOTD
block. Unrelated administrator notices are preserved.

## Archive completed runs

Keep recent logs and telemetry on local SSDs for about 30 days. Use the
host's data filesystem or account home as appropriate; do not dump large
job output into root-backed temporary directories.

Preview and archive:

~~~sh
gpuq archive --older-than-days 30 --dry-run
gpuq archive --older-than-days 30 --prune-local
~~~

This handles only gpuq's dedicated artifact directories, not existing
repositories or unrelated logs. Pin needed comparisons.
Archive useful old checkpoints explicitly when active work no longer
needs them. Keep final/best evidence; moving to cold storage is not a
deletion policy.

An optional per-user timer can automate the log policy.

The user's gpuq-archive.service:

~~~ini
[Unit]
Description=Archive old completed experiment logs

[Service]
Type=oneshot
ExecStart=/path/to/installation/bin/gpuq --config /path/to/private/config.json archive --older-than-days 30 --prune-local
~~~

The user's gpuq-archive.timer:

~~~ini
[Unit]
Description=Daily completed-experiment archive check

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
~~~

Save them in your systemd user unit directory. Review a dry run and archive
configuration before enabling the timer. Each user's timer archives only
that user's runs. Checkpoints are not included by this timer.

Keep a bounded system journal for operational logs according to your host
policy. Experiment stdout/stderr is independently saved in each run's
logs directory, so journal rotation is not the archival mechanism.

## Investigate a stalled reservation

~~~sh
gpuq show RUN_ID
systemctl --user status gpuq-RUN_ID.service
journalctl --user -u gpuq-RUN_ID.service
gpuq reconcile RUN_ID
~~~

Reconciliation refuses active services and busy GPUs. Cancel your own job
through gpuq; never delete another user's row or kill a process just
because its estimate expired. A reboot does not automatically restart
experiments; resume from a saved checkpoint as a new run with --parent-run.

## Validate without using a GPU

Use an isolated local database and an additional CPU-test resource:

~~~json
{
  "guard_mode": "exit-zero-idle",
  "guard": ["/usr/bin/true"]
}
~~~

This custom guard is for CPU/control-path tests, not a GPU-idle check.
Test two user accounts submitting to the same resource, cancellation of
a sleeping process tree, and the archive destination independently.

The unit suite tests concurrent FIFO claims, owner checks, slot retention,
archive failures, checkpoint retention and streaming telemetry.
Actual GPU training/inference performance is not a queue benchmark.
