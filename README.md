<div align="center">

# CLASP

### Closed-Loop Adaptive Scheduling Policy

**Latency-aware scheduling for Linux EEVDF.**  
*Less guessing. More measuring. Better responsiveness under load.*

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Linux](https://img.shields.io/badge/Linux-7.3.3-FCC624?logo=linux&logoColor=black)
![Status](https://img.shields.io/badge/status-stable-brightgreen)
![License](https://img.shields.io/badge/license-GPL--2.0-green)

[Why CLASP?](#why-clasp) ·
[How it works](#how-it-works) ·
[Results](#scheduler-comparison) ·
[Install](#build-and-install) ·
[Tune](#tunables) ·
[Test](#test-harness)

</div>

CLASP 1.0 is the stable release of this Linux EEVDF latency controller, targeting **measured wake-to-run latency**.

Instead of guessing whether a task is interactive from burst-time heuristics, CLASP asks a simpler question:

> **Did this task actually run on time?**

If not, CLASP grants a bounded amount of virtual-time credit, moving the task earlier in EEVDF scheduling order. As latency recovers, boost decays above a bounded credit floor.

> [!WARNING]
> CLASP's reference integration targets **upstream Linux 7.3.3**, and
> `distrib/run.py` rebases it to newer stable kernels. The 1.0 designation marks
> the release and integration interface; it is not a guarantee for every
> production workload. Current performance results come from one KVM host.
> Four of seven CLASP IPC comparisons hit a 2.6–2.8 ms p99 tail, so this
> release must not be presented as having stable performance on that evidence.

## Why CLASP?

- 🎯 **Closed-loop control** - reacts to observed latency, not assumed task behavior.
- 🚫 **No per-tick accounting** - keeps the scheduler hot path lightweight.
- ⚖️ **No weight or PELT changes** — leaves `se->load.weight`, PELT averages, and hierarchical load untouched.
- 🪙 **Explicitly bounded boost** - a per-task token bucket limits extra CPU time.
- 🌱 **O(1) fork handling** - no child-list scans or ancestor walks.
- ⚡ **Static-key bypass when disabled** — wake/sleep hooks are skipped; O(1) fork initialization remains.

### Headline result

Across seven CLASP 1.0.0 comparisons with **16 CPU hogs**, the best IPC p99 was **13.0 µs**, but the median was **2693.8 µs** because four runs reached the one-slice tail. All runs are included below—successful runs are not cherry-picked.

| CLASP IPC p99 | Best | Median | Worst |
|---|---:|---:|---:|
| Seven independent runs | **13.0 µs** | **2693.8 µs** | **2797.4 µs** |

See [Scheduler comparison](#scheduler-comparison) for the upstream EEVDF, CachyOS, BORE, and LTS results, throughput, and validation limits.

## How it works

![CLASP task state, control loop, guardrails, and EEVDF integration](assets/CLASP-Diagram.webp)

```text
Sleep cadence ──► Latency target
                       │
Wake-to-run sample ────┼──► Target missed?
                       │          │
                       │          ├─ No ──► Hold, then decay above floor
                       │          │
                       │          └─ Yes ─► Increase boost + mark URGENT
                       │                     │
                       │                     ▼
                       │          Bounded virtual-time credit
                       │                     │
                       │                     ▼
                       │          Earlier EEVDF position/deadline
                       │                     │
                       └────────────► Eligible preemption when needed
```

### 1. Learn the task’s cadence

Each task keeps a **96-byte context on x86_64** containing run, sleep, latency, boost, budget, and fork-lineage state.

Its latency target comes from voluntary sleep duration:

```text
target = clamp(sleep_avg / 4, 250 µs, 8 ms)
```

Examples:

- A task waking every **1 ms** receives the **250 µs** minimum target.
- A **60 Hz** task receives a target of approximately **4 ms**.
- A once-per-second daemon receives the **8 ms** cap.

The sleep average adapts downward immediately and upward slowly, allowing tasks to change cadence without retaining an overly relaxed target.

### 2. Measure the real delay

Latency is measured with `sched_clock()`, from wakeup to actual execution.

`rq_clock` is deliberately avoided: scheduler-clock quantization can report sub-tick wakeups as zero, hiding exactly the delays CLASP needs to observe.

### 3. Respond to misses—and remember them

When observed latency exceeds the target:

- the task becomes `URGENT`;
- its boost increases additively;
- a hold window of `8 × sleep_avg`, clamped to **10–50 ms**, begins.

During the hold window, on-time samples do not reduce the boost. This prevents one tail-latency miss from being forgotten before the next one arrives. Afterward, comfortably early samples decay the boost by **1/16**, subject to a `target / 2` credit floor while the task remains eligible.

Urgency is remembered rather than cleared on the first on-time sample. This is intentional: a recent-miss-only variant allowed the one-slice tail to return in KVM testing. An urgent task can force preemption only while it has credit, passes the eligibility gates, and satisfies EEVDF’s eligibility and deadline checks.

### 4. Grant credit, not permanent priority

Credit is applied as negative lag in `place_entity()`:

```c
se->vruntime = avg_vruntime - (vlag + credit_vruntime);
```

This moves the waking task—and its virtual deadline—earlier without changing its requested slice or task weight.

A per-task token bucket bounds the grant:

- default refill: **100% of elapsed wall time**, using a division-free fast path;
- other refill percentages retain integer division;
- default capacity: **8 ms**;
- outstanding credit is consumed by actual runtime;
- negative-lag repair is charged to the same budget.

### Built-in guardrails

Credit is limited to ordinary `SCHED_NORMAL` tasks that:

- are not from fork-heavy or probationary lineage;
- have a duty cycle below **80%**;
- sleep for at least **100 µs** per cadence.

The cadence gate avoids forcing preemption for sub-100 µs IPC ping-pong, where normal EEVDF wakeup preemption already works and extra intervention adds overhead. Ineligible wakes still update cadence, budget, and latency samples, but skip negative-lag conversion and credit-grant work.

Fork handling is entirely O(1): threads inherit the creating thread's latency profile, processes inherit compact parent/child EWMA state, and rapid forkers produce probationary children. A voluntary sleep ends probation and clears the fork-heavy mark. Fork state is initialized even while scheduling hooks are disabled, preventing children from inheriting stale timestamps or duplicated budget.

## CLASP vs. BORE

| | BORE 7.x | CLASP 1.0.0 |
|---|---|---|
| Primary signal | Burst time since sleep/yield | Actual wake-to-run latency |
| Control model | Open-loop burst mapping | Closed-loop AIMD with hold |
| Priority lever | Persistent task-weight changes | Bounded virtual-time credit |
| Per-tick work | `update_curr` hook | None |
| Weight/PELT changes | Yes | No |
| Fork handling | Child lists and ancestor walks | O(1) EWMA and fork-rate marking |
| Extra CPU bound | Implicit in weight | Explicit token bucket |
| Main trade-off | More aggressive at low load | Directly targets measured tail latency |

## Build and install

CLASP is distributed as a standard patch for a pristine **Linux Kernel** tree.

Example for the Linux 7.3.3 patch:

```sh
cd linux-7.3.3

patch -p1 < /path/to/patches/stable/0001-linux7.3.3-clasp-1.0.0.patch

make olddefconfig        # CONFIG_SCHED_CLASP defaults to y
grep CONFIG_SCHED_CLASP .config
make -j"$(nproc)"
```

No kernel command-line parameter is required. CLASP boots enabled by default.

### Latest-kernel automation

```sh
python3 distrib/run.py
```

With no arguments, the distribution tool:

1. finds the latest stable Linux release on kernel.org;
2. downloads and checksum-verifies the source;
3. rebases the newest CLASP patch onto it;
4. regenerates a clean versioned patch;
5. validates the result with an apply check, scheduler compile, and a
   QEMU/KVM quick smoke boot when KVM is available;
6. installs the validated patch as
   `patches/stable/0001-linux<version>-clasp-<version>.patch`.

See [`distrib/README.md`](distrib/README.md) for details, options, and the
cache/work-directory layout.

To disable or re-enable it at runtime:

```sh
sysctl -w kernel.sched_clasp=0
sysctl -w kernel.sched_clasp=1
```

Disabling CLASP preserves existing task state but stops new credit and forced-preemption decisions.

> [!NOTE]
> For CachyOS or other patched kernels, apply CLASP to the matching upstream source first, then port the small `fair.c` hunks. The surrounding EEVDF code is the main source of patch offsets.

<details>
<summary>Files touched by the patch</summary>

```text
include/linux/sched.h
include/linux/sched/clasp.h     (new)
init/Kconfig
kernel/sched/Makefile
kernel/sched/clasp.c            (new)
kernel/sched/core.c
kernel/sched/fair.c
```

</details>

## Tunables

All controls live under `/proc/sys/kernel/`.

| Sysctl | Default | Range | Purpose |
|---|---:|---:|---|
| `sched_clasp` | 1 | 0–1 | Master enable |
| `sched_clasp_preempt` | 1 | 0–1 | Allow measured-miss forced preemption |
| `sched_clasp_target_min_us` | 250 | 0–1000000 | Minimum latency target |
| `sched_clasp_target_max_us` | 8000 | 0–1000000 | Maximum latency target |
| `sched_clasp_target_shift` | 2 | 0–8 | `target = sleep_avg >> shift` |
| `sched_clasp_boost_max_us` | 4000 | 0–1000000 | Maximum desired credit per wake |
| `sched_clasp_budget_pct` | 100 | 0–100 | Token refill as a percentage of elapsed time |
| `sched_clasp_budget_cap_us` | 8000 | 0–1000000 | Token-bucket capacity |
| `sched_clasp_duty_pct` | 80 | 0–100 | Maximum eligible duty cycle |
| `sched_clasp_min_sleep_us` | 100 | 0–1000000 | Minimum eligible sleep cadence |
| `sched_clasp_fork_penalty_us` | 4000 | 0–1000000 | Initial run floor for fork-heavy children |

Debug counters are available through:

```sh
cat /sys/kernel/debug/sched_clasp
```

```text
cpu wakeups boosted misses credit_ns lat_sum_ns lat_max_ns boost_sum_ns preempt_try preempt_ok ineligible
```

`preempt_try` counts requested forced preemptions, `preempt_ok` counts those passing EEVDF eligibility, and `ineligible` counts wakes with negative EEVDF `vlag`.

## Test harness

The self-contained harness builds a tiny static initramfs and boots QEMU/KVM with **8 vCPUs and 4 GiB RAM**—no root filesystem or modules required.

```sh
tests/run-vm.sh /boot/vmlinuz-linux-cachyos cachyos-default
tests/run-vm.sh arch/x86/boot/bzImage clasp
```

Results are written to:

```text
.build/results/<tag>.log
```

The harness automatically runs CLASP on/off, BORE on/off, or baseline phases depending on available sysctls. Add `clasp.reverse=1` to the kernel command line for a second run with the CLASP phases reversed.

For a short smoke test, use `clasp.quick=1`; this deliberately applies more aggressive tunables and is not a default-configuration benchmark. For a shorter default-tunables tuning run, use `clasp.tune=1` to test 16-hog IPC and eight-pair pipe off/on. For repeated cross-scheduler comparisons, use `clasp.compare=1` to run only the default 16-hog IPC probe with CLASP or BORE enabled when available.

Measurements cover IPC and timer wake latency, pipe throughput, CPU throughput, fork throughput, and fork-storm latency. Runs require a completion marker and reject benchmark or control-write errors. See [`tests/README.md`](tests/README.md) for interpretation and caveats.

## Harness validation

The original harness had two material reporting defects: p999 used a percent rather than per-mille index and therefore returned the maximum, and the compiler removed the unused CPU workload. The old p999 values and CPU-throughput percentages are not valid performance evidence. The corrected harness preserves the CPU computation, calculates p999 separately from max, checks affinity and control writes, uses atomic IPC timestamps, and supports reversed A/B phase order.

Historical BORE numbers and CLASP on/off ratios are not used for the release ranking. Same-kernel controls remain useful during development, but the 1.0 comparison leads with independent upstream EEVDF, CachyOS, BORE, and LTS kernels.

Aggressive quick mode is a functional smoke test only; variable quick-mode results are not used as release performance evidence. The full default-configuration suites and repeated `clasp.compare=1` runs below are the performance baseline.

## Scheduler comparison

**Environment:** CachyOS host with an Intel CPU, QEMU/KVM with 8 vCPUs and 4 GiB guest RAM, `HZ=1000`, and full preemption. QEMU was pinned to host CPUs `0,2,4,...,14`; the separate libvirt guest was left running and was not used for measurements. Upstream EEVDF and CLASP use Linux 7.3.3, clang/LLD 23.1.4, `CONFIG_LTO_NONE=y`, and configurations differing only in `CONFIG_SCHED_CLASP`.

Each IPC result is one 4-second run with 4,000 samples at a 1 ms cadence. Every kernel completes the full suite once and six additional IPC-only rounds, with kernel order alternated between rounds. Seven independent runs per kernel are reported using medians, ranges, and tail-failure rates rather than pooling percentiles.

### Tested kernels

| Scheduler / kernel | Version | Compiler | Comparison role |
|---|---|---|---|
| Upstream EEVDF | 7.3.3 | clang/LLD 23.1.4 | Matching configuration, `CONFIG_SCHED_CLASP=n` |
| **CLASP 1.0.0** | **7.3.3** | **clang/LLD 23.1.4** | **Matching configuration, `CONFIG_SCHED_CLASP=y`** |
| CachyOS default | 7.3.1 | clang/LLD 23.1.4 | External distribution kernel |
| CachyOS BORE custom | 7.2.9 | clang/LLD 23.1.1 | External BORE kernel, `sched_bore=1` |
| CachyOS LTS | 6.18.55 | GCC 16.2.1 | External long-term-support kernel |

### IPC wakeup latency under 16 hogs

![Seven-run IPC p99 comparison for upstream EEVDF, CLASP, CachyOS, BORE, and LTS](assets/CLASP-Results.webp)

| Kernel | p90 median | p99 median | p99 min–max | Mean median |
|---|---:|---:|---:|---:|
| Upstream EEVDF 7.3.3 | 2780.6 µs | 2797.8 µs | 2764.0–2802.4 µs | 695.0 µs |
| **CLASP 1.0.0** | **11.6 µs** | **2693.8 µs** | **13.0–2797.4 µs** | **69.1 µs** |
| CachyOS default 7.3.1 | 1593.2 µs | 1605.3 µs | 1600.7–1628.5 µs | 693.6 µs |
| CachyOS BORE 7.2.9 | 12.6 µs | **1383.4 µs** | 1266.8–1575.1 µs | 63.2 µs |
| CachyOS LTS 6.18.55 | 1980.5 µs | 1983.4 µs | 1982.0–1985.8 µs | 1918.3 µs |

BORE has the best median p99, followed by CachyOS, LTS, CLASP, and upstream EEVDF. CLASP's median p90 is excellent, but four of its seven p99 results are in the 2693.8–2797.4 µs range. Its three other runs reach 13.0–20.4 µs. This is a bimodal, unstable result—not a general latency win over other schedulers.

> [!IMPORTANT]
> Eligible preemption-attempt counts were similar in good and bad CLASP runs, so the cause of these failures is not yet established. A stable release label does not resolve this correctness and repeatability blocker.

### Throughput comparison

These are absolute results from each kernel's single full-suite run. Pipe rates are millions of messages/s, CPU rates are billions of operations/s, and fork rates are forks/s. Different kernel versions and compilers make the matching EEVDF/CLASP pair the cleanest comparison.

| Kernel | Pipe 1 | Pipe 4 | Pipe 8 | CPU | Fork |
|---|---:|---:|---:|---:|---:|
| Upstream EEVDF 7.3.3 | 1.696 | 5.781 | 10.112 | 5.319 | 705 |
| **CLASP 1.0.0** | **1.629** | **5.298** | **9.810** | **5.386** | **704** |
| CachyOS default 7.3.1 | 1.789 | 6.230 | 10.879 | 5.405 | 1237 |
| CachyOS BORE 7.2.9 | 1.652 | 5.739 | 10.190 | 5.428 | 1000 |
| CachyOS LTS 6.18.55 | 1.266 | 4.392 | 7.994 | 5.449 | 1000 |

Against its matching upstream EEVDF build, CLASP changed Pipe 1/4/8 throughput by **−4.0%, −8.3%, and −3.0%**, CPU throughput by **+1.3%**, and fork throughput by **−0.1%**. These are single-run, order-sensitive results, not proof of a general throughput advantage. Pipe measurements also include shared atomic-counter contention.

## Validation limits

CLASP 1.0 is a stable release designation, but the following limits still apply:

- **More benchmarking:** repeated runs, more machines, and identical-config A/B builds.
- **Outlier analysis:** separate host steal from guest scheduling with tracing and `perf sched`.
- **Topology:** wake CPU selection, SMT siblings, and LLC contention are not yet adapted.
- **Fairness:** test cgroups, bandwidth limits, and autogroup behavior.
- **Mixed workloads:** validate RT/DL, `sched_ext`, gaming, audio, and input-to-photon behavior.
- **Portability:** EEVDF integration hunks are small but kernel-version-sensitive.
- **Security:** tasks can earn bounded extra CPU through sleep/miss behavior; multi-tenant risk needs review.

## Credits

CLASP builds on the upstream Linux scheduler and EEVDF design. It was developed as an alternative to burst-time heuristics such as BORE.

The benchmark harness is self-contained and does not include BORE code.

## License

**GPL-2.0** — see [`LICENSE.txt`](LICENSE.txt).
