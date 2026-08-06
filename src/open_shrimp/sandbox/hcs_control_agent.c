/* Control-channel agent for the HCS guest's initramfs (PID 1's only service).
 *
 * Listens on AF_VSOCK port 0x5000 -- the port `hcs_helpers.CONTROL_PORT`
 * names -- and prints AGENT-LISTENING on the console once bound, which is the
 * marker the host waits for before it starts issuing commands.
 *
 * Protocol: one connection per command, request is a single '\n'-terminated
 * line, connection close means output is complete.  Everything runs through
 * /bin/sh -c with stderr folded into stdout, except two in-agent commands
 * that cannot be shell commands because busybox mount cannot dial a socket:
 *
 *   @mount <port> <aname> <target> [extra-opts]
 *       Dial AF_VSOCK to the host (VMADDR_CID_HOST) at <port> -- the VM
 *       worker hosts the 9p server for a Plan9Share there -- then issue
 *       mount(2) with type "9p" and trans=fd over that socket.  <extra-opts>
 *       is appended verbatim to the mount data string, so the host owns the
 *       version/msize choice.  Replies MOUNT-OK or MOUNT-FAIL with the
 *       failing step and raw errno.
 *   @umount <target>
 *       umount2(<target>, 0), so a share can be remounted with new options.
 *
 * Statically linked on purpose: the initramfs root carries neither a dynamic
 * loader nor a libc.  Built by scripts/build_hcs_initrd.sh.
 */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <linux/vm_sockets.h>

#define AGENT_PORT 0x5000
#define CONNECT_TIMEOUT_MS 10000

/* Bounded connect: a wedged dial must not stall the single-threaded agent
 * loop, so connect non-blocking and poll with a deadline. */
static int vsock_dial_host(unsigned port, int *err_out)
{
	int fd = socket(AF_VSOCK, SOCK_STREAM, 0);
	if (fd < 0) {
		*err_out = errno;
		return -1;
	}
	struct sockaddr_vm a;
	memset(&a, 0, sizeof(a));
	a.svm_family = AF_VSOCK;
	a.svm_cid = VMADDR_CID_HOST;
	a.svm_port = port;
	fcntl(fd, F_SETFL, O_NONBLOCK);
	int rc = connect(fd, (struct sockaddr *)&a, sizeof(a));
	if (rc < 0 && errno == EINPROGRESS) {
		struct pollfd p = { .fd = fd, .events = POLLOUT };
		rc = poll(&p, 1, CONNECT_TIMEOUT_MS);
		if (rc == 1) {
			int soerr = 0;
			socklen_t sl = sizeof(soerr);
			getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl);
			errno = soerr;
			rc = soerr ? -1 : 0;
		} else {
			errno = ETIMEDOUT;
			rc = -1;
		}
	}
	if (rc < 0) {
		*err_out = errno;
		close(fd);
		return -1;
	}
	fcntl(fd, F_SETFL, 0);
	return fd;
}

static void handle_mount(int cs, const char *args)
{
	unsigned port = 0;
	char aname[128], target[128], extra[256];
	extra[0] = '\0';
	int n = sscanf(args, "%u %127s %127s %255s", &port, aname, target, extra);
	if (n < 3) {
		dprintf(cs, "MOUNT-FAIL step=parse args='%s'\n", args);
		return;
	}
	int err = 0;
	int fd = vsock_dial_host(port, &err);
	if (fd < 0) {
		dprintf(cs, "MOUNT-FAIL step=connect port=%u errno=%d (%s)\n",
			port, err, strerror(err));
		return;
	}
	mkdir(target, 0755);
	char data[512];
	snprintf(data, sizeof(data), "trans=fd,rfdno=%d,wfdno=%d,aname=%s%s%s",
		 fd, fd, aname, extra[0] ? "," : "", extra);
	if (mount(aname, target, "9p", 0, data) < 0) {
		dprintf(cs, "MOUNT-FAIL step=mount errno=%d (%s) data=%s\n",
			errno, strerror(errno), data);
		close(fd);
		return;
	}
	dprintf(cs, "MOUNT-OK target=%s data=%s\n", target, data);
	/* The 9p client fget()s rfdno/wfdno at mount time; the mount holds its
	 * own references, so the agent's fd can close. */
	close(fd);
}

static void handle_umount(int cs, const char *args)
{
	char target[128];
	if (sscanf(args, "%127s", target) != 1) {
		dprintf(cs, "UMOUNT-FAIL step=parse\n");
		return;
	}
	if (umount2(target, 0) < 0) {
		dprintf(cs, "UMOUNT-FAIL target=%s errno=%d (%s)\n",
			target, errno, strerror(errno));
		return;
	}
	dprintf(cs, "UMOUNT-OK target=%s\n", target);
}

int main(void)
{
	int ls = socket(AF_VSOCK, SOCK_STREAM, 0);
	if (ls < 0) {
		perror("AGENT socket(AF_VSOCK)");
		return 1;
	}

	struct sockaddr_vm addr;
	memset(&addr, 0, sizeof(addr));
	addr.svm_family = AF_VSOCK;
	addr.svm_cid = VMADDR_CID_ANY;
	addr.svm_port = AGENT_PORT;
	if (bind(ls, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		perror("AGENT bind");
		return 1;
	}
	if (listen(ls, 4) < 0) {
		perror("AGENT listen");
		return 1;
	}

	printf("AGENT-LISTENING port=%d\n", AGENT_PORT);
	fflush(stdout);

	for (;;) {
		int cs = accept(ls, NULL, NULL);
		if (cs < 0)
			continue;

		char cmd[4096];
		size_t off = 0;
		while (off < sizeof(cmd) - 1) {
			char c;
			ssize_t n = read(cs, &c, 1);
			if (n <= 0 || c == '\n')
				break;
			cmd[off++] = c;
		}
		cmd[off] = '\0';

		if (strncmp(cmd, "@mount ", 7) == 0) {
			handle_mount(cs, cmd + 7);
			close(cs);
			continue;
		}
		if (strncmp(cmd, "@umount ", 8) == 0) {
			handle_umount(cs, cmd + 8);
			close(cs);
			continue;
		}

		/* Fold stderr into stdout so a single stream carries both. */
		char full[4200];
		snprintf(full, sizeof(full), "%s 2>&1", cmd);
		FILE *p = popen(full, "r");
		if (p) {
			char out[4096];
			size_t m;
			while ((m = fread(out, 1, sizeof(out), p)) > 0) {
				size_t w = 0;
				while (w < m) {
					ssize_t k = write(cs, out + w, m - w);
					if (k <= 0)
						break;
					w += (size_t)k;
				}
			}
			pclose(p);
		}
		close(cs);
	}
	return 0;
}
