# Run limits

A hung job holds its resource until someone notices. A distributed
trainer whose worker crashed, for example, can stay alive indefinitely
with the GPU idle. The expected runtime given at submission is only an
estimate. Two opt-in limits let the queue end such a run itself.

```sh
gpuq submit --experiment demo --expected-minutes 20 \
  --timeout-minutes 40 --idle-minutes 10 -- /path/to/python train.py
```

- `--timeout-minutes T` ends the payload once it has run for T minutes.
- `--idle-minutes N` ends the payload once the device has stayed at or below
  `--idle-percent` utilization (default 5) for N minutes in a row.

Both are per run, and neither is set by default. When a limit is reached, the
worker records a `limit_reached` event, terminates the payload's main process
and exits. systemd then drains the rest of the unit's process tree, exactly as
for a cancellation. The run ends `failed`, with `service_result` set to
`timeout` or `idle`; `outcome.json` records the payload's exit code and the
limit.

## Choosing values

The idle clock starts when the payload launches, so N must cover any
startup phase that leaves the device unused: model downloads, data
preparation, compilation, CPU-only evaluation. Some phases of a working
job also leave the device idle, such as checkpoint writes, rollout
generation on another device, or waiting on a remote service. Choose N
longer than the longest such phase.

The idle reading is device-wide gputop telemetry. Utilization on AMD
devices can read zero while the device is runtime-suspended, and a zero
reading also counts as idle. Without telemetry (no gputop module, or the
collector failed), idle time is unknown. The idle limit is then not
enforced, and the worker logs that once at launch; unknown is never
treated as idle. The timeout needs no telemetry.

## Requeued runs

Limits apply per attempt. A preempted low-priority run that is requeued
starts a fresh timeout and idle clock. Once a preemption notice arrives,
the limits stop applying to that attempt: the grace period decides, and
the run is requeued rather than failed.
