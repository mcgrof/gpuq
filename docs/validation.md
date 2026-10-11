<!-- SPDX-License-Identifier: MIT -->
# Initial validation

The initial implementation was checked on Linux with real systemd user
managers, two Unix accounts and a separately installed gputop checkout
at revision `6ef6d04`. CPU-only service commands and read-only GPU sampling
were used. Runtime configurations and research artifacts stay outside
this source repository.

| Check | Observed result |
| --- | --- |
| Unit suite | 33 tests pass, including explicit deployment configuration, concurrent FIFO claims and archive failure paths. |
| Multi-user FIFO | A second account's job started after the first account's job finished. |
| Descendant cleanup | A sleeping descendant left by the command was gone before the next reservation started. |
| Cancellation | Cancelling a managed job removed its sleeping descendant and recorded a terminal cancelled run. |
| Virtual environment | A submitted virtual-environment interpreter imported its installed PyTorch package under the submitting account. |
| NVIDIA collector | Device-wide samples, device identity and a successful summary. |
| AMD collector | Device-wide samples, PCI identity and a successful summary. |
| NFS archive | Logs and telemetry copied to a mounted NFS export, passed SHA-256 verification and were removed locally; the local manifest and queue history remained. |
| Package | Installation exposes the gpuq command and includes the MIT license files. |

The AMD monitor returned zero for power on the tested host. This does
not establish zero power consumption or usable energy accounting. Sensor
availability depends on gputop and the driver; inspect the raw measurements
before using summary energy in comparisons.

The live FIFO, cleanup and cancellation checks used the NVIDIA machine's
user-service managers with a CPU test resource. The AMD machine qualified
the collector only. GPU training performance, AMD scheduling guards,
large queues and sustained multi-day runs need separate qualification.

Repeat the unit and package checks with:

```sh
make check
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/gpuq --version
```

See [operations](operations.md) for an isolated CPU test resource and
the service-level checks to repeat when changing systemd integration.
