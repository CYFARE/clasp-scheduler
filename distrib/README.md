# CLASP distribution tooling

`run.py` rebases CLASP onto the latest stable upstream Linux source, creates a
fresh patch, validates it, and installs it under `patches/stable/`.

```sh
python3 distrib/run.py
```

No arguments are required.  Optional flags:

```text
--skip-kvm     skip the bzImage + QEMU/KVM smoke test
-j N           build parallelism (default: CPU count)
```

## What a run does

1. Reads `https://www.kernel.org/releases.json` and selects the latest stable
   kernel.
2. Downloads `linux-<version>.tar.xz` from `cdn.kernel.org` and verifies it
   against the kernel.org `sha256sums.asc` when available.
3. Extracts a pristine source tree and snapshots the files touched by the
   newest CLASP patch in `patches/stable/`.
4. Applies that reference patch to the latest source with `patch --fuzz=3`.
5. Regenerates a clean `git format-patch` from the pristine and patched
   versions of exactly those files.
6. Re-applies the generated patch with `patch --dry-run --fuzz=0` in a clean
   git worktree.
7. Builds `kernel/sched/clasp.o`, `kernel/sched/fair.o`, and
   `kernel/sched/core.o` from a `defconfig` with CLASP enabled.
8. When KVM is available, builds a `bzImage`, boots it with the repository's
   initramfs, runs the quick CLASP smoke test, and requires:
   - `CONTROLS clasp=1`
   - both CLASP-off and CLASP-on 16-hog IPC `RESULT` lines
   - no benchmark `ERROR` lines
   - `QUICK_DONE`
9. Installs the validated patch as:

   ```text
   patches/stable/0001-linux<kernel>-clasp-<clasp>.patch
   ```

If an installed patch with the same name already exists, it is replaced only
after the new version has passed all validation.

## Requirements

Host tools: `git`, `patch`, `tar`, `make`, and `gcc` or `clang`.  The KVM
smoke additionally needs `/dev/kvm`, `qemu-system-x86_64`, `gcc` with static
libc support, `cpio`, `grep`, `gzip`, `tee`, and `timeout`.

The script checks for at least 8 GiB free in `.build/distrib/` and fails early
if the space is not available.

## Work directory

Everything is cached under:

```text
.build/distrib/
├── downloads/linux-<version>.tar.xz
└── linux-<version>/
    ├── src/          pristine latest source + reference patch applied
    ├── patchgen/     small git repo used to generate the patch
    ├── build/        defconfig + scheduler objects + bzImage
    ├── logs/         subprocess logs
    └── *.patch       generated patch before installation
```

On success, older `linux-*` work directories and tarballs are removed.  They
are left in place on failure for debugging.

## Failure cases

- **Reference patch does not apply.** Upstream EEVDF integration has changed;
  inspect `logs/apply-reference.log` and the `.rej` files, port the small
  `fair.c` hunks, then add the updated base patch under `patches/stable/`.
- **Scheduler compile fails.** The generated patch is not installed.  The
  build log is in `logs/scheduler-build.log`.
- **KVM smoke fails.** The patch is not installed.  Check
  `logs/kvm-<version>.log` and `.build/results/`.
- **No network.** `releases.json` is fetched every run; the tarball itself is
  cached.

The KVM smoke is a validation boot, not a performance benchmark.  Use
`tests/run-vm.sh <bzImage> <tag>` for the full matrix.

## Repository name

The repository/folder is `clasp-scheduler`, following the short-name style of
`bore-scheduler`.  CLASP stands for **Closed-Loop Adaptive Scheduling Policy**.
