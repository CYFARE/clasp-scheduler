# CLASP KVM benchmark

This directory contains a self-contained scheduler benchmark.  It boots a
kernel with a tiny static initramfs, so no root filesystem, modules, or
installed userspace are required.

## Requirements

Host tools:

* `qemu-system-x86_64` with KVM (`/dev/kvm`)
* `gcc` with static glibc support (`gcc -static` works)
* `cpio`, `gzip`, `taskset`, `timeout`

The host is assumed to have at least two physical CPU threads available for
the guest.  The runner pins QEMU to one hardware thread per physical P-core
(`0,2,4,...,14` on a 0/1 SMT-sibling topology) and creates an 8-vCPU guest.

## Usage

```sh
# Build or obtain a bzImage, then:
tests/run-vm.sh /path/to/bzImage my-tag
```

The log is written to `.build/results/my-tag.log`; the full serial output is
also echoed to the console.

The initramfs is generated in `.build/initramfs/` and cached only for the
duration of the run; it is rebuilt on every invocation.

### Phase selection

The runner detects the kernel's control interface:

* `sched_clasp` present: runs `ctrl` (`sched_clasp=0`) and `clasp`
  (`sched_clasp=1`) over the same suite.
* `sched_bore` present: runs `bore-off` and `bore-on`.
* neither: runs one `base` phase.

For a short smoke test:

```sh
APPEND="console=ttyS0 rdinit=/init panic=1 quiet clasp.quick=1" \
    tests/run-vm.sh /path/to/bzImage quick
```

`clasp.quick=1` runs a CPU sample plus the aggressive 16-hog IPC test (and,
on CLASP kernels, a ctrl/on comparison with aggressive tunables).

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
* `fork`: four processes repeatedly fork a 2 ms child and `waitpid()`.

Each result is a single `RESULT ...` line with p50/p90/p99/p999/max/mean in
microseconds, or messages/operations/forks per second.

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
* `quiet` suppresses `pr_info`; use `loglevel=8` when debugging kernel
  messages.
* The static glibc binaries are intentionally minimal; `alarm()`, pipes, and
  vDSO `clock_gettime()` are the only interfaces used.
