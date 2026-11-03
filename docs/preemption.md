<!-- SPDX-License-Identifier: MIT -->
# Low priority and preemption

A low-priority job uses a resource only while no normal job is waiting.
When a normal job reaches the head of the queue, the low job is asked to
checkpoint and exit; it then goes back to the queue under the same run ID
and run directory, and resumes when the resource is free again.

```sh
gpuq submit --experiment sweep --priority low --expected-minutes 240 \
  --preempt-grace-minutes 15 -- /path/to/env/bin/python train.py
gpuq preempt RUN_ID          # ask your own run (or anyone's low run) to yield now
gpuq preempt --stop RUN_ID   # yours only: checkpoint and end as cancelled
```

## Ordering

Queued jobs claim in the order (normal before low, then submission
sequence). A requeued job keeps its sequence number, so it resumes ahead
of low jobs submitted after it. Normal jobs are never preempted by the
queue; only their owner can ask them to yield.

## The protocol

1. The worker of the normal job at the queue head sets `preempt_requested`
   on the running low job and records an event. Workers run as their own
   users, so nobody signals another user's processes: the low job's own
   worker sees the request at its next poll.
2. That worker writes `$GPUQ_PREEMPT_FILE` (`preempt.json` in the run
   directory: request time, grace, mode) and sends `--preempt-signal`
   (default `SIGUSR1`, or `none`) to the payload's main process only.
3. The payload saves its state under `$GPUQ_RUN_DIR/checkpoints/` at its
   next safe point and exits with a nonzero status (75, `EX_TEMPFAIL`, is
   conventional). An exit status of 0 means the work finished: the run
   completes and is not requeued.
4. If the payload is still running when the grace period ends, the worker
   terminates it as in a cancellation. It is still requeued and restarts
   from its last periodic checkpoint.
5. The finalizer runs after systemd has drained the unit's process tree,
   as for any other job. Only then does it return the row to `queued`,
   which releases the slot, and launch a new unit named
   `gpuq-RUN_ID-rN.service`. Logs append to the same files.

The payload sees `GPUQ_PRIORITY`, `GPUQ_PREEMPT_FILE`,
`GPUQ_PREEMPT_SIGNAL` and `GPUQ_ATTEMPT` (1 on the first start). It should
remove nothing in `checkpoints/` on start; it resumes from what it finds.

## Shell wrappers

The signal goes to the payload's main process only, so that helper
processes such as data loaders or inference engines do not receive a
signal they do not handle. A shell script as the main process must either
`exec` its program, trap the signal and forward it, or ignore it
(`trap '' USR1`) and let the program poll `$GPUQ_PREEMPT_FILE`. An
untrapped `SIGUSR1` terminates bash, and its program would then be killed
by the unit's cleanup before it could checkpoint.

## Upgrading

The new columns (`priority`, `preempt_requested`, `preempt_signal`,
`preempt_grace_seconds`, `requeues`) are added to an existing database on
first use. Older clients still read and write the wider table, but they
ignore priorities and never preempt, so upgrade every client on a host
together, while the queue is empty.
