/* Native half of the HCS computer-use RDP session (see hcs_rdp.py).
 *
 * Holds one persistent FreeRDP client connection to the guest's weston-RDP
 * desktop and exposes it over a loopback control socket:
 *
 *   - frames: gdi software decoding into a memory buffer; every paint is
 *     retained in a stable copy with a sequence number, so the latest frame
 *     is always servable (the RDP stream is damage-driven — a static
 *     desktop paints nothing, and callers must never wait for a paint).
 *   - input: mouse/keyboard PDUs via freerdp_input_send_*.
 *   - transport: dials AF_HYPERV to (RuntimeId, svc(3389)) itself through
 *     an in-process TCP shim (FreeRDP's client entry points take a
 *     host:port, not a pre-connected socket), or a plain TCP target.
 *   - lifecycle: reconnects with backoff when the RDP session drops (the
 *     in-guest supervision restarts weston; this side redials).
 *
 * Control protocol (little-endian, one client at a time; a new connection
 * replaces the old): requests are 13 bytes [u8 op][u32 a][u32 b][u32 c].
 *   op=1 GETFRAME(a=last_seq) -> [u8 ok][u8 connected][u32 seq][u16 w]
 *        [u16 h][u32 len][len bytes BGRX32]; len==0 when seq==last_seq or
 *        no frame has been decoded yet.
 *   op=2 MOUSE(a=flags, b=x, c=y) -> [u8 ok]
 *   op=3 KEY(a=scancode|0x100-extended, b=down) -> [u8 ok]
 *   op=5 STATUS -> [u8 ok][u8 connected][u32 seq][u16 w][u16 h]
 *   op=6 QUIT -> helper exits
 *
 * Usage: hcs_rdp_helper.exe <hv:RUNTIME-GUID | tcp:host:port> <WxH>
 * Prints "HELPER-CONTROL-PORT <port>" on stdout once the control socket
 * listens.
 *
 * Build (MSYS2 mingw64; winpr/winsock.h, never winsock2.h, and
 * -D__STDC_NO_THREADS__ — winpr's platform.h pulls C11 <threads.h> that
 * mingw-w64 lacks):
 *   gcc hcs_rdp_helper.c -o hcs_rdp_helper.exe -D__STDC_NO_THREADS__ -O2 \
 *     $(pkg-config --cflags --libs freerdp-client3 freerdp3 winpr3) -lws2_32
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <winpr/wtypes.h>
#include <winpr/winsock.h>
#include <winpr/synch.h>
#include <freerdp/freerdp.h>
#include <freerdp/client.h>
#include <freerdp/client/cmdline.h>
#include <freerdp/gdi/gdi.h>
#include <freerdp/input.h>

#ifndef AF_HYPERV
#define AF_HYPERV 34
#endif
#define HV_PROTOCOL_RAW 1
#define RDP_VSOCK_PORT 3389

typedef struct
{
	USHORT Family;
	USHORT Reserved;
	GUID VmId;
	GUID ServiceId;
} SOCKADDR_HV_COMPAT;

typedef struct
{
	rdpClientContext common;
} HelperContext;

#pragma pack(push, 1)
typedef struct
{
	BYTE op;
	UINT32 a, b, c;
} CtlRequest;

typedef struct
{
	BYTE ok, connected;
	UINT32 seq;
	UINT16 w, h;
	UINT32 len;
} FrameHdr;

typedef struct
{
	BYTE ok, connected;
	UINT32 seq;
	UINT16 w, h;
} StatusHdr;
#pragma pack(pop)

/* Single-threaded by design: the FreeRDP event pump, frame retention and
 * control-command handling all run on the main thread (FreeRDP input send
 * is not documented thread-safe).  Only the hvsocket<->TCP shim uses
 * worker threads, and it shares no RDP state. */
static volatile BOOL g_quit = FALSE;
static BOOL g_connected = FALSE;
static rdpInput* g_input = NULL;
static BYTE* g_frame = NULL;
static UINT32 g_seq = 0;
static UINT16 g_w = 0, g_h = 0;

static SOCKET g_ctl_listen = INVALID_SOCKET;
static SOCKET g_ctl = INVALID_SOCKET;
static BYTE g_req[sizeof(CtlRequest)];
static int g_req_have = 0;

static GUID g_vm_id;
static char g_tcp_host[256];
static int g_tcp_port = 0;

/* ---- sockets ---------------------------------------------------------- */

static BOOL set_nonblocking(SOCKET s)
{
	u_long on = 1;
	return ioctlsocket(s, FIONBIO, &on) == 0;
}

static BOOL send_all(SOCKET s, const void* buf, int len)
{
	const char* p = (const char*)buf;
	UINT64 deadline = GetTickCount64() + 10000;
	while (len > 0)
	{
		int n = send(s, p, len, 0);
		if (n > 0)
		{
			p += n;
			len -= n;
			continue;
		}
		if (n == SOCKET_ERROR && WSAGetLastError() == WSAEWOULDBLOCK)
		{
			fd_set wf;
			struct timeval tv = { 0, 200000 };
			FD_ZERO(&wf);
			FD_SET(s, &wf);
			select(0, NULL, &wf, NULL, &tv);
			if (GetTickCount64() > deadline)
				return FALSE;
			continue;
		}
		return FALSE;
	}
	return TRUE;
}

static SOCKET listen_loopback(int* port_out)
{
	SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
	struct sockaddr_in addr;
	int alen = sizeof(addr);
	if (s == INVALID_SOCKET)
		return INVALID_SOCKET;
	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_addr.s_addr = htonl(0x7F000001);
	addr.sin_port = 0;
	if (bind(s, (struct sockaddr*)&addr, sizeof(addr)) != 0 || listen(s, 4) != 0 ||
	    getsockname(s, (struct sockaddr*)&addr, &alen) != 0)
	{
		closesocket(s);
		return INVALID_SOCKET;
	}
	*port_out = ntohs(addr.sin_port);
	return s;
}

/* ---- hvsocket <-> TCP shim -------------------------------------------- */

static SOCKET dial_hvsocket(void)
{
	SOCKADDR_HV_COMPAT sa;
	SOCKET s = socket(AF_HYPERV, SOCK_STREAM, HV_PROTOCOL_RAW);
	if (s == INVALID_SOCKET)
		return INVALID_SOCKET;
	memset(&sa, 0, sizeof(sa));
	sa.Family = AF_HYPERV;
	sa.VmId = g_vm_id;
	/* The vsock service GUID template: the port number in Data1. */
	sa.ServiceId.Data1 = RDP_VSOCK_PORT;
	sa.ServiceId.Data2 = 0xfacb;
	sa.ServiceId.Data3 = 0x11e6;
	memcpy(sa.ServiceId.Data4, "\xbd\x58\x64\x00\x6a\x79\x86\xd3", 8);
	if (connect(s, (struct sockaddr*)&sa, sizeof(sa)) != 0)
	{
		closesocket(s);
		return INVALID_SOCKET;
	}
	return s;
}

typedef struct
{
	SOCKET from;
	SOCKET to;
} PumpPair;

static DWORD WINAPI pump_thread(LPVOID arg)
{
	PumpPair* p = (PumpPair*)arg;
	char buf[65536];
	for (;;)
	{
		int n = recv(p->from, buf, sizeof(buf), 0);
		if (n <= 0)
			break;
		if (!send_all(p->to, buf, n))
			break;
	}
	shutdown(p->from, SD_BOTH);
	shutdown(p->to, SD_BOTH);
	free(p);
	return 0;
}

static DWORD WINAPI shim_accept_thread(LPVOID arg)
{
	SOCKET listener = (SOCKET)(UINT_PTR)arg;
	for (;;)
	{
		SOCKET cli = accept(listener, NULL, NULL);
		SOCKET hv;
		PumpPair *a, *b;
		if (cli == INVALID_SOCKET)
			return 0;
		hv = dial_hvsocket();
		if (hv == INVALID_SOCKET)
		{
			printf("SHIM: hvsocket dial failed err=%d\n", WSAGetLastError());
			fflush(stdout);
			closesocket(cli);
			continue;
		}
		a = (PumpPair*)malloc(sizeof(PumpPair));
		b = (PumpPair*)malloc(sizeof(PumpPair));
		a->from = cli;
		a->to = hv;
		b->from = hv;
		b->to = cli;
		CloseHandle(CreateThread(NULL, 0, pump_thread, a, 0, NULL));
		CloseHandle(CreateThread(NULL, 0, pump_thread, b, 0, NULL));
	}
}

/* ---- control channel --------------------------------------------------- */

static void ctl_reply_status(BYTE op)
{
	if (op == 1)
	{
		FrameHdr h = { 1, (BYTE)g_connected, g_seq, g_w, g_h, 0 };
		send_all(g_ctl, &h, sizeof(h));
	}
	else
	{
		BYTE ok = 1;
		send_all(g_ctl, &ok, 1);
	}
}

static void ctl_handle(const CtlRequest* req)
{
	switch (req->op)
	{
		case 1: /* GETFRAME */
		{
			BOOL fresh = (g_seq != 0 && req->a != g_seq && g_frame != NULL);
			FrameHdr h = { 1, (BYTE)g_connected, g_seq, g_w, g_h,
				           fresh ? (UINT32)g_w * g_h * 4 : 0 };
			if (!send_all(g_ctl, &h, sizeof(h)))
				return;
			if (fresh)
				send_all(g_ctl, g_frame, (int)h.len);
			break;
		}
		case 2: /* MOUSE */
			if (g_connected && g_input)
				freerdp_input_send_mouse_event(g_input, (UINT16)req->a, (UINT16)req->b,
				                               (UINT16)req->c);
			ctl_reply_status(2);
			break;
		case 3: /* KEY */
			if (g_connected && g_input)
				freerdp_input_send_keyboard_event_ex(g_input, req->b ? TRUE : FALSE, FALSE,
				                                     req->a);
			ctl_reply_status(3);
			break;
		case 5: /* STATUS */
		{
			StatusHdr h = { 1, (BYTE)g_connected, g_seq, g_w, g_h };
			send_all(g_ctl, &h, sizeof(h));
			break;
		}
		case 6: /* QUIT */
		{
			BYTE ok = 1;
			send_all(g_ctl, &ok, 1);
			g_quit = TRUE;
			break;
		}
		default:
			ctl_reply_status(0);
			break;
	}
}

static void poll_control(void)
{
	SOCKET fresh = accept(g_ctl_listen, NULL, NULL);
	if (fresh != INVALID_SOCKET)
	{
		if (g_ctl != INVALID_SOCKET)
			closesocket(g_ctl);
		set_nonblocking(fresh);
		g_ctl = fresh;
		g_req_have = 0;
	}
	if (g_ctl == INVALID_SOCKET)
		return;
	for (;;)
	{
		int n = recv(g_ctl, (char*)g_req + g_req_have, (int)sizeof(g_req) - g_req_have, 0);
		if (n > 0)
		{
			g_req_have += n;
			if (g_req_have == (int)sizeof(g_req))
			{
				CtlRequest req;
				memcpy(&req, g_req, sizeof(req));
				g_req_have = 0;
				ctl_handle(&req);
				if (g_quit)
					return;
				continue;
			}
			continue;
		}
		if (n == SOCKET_ERROR && WSAGetLastError() == WSAEWOULDBLOCK)
			return;
		/* Closed or errored — drop the client, keep listening. */
		closesocket(g_ctl);
		g_ctl = INVALID_SOCKET;
		g_req_have = 0;
		return;
	}
}

/* ---- FreeRDP client ---------------------------------------------------- */

static BOOL helper_end_paint(rdpContext* context)
{
	rdpGdi* gdi = context->gdi;
	UINT32 y;
	if (!gdi || !gdi->primary_buffer)
		return TRUE;
	if (gdi->width != g_w || gdi->height != g_h || !g_frame)
	{
		free(g_frame);
		g_w = (UINT16)gdi->width;
		g_h = (UINT16)gdi->height;
		g_frame = (BYTE*)malloc((size_t)g_w * g_h * 4);
		if (!g_frame)
			return TRUE;
	}
	for (y = 0; y < g_h; y++)
		memcpy(g_frame + (size_t)y * g_w * 4, gdi->primary_buffer + (size_t)y * gdi->stride,
		       (size_t)g_w * 4);
	g_seq++;
	return TRUE;
}

static BOOL helper_post_connect(freerdp* instance)
{
	if (!gdi_init(instance, PIXEL_FORMAT_BGRX32))
		return FALSE;
	instance->context->update->EndPaint = helper_end_paint;
	printf("CONNECTED %ux%u\n", instance->context->gdi->width, instance->context->gdi->height);
	fflush(stdout);
	return TRUE;
}

static void helper_post_disconnect(freerdp* instance)
{
	gdi_free(instance);
}

static BOOL helper_client_new(freerdp* instance, rdpContext* context)
{
	(void)context;
	instance->PostConnect = helper_post_connect;
	instance->PostDisconnect = helper_post_disconnect;
	return TRUE;
}

static void helper_client_free(freerdp* instance, rdpContext* context)
{
	(void)instance;
	(void)context;
}

static int helper_start(rdpContext* context)
{
	(void)context;
	return 0;
}

static int helper_stop(rdpContext* context)
{
	(void)context;
	return 0;
}

static void run_session(const char* host, int port, const char* size)
{
	RDP_CLIENT_ENTRY_POINTS ep = { 0 };
	rdpContext* context;
	freerdp* instance;
	char varg[300], sarg[64];
	char* argv[8];
	int argc = 0;

	snprintf(varg, sizeof(varg), "/v:%s:%d", host, port);
	snprintf(sarg, sizeof(sarg), "/size:%s", size);
	/* Every argv entry must be writable: the parser scrubs credential
	 * args in place (process-list hygiene), and writing into a string
	 * literal is an access violation. */
	argv[argc++] = _strdup("hcs_rdp_helper");
	argv[argc++] = _strdup(varg);
	argv[argc++] = _strdup("/cert:ignore");
	argv[argc++] = _strdup("/sec:tls");
	/* weston-rdp with TLS security ignores credentials, but without them
	 * the client-common entry points prompt on stdin and freerdp_connect
	 * blocks forever (headless: nobody answers). */
	argv[argc++] = _strdup("/u:openshrimp");
	argv[argc++] = _strdup("/p:openshrimp");
	argv[argc++] = _strdup(sarg);
	argv[argc] = NULL;

	ep.Version = RDP_CLIENT_INTERFACE_VERSION;
	ep.Size = sizeof(RDP_CLIENT_ENTRY_POINTS_V1);
	ep.ContextSize = sizeof(HelperContext);
	ep.ClientNew = helper_client_new;
	ep.ClientFree = helper_client_free;
	ep.ClientStart = helper_start;
	ep.ClientStop = helper_stop;

	context = freerdp_client_context_new(&ep);
	if (!context)
		goto out_argv;
	instance = context->instance;
	if (freerdp_client_settings_parse_command_line(context->settings, argc, argv, FALSE) < 0)
	{
		freerdp_client_context_free(context);
		goto out_argv;
	}
	if (!freerdp_connect(instance))
	{
		printf("CONNECT-FAILED 0x%08X\n", freerdp_get_last_error(context));
		fflush(stdout);
		freerdp_client_context_free(context);
		goto out_argv;
	}
	g_connected = TRUE;
	g_input = context->input;

	while (!g_quit && !freerdp_shall_disconnect_context(context))
	{
		HANDLE handles[MAXIMUM_WAIT_OBJECTS] = { 0 };
		DWORD n = freerdp_get_event_handles(context, handles, ARRAYSIZE(handles));
		if (n == 0)
			break;
		WaitForMultipleObjects(n, handles, FALSE, 50);
		if (!freerdp_check_event_handles(context))
			break;
		poll_control();
	}

	g_connected = FALSE;
	g_input = NULL;
	freerdp_disconnect(instance);
	freerdp_client_context_free(context);
	printf("DISCONNECTED\n");
	fflush(stdout);
out_argv:
	for (int i = 0; i < argc; i++)
		free(argv[i]);
}

/* ---- main -------------------------------------------------------------- */

static BOOL parse_guid(const char* s, GUID* out)
{
	unsigned int d1, d2, d3, b[8];
	if (sscanf(s, "%8x-%4x-%4x-%2x%2x-%2x%2x%2x%2x%2x%2x", &d1, &d2, &d3, &b[0], &b[1],
	           &b[2], &b[3], &b[4], &b[5], &b[6], &b[7]) != 11)
		return FALSE;
	out->Data1 = d1;
	out->Data2 = (USHORT)d2;
	out->Data3 = (USHORT)d3;
	for (int i = 0; i < 8; i++)
		out->Data4[i] = (BYTE)b[i];
	return TRUE;
}

int main(int argc, char* argv[])
{
	WSADATA wsa;
	int ctl_port = 0;
	const char* size;
	const char* rdp_host;
	int rdp_port;

	if (argc < 3)
	{
		printf("usage: %s <hv:RUNTIME-GUID | tcp:host:port> <WxH>\n", argv[0]);
		return 2;
	}
	size = argv[2];

	/* Explicit init is load-bearing: without it every connect fails as
	 * ERRCONNECT_DNS_NAME_NOT_FOUND, even for literal IPs. */
	WSAStartup(MAKEWORD(2, 2), &wsa);

	if (strncmp(argv[1], "hv:", 3) == 0)
	{
		int shim_port = 0;
		SOCKET shim;
		if (!parse_guid(argv[1] + 3, &g_vm_id))
		{
			printf("bad runtime GUID: %s\n", argv[1] + 3);
			return 2;
		}
		shim = listen_loopback(&shim_port);
		if (shim == INVALID_SOCKET)
		{
			printf("shim listen failed\n");
			return 1;
		}
		CloseHandle(CreateThread(NULL, 0, shim_accept_thread, (LPVOID)(UINT_PTR)shim, 0, NULL));
		rdp_host = "127.0.0.1";
		rdp_port = shim_port;
	}
	else if (strncmp(argv[1], "tcp:", 4) == 0)
	{
		char* colon;
		strncpy(g_tcp_host, argv[1] + 4, sizeof(g_tcp_host) - 1);
		colon = strrchr(g_tcp_host, ':');
		if (!colon)
		{
			printf("bad tcp target: %s\n", argv[1]);
			return 2;
		}
		*colon = 0;
		g_tcp_port = atoi(colon + 1);
		rdp_host = g_tcp_host;
		rdp_port = g_tcp_port;
	}
	else
	{
		printf("bad target: %s\n", argv[1]);
		return 2;
	}

	g_ctl_listen = listen_loopback(&ctl_port);
	if (g_ctl_listen == INVALID_SOCKET)
	{
		printf("control listen failed\n");
		return 1;
	}
	set_nonblocking(g_ctl_listen);
	printf("HELPER-CONTROL-PORT %d\n", ctl_port);
	fflush(stdout);

	while (!g_quit)
	{
		run_session(rdp_host, rdp_port, size);
		if (g_quit)
			break;
		/* Reconnect backoff: keep serving the retained frame meanwhile. */
		for (int i = 0; i < 20 && !g_quit; i++)
		{
			poll_control();
			Sleep(50);
		}
	}
	return 0;
}
