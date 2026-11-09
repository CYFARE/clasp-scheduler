# CLASP KVM benchmark

This directory contains a self-contained scheduler benchmark.  It boots a
kernel with a tiny static initramfs, so no root filesystem, modules, or
installed userspace are required.

## Requirements

Host tools:

* `qemu-system-x86_64` with KVM (`/dev/kvm`)
* `gcc` with static glibc support (`gcc -static` works)
* `cpio`, `gzip`, `grep`, `taskset`, `timeout`

The runner pins QEMU to even host CPU IDs (`0,2,4,...,14`, limited by
`nproc`) and creates an 8-vCPU guest.  This avoids SMT siblings only when
siblings have adjacent IDs, as on the reference host.  Check the host's
`thread_siblings_list` and adjust `CPUS` in `run-vm.sh` for other topologies.

## Usage

```sh
# Build or obtain a bzImage, then:
tests/run-vm.sh /path/to/bzImage my-tag
```

The log is written to `.build/results/my-tag.log`; the full serial output is
also echoed to the console.  The runner exits nonzero if QEMU fails, a benchmark
or control write reports `ERROR`, no `RESULT` is produced, or the expected
`BENCH_DONE`/`QUICK_DONE`/`TUNE_DONE` marker is missing.

The initramfs is generated in `.build/initramfs/` and cached only for the
duration of the run; it is rebuilt on every invocation.

### Phase selection

The runner detects the kernel's control interface:

* `sched_clasp` present: runs `ctrl` (`sched_clasp=0`) and `clasp`
  (`sched_clasp=1`) over the same suite.
* `sched_bore` present: runs `bore-off` and `bore-on`.
* neither: runs one `base` phase.

Run the suite again with `clasp.reverse=1` to reverse the CLASP phase order.
Use both orders for A/B measurements so phase-order effects are visible:

```sh
APPEND="console=ttyS0 rdinit=/init panic=1 quiet clasp.reverse=1" \
    tests/run-vm.sh /path/to/bzImage my-tag-reverse
```

For a short smoke test:

```sh
APPEND="console=ttyS0 rdinit=/init panic=1 quiet clasp.quick=1" \
    tests/run-vm.sh /path/to/bzImage quick
```

`clasp.quick=1` runs a CPU sample plus an aggressive 16-hog ctrl/on
comparison.  The enabled phase sets `target_min_us=100`, `boost_max_us=8000`,
`budget_cap_us=16000`, and `duty_pct=100`.  These are smoke-test settings, not
the defaults used by the full suite.

For a shorter tuning run with the default sysctls, use `clasp.tune=1` instead.
It runs 16-hog IPC and eight-pair pipe with CLASP off and then on, and ends
with `TUNE_DONE`.

## Benchmarks in `latency_probe.c`

The probe binary is statically linked and invoked as:

```text
bench <tag> latency <hogs> <forkers> <interval_us> <seconds>
bench <tag> ipc <hogs> <interval_us> <seconds>
bench <tag> pipe <pairs> <seconds>
bench <tag> cpu <threads> <seconds>
bench <tag> fork <forkers> <seconds>
```

All load is pinned with `sched_setaffinity`:

* `latency`: the main probe thread sleeps with absolute
  `clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME)` and records
  `actual - deadline`.  Hogs are threads pinned to the probe's vCPU.  In a VM
  this includes guest timer-interrupt delivery, so it is reported separately
  from `ipc`.
* `ipc`: a waker thread on vCPU 1 writes a pipe byte to the probe on vCPU 0;
  the probe reads it and timestamps the return.  The waker waits for an ack
  before the next sample.  This measures the wakeup IPI + scheduler latency of
  the loaded vCPU without timer-virtualization jitter.
* `forkers`: child processes fork/exit loop.  Each child runs for ~2 ms.
* `pipe`: each pair has two processes ping-ponging one byte over two pipes.
  Each pair is pinned to one vCPU.  `alarm()` bounds the run.
* `cpu`: one busy thread per vCPU.
* `fork`: each forker repeatedly forks a 2 ms child and calls `waitpid()`;
  the suite uses four forkers.

Each result is a single `RESULT ...` line with p50/p90/p99/p999/max/mean in
microseconds, or messages/operations/forks per second.  p999 uses a separate
per-mille index; it is not an alias for the maximum.

## Interpreting results

* Use `ipc` for scheduler policy comparisons.  `latency` is user-visible but
  timer-delivery dominated in a VM.
* Compare CLASP against its own `ctrl` phase first; this isolates the
  mechanism from compiler/config differences.  Then compare against the
  installed CachyOS kernel and an upstream baseline.
* Pipe/cpu/fork are throughput costs.  A scheduler that improves latency at
  the cost of every context switch is not a win for server workloads.
* A single 4 s run is enough to see a one-slice (2.8 ms) tail, but not enough
  for production claims.  Repeat runs and report distributions.

## Known harness caveats

* The 16550 console can short-write long lines; the init and benchmark use a
  retrying `write_all()` rather than raw `printf`.
* Pipe workers share one atomic message counter.  Cross-CPU cache-line
  contention is therefore part of the pipe throughput result; use it for
  same-kernel on/off comparisons, not as a pure context-switch benchmark.
* `quiet` suppresses `pr_info`; use `loglevel=8` when debugging kernel
  messages.
* The static glibc binaries need no installed userspace and use the vDSO
  `clock_gettime()` when available.
