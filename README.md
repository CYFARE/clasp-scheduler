<div align="center">

# CLASP

### Closed-Loop Adaptive Scheduling Policy

**Latency-aware scheduling for Linux EEVDF.**  
*Less guessing. More measuring. Better responsiveness under load.*

![Version](https://img.shields.io/badge/version-0.1.0-blue)
![Linux](https://img.shields.io/badge/Linux-7.3.1-FCC624?logo=linux&logoColor=black)
![Status](https://img.shields.io/badge/status-experimental-orange)
![License](https://img.shields.io/badge/license-GPL--2.0-green)

[Why CLASP?](#why-clasp) ·
[How it works](#how-it-works) ·
[Results](#initial-results) ·
[Install](#build-and-install) ·
[Tune](#tunables) ·
[Test](#test-harness)

</div>

CLASP is an experimental Linux EEVDF extension that targets **measured wake-to-run latency**.

Instead of guessing whether a task is interactive from burst-time heuristics, CLASP asks a simpler question:

> **Did this task actually run on time?**

If not, CLASP grants a bounded amount of virtual-time credit, moving the task earlier in EEVDF scheduling order. As latency recovers, the boost gradually decays.

> [!WARNING]
> CLASP's reference integration targets **upstream Linux 7.3.1**. The
> `distrib/run.py` tool rebases it to the latest stable kernel. It is not yet
> production-ready. Current results come from an initial single-VM KVM
> benchmark suite.

## Why CLASP?

- 🎯 **Closed-loop control** — reacts to observed latency, not assumed task behavior.
- 🚫 **No per-tick accounting** — keeps the scheduler hot path lightweight.
- ⚖️ **No weight or PELT changes** — leaves `se->load.weight`, PELT averages, and hierarchical load untouched.
- 🪙 **Explicitly bounded boost** — a per-task token bucket limits extra CPU time.
- 🌱 **O(1) fork handling** — no child-list scans or ancestor walks.
- ⚡ **Zero overhead when disabled** — runtime control uses a static key.

### Headline result

With **16 CPU hogs** competing with the probe, IPC wakeup p99 dropped from **1.59 ms** on CachyOS and **20.0 µs** on BORE to **13.9 µs** on CLASP.

| Kernel | p90 | p99 | Mean |
|---|---:|---:|---:|
| CachyOS 7.3.1 default | 1083.0 µs | 1590.6 µs | 191.4 µs |
| BORE 7.0.1 | 13.1 µs | 20.0 µs | 32.7 µs |
| **CLASP** | **11.7 µs** | **13.9 µs** | **24.4 µs** |

These are preliminary measurements; see [Initial results](#initial-results) for context and caveats.

## How it works

```text
Sleep cadence ──► Latency target
                       │
Wake-to-run sample ────┼──► Target missed?
                       │          │
                       │          ├─ No ──► Hold, then decay boost
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

Each task keeps a small, approximately **92-byte** context containing run, sleep, latency, boost, budget, and fork-lineage state.

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

During the hold window, on-time samples do not reduce the boost. This prevents one tail-latency miss from being forgotten before the next one arrives. Afterward, comfortably early samples decay the boost by **1/16**.

An urgent task can force preemption only while it has credit, passes the eligibility gates, and satisfies EEVDF’s eligibility and deadline checks.

### 4. Grant credit, not permanent priority

Credit is applied as negative lag in `place_entity()`:

```c
se->vruntime = avg_vruntime - (vlag + credit_vruntime);
```

This moves the waking task—and its virtual deadline—earlier without changing its requested slice or task weight.

A per-task token bucket bounds the grant:

- default refill: **100% of elapsed wall time**;
- default capacity: **8 ms**;
- outstanding credit is consumed by actual runtime;
- negative-lag repair is charged to the same budget.

### Built-in guardrails

Credit is limited to ordinary `SCHED_NORMAL` tasks that:

- are not from fork-heavy or probationary lineage;
- have a duty cycle below **80%**;
- sleep for at least **100 µs** per cadence.

The cadence gate avoids forcing preemption for sub-100 µs IPC ping-pong, where normal EEVDF wakeup preemption already works and extra intervention adds overhead.

Fork handling is entirely O(1): threads inherit the process profile, processes inherit compact parent/child EWMA state, and rapid forkers produce probationary children. A voluntary sleep ends probation.

## CLASP vs. BORE

| | BORE 7.x | CLASP 0.1.0 |
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

Example for Linux 7.3.1 patch:

```sh
cd linux-7.3.1

patch -p1 < /path/to/patches/stable/0001-linux7.3.1-clasp-0.1.0.patch

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

The harness automatically runs CLASP on/off, BORE on/off, or baseline phases depending on available sysctls. For a short smoke test, add this to the kernel command line:

```text
clasp.quick=1
```

Measurements cover IPC and timer wake latency, pipe throughput, CPU throughput, fork throughput, and fork-storm latency.

## Initial results

**Environment:** CachyOS host, Intel CPU, QEMU/KVM with 8 vCPUs and 4 GiB guest RAM, `HZ=1000`, `PREEMPT_DYNAMIC=y`, clang 23.1.4 with ThinLTO.

Each latency cell represents one 4-second run with 4,000 samples. The probe uses a 1 ms IPC cadence.

### IPC wakeup latency under 16 hogs

| Kernel / phase | p50 | p90 | p99 | p999 | Mean |
|---|---:|---:|---:|---:|---:|
| Upstream 7.3.1 baseline | 9.7 µs | 2710.1 µs | 2800.3 µs | 18492.3 µs | 712.0 µs |
| CachyOS 7.3.1 default | 9.7 µs | 1083.0 µs | 1590.6 µs | 10409.2 µs | 191.4 µs |
| BORE 7.0.1 | 10.8 µs | 13.1 µs | 20.0 µs | 16046.6 µs | 32.7 µs |
| CLASP kernel, disabled | 7.7 µs | 2783.5 µs | 2802.5 µs | 19114.9 µs | 1048.8 µs |
| **CLASP, enabled** | **6.9 µs** | **11.7 µs** | **13.9 µs** | 20371.5 µs | **24.4 µs** |

The important change is the **tail**: p90 and p99 fall from roughly one EEVDF base slice—about 2.8 ms—to approximately 12–14 µs.

CLASP leads BORE on p50, p90, p99, and mean in this run. BORE records the better rare p999 sample. At lower load, BORE remains slightly faster at four hogs, while CLASP leads at one and sixteen hogs.

> [!IMPORTANT]
> Rare 10–20 ms p999 outliers remain. They correlate with host/vCPU scheduling and fork-storm tails and are **not** claimed as scheduler improvements.

### Relative throughput cost

Absolute throughput is not directly comparable across these kernels because BORE uses its own slice configuration and CachyOS carries an additional patchset. The cleaner comparison is each scheduler against its own disabled phase:

| Scheduler, on vs. off | Pipe 1 | Pipe 4 | Pipe 8 | CPU | Fork |
|---|---:|---:|---:|---:|---:|
| BORE | +2.1% | −4.7% | −3.5% | −2.0% | +0.7% |
| CLASP | +0.3% | −4.3% | −8.0% | −0.8% | 0% |

Both schedulers pay a few percent on scheduler-heavy ping-pong. CLASP shows lower CPU and fork overhead; BORE shows lower eight-pair pipe overhead.

## Before production use

CLASP is promising, but still needs broader validation:

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
