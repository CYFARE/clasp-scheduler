#!/usr/bin/env python3
"""CLASP distribution: fetch the latest stable Linux, rebase CLASP onto it,
regenerate a clean patch, validate it, and install it in patches/stable/.

Usage:
    python3 distrib/run.py

No arguments are required.  Optional:
    --skip-kvm   skip the bzImage + QEMU/KVM smoke test
    -j N         build parallelism (default: CPU count)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = REPO_ROOT / "patches" / "stable"
TESTS_SCRIPT = REPO_ROOT / "tests" / "run-vm.sh"
WORK = REPO_ROOT / ".build" / "distrib"

PATCH_RE = re.compile(
    r"^0001-linux(?P<kernel>\d+\.\d+(?:\.\d+)?(?:-rc\d+)?)"
    r"-clasp-(?P<clasp>[0-9A-Za-z][0-9A-Za-z._-]*)\.patch$"
)
KERNEL_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-rc(\d+))?$")

# Options needed for the scheduler compile and the optional KVM smoke boot.
# The Kconfig default for SCHED_CLASP is y, but set it explicitly so a
# distribution build cannot silently test a kernel without CLASP.
CONFIG_ENABLE = [
    "SCHED_CLASP",
    "BLK_DEV_INITRD",
    "RD_GZIP",
    "SERIAL_8250",
    "SERIAL_8250_CONSOLE",
    "DEVTMPFS",
    "DEVTMPFS_MOUNT",
    "TMPFS",
    "PROC_FS",
    "SYSFS",
    "SYSCTL",
    "PRINTK",
    "SMP",
    "ACPI",
    "PCI",
    "BINFMT_ELF",
    "FUTEX",
    "DEBUG_FS",
    "PREEMPT",
    "PREEMPT_DYNAMIC",
    "HZ_1000",
]

CONFIG_DISABLE = [
    "PREEMPT_LAZY",
]

GIT_ID = [
    "-c",
    "user.name=CLASP-Scheduler",
    "-c",
    "user.email=security@cyfare.net",
]


class DistError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[clasp] {message}", flush=True)


def tail(path: Path, lines: int = 40) -> str:
    try:
        data = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])


def run(
    cmd,
    cwd: Path | None = None,
    logfile: Path | None = None,
    check: bool = True,
    env: dict | None = None,
    timeout: int | None = None,
):
    cmd = [str(part) for part in cmd]
    log("$ " + " ".join(cmd))

    if logfile is not None:
        logfile = Path(logfile)
        logfile.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(logfile, "wb") as stream:
                proc = subprocess.run(
                    cmd,
                    cwd=str(cwd) if cwd is not None else None,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    env=env,
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired:
            raise DistError(
                f"command timed out after {timeout}s; see {logfile}"
            ) from None
    else:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise DistError(f"command timed out after {timeout}s: {cmd[0]}")
        if proc.stdout and proc.stdout.strip():
            print(proc.stdout.rstrip())

    if check and proc.returncode != 0:
        if logfile is not None:
            print(tail(Path(logfile)), file=sys.stderr)
        raise DistError(
            f"command failed with status {proc.returncode}: {' '.join(cmd)}"
        )
    return proc


def http_get(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "clasp-distrib/0.1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "clasp-distrib/0.1"},
    )
    log(f"downloading {url}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, \
                open(part, "wb") as stream:
            total = int(response.headers.get("Content-Length", "0") or 0)
            done = 0
            next_report = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if pct >= next_report:
                        log(f"  {pct:3d}%  {done / (1024 * 1024):.1f} MiB")
                        next_report = pct + 10
        os.replace(part, destination)
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise DistError(f"download failed: {exc}") from exc


def kernel_key(version: str):
    match = KERNEL_RE.match(version)
    if not match:
        return (0, 0, 0, 0)
    major, minor, patch, rc = match.groups()
    return (
        int(major),
        int(minor),
        int(patch or 0),
        int(rc) if rc is not None else 99999,
    )


def kernel_source_url(version: str) -> str:
    major = version.split(".")[0]
    subdir = "testing" if "-rc" in version else f"v{major}.x"
    return (
        f"https://cdn.kernel.org/pub/linux/kernel/{subdir}/"
        f"linux-{version}.tar.xz"
    )


def latest_release():
    try:
        payload = json.loads(
            http_get("https://www.kernel.org/releases.json").decode()
        )
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise DistError(
            "could not fetch kernel.org releases.json; check network access"
        ) from exc

    stable = payload.get("latest_stable") or {}
    version = stable.get("version")
    if version:
        return version, stable.get("source") or kernel_source_url(version)

    releases = [
        entry for entry in payload.get("releases", [])
        if entry.get("moniker") == "stable" and entry.get("version")
    ]
    if not releases:
        raise DistError("kernel.org releases.json has no stable release")
    entry = max(releases, key=lambda item: kernel_key(item["version"]))
    version = entry["version"]
    return version, entry.get("source") or kernel_source_url(version)


def affected_files(patch: Path) -> list[str]:
    paths = set()
    for line in patch.read_text(errors="replace").splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            paths.add(path[2:])
    if not paths:
        raise DistError(f"could not read file list from {patch}")
    return sorted(paths)


def clasp_version_key(version: str):
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", version)
    )


def find_reference(latest: str):
    latest_key = kernel_key(latest)
    candidates = []
    for patch in sorted(PATCH_DIR.glob("*.patch")):
        match = PATCH_RE.match(patch.name)
        if not match:
            continue
        key = kernel_key(match.group("kernel"))
        if key <= latest_key:
            clasp_version = match.group("clasp")
            candidates.append(
                (
                    key,
                    clasp_version_key(clasp_version),
                    patch,
                    match.group("kernel"),
                    clasp_version,
                )
            )
    if not candidates:
        raise DistError(
            f"no CLASP patch for a kernel <= {latest} in {PATCH_DIR}; "
            "add a base patch that can be rebased"
        )
    _, _, patch, reference_kernel, clasp_version = max(
        candidates, key=lambda item: (item[0], item[1])
    )
    return patch, reference_kernel, clasp_version


def expected_sha256(source_url: str) -> str | None:
    name = source_url.rstrip("/").rsplit("/", 1)[-1]
    base = source_url.rstrip("/").rsplit("/", 1)[0]
    try:
        text = http_get(base + "/sha256sums.asc").decode(errors="replace")
    except urllib.error.URLError:
        return None
    match = re.search(
        r"([0-9a-fA-F]{64})\s+\*?" + re.escape(name) + r"\b",
        text,
    )
    return match.group(1).lower() if match else None


def ensure_tarball(version: str, source_url: str) -> Path:
    downloads = WORK / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    name = f"linux-{version}.tar.xz"
    target = downloads / name
    checksum = expected_sha256(source_url)

    if target.exists() and checksum:
        if sha256_file(target) != checksum:
            log("cached tarball checksum mismatch; downloading again")
            target.unlink()
    if not target.exists():
        download(source_url, target)

    actual = sha256_file(target)
    if checksum and actual != checksum:
        target.unlink(missing_ok=True)
        raise DistError(
            f"SHA256 mismatch for {name}: expected {checksum}, got {actual}"
        )
    if not checksum:
        log("warning: no kernel.org sha256 found; recorded digest only")
    log(f"tarball sha256 {actual}")
    return target


def extract_source(tarball: Path, source: Path, logfile: Path) -> None:
    if source.exists():
        shutil.rmtree(source)
    source.mkdir(parents=True)
    run(
        ["tar", "-xf", tarball, "-C", source, "--strip-components=1"],
        logfile=logfile,
        timeout=900,
    )
    if not (source / "Makefile").exists() or \
            not (source / "kernel" / "sched" / "fair.c").exists():
        raise DistError(f"{tarball.name} did not extract a Linux source tree")


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def git(repo: Path, *args):
    return ["git", "-C", str(repo), *GIT_ID, *args]


def snapshot_baseline(source: Path, patchgen: Path, files: list[str]) -> str:
    if patchgen.exists():
        shutil.rmtree(patchgen)
    patchgen.mkdir(parents=True)

    for relative in files:
        src = source / relative
        if src.exists():
            copy_file(src, patchgen / relative)
    if not any(patchgen.rglob("*")):
        raise DistError("reference patch has no readable files")

    run(git(patchgen, "init", "-q"))
    run(git(patchgen, "add", "-A"))
    run(git(patchgen, "commit", "-q", "-m", "baseline"))
    return run(
        git(patchgen, "rev-parse", "HEAD"), check=True
    ).stdout.strip()


def apply_reference(source: Path, reference: Path, logfile: Path) -> None:
    log(f"applying reference patch {reference.name}")
    proc = run(
        [
            "patch",
            "-p1",
            "--batch",
            "--forward",
            "--fuzz=3",
            "--no-backup-if-mismatch",
            "-i",
            reference,
        ],
        cwd=source,
        logfile=logfile,
        check=False,
    )
    rejects = list(source.rglob("*.rej"))
    if proc.returncode != 0 or rejects:
        if rejects:
            log("rejects: " + ", ".join(str(p) for p in rejects[:10]))
        print(tail(logfile), file=sys.stderr)
        raise DistError(
            "reference patch does not apply to the selected kernel; "
            "the EEVDF integration needs a manual port"
        )
    for original in source.rglob("*.orig"):
        original.unlink(missing_ok=True)


def generate_patch(
    source: Path,
    patchgen: Path,
    files: list[str],
    version: str,
    clasp_version: str,
    output: Path,
) -> None:
    for relative in files:
        src = source / relative
        dst = patchgen / relative
        if src.exists():
            copy_file(src, dst)
        elif dst.exists():
            dst.unlink()
        else:
            raise DistError(
                f"reference patch did not produce {relative}; cannot generate"
            )

    run(git(patchgen, "add", "-A"))
    run(
        git(
            patchgen,
            "commit",
            "-q",
            "--signoff",
            "-m",
            f"linux{version}-clasp-{clasp_version}",
            "-m",
            "Add bounded wake-to-run latency feedback to EEVDF with "
            "per-task credit, eligibility guardrails, and O(1) fork handling.",
        )
    )

    changed = run(
        git(patchgen, "diff", "--name-only", "HEAD~1", "HEAD")
    ).stdout.splitlines()
    if set(changed) != set(files):
        raise DistError(
            "generated change set does not match the reference patch file list"
        )

    proc = run(
        git(patchgen, "diff", "--stat", "HEAD~1", "HEAD"),
        check=True,
    )
    print(proc.stdout.rstrip())

    proc = subprocess.run(
        git(patchgen, "format-patch", "-1", "--stdout"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0 or not proc.stdout:
        print(proc.stderr.decode(errors="replace"), file=sys.stderr)
        raise DistError("git format-patch failed")
    output.write_bytes(proc.stdout)
    if b"Subject: [PATCH] " not in proc.stdout:
        raise DistError("generated patch has no git format-patch subject")
    log(f"generated {output} ({len(proc.stdout)} bytes)")


def validate_patch(
    patchgen: Path,
    generated: Path,
    baseline_commit: str,
    verify: Path,
    logfile: Path,
) -> None:
    if verify.exists():
        shutil.rmtree(verify)
    run(
        git(patchgen, "worktree", "add", "--detach", verify, baseline_commit),
        logfile=logfile,
    )
    try:
        run(
            [
                "patch",
                "-p1",
                "--batch",
                "--dry-run",
                "--fuzz=0",
                "-i",
                generated,
            ],
            cwd=verify,
            logfile=logfile,
        )
    finally:
        run(
            git(patchgen, "worktree", "remove", "--force", verify),
            check=False,
        )
    log("generated patch re-applies cleanly to pristine files")


def build_validate(
    source: Path,
    root: Path,
    jobs: int,
    want_kvm: bool,
) -> Path | None:
    build = root / "build"
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)

    make = ["make"]
    if shutil.which("clang") and shutil.which("ld.lld"):
        make.append("LLVM=1")
    make.extend([f"O={build}"])

    run(make + ["defconfig"], cwd=source, logfile=logs / "defconfig.log")

    config = build / ".config"
    if not config.exists():
        raise DistError("defconfig did not create .config")
    scripts_config = source / "scripts" / "config"
    for option in CONFIG_ENABLE:
        run(
            [scripts_config, "--file", config, "-e", option],
            cwd=source,
            logfile=logs / "config.log",
            check=False,
        )
    for option in CONFIG_DISABLE:
        run(
            [scripts_config, "--file", config, "-d", option],
            cwd=source,
            logfile=logs / "config.log",
            check=False,
        )

    run(make + ["olddefconfig"], cwd=source, logfile=logs / "olddefconfig.log")
    config_text = config.read_text(errors="replace")
    if "CONFIG_SCHED_CLASP=y" not in config_text:
        raise DistError(
            "CONFIG_SCHED_CLASP was not enabled after olddefconfig; "
            "the Kconfig hunk may not have applied"
        )
    if want_kvm:
        required = [
            "CONFIG_BLK_DEV_INITRD=y",
            "CONFIG_RD_GZIP=y",
            "CONFIG_SERIAL_8250=y",
            "CONFIG_SERIAL_8250_CONSOLE=y",
            "CONFIG_DEVTMPFS=y",
            "CONFIG_PROC_FS=y",
            "CONFIG_SYSCTL=y",
        ]
        missing = [item for item in required if item not in config_text]
        if missing:
            raise DistError(
                "KVM smoke config is missing " + ", ".join(missing)
            )

    log("building scheduler objects")
    run(
        make + [
            f"-j{jobs}",
            "kernel/sched/clasp.o",
            "kernel/sched/fair.o",
            "kernel/sched/core.o",
        ],
        cwd=source,
        logfile=logs / "scheduler-build.log",
    )

    if not want_kvm:
        return None

    log("building bzImage for the KVM smoke test")
    run(
        make + [f"-j{jobs}", "bzImage"],
        cwd=source,
        logfile=logs / "bzimage-build.log",
    )
    image = build / "arch" / "x86" / "boot" / "bzImage"
    if not image.exists():
        raise DistError("bzImage was not produced")
    return image


def kvm_status():
    missing = []
    if not Path("/dev/kvm").exists():
        missing.append("/dev/kvm")
    for tool in (
        "qemu-system-x86_64",
        "gcc",
        "cpio",
        "grep",
        "gzip",
        "tee",
        "timeout",
    ):
        if shutil.which(tool) is None:
            missing.append(tool)
    if not TESTS_SCRIPT.exists():
        missing.append(str(TESTS_SCRIPT))
    return (not missing, missing)


def kvm_smoke(image: Path, version: str, logfile: Path) -> None:
    env = os.environ.copy()
    env["APPEND"] = (
        "console=ttyS0 rdinit=/init panic=1 quiet clasp.quick=1 "
        "preempt=full"
    )
    log("running QEMU/KVM quick smoke test")
    proc = run(
        [TESTS_SCRIPT, image, f"distrib-{version}"],
        cwd=REPO_ROOT,
        logfile=logfile,
        check=False,
        env=env,
        timeout=600,
    )
    text = logfile.read_text(errors="replace")
    if proc.returncode != 0:
        print(tail(logfile), file=sys.stderr)
        raise DistError("QEMU/KVM smoke test failed")
    if "QUICK_DONE" not in text:
        print(tail(logfile), file=sys.stderr)
        raise DistError("QEMU/KVM quick smoke test did not complete")
    if "CONTROLS clasp=1" not in text:
        print(tail(logfile), file=sys.stderr)
        raise DistError(
            "the booted kernel did not expose the CLASP control interface"
        )

    lines = text.splitlines()
    if any(line.startswith("ERROR ") for line in lines):
        print(tail(logfile), file=sys.stderr)
        raise DistError("QEMU/KVM smoke test reported a benchmark error")
    ipc_results = [
        line for line in lines
        if line.startswith("RESULT ipc ") and " tag=q " in line
        and " hogs=16 " in line
    ]
    if len(ipc_results) != 2:
        print(tail(logfile), file=sys.stderr)
        raise DistError(
            "QEMU/KVM smoke test did not produce both off/on IPC results"
        )
    for line in lines:
        if line.startswith("RESULT "):
            print("  " + line)
    log("KVM smoke test passed")


def install_patch(
    generated: Path,
    version: str,
    clasp_version: str,
) -> tuple[Path, str]:
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    destination = PATCH_DIR / (
        f"0001-linux{version}-clasp-{clasp_version}.patch"
    )
    data = generated.read_bytes()

    if destination.exists():
        if destination.read_bytes() == data:
            status = "unchanged"
        else:
            status = "updated"
    else:
        status = "created"

    tmp = destination.with_name(destination.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, destination)
    return destination, status


def cleanup_old(current_root: Path, version: str) -> None:
    for old in WORK.glob("linux-*"):
        if old != current_root and old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
    downloads = WORK / "downloads"
    if downloads.exists():
        keep = f"linux-{version}.tar.xz"
        for tarball in downloads.glob("linux-*.tar.xz"):
            if tarball.name != keep:
                tarball.unlink(missing_ok=True)


def check_tools() -> None:
    required = ["git", "patch", "tar", "make"]
    missing = [tool for tool in required if shutil.which(tool) is None]
    if not shutil.which("gcc") and not shutil.which("clang"):
        missing.append("gcc or clang")
    if missing:
        raise DistError("missing required tools: " + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebase CLASP onto the latest stable Linux and install "
                    "a validated patch under patches/stable/.",
    )
    parser.add_argument(
        "--skip-kvm",
        action="store_true",
        help="skip the bzImage + QEMU/KVM smoke test",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 8,
        help="build parallelism (default: CPU count)",
    )
    args = parser.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(WORK).free
    if free < 8 * 1024 ** 3:
        print(
            f"[clasp] ERROR: at least 8 GiB free is required in {WORK}; "
            f"only {free / 1024 ** 3:.1f} GiB available",
            file=sys.stderr,
        )
        return 1

    try:
        check_tools()
        version, source_url = latest_release()
        reference, reference_kernel, clasp_version = find_reference(version)
        reference_key = kernel_key(reference_kernel)

        log(f"latest stable kernel: {version}")
        log(f"reference patch: {reference.name}")
        log(f"CLASP version: {clasp_version}")

        root = WORK / f"linux-{version}"
        for name in ("src", "patchgen", "verify", "build", "logs"):
            path = root / name
            if path.exists():
                shutil.rmtree(path)
        root.mkdir(parents=True, exist_ok=True)
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)

        tarball = ensure_tarball(version, source_url)
        source = root / "src"
        extract_source(tarball, source, logs / "extract.log")
        log(f"extracted source to {source}")

        files = affected_files(reference)
        patchgen = root / "patchgen"
        baseline_commit = snapshot_baseline(source, patchgen, files)
        log(f"touched files: {', '.join(files)}")

        apply_reference(source, reference, logs / "apply-reference.log")
        log("reference patch applied to latest stable source")

        generated = root / (
            f"0001-linux{version}-clasp-{clasp_version}.patch"
        )
        generate_patch(
            source,
            patchgen,
            files,
            version,
            clasp_version,
            generated,
        )
        validate_patch(
            patchgen,
            generated,
            baseline_commit,
            root / "verify",
            logs / "validate-patch.log",
        )

        kvm_ok, kvm_missing = kvm_status()
        want_kvm = kvm_ok and not args.skip_kvm
        if args.skip_kvm:
            log("KVM smoke test skipped by request")
        elif not kvm_ok:
            log("KVM smoke test skipped; missing: " + ", ".join(kvm_missing))

        image = build_validate(source, root, max(1, args.jobs), want_kvm)
        if image is not None:
            kvm_smoke(image, version, logs / f"kvm-{version}.log")

        destination, status = install_patch(
            generated, version, clasp_version
        )
        log(f"installed {destination} ({status})")
        log(f"sha256 {sha256_file(destination)}")
        if reference_key == kernel_key(version):
            log("reference was already for this kernel version")
        log(f"work directory: {root}")

        cleanup_old(root, version)
        return 0
    except DistError as exc:
        print(f"[clasp] ERROR: {exc}", file=sys.stderr)
        print(f"[clasp] work directory kept: {WORK}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[clasp] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
