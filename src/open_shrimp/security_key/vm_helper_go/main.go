package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha1"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math/bits"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

const (
	uhidDestroy        = 1
	uhidStart          = 2
	uhidStop           = 3
	uhidOpen           = 4
	uhidClose          = 5
	uhidOutput         = 6
	uhidGetReport      = 9
	uhidGetReportReply = 10
	uhidCreate2        = 11
	uhidInput2         = 12
	uhidSetReport      = 13
	uhidSetReportReply = 14

	uhidDataMax = 4096
	busUSB      = 0x03
)

var fidoHIDReportDescriptor = []byte{
	0x06, 0xd0, 0xf1,
	0x09, 0x01,
	0xa1, 0x01,
	0x09, 0x20,
	0x15, 0x00,
	0x26, 0xff, 0x00,
	0x75, 0x08,
	0x95, 0x40,
	0x81, 0x02,
	0x09, 0x21,
	0x15, 0x00,
	0x26, 0xff, 0x00,
	0x75, 0x08,
	0x95, 0x40,
	0x91, 0x02,
	0xc0,
}

type options struct {
	relayURL  string
	sessionID string
	token     string
	device    string
	name      string
	phys      string
	uniq      string
}

type wsConn struct {
	conn net.Conn
	br   *bufio.Reader
}

func fixedBytes(value string, size int) []byte {
	out := make([]byte, size)
	copy(out, []byte(value))
	return out
}

func u16(v int) []byte {
	out := make([]byte, 2)
	binary.LittleEndian.PutUint16(out, uint16(v))
	return out
}

func u32(v int) []byte {
	out := make([]byte, 4)
	binary.LittleEndian.PutUint32(out, uint32(v))
	return out
}

func create2Event(name, phys, uniq string) []byte {
	buf := bytes.NewBuffer(nil)
	buf.Write(u32(uhidCreate2))
	buf.Write(fixedBytes(name, 128))
	buf.Write(fixedBytes(phys, 64))
	buf.Write(fixedBytes(uniq, 64))
	buf.Write(u16(len(fidoHIDReportDescriptor)))
	buf.Write(u16(busUSB))
	buf.Write(u32(0x1209))
	buf.Write(u32(0xF1D0))
	buf.Write(u32(1))
	buf.Write(u32(0))
	buf.Write(fidoHIDReportDescriptor)
	return buf.Bytes()
}

func input2Event(report []byte) ([]byte, error) {
	if len(report) > uhidDataMax {
		return nil, fmt.Errorf("input report too large: %d > %d", len(report), uhidDataMax)
	}
	buf := bytes.NewBuffer(nil)
	buf.Write(u32(uhidInput2))
	buf.Write(u16(len(report)))
	buf.Write(report)
	return buf.Bytes(), nil
}

func getReportReply(reqID uint32) []byte {
	buf := bytes.NewBuffer(nil)
	buf.Write(u32(uhidGetReportReply))
	buf.Write(u32(int(reqID)))
	buf.Write(u16(0))
	buf.Write(u16(0))
	return buf.Bytes()
}

func setReportReply(reqID uint32) []byte {
	buf := bytes.NewBuffer(nil)
	buf.Write(u32(uhidSetReportReply))
	buf.Write(u32(int(reqID)))
	buf.Write(u16(0))
	return buf.Bytes()
}

func audit(event string, fields map[string]any) {
	if fields == nil {
		fields = map[string]any{}
	}
	fields["ts"] = time.Now().UnixMilli()
	fields["event"] = event
	encoded, _ := json.Marshal(fields)
	fmt.Println(string(encoded))
}

func parseUHIDEvent(raw []byte) (string, []byte, uint32) {
	if len(raw) < 4 {
		return "short", nil, 0
	}
	eventType := binary.LittleEndian.Uint32(raw[:4])
	switch eventType {
	case uhidOutput:
		if len(raw) < 4+uhidDataMax+2 {
			return "short_output", nil, 0
		}
		size := int(binary.LittleEndian.Uint16(raw[4+uhidDataMax : 4+uhidDataMax+2]))
		if size > uhidDataMax {
			size = uhidDataMax
		}
		return "output", raw[4 : 4+size], 0
	case uhidGetReport:
		if len(raw) < 8 {
			return "short_get_report", nil, 0
		}
		return "get_report", nil, binary.LittleEndian.Uint32(raw[4:8])
	case uhidSetReport:
		if len(raw) < 8 {
			return "short_set_report", nil, 0
		}
		return "set_report", nil, binary.LittleEndian.Uint32(raw[4:8])
	case uhidStart:
		return "start", nil, 0
	case uhidStop:
		return "stop", nil, 0
	case uhidOpen:
		return "open", nil, 0
	case uhidClose:
		return "close", nil, 0
	default:
		return fmt.Sprintf("unknown_%d", eventType), nil, 0
	}
}

func sessionURL(base, sessionID, token string) (string, error) {
	u, err := url.Parse(strings.TrimRight(base, "/"))
	if err != nil {
		return "", err
	}
	if u.Scheme != "ws" && u.Scheme != "wss" {
		return "", fmt.Errorf("relay URL must start with ws:// or wss://")
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/api/security-key/sessions/" + sessionID + "/vm"
	q := u.Query()
	q.Set("token", token)
	u.RawQuery = q.Encode()
	return u.String(), nil
}

func dialWebSocket(rawURL string) (*wsConn, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, err
	}
	addr := u.Host
	if !strings.Contains(addr, ":") {
		if u.Scheme == "wss" {
			addr += ":443"
		} else {
			addr += ":80"
		}
	}
	var conn net.Conn
	if u.Scheme == "wss" {
		conn, err = tls.Dial("tcp", addr, &tls.Config{ServerName: u.Hostname()})
	} else {
		conn, err = net.Dial("tcp", addr)
	}
	if err != nil {
		return nil, err
	}

	keyBytes := make([]byte, 16)
	if _, err := rand.Read(keyBytes); err != nil {
		conn.Close()
		return nil, err
	}
	key := base64.StdEncoding.EncodeToString(keyBytes)
	path := u.RequestURI()
	req := fmt.Sprintf(
		"GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n",
		path,
		u.Host,
		key,
	)
	if _, err := conn.Write([]byte(req)); err != nil {
		conn.Close()
		return nil, err
	}

	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, &http.Request{Method: "GET"})
	if err != nil {
		conn.Close()
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusSwitchingProtocols {
		conn.Close()
		return nil, fmt.Errorf("websocket upgrade failed: %s", resp.Status)
	}
	acceptSeed := key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
	sum := sha1.Sum([]byte(acceptSeed))
	expected := base64.StdEncoding.EncodeToString(sum[:])
	if resp.Header.Get("Sec-WebSocket-Accept") != expected {
		conn.Close()
		return nil, errors.New("invalid websocket accept header")
	}
	return &wsConn{conn: conn, br: br}, nil
}

func (w *wsConn) Close() error {
	return w.conn.Close()
}

func (w *wsConn) SendBinary(payload []byte) error {
	return w.sendFrame(0x2, payload)
}

func (w *wsConn) SendPong(payload []byte) error {
	return w.sendFrame(0xA, payload)
}

func (w *wsConn) sendFrame(opcode byte, payload []byte) error {
	header := []byte{0x80 | opcode}
	length := len(payload)
	if length < 126 {
		header = append(header, 0x80|byte(length))
	} else if length <= 65535 {
		header = append(header, 0x80|126, byte(length>>8), byte(length))
	} else {
		header = append(header, 0x80|127)
		for i := 7; i >= 0; i-- {
			header = append(header, byte(uint64(length)>>uint(i*8)))
		}
	}
	mask := make([]byte, 4)
	if _, err := rand.Read(mask); err != nil {
		return err
	}
	masked := make([]byte, len(payload))
	for i, b := range payload {
		masked[i] = b ^ mask[i%4]
	}
	_, err := w.conn.Write(append(append(header, mask...), masked...))
	return err
}

func (w *wsConn) ReadMessage() (byte, []byte, error) {
	first, err := w.br.ReadByte()
	if err != nil {
		return 0, nil, err
	}
	second, err := w.br.ReadByte()
	if err != nil {
		return 0, nil, err
	}
	opcode := first & 0x0f
	masked := second&0x80 != 0
	length := uint64(second & 0x7f)
	if length == 126 {
		buf := make([]byte, 2)
		if _, err := io.ReadFull(w.br, buf); err != nil {
			return 0, nil, err
		}
		length = uint64(binary.BigEndian.Uint16(buf))
	} else if length == 127 {
		buf := make([]byte, 8)
		if _, err := io.ReadFull(w.br, buf); err != nil {
			return 0, nil, err
		}
		length = binary.BigEndian.Uint64(buf)
	}
	var mask []byte
	if masked {
		mask = make([]byte, 4)
		if _, err := io.ReadFull(w.br, mask); err != nil {
			return 0, nil, err
		}
	}
	if length > uhidDataMax+16 {
		return 0, nil, fmt.Errorf("websocket frame too large: %d", length)
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(w.br, payload); err != nil {
		return 0, nil, err
	}
	if masked {
		for i := range payload {
			payload[i] ^= mask[i%4]
		}
	}
	return opcode, payload, nil
}

func uhidToWS(ctx context.Context, fd int, ws *wsConn, done chan<- error) {
	raw := make([]byte, 4+uhidDataMax+8)
	for {
		select {
		case <-ctx.Done():
			done <- nil
			return
		default:
		}
		n, err := syscall.Read(fd, raw)
		if err != nil {
			done <- err
			return
		}
		event, report, reqID := parseUHIDEvent(raw[:n])
		switch event {
		case "output":
			payload := append([]byte{0x01}, report...)
			if err := ws.SendBinary(payload); err != nil {
				done <- err
				return
			}
			audit("vm_output_report", map[string]any{"size": len(report)})
		case "get_report":
			_, _ = syscall.Write(fd, getReportReply(reqID))
			audit("get_report_reply", map[string]any{"id": reqID})
		case "set_report":
			_, _ = syscall.Write(fd, setReportReply(reqID))
			audit("set_report_reply", map[string]any{"id": reqID})
		default:
			audit("uhid_event", map[string]any{"type": event})
		}
	}
}

func wsToUHID(ctx context.Context, fd int, ws *wsConn, done chan<- error) {
	for {
		select {
		case <-ctx.Done():
			done <- nil
			return
		default:
		}
		opcode, payload, err := ws.ReadMessage()
		if err != nil {
			done <- err
			return
		}
		switch opcode {
		case 0x1:
			var control map[string]any
			if json.Unmarshal(payload, &control) == nil {
				audit("control", map[string]any{"type": control["type"]})
			}
		case 0x2:
			if len(payload) == 0 {
				continue
			}
			if payload[0] == 0x02 {
				event, err := input2Event(payload[1:])
				if err != nil {
					done <- err
					return
				}
				_, _ = syscall.Write(fd, event)
				audit("phone_input_report", map[string]any{"size": len(payload) - 1})
			} else if payload[0] == 0x03 {
				audit("keepalive", nil)
			} else {
				audit("ignored_frame", map[string]any{"frame_type": payload[0]})
			}
		case 0x8:
			done <- io.EOF
			return
		case 0x9:
			_ = ws.SendPong(payload)
		}
	}
}

func run(opts options) error {
	remote, err := sessionURL(opts.relayURL, opts.sessionID, opts.token)
	if err != nil {
		return err
	}
	fd, err := syscall.Open(opts.device, syscall.O_RDWR|syscall.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	defer syscall.Close(fd)

	if _, err := syscall.Write(fd, create2Event(opts.name, opts.phys, opts.uniq)); err != nil {
		return err
	}
	defer func() { _, _ = syscall.Write(fd, u32(uhidDestroy)) }()
	audit("created", map[string]any{"device": opts.device, "name": opts.name})

	ws, err := dialWebSocket(remote)
	if err != nil {
		return err
	}
	defer ws.Close()
	audit("connected", nil)

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	done := make(chan error, 2)
	go uhidToWS(ctx, fd, ws, done)
	go wsToUHID(ctx, fd, ws, done)

	err = <-done
	cancel()
	_ = ws.Close()
	if errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) || err == nil {
		return nil
	}
	return err
}

func main() {
	var opts options
	flag.StringVar(&opts.relayURL, "relay-url", "", "Relay base URL, ws://host:port")
	flag.StringVar(&opts.sessionID, "session-id", "", "Security-key session ID")
	flag.StringVar(&opts.token, "token", "", "VM endpoint token")
	flag.StringVar(&opts.device, "device", "/dev/uhid", "UHID device path")
	flag.StringVar(&opts.name, "name", "OpenShrimp Virtual FIDO Key", "Virtual HID device name")
	flag.StringVar(&opts.phys, "phys", "openshrimp/security-key-uhid", "Virtual HID physical path")
	flag.StringVar(&opts.uniq, "uniq", "openshrimp-fido-v1", "Virtual HID unique ID")
	flag.Parse()

	if opts.relayURL == "" || opts.sessionID == "" || opts.token == "" {
		flag.Usage()
		os.Exit(2)
	}
	if bits.UintSize < 32 {
		log.Fatal("unsupported architecture")
	}
	if err := run(opts); err != nil {
		log.Fatal(err)
	}
}
