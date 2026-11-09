#!/usr/bin/env python3
"""Add the latest CachyOS kernel with CLASP to CachyOS Kernel Manager.

The manager's Configure dialog refreshes its linux-cachyos Git checkout every
time it opens. This script keeps the generated patch and PKGBUILD changes in a
small local commit so that refresh does not remove them. It does not build or
install a kernel; use the manager's Build kernel button for that.

Usage:
    python3 distrib/option-cachyos.py

Then open CachyOS Kernel Manager, click Configure, load the printed TOML
preset, and click Build kernel. The default CachyOS scheduler must remain
selected because the apply check is made against that source configuration.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import run as clasp

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = Path.home() / ".cache" / "cachyos-km"
DEFAULT_WORK = REPO_ROOT / ".build" / "cachyos-option"
PKGBUILDS_URL = "https://github.com/cachyos/linux-cachyos.git"
COMMIT_SUBJECT = "CLASP: add CachyOS kernel manager option"
KERNEL_PACKAGE = "linux-cachyos-clasp"

PRESET = """\
hardly_check = true
per_gov_check = false
tcp_bbr3_check = false
cachy_config_check = true
nconfig_check = false
xconfig_check = false
localmodcfg_check = false
use_current_check = false
builtin_zfs_check = false
builtin_nvidia_open_check = false
build_debug_check = false
hz_ticks_combo = "1000"
tickrate_combo = "full"
preempt_combo = "full"
hugepage_combo = "always"
lto_combo = "thin"
cpu_opt_combo = "manual"
custom_name_edit = "linux-cachyos-clasp"
"""


class OptionError(RuntimeError):
    pass


def quiet_run(
    cmd,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    cmd = [str(part) for part in cmd]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = "\n".join(proc.stdout.splitlines()[-30:])
        if detail:
            print(detail, file=sys.stderr)
        raise OptionError(
            f"command failed with status {proc.returncode}: {' '.join(cmd)}"
        )
    return proc


def git(repo: Path, *args, check: bool = True):
    return quiet_run(["git", "-C", repo, *args], check=check)


def check_tools() -> None:
    required = ["bash", "git", "patch", "tar"]
    missing = [tool for tool in required if shutil.which(tool) is None]
    if missing:
        raise OptionError("missing required tools: " + ", ".join(missing))


def expected_remote(url: str) -> bool:
    normalized = url.rstrip("/").removesuffix(".git").lower()
    return normalized in {
        "https://github.com/cachyos/linux-cachyos",
        "http://github.com/cachyos/linux-cachyos",
        "git@github.com:cachyos/linux-cachyos",
        "ssh://git@github.com/cachyos/linux-cachyos",
    }


def refresh_checkout(repo: Path) -> None:
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout
    if branch.strip() != "master":
        raise OptionError(
            f"{repo} is not on the master branch; refusing to replace it"
        )

    remote = git(repo, "remote", "get-url", "origin").stdout.strip()
    if not expected_remote(remote):
        raise OptionError(f"unexpected origin in {repo}: {remote}")

    dirty = git(repo, "status", "--porcelain", "--untracked-files=no").stdout
    if dirty:
        raise OptionError(
            f"tracked files in {repo} have local edits; restore them first\n"
            f"{dirty.rstrip()}"
        )

    clasp.log("fetching the latest CachyOS PKGBUILDs")
    git(repo, "fetch", "origin", "master")
    local_subjects = [
        line for line in git(
            repo, "log", "--no-merges", "--format=%s", "origin/master..HEAD"
        ).stdout.splitlines() if line
    ]
    if len(local_subjects) > 1 or (
        local_subjects and local_subjects[0] != COMMIT_SUBJECT
    ):
        raise OptionError(
            "the manager checkout has unrelated local commits; "
            "refusing to replace them"
        )

    proc = git(repo, "rebase", "origin/master", check=False)
    if proc.returncode != 0:
        git(repo, "rebase", "--abort", check=False)
        print(proc.stdout, file=sys.stderr)
        raise OptionError(
            "could not rebase the existing CLASP option onto the latest "
            "CachyOS PKGBUILD; the checkout was left unchanged"
        )

    head = git(repo, "log", "-1", "--format=%s").stdout.strip()
    if head == COMMIT_SUBJECT:
        git(repo, "reset", "--hard", "HEAD~1")

    head_id = git(repo, "rev-parse", "HEAD").stdout.strip()
    origin_id = git(repo, "rev-parse", "origin/master").stdout.strip()
    if head_id != origin_id:
        raise OptionError(
            "could not isolate the CLASP changes from other local history"
        )


def prepare_checkout(cache: Path) -> Path:
    repo = cache / "pkgbuilds"
    if repo.exists() and not (repo / ".git").exists():
        raise OptionError(f"{repo} exists but is not a Git checkout")
    if not repo.exists():
        cache.mkdir(parents=True, exist_ok=True)
        clasp.log(f"cloning {PKGBUILDS_URL}")
        clasp.run(["git", "clone", PKGBUILDS_URL, repo], timeout=300)
    refresh_checkout(repo)
    return repo


def pkgbuild_metadata(pkgbuild: Path) -> tuple[str, str, str]:
    script = r'''
source "$1"
printf 'pkgver=%s\n' "$pkgver"
printf 'srcname=%s\n' "$_srcname"
for item in "${source[@]}"; do
    case "$item" in
        *.tar.gz) printf 'archive=%s\n' "$item" ;;
    esac
done
'''
    env = os.environ.copy()
    env.update({
        "_use_llvm_lto": "none",
        "_build_zfs": "no",
        "_build_nvidia_open": "no",
        "_build_nvidia_580xx": "no",
        "_build_r8125": "no",
        "_cpusched": "cachyos",
    })
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script, "bash", pkgbuild],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        print(proc.stdout, file=sys.stderr)
        raise OptionError(f"could not evaluate {pkgbuild}")

    values = {}
    for line in proc.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"pkgver", "srcname", "archive"}:
            values.setdefault(key, value)
    missing = {"pkgver", "srcname", "archive"} - values.keys()
    if missing:
        raise OptionError(
            "CachyOS PKGBUILD metadata is missing: " + ", ".join(sorted(missing))
        )

    version = values["pkgver"]
    srcname = values["srcname"]
    archive = values["archive"]
    if not clasp.KERNEL_RE.match(version):
        raise OptionError(f"unsupported CachyOS kernel version: {version}")
    if not re.fullmatch(r"[0-9A-Za-z._-]+", srcname):
        raise OptionError(f"unsafe CachyOS source directory name: {srcname}")
    expected_suffix = f"/{srcname}.tar.gz"
    if not archive.startswith("https://") or not archive.endswith(expected_suffix):
        raise OptionError(f"unexpected CachyOS source URL: {archive}")
    return version, srcname, archive


def existing_patch_files(patch: Path) -> list[str]:
    paths = []
    current = None
    for line in patch.read_text(errors="replace").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            current = parts[3][2:] if len(parts) >= 4 else None
        elif line.startswith("--- ") and current is not None:
            if line.strip() != "--- /dev/null":
                paths.append(current)
            current = None
    return sorted(set(paths))


def extract_patch_inputs(tarball: Path, source: Path, files: list[str]) -> None:
    listing = quiet_run(["tar", "-tf", tarball]).stdout.splitlines()
    wanted = set(files)
    members = []
    for member in listing:
        _, separator, relative = member.partition("/")
        if separator and relative in wanted:
            members.append(member)
    if not members:
        raise OptionError(f"no CLASP input files found in {tarball.name}")

    if source.exists():
        shutil.rmtree(source)
    source.mkdir(parents=True)
    clasp.run(
        ["tar", "-xf", tarball, "-C", source, "--strip-components=1", *members],
        timeout=900,
    )


def generate_cachyos_patch(
    work: Path,
    version: str,
    srcname: str,
    archive: str,
) -> tuple[Path, str]:
    reference, reference_kernel, clasp_version = clasp.find_reference(version)
    clasp.log(f"CachyOS kernel: {version}")
    clasp.log(f"CLASP reference: {reference.name} ({reference_kernel})")

    downloads = work / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    tarball = downloads / f"{srcname}.tar.gz"
    if not tarball.exists():
        clasp.download(archive, tarball)

    root = work / srcname
    for name in ("src", "patchgen", "verify", "logs"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)
    logs = root / "logs"
    logs.mkdir(parents=True)

    source = root / "src"
    patchgen = root / "patchgen"
    files = clasp.affected_files(reference)
    required = existing_patch_files(reference)
    clasp.log("extracting the files needed for the CLASP apply check")
    extract_patch_inputs(tarball, source, files)
    missing = [relative for relative in required if not (source / relative).is_file()]
    if missing:
        raise OptionError(
            "CachyOS archive is missing patch inputs: " + ", ".join(missing)
        )

    baseline = clasp.snapshot_baseline(source, patchgen, files)
    clasp.apply_reference(source, reference, logs / "apply-reference.log")
    generated = root / f"0001-cachyos{version}-clasp-{clasp_version}.patch"
    clasp.generate_patch(
        source, patchgen, files, version, clasp_version, generated
    )
    clasp.validate_patch(
        patchgen,
        generated,
        baseline,
        root / "verify",
        logs / "validate-patch.log",
    )
    clasp.log(f"CLASP applies cleanly to the CachyOS {version} source")
    return generated, clasp_version


def add_source_entry(text: str, patch_name: str) -> str:
    match = re.search(r"(?ms)^source=\(\n.*?^\)\n", text)
    if match is None:
        raise OptionError("could not find the initial PKGBUILD source array")
    entry = f'source+=("{patch_name}")\n'
    return text[:match.end()] + entry + text[match.end():]


def add_config_entry(text: str) -> str:
    marker = "    ### Rewrite configuration\n"
    if text.count(marker) != 1:
        raise OptionError("could not find the PKGBUILD configuration step")
    entry = (
        '    echo "Enabling CLASP scheduler..."\n'
        "    scripts/config -e SCHED_CLASP\n\n"
    )
    return text.replace(marker, entry + marker, 1)


def install_option(repo: Path, generated: Path) -> Path:
    package_dir = repo / "linux-cachyos"
    pkgbuild = package_dir / "PKGBUILD"
    destination = package_dir / generated.name
    if destination.exists():
        raise OptionError(f"refusing to overwrite untracked file {destination}")

    original = pkgbuild.read_text()
    updated = add_config_entry(add_source_entry(original, generated.name))
    created = False
    try:
        shutil.copy2(generated, destination)
        created = True
        pkgbuild.write_text(updated)
        git(
            repo,
            "add",
            "--",
            "linux-cachyos/PKGBUILD",
            f"linux-cachyos/{destination.name}",
        )
        git(
            repo,
            "-c",
            "user.name=CLASP-Scheduler",
            "-c",
            "user.email=security@cyfare.net",
            "commit",
            "-m",
            COMMIT_SUBJECT,
        )
    except Exception:
        git(repo, "reset", "--hard", "HEAD", check=False)
        if created:
            destination.unlink(missing_ok=True)
        raise
    return destination


def write_preset(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / "clasp.toml"
    temporary = cache / "clasp.toml.tmp"
    temporary.write_text(PRESET)
    os.replace(temporary, destination)
    return destination


def parse_args():
    parser = argparse.ArgumentParser(
        description="Add the latest CachyOS kernel and a source-checked CLASP "
                    "patch to CachyOS Kernel Manager's Configure dialog.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"manager cache directory (default: {DEFAULT_CACHE})",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK,
        help=f"download and patch-generation cache (default: {DEFAULT_WORK})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cache = args.cache_dir.expanduser().resolve()
    work = args.work_dir.expanduser().resolve()
    try:
        check_tools()
        repo = prepare_checkout(cache)
        pkgbuild = repo / "linux-cachyos" / "PKGBUILD"
        if not pkgbuild.is_file():
            raise OptionError(f"missing {pkgbuild}")
        version, srcname, archive = pkgbuild_metadata(pkgbuild)
        generated, clasp_version = generate_cachyos_patch(
            work, version, srcname, archive
        )
        destination = install_option(repo, generated)
        preset = write_preset(cache)

        clasp.log(f"installed {destination.name} (CLASP {clasp_version})")
        print(f"""
Next steps in CachyOS Kernel Manager:
  1. Click Configure.
  2. Keep 'CachyOS default Scheduler (tuned EEVDF)' selected.
  3. Click Load and select: {preset}
  4. On the Patches tab, confirm that {destination.name} is listed.
  5. Click Build kernel. After a successful build, accept the install prompt.

The preset names the packages {KERNEL_PACKAGE} and {KERNEL_PACKAGE}-headers,
so they do not replace the normal linux-cachyos packages. Re-run this script
when you want to refresh the option to a newer CachyOS kernel or CLASP patch.
""".rstrip())
        return 0
    except (OptionError, clasp.DistError, OSError) as exc:
        print(f"[clasp] ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[clasp] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
