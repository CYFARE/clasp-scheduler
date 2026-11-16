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
`BENCH_DONE`/`QUICK_DONE`/`TUNE_DONE`/`COMPARE_DONE`/`FOCUS_DONE`/
`ADAPT_DONE`/`FLOOR_DONE` marker is missing.

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

For focused overhead measurements, use `clasp.focus=1`. It runs 16-hog IPC and
one-, four-, and eight-pair pipe throughput with CLASP off and then on, and
ends with `FOCUS_DONE`. Add `clasp.reverse=1` to reverse the phase order. Pipe
workers sleep below CLASP's cadence gate, so this suite exercises the
short-cadence fast path and should keep on/off throughput within run noise.

For cadence-transition validation, use `clasp.adapt=1`. It keeps the same IPC
threads alive through 50 µs→1 ms and 1 ms→50 µs transitions, reports each side
separately, runs the pair with CLASP off and then on, and ends with
`ADAPT_DONE`.

For a lower-bound reference, use `clasp.floor=1`. It runs the same 16-hog IPC
probe with CLASP and then with only the receiver in `SCHED_FIFO`, ending with
`FLOOR_DONE`. The real-time result approximates the KVM wakeup/IPI floor; it is
not a fair-scheduling configuration or a supported CLASP operating mode.

For cross-scheduler comparisons, use `clasp.compare=1`.  It enables CLASP or
BORE when the corresponding control exists, runs one default 16-hog IPC test,
and ends with `COMPARE_DONE`.  Upstream and other baseline kernels run the same
probe without changing scheduler controls.  The IPC waker sleeps
`interval_us` after each completed ack instead of catching up an absolute
schedule, so one late sample cannot turn into a burst of sub-100 µs samples.

## Benchmarks in `latency_probe.c`

The probe binary is statically linked and invoked as:

```text
bench <tag> latency <hogs> <forkers> <interval_us> <seconds>
bench <tag> ipc <hogs> <interval_us> <seconds>
bench <tag> ipc-rt <hogs> <interval_us> <seconds>
bench <tag> ipc-adapt <hogs> <interval_us> <later_interval_us> <samples>
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
  and then sleeps `interval_us` before the next sample; it does not catch up
  missed ticks.  This measures the wakeup IPI + scheduler latency of the
  loaded vCPU without timer-virtualization jitter or a self-inflicted cadence
  change.
* `ipc-rt`: uses the same setup but runs only the receiving thread as
  `SCHED_FIFO` priority 1 after creating the normal waker and hog threads.
* `ipc-adapt`: uses the same IPC setup but switches to `later_interval_us`
  after `samples` acknowledgements, then collects another `samples` results.
  The two intervals are reported separately to expose cadence-adaptation bugs.
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
* During development, compare CLASP against its own `ctrl` phase to isolate
  the mechanism.  Release comparisons should instead lead with pristine
  upstream EEVDF built with the same configuration (`CONFIG_SCHED_CLASP=n`).
* A delayed-dequeue wake that may earn credit must pass through
  `place_entity()`; an ineligible wake can use CLASP's lightweight accounting
  path without another EEVDF placement.  Sleeps below the cadence gate use the
  short-cadence fast path, deferring token-bucket refill and latency sampling
  until a wake can pass the gate.  The `ipc` results exercise the eligible and
  ineligible paths, the pipe results exercise the short-cadence path, and
  `ipc-adapt` checks transitions between them.
* Compare CachyOS, BORE, LTS, and other kernels as external references with
  their versions, compilers, and configurations recorded.  Do not present a
  CLASP on/off ratio as a cross-scheduler performance ranking.
* Historical 1.0.0 IPC numbers used catch-up pacing.  Do not mix them with
  1.0.1 results.
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
* The `latency` benchmark still uses an absolute schedule and can take
  back-to-back samples after a long delay.  The `ipc` benchmark deliberately
  does not, because catch-up bursts drove the probe below the 100 µs sleep
  gate and created a false bimodal tail in 1.0.0.
* `quiet` suppresses `pr_info`; use `loglevel=8` when debugging kernel
  messages.
* The static glibc binaries need no installed userspace and use the vDSO
  `clock_gettime()` when available.
