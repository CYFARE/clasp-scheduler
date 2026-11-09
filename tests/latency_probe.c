// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int ncpu;
static int pin_cpu_id;
static _Atomic int *shared_stop;
static unsigned long *shared_count;
static unsigned long *shared_ops;

static inline void cpu_relax(void)
{
	__asm__ __volatile__("pause" ::: "memory");
}

static void write_all(int fd, const char *buf, size_t len)
{
	while (len) {
		ssize_t n = write(fd, buf, len);

		if (n > 0) {
			buf += n;
			len -= (size_t)n;
		} else if (n < 0 && errno == EINTR) {
			continue;
		} else {
			break;
		}
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
	if (n >= (int)sizeof(buf))
		n = sizeof(buf) - 1;
	write_all(STDOUT_FILENO, buf, (size_t)n);
}

static long long now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static void pin_to_cpu(int cpu)
{
	cpu_set_t set;

	if (ncpu <= 0)
		return;
	CPU_ZERO(&set);
	CPU_SET(cpu % ncpu, &set);
	if (sched_setaffinity(0, sizeof(set), &set)) {
		perror("sched_setaffinity");
		exit(1);
	}
}

static void busy_for_ns(long long duration)
{
	long long end = now_ns() + duration;

	while (now_ns() < end)
		cpu_relax();
}

static void *hog_thread(void *arg)
{
	uint64_t x = 0x9e3779b97f4a7c15ULL;
	unsigned long ops = 0;

	(void)arg;
	pin_to_cpu(pin_cpu_id);
	while (!*shared_stop) {
		int i;

		for (i = 0; i < 4096; i++) {
			x ^= x << 13;
			x ^= x >> 7;
			x ^= x << 17;
		}
		__asm__ __volatile__("" : "+r"(x));
		ops += 4096;
	}
	__sync_fetch_and_add(shared_ops, ops);
	return NULL;
}

static void forker_process(void)
{
	pin_to_cpu(pin_cpu_id);

	while (!*shared_stop) {
		pid_t pid = fork();

		if (pid == 0) {
			pin_to_cpu(pin_cpu_id);
			busy_for_ns(2000000);
			_exit(0);
		} else if (pid > 0) {
			int status;

			waitpid(pid, &status, 0);
			__sync_fetch_and_add(shared_count, 1);
		} else {
			_exit(1);
		}
	}
}

static int cmp_u32(const void *a, const void *b)
{
	uint32_t x = *(const uint32_t *)a;
	uint32_t y = *(const uint32_t *)b;

	return (x > y) - (x < y);
}

static uint32_t latency_percentile(const uint32_t *v, long n, int pct)
{
	long idx;

	if (!n)
		return 0;
	idx = (n * pct) / 100;
	if (idx >= n)
		idx = n - 1;
	return v[idx];
}

static uint32_t latency_permille(const uint32_t *v, long n, int permille)
{
	long idx;

	if (!n)
		return 0;
	idx = (n * permille) / 1000;
	if (idx >= n)
		idx = n - 1;
	return v[idx];
}

static int run_latency(const char *tag, int hogs, int forkers,
		       int interval_us, int seconds)
{
	long max_samples = (long)seconds * 1000000L / interval_us + 10000;
	uint32_t *lat;
	pthread_t *threads = NULL;
	pid_t *forkers_pid = NULL;
	long n = 0;
	unsigned long long sum = 0;
	long long end, start;
	struct timespec wake;
	int i;

	lat = calloc(max_samples, sizeof(*lat));
	if (!lat)
		return 1;

	out("LAT setup hogs=%d forkers=%d max=%ld\n", hogs, forkers,
	    max_samples);

	*shared_stop = 0;
	*shared_count = 0;
	*shared_ops = 0;

	if (hogs > 0) {
		threads = calloc(hogs, sizeof(*threads));
		if (!threads)
			return 1;
		for (i = 0; i < hogs; i++) {
			if (pthread_create(&threads[i], NULL, hog_thread, NULL)) {
				perror("pthread_create");
				return 1;
			}
		}
	}

	if (forkers > 0) {
		forkers_pid = calloc(forkers, sizeof(*forkers_pid));
		if (!forkers_pid)
			return 1;
		for (i = 0; i < forkers; i++) {
			pid_t pid = fork();

			if (pid == 0) {
				forker_process();
				_exit(0);
			}
			if (pid < 0) {
				perror("fork");
				return 1;
			}
			forkers_pid[i] = pid;
		}
	}

	/* Let the load and the scheduler settle before measuring. */
	pin_to_cpu(pin_cpu_id);
	out("LAT warm\n");
	{
		struct timespec warm = { .tv_sec = 0, .tv_nsec = 500000000 };

		nanosleep(&warm, NULL);
	}
	out("LAT go\n");

	clock_gettime(CLOCK_MONOTONIC, &wake);
	start = now_ns();
	end = start + (long long)seconds * 1000000000LL;

	while (n < max_samples) {
		long long expected, late;
		int ret;

		wake.tv_nsec += (long)interval_us * 1000L;
		while (wake.tv_nsec >= 1000000000L) {
			wake.tv_sec++;
			wake.tv_nsec -= 1000000000L;
		}

		do {
			ret = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME,
					      &wake, NULL);
		} while (ret == EINTR);

		{
			struct timespec got;

			clock_gettime(CLOCK_MONOTONIC, &got);
			expected = (long long)wake.tv_sec * 1000000000LL +
				   wake.tv_nsec;
			late = ((long long)got.tv_sec * 1000000000LL +
				got.tv_nsec) - expected;
		}
		if (late < 0)
			late = 0;
		if (late > UINT32_MAX)
			late = UINT32_MAX;

		lat[n++] = (uint32_t)late;
		sum += late;

		if (now_ns() >= end)
			break;
	}

	*shared_stop = 1;

	out("LAT measured n=%ld\n", n);

	if (threads) {
		for (i = 0; i < hogs; i++)
			pthread_join(threads[i], NULL);
	}
	if (forkers_pid) {
		for (i = 0; i < forkers; i++)
			waitpid(forkers_pid[i], NULL, 0);
	}

	qsort(lat, n, sizeof(*lat), cmp_u32);

	out("RESULT latency tag=%s hogs=%d forkers=%d interval_us=%d "
	       "samples=%ld p50_us=%.1f p90_us=%.1f p99_us=%.1f "
	       "p999_us=%.1f max_us=%.1f mean_us=%.1f forks=%lu\n",
	       tag, hogs, forkers, interval_us, n,
	       latency_percentile(lat, n, 50) / 1000.0,
	       latency_percentile(lat, n, 90) / 1000.0,
	       latency_percentile(lat, n, 99) / 1000.0,
	       latency_permille(lat, n, 999) / 1000.0,
	       lat[n - 1] / 1000.0,
	       n ? (double)sum / n / 1000.0 : 0.0,
	       *shared_count);

	free(lat);
	free(threads);
	free(forkers_pid);
	return 0;
}

static void pipe_child(int idx, int rfd, int wfd, int seconds)
{
	char c = 'x';

	pin_to_cpu(idx % ncpu);
	alarm((unsigned int)seconds + 1);

	for (;;) {
		if (write(wfd, &c, 1) != 1)
			break;
		if (read(rfd, &c, 1) != 1)
			break;
		__sync_fetch_and_add(shared_count, 1);
	}
	_exit(0);
}

static int run_pipe(const char *tag, int pairs, int seconds)
{
	pid_t *pids;
	int i;

	if (pairs <= 0)
		return 1;

	*shared_count = 0;
	pids = calloc((size_t)pairs * 2, sizeof(*pids));
	if (!pids)
		return 1;

	for (i = 0; i < pairs; i++) {
		int p1[2], p2[2];
		pid_t a, b;

		if (pipe(p1) || pipe(p2)) {
			perror("pipe");
			return 1;
		}

		a = fork();
		if (a == 0) {
			close(p1[0]);
			close(p2[1]);
			pipe_child(i, p2[0], p1[1], seconds);
		}
		b = fork();
		if (b == 0) {
			close(p2[0]);
			close(p1[1]);
			pipe_child(i, p1[0], p2[1], seconds);
		}

		close(p1[0]);
		close(p1[1]);
		close(p2[0]);
		close(p2[1]);
		if (a < 0 || b < 0) {
			perror("fork");
			return 1;
		}
		pids[i * 2] = a;
		pids[i * 2 + 1] = b;
	}

	for (i = 0; i < pairs * 2; i++)
		waitpid(pids[i], NULL, 0);

	out("RESULT pipe tag=%s pairs=%d seconds=%d msgs=%lu "
	       "msgs_per_sec=%.0f\n",
	       tag, pairs, seconds, *shared_count,
	       (double)*shared_count / (seconds + 1));

	free(pids);
	return 0;
}

static void *cpu_thread(void *arg)
{
	long idx = (long)arg;
	uint64_t x = 0x9e3779b97f4a7c15ULL + (uint64_t)idx;
	unsigned long ops = 0;

	pin_to_cpu((int)idx % ncpu);
	while (!*shared_stop) {
		int i;

		for (i = 0; i < 4096; i++) {
			x ^= x << 13;
			x ^= x >> 7;
			x ^= x << 17;
		}
		__asm__ __volatile__("" : "+r"(x));
		ops += 4096;
	}
	shared_ops[idx] = ops;
	return NULL;
}

static int run_cpu(const char *tag, int threads, int seconds)
{
	pthread_t *th;
	unsigned long long total = 0;
	struct timespec run = { .tv_sec = seconds, .tv_nsec = 0 };
	long i;

	if (threads <= 0)
		return 1;

	th = calloc(threads, sizeof(*th));
	if (!th)
		return 1;

	*shared_stop = 0;
	memset(shared_ops, 0, (size_t)threads * sizeof(*shared_ops));

	for (i = 0; i < threads; i++) {
		if (pthread_create(&th[i], NULL, cpu_thread, (void *)i))
			return 1;
	}

	nanosleep(&run, NULL);
	*shared_stop = 1;

	for (i = 0; i < threads; i++) {
		pthread_join(th[i], NULL);
		total += shared_ops[i];
	}

	out("RESULT cpu tag=%s threads=%d seconds=%d ops=%llu ops_per_sec=%.0f\n",
	       tag, threads, seconds, total,
	       (double)total / seconds);

	free(th);
	return 0;
}

static int run_fork(const char *tag, int forkers, int seconds)
{
	pid_t *pids;
	struct timespec run = { .tv_sec = seconds, .tv_nsec = 0 };
	int i;

	if (forkers <= 0)
		return 1;

	pids = calloc(forkers, sizeof(*pids));
	if (!pids)
		return 1;

	*shared_stop = 0;
	*shared_count = 0;

	for (i = 0; i < forkers; i++) {
		pid_t pid = fork();

		if (pid == 0) {
			forker_process();
			_exit(0);
		}
		if (pid < 0) {
			perror("fork");
			return 1;
		}
		pids[i] = pid;
	}

	nanosleep(&run, NULL);
	*shared_stop = 1;

	for (i = 0; i < forkers; i++)
		waitpid(pids[i], NULL, 0);

	out("RESULT fork tag=%s forkers=%d seconds=%d forks=%lu forks_per_sec=%.0f\n",
	       tag, forkers, seconds, *shared_count,
	       (double)*shared_count / seconds);

	free(pids);
	return 0;
}

struct ipc_args {
	int wfd;
	int ackfd;
	long samples;
	int interval_us;
	_Atomic uint64_t *wake_ts;
};

static void *ipc_waker_thread(void *arg)
{
	struct ipc_args *a = arg;
	struct timespec next;
	char c = 'x';
	long j;

	pin_to_cpu(1);
	clock_gettime(CLOCK_MONOTONIC, &next);
	for (j = 0; j < a->samples; j++) {
		next.tv_nsec += (long)a->interval_us * 1000L;
		while (next.tv_nsec >= 1000000000L) {
			next.tv_sec++;
			next.tv_nsec -= 1000000000L;
		}
		while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME,
				       &next, NULL) == EINTR)
			;
		*a->wake_ts = (uint64_t)now_ns();
		if (write(a->wfd, &c, 1) != 1)
			break;
		if (read(a->ackfd, &c, 1) != 1)
			break;
	}
	return NULL;
}

static int run_ipc(const char *tag, int hogs, int interval_us, int seconds)
{
	pthread_t *threads = NULL, waker;
	struct ipc_args args;
	uint32_t *lat;
	_Atomic uint64_t *wake_ts;
	long samples, i, n = 0;
	unsigned long long sum = 0;
	int p1[2], p2[2];

	if (ncpu < 2 || interval_us <= 0 || seconds <= 0)
		return 1;

	samples = (long)seconds * 1000000L / interval_us;
	if (samples <= 0)
		return 1;

	lat = calloc(samples, sizeof(*lat));
	if (!lat)
		return 1;

	wake_ts = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
		       MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (wake_ts == MAP_FAILED)
		return 1;

	if (pipe(p1) || pipe(p2))
		return 1;

	*shared_stop = 0;
	*shared_count = 0;
	*shared_ops = 0;

	if (hogs > 0) {
		threads = calloc(hogs, sizeof(*threads));
		if (!threads)
			return 1;
		for (i = 0; i < hogs; i++) {
			if (pthread_create(&threads[i], NULL, hog_thread, NULL))
				return 1;
		}
	}

	args.wfd = p1[1];
	args.ackfd = p2[0];
	args.samples = samples;
	args.interval_us = interval_us;
	args.wake_ts = wake_ts;

	/*
	 * The waker runs on a different vCPU.  Its timer is not competing with
	 * the hogs, so what remains is the pipe wakeup IPI plus the probe's
	 * scheduling latency on the loaded CPU.
	 */
	if (pthread_create(&waker, NULL, ipc_waker_thread, &args))
		return 1;

	pin_to_cpu(0);
	for (i = 0; i < samples; i++) {
		long long t;
		char c;

		if (read(p1[0], &c, 1) != 1)
			break;
		t = now_ns();
		if (t >= (long long)*wake_ts) {
			long long d = t - (long long)*wake_ts;

			lat[n++] = d > UINT32_MAX ? UINT32_MAX :
				   (uint32_t)d;
			sum += (unsigned long long)d;
		}
		if (write(p2[1], &c, 1) != 1)
			break;
	}

	pthread_join(waker, NULL);
	*shared_stop = 1;
	if (threads) {
		for (i = 0; i < hogs; i++)
			pthread_join(threads[i], NULL);
	}

	qsort(lat, n, sizeof(*lat), cmp_u32);
	out("RESULT ipc tag=%s hogs=%d interval_us=%d samples=%ld "
	    "p50_us=%.1f p90_us=%.1f p99_us=%.1f p999_us=%.1f "
	    "max_us=%.1f mean_us=%.1f\n",
	    tag, hogs, interval_us, n,
	    latency_percentile(lat, n, 50) / 1000.0,
	    latency_percentile(lat, n, 90) / 1000.0,
	    latency_percentile(lat, n, 99) / 1000.0,
	    latency_permille(lat, n, 999) / 1000.0,
	    n ? lat[n - 1] / 1000.0 : 0.0,
	    n ? (double)sum / n / 1000.0 : 0.0);

	free(lat);
	free(threads);
	return 0;
}

static void usage(void)
{
	fprintf(stderr,
		"usage: bench <tag> latency <hogs> <forkers> <interval_us> <sec>\n"
		"       bench <tag> ipc <hogs> <interval_us> <sec>\n"
		"       bench <tag> pipe <pairs> <sec>\n"
		"       bench <tag> cpu <threads> <sec>\n"
		"       bench <tag> fork <forkers> <sec>\n");
}

int main(int argc, char **argv)
{
	void *mem;

	setvbuf(stdout, NULL, _IONBF, 0);
	ncpu = (int)sysconf(_SC_NPROCESSORS_ONLN);
	if (ncpu <= 0)
		ncpu = 1;
	pin_cpu_id = 0;

	mem = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
		   MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (mem == MAP_FAILED)
		return 1;
	shared_stop = mem;
	shared_count = (unsigned long *)((char *)mem + 64);
	shared_ops = (unsigned long *)((char *)mem + 128);

	if (argc < 3) {
		usage();
		return 1;
	}

	out("BEGIN %s %s\n", argv[1], argv[2]);

	if (!strcmp(argv[2], "latency") && argc == 7)
		return run_latency(argv[1], atoi(argv[3]), atoi(argv[4]),
				   atoi(argv[5]), atoi(argv[6]));
	if (!strcmp(argv[2], "pipe") && argc == 5)
		return run_pipe(argv[1], atoi(argv[3]), atoi(argv[4]));
	if (!strcmp(argv[2], "cpu") && argc == 5)
		return run_cpu(argv[1], atoi(argv[3]), atoi(argv[4]));
	if (!strcmp(argv[2], "fork") && argc == 5)
		return run_fork(argv[1], atoi(argv[3]), atoi(argv[4]));
	if (!strcmp(argv[2], "ipc") && argc == 6)
		return run_ipc(argv[1], atoi(argv[3]), atoi(argv[4]),
			       atoi(argv[5]));

	usage();
	return 1;
}
