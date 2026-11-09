#!/usr/bin/env bash
#
# Boot a kernel in QEMU/KVM with the CLASP benchmark initramfs.
#
#   tests/run-vm.sh <path-to-bzImage> <tag>
#
# Results are tee'd to .build/results/<tag>.log.  The VM runs the phase
# named "ctrl" first (sched_clasp=0) and then "clasp" (sched_clasp=1) when
# the kernel exposes the CLASP sysctl; otherwise it runs one "base" phase.
#
set -euo pipefail

if [ "$#" -lt 2 ]; then
	echo "usage: $0 <bzImage> <tag>" >&2
	exit 1
fi

KERNEL=$(readlink -f "$1")
TAG=$2
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUILD="$ROOT/.build"
IR="$BUILD/initramfs"
INITRD="$BUILD/initramfs-$TAG.img"
LOG="$BUILD/results/$TAG.log"
APPEND=${APPEND:-"console=ttyS0 rdinit=/init panic=1 quiet"}

mkdir -p "$BUILD/results"
rm -rf "$IR"
mkdir -p "$IR/bin" "$IR/proc" "$IR/sys" "$IR/dev" "$IR/tmp"

gcc -O2 -static -pthread -o "$IR/bench" "$ROOT/tests/latency_probe.c"
gcc -O2 -static -o "$IR/init" "$ROOT/tests/init.c"
chmod +x "$IR/init" "$IR/bench"

(
	cd "$IR"
	find . -print0 | cpio --null -o --format=newc 2>/dev/null | gzip -9
) > "$INITRD"

# Pin QEMU to one hardware thread per physical P-core when possible.  The
# host commonly has SMT siblings at 0/1, 2/3, ...; using even CPUs avoids
# vCPU siblings landing on the same physical core.  Adapt to the host's CPU
# count so the distribution script also works on smaller machines.
CPUS=""
NCPU=$(nproc)
for cpu in 0 2 4 6 8 10 12 14; do
	if [ "$cpu" -lt "$NCPU" ]; then
		if [ -n "$CPUS" ]; then
			CPUS="$CPUS,$cpu"
		else
			CPUS="$cpu"
		fi
	fi
done

RUNNER=()
if command -v taskset >/dev/null 2>&1 && [ -n "$CPUS" ]; then
	RUNNER=(taskset -c "$CPUS")
fi

"${RUNNER[@]}" timeout 300 qemu-system-x86_64 \
	-enable-kvm -cpu host -smp 8 -m 4096 \
	-kernel "$KERNEL" -initrd "$INITRD" \
	-append "$APPEND" \
	-nographic -no-reboot 2>&1 | tee "$LOG"

if grep -aq '^ERROR ' "$LOG"; then
	echo "benchmark reported an error; see $LOG" >&2
	exit 1
fi
if ! grep -aq '^RESULT ' "$LOG"; then
	echo "benchmark produced no results; see $LOG" >&2
	exit 1
fi
if ! grep -aqE '^(BENCH_DONE|QUICK_DONE|TUNE_DONE)' "$LOG"; then
	echo "benchmark did not complete; see $LOG" >&2
	exit 1
fi
