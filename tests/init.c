// SPDX-License-Identifier: GPL-2.0
/*
 * Minimal static PID 1 for the CLASP KVM benchmark image.  It mounts the
 * pseudo-filesystems, runs /bench for each phase, prints machine-readable
 * output, and powers the VM off.  No shell or external tools are needed.
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static void write_all(int fd, const char *buf, size_t len)
{
	while (len) {
		ssize_t n = write(fd, buf, len);

		if (n <= 0)
			break;
		buf += n;
		len -= (size_t)n;
	}
}

static void out(const char *fmt, ...)
{
	char buf[1024];
	va_list ap;
	int n;

	va_start(ap, fmt);
	n = vsnprintf(buf, sizeof(buf), fmt, ap);
	va_end(ap);
	if (n < 0)
		return;
	if (n > (int)sizeof(buf))
		n = sizeof(buf);
	write_all(STDOUT_FILENO, buf, (size_t)n);
}

static void show_file(const char *path)
{
	char buf[4096];
	ssize_t n;
	int fd = open(path, O_RDONLY);

	if (fd < 0)
		return;
	while ((n = read(fd, buf, sizeof(buf))) > 0)
		write_all(STDOUT_FILENO, buf, (size_t)n);
	close(fd);
}

static int write_str(const char *path, const char *value)
{
	ssize_t n;
	int fd = open(path, O_WRONLY);

	if (fd < 0)
		return -1;
	n = write(fd, value, strlen(value));
	close(fd);
	return n == (ssize_t)strlen(value) ? 0 : -1;
}

static int run(char *const argv[])
{
	pid_t pid = fork();
	int status;

	if (pid == 0) {
		execv(argv[0], argv);
		_exit(127);
	}
	if (pid < 0)
		return -1;
	if (waitpid(pid, &status, 0) < 0)
		return -1;
	return status;
}

static void bench(const char *tag, const char *mode, const char *a,
		  const char *b, const char *c, const char *d)
{
	char *argv[] = { "/bench", (char *)tag, (char *)mode,
			 (char *)a, (char *)b, (char *)c, (char *)d, NULL };
	int status;

	out("RUN %s %s %s %s %s %s\n", tag, mode,
	       a ? a : "-", b ? b : "-", c ? c : "-", d ? d : "-");
	status = run(argv);
	if (status)
		out("ERROR bench %s %s status=%d\n", tag, mode, status);
}

static int cmdline_has(const char *needle)
{
	char buf[1024];
	ssize_t n;
	int fd = open("/proc/cmdline", O_RDONLY);

	if (fd < 0)
		return 0;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return 0;
	buf[n] = 0;
	return strstr(buf, needle) != NULL;
}

static void suite(const char *tag)
{
	out("PHASE %s\n", tag);

	/* Wake-to-run latency, all load pinned to one vCPU. */
	bench(tag, "latency", "0", "0", "1000", "3");
	bench(tag, "latency", "1", "0", "1000", "4");
	bench(tag, "latency", "4", "0", "1000", "4");
	bench(tag, "latency", "8", "0", "1000", "4");
	bench(tag, "latency", "16", "0", "1000", "4");

	/* Fork-storm interference. */
	bench(tag, "latency", "4", "2", "1000", "4");

	/* IPC wakeup latency: the waker is on another vCPU. */
	bench(tag, "ipc", "1", "1000", "4", NULL);
	bench(tag, "ipc", "4", "1000", "4", NULL);
	bench(tag, "ipc", "8", "1000", "4", NULL);
	bench(tag, "ipc", "16", "1000", "4", NULL);

	/* Scheduler-heavy ping-pong and CPU throughput. */
	bench(tag, "pipe", "1", "3", NULL, NULL);
	bench(tag, "pipe", "4", "3", NULL, NULL);
	bench(tag, "pipe", "8", "3", NULL, NULL);
	bench(tag, "cpu", "8", "3", NULL, NULL);

	/* Fork/exit throughput. */
	bench(tag, "fork", "4", "3", NULL, NULL);
}

int main(void)
{
	int has_clasp, has_bore;

	setvbuf(stdout, NULL, _IONBF, 0);

	mount("proc", "/proc", "proc", 0, NULL);
	mount("sysfs", "/sys", "sysfs", 0, NULL);
	mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
	mkdir("/sys/kernel/debug", 0755);
	mount("debugfs", "/sys/kernel/debug", "debugfs", 0, NULL);

	out("BOOT ");
	show_file("/proc/version");
	out("CMDLINE ");
	show_file("/proc/cmdline");
	out("ONLINE ");
	show_file("/sys/devices/system/cpu/online");

	has_clasp = access("/proc/sys/kernel/sched_clasp", F_OK) == 0;
	has_bore = access("/proc/sys/kernel/sched_bore", F_OK) == 0;
	out("CONTROLS clasp=%d bore=%d\n", has_clasp, has_bore);

	if (cmdline_has("clasp.quick=1")) {
		out("QUICK\n");
		bench("q", "cpu", "1", "1", NULL, NULL);

		if (has_clasp) {
			write_str("/proc/sys/kernel/sched_clasp", "0");
			bench("q", "latency", "16", "0", "1000", "4");
			bench("q", "ipc", "16", "1000", "4", NULL);
			show_file("/sys/kernel/debug/sched_clasp");

			write_str("/proc/sys/kernel/sched_clasp", "1");
			write_str("/proc/sys/kernel/sched_clasp_target_min_us", "100");
			write_str("/proc/sys/kernel/sched_clasp_target_max_us", "8000");
			write_str("/proc/sys/kernel/sched_clasp_boost_max_us", "8000");
			write_str("/proc/sys/kernel/sched_clasp_budget_pct", "100");
			write_str("/proc/sys/kernel/sched_clasp_budget_cap_us", "16000");
			write_str("/proc/sys/kernel/sched_clasp_duty_pct", "100");
			bench("q", "latency", "16", "0", "1000", "4");
			bench("q", "ipc", "16", "1000", "4", NULL);
			show_file("/sys/kernel/debug/sched_clasp");
			bench("q", "latency", "8", "0", "1000", "4");
		} else if (has_bore) {
			write_str("/proc/sys/kernel/sched_bore", "0");
			bench("q", "ipc", "16", "1000", "4", NULL);
			write_str("/proc/sys/kernel/sched_bore", "1");
			bench("q", "ipc", "16", "1000", "4", NULL);
			bench("q", "latency", "16", "0", "1000", "4");
		} else {
			bench("q", "latency", "0", "0", "1000", "3");
		}

		out("QUICK_DONE\n");
		goto done;
	}

	if (has_clasp) {
		write_str("/proc/sys/kernel/sched_clasp", "0");
		suite("ctrl");
		show_file("/sys/kernel/debug/sched_clasp");

		write_str("/proc/sys/kernel/sched_clasp", "1");
		suite("clasp");
		show_file("/sys/kernel/debug/sched_clasp");
	} else if (has_bore) {
		write_str("/proc/sys/kernel/sched_bore", "0");
		suite("bore-off");

		write_str("/proc/sys/kernel/sched_bore", "1");
		suite("bore-on");
	} else {
		suite("base");
	}

	out("BENCH_DONE\n");
done:
	sync();
	sleep(1);
	reboot(RB_POWER_OFF);

	/* QEMU with -no-reboot exits on either poweroff or reboot. */
	sync();
	reboot(RB_AUTOBOOT);
	for (;;)
		pause();
}
