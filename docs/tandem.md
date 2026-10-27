<!-- SPDX-License-Identifier: MIT -->
# Tandem reservations across hosts

`gpuq tandem` coordinates two or more ordinary host-local FIFO jobs. Each
participant holds its reservation at a start barrier. Payloads are released
only after all participants own their resources and have reached that barrier.
This supports workloads such as an encoder on one GPU and a decoder on another.

The coordinator uses your existing SSH identities, JSON requests over SSH,
and a systemd user service. Every participant runs as its submitting account
inside the normal gpuq worker and systemd control group. Queue databases stay
local. There is no shared SQLite file, new network listener or root payload.
Install this gpuq version on the coordinator and every participant first.

## Private plan

Save a deployment-specific plan outside the public source tree:

```json
{
  "experiment": "encoder-decoder",
  "state_root": "/path/to/private/coordination",
  "wait_seconds": 86400,
  "run_seconds": 1800,
  "lease_seconds": 90,
  "members": [
    {
      "name": "encoder",
      "ssh": "node-a",
      "program": ["/path/to/installation/bin/gpuq"],
      "config": "/path/to/private/queue.json",
      "resource": "gpu",
      "cwd": "/path/to/encoder",
      "command": ["/path/to/environment/bin/python", "encode.py"],
      "expected_minutes": 30
    },
    {
      "name": "decoder",
      "ssh": "node-b",
      "program": ["/path/to/installation/bin/gpuq"],
      "config": "/path/to/private/queue.json",
      "resource": "gpu",
      "cwd": "/path/to/decoder",
      "command": ["/path/to/environment/bin/python", "decode.py"],
      "expected_minutes": 30
    }
  ]
}
```

Use SSH aliases already configured for noninteractive access. `ssh: "local"`
executes the protocol locally and is useful for CPU-only integration tests.
The `program` value is an argv prefix, so a wrapper may select installation
and private defaults. `config` is optional when that wrapper supplies it.
Payload commands are argv lists, not shell strings. To use a shell, name its
executable and script explicitly. The normal resource device environment is
inherited by the payload. Use absolute interpreter and working-directory paths.

`state_root` must use host-local persistent storage. A group gets a private
subdirectory there, a stable ID and manifest, and member run IDs allocated
before submission. These records include commands, SSH targets and deployment
paths: keep them private. `show` displays the most recent coordinator snapshot,
not an independent live audit of every host.

```sh
gpuq tandem submit /path/to/private/tandem.json
# The command prints the new group directory.
gpuq tandem show /path/to/private/coordination/tandem-GROUP_ID
gpuq tandem cancel /path/to/private/coordination/tandem-GROUP_ID
```

The coordinator does not require its own GPU queue configuration. Normal
host commands still use their configured local queue. `parent_run` may be
set per participant to retain retry lineage. Payloads receive `GPUQ_TANDEM_ID`
and `GPUQ_TANDEM_MEMBER` in addition to the ordinary run variables.

## Ordering, failures and limits

The coordinator discovers each endpoint's canonical configured `host` and
resource identity and rejects duplicate resources. It first waits without
reserving GPUs until all requested queues have no pending or active jobs, then
acquires resources in sorted order. A busy peer therefore does not immediately
tie up an otherwise idle GPU. Only after an earlier participant reports ready does it submit the
next participant. All coordinators must use the same shared production
configuration for each physical resource, with unique stable host identities.
This consistent acquisition order prevents crossed-reservation deadlocks.
SSH aliases do not determine ordering. Each ordinary local FIFO is respected.

This is coordinated acquisition, not an atomic cluster-wide allocation. An
earlier GPU can sit reserved if a new local submission races admission.
Continuous local submissions can delay tandem admission; there is no global
FIFO across tandem coordinators. `wait_seconds` bounds
that acquisition interval. Once all members are ready, `run_seconds` bounds
execution. The expected-minutes values are still local estimates; they include
any barrier wait after that member acquires its reservation. Release messages
arrive sequentially, so payload launch is not clock-synchronized. Applications
must provide their own server-readiness and protocol barriers.

The coordinator renews short host-local monotonic leases through the protocol.
The lease must cover two rounds of bounded remote calls plus polling; the plan
validator enforces this. A participant checks its lease before starting and
throughout execution. Loss of the coordinator, a failed renewal or an expired
lease stops the participant. The ordinary systemd stop-post finalizer releases
its queue slot only after descendants have drained. A failed member aborts its
peers; group cancellation touches only the group's authenticated member IDs.
The token prevents accidental cross-group control, not same-account attacks.
This remains a cooperative scheduler, not a distributed security boundary.

Unreachable members or ambiguous cleanup leave the group `needs_review` with
its recorded member IDs. Do not infer that resources are free from a stale
group snapshot or elapsed lease. Inspect the actual member queues and systemd
units, and use normal owner-only reconciliation after cleanup is verified.
A coordinator killed before cleanup can leave stale group status; member
leases still bound their execution. Never delete a database or cancel another
user's reservation to recover a group. Do not restart an old coordinator
manifest; submit a fresh plan and use member parent IDs to retain lineage.

Cancellation and deadline failures can take time while units drain. Keep
application descendants in the managed control group. No shared GPU memory,
model/KV compatibility, shared storage, RDMA transport or application retry is
provided by scheduling. Those contracts remain with the experiment.

## Validation

Run `make test` for ordering, duplicate-resource, token/owner, lease-expiry,
failure propagation and ordinary FIFO regression coverage. Changes also need
real systemd user-service checks using isolated local databases and CPU-only
resources. Test multiple participants, cancellation, coordinator loss and a
failed payload before adopting a deployment. Record real deployment receipts
outside the public repository.
