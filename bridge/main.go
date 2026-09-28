// wa-bridge links to a WhatsApp account as a companion device (like WhatsApp Web) and
// exposes a small HTTP API on localhost for the voice server: send a text to a contact,
// and long-poll for that contact's replies.
//
//	GET  /qr                                      -> page with a self-refreshing QR code to scan
//	POST /pair     {"phone": "15551234567"}       -> {"code": "ABCD-EFGH"}  (enter in WhatsApp > Linked devices)
//	GET  /status                                  -> {"paired", "connected", "logged_in"}
//	POST /send     {"contact": "Instinct", "text"} -> {"id", "timestamp"}
//	GET  /replies?contact=&request=&since=&after=&wait=
//	                                              -> {"messages": [...]} the contact's replies to message `request`
//	GET  /chat?contact=                           -> recent messages in that chat, both directions (for setup)
//	POST /revoke   {"contact", "id"}              -> delete one of our messages for everyone
//	GET  /contacts?q=                             -> contacts whose names contain q (for setup)
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/skip2/go-qrcode"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
	"modernc.org/sqlite"
)

// Pure-Go SQLite under the driver name whatsmeow's store expects, so the binary
// cross-compiles without cgo.
func init() { sql.Register("sqlite3", &sqlite.Driver{}) }

var digits = regexp.MustCompile(`^\+?[0-9]{6,15}$`)

// The device's contact and LID stores only exist once the device is paired.
var errNotPaired = errors.New(`not paired: POST {"phone": "..."} to /pair first`)

// message is one entry in a one-to-one chat, in arrival order (Seq). FromMe covers
// this bridge's sends and anything typed on the user's other devices.
type message struct {
	Seq       int64  `json:"seq"`
	ID        string `json:"id"`
	Timestamp int64  `json:"timestamp"`
	Text      string `json:"text"`
	FromMe    bool   `json:"from_me"`
	Quoted    string `json:"quoted,omitempty"` // ID of the message this one replies to, if quoted
	users     []string
}

func (c *contact) has(m message) bool {
	for _, u := range m.users {
		if c.users[u] {
			return true
		}
	}
	return false
}

func quotedID(m *waE2E.Message) string {
	for _, ci := range []*waE2E.ContextInfo{m.GetExtendedTextMessage().GetContextInfo(),
		m.GetImageMessage().GetContextInfo(), m.GetAudioMessage().GetContextInfo(),
		m.GetDocumentMessage().GetContextInfo(), m.GetVideoMessage().GetContextInfo()} {
		if id := ci.GetStanzaID(); id != "" {
			return id
		}
	}
	return ""
}

func (b *bridge) record(m message) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.seq++
	m.Seq = b.seq
	b.messages = append(b.messages, m)
	if len(b.messages) > 500 {
		b.messages = b.messages[len(b.messages)-500:]
	}
	close(b.changed)
	b.changed = make(chan struct{})
}

// repliesTo picks the contact's answers to `request` from the chat, which may also hold
// unrelated conversation typed on the phone. Must hold b.mu.
//   - A reply quoting the request is always an answer; one quoting another message never is.
//   - Unquoted contact messages count only until the user sends anything else in the chat,
//     since later replies may answer that instead.
//   - If the user's previous message was still unanswered when the request went out, the
//     next reply probably answers it, so only quoted replies count.
//
// If the bridge restarted and no longer has the request, the chat is read from `since`.
func (b *bridge) repliesTo(c *contact, request string, since, after int64) []message {
	var chat []message
	for _, m := range b.messages {
		if c.has(m) {
			chat = append(chat, m)
		}
	}
	begin, known := -1, false
	for i, m := range chat {
		if m.FromMe && m.ID == request {
			begin, known = i, true
			break
		}
	}
	if !known {
		begin = len(chat) - 1 // nothing at or after since: no replies yet
		for i, m := range chat {
			if m.Timestamp >= since {
				begin = i - 1
				break
			}
		}
	}
	quotedOnly := known && begin > 0 && chat[begin-1].FromMe &&
		chat[begin].Timestamp-chat[begin-1].Timestamp < 600
	var found []message
	for _, m := range chat[begin+1:] {
		switch {
		case m.FromMe:
			quotedOnly = true
		case m.Quoted != "":
			if m.Quoted == request && m.Seq > after {
				found = append(found, m)
			}
		case !quotedOnly && m.Text != "" && m.Seq > after:
			found = append(found, m)
		}
	}
	return found
}

// contact is one chat partner, known by phone-number JID and possibly a LID alias;
// incoming messages may be addressed with either.
type contact struct {
	name     string
	sendTo   types.JID
	users    map[string]bool
	resolved time.Time
}

type bridge struct {
	client *whatsmeow.Client

	mu       sync.Mutex
	seq      int64
	messages []message    // recent incoming one-to-one texts, newest last
	changed  chan struct{} // closed and replaced whenever a message arrives
	contacts map[string]*contact

	pairMu  sync.Mutex
	pairing bool   // a linking session is open
	qrCode  string // current QR payload; WhatsApp rotates it every 20-60 s
}

func (b *bridge) onEvent(evt any) {
	switch v := evt.(type) {
	case *events.Message:
		if v.Info.IsGroup || v.Info.Chat.Server == types.BroadcastServer {
			return
		}
		text := v.Message.GetConversation()
		if text == "" {
			text = v.Message.GetExtendedTextMessage().GetText()
		}
		// Non-text messages (photos, voice notes) are kept too: they still mark turns.
		// Every identity the chat partner might appear under (phone JID or LID). The
		// user's own IDs appear here for sent messages but never match a contact.
		var users []string
		for _, id := range []types.JID{v.Info.Chat, v.Info.Sender, v.Info.SenderAlt, v.Info.RecipientAlt} {
			if !id.IsEmpty() {
				users = append(users, id.User)
			}
		}
		b.record(message{ID: v.Info.ID, Timestamp: v.Info.Timestamp.Unix(), Text: text,
			FromMe: v.Info.IsFromMe, Quoted: quotedID(v.Message), users: users})
	case *events.Connected:
		log.Print("connected to WhatsApp")
	case *events.Disconnected:
		log.Print("disconnected from WhatsApp; reconnecting")
	case *events.PairSuccess:
		log.Printf("paired as %s", v.ID)
	case *events.LoggedOut:
		log.Print("logged out: the device was unlinked; pair again")
	}
}

// resolve finds a chat by exact (then partial) contact, first, push or business name,
// or takes a phone number directly. Successful lookups are cached for ten minutes.
func (b *bridge) resolve(ctx context.Context, name string) (*contact, error) {
	name = strings.TrimSpace(name)
	if b.client.Store.ID == nil {
		return nil, errNotPaired
	}
	b.mu.Lock()
	if c := b.contacts[name]; c != nil && time.Since(c.resolved) < 10*time.Minute {
		b.mu.Unlock()
		return c, nil
	}
	b.mu.Unlock()
	var jid types.JID
	var aliases []types.JID
	if digits.MatchString(name) {
		jid = types.NewJID(strings.TrimPrefix(name, "+"), types.DefaultUserServer)
	} else {
		all, err := b.client.Store.Contacts.GetAllContacts(ctx)
		if err != nil {
			return nil, err
		}
		var exact, partial []types.JID
		for id, info := range all {
			for _, n := range []string{info.FullName, info.FirstName, info.PushName, info.BusinessName} {
				n = strings.TrimSpace(n)
				if strings.EqualFold(n, name) {
					exact = append(exact, id)
					break
				}
				if n != "" && strings.Contains(strings.ToLower(n), strings.ToLower(name)) {
					partial = append(partial, id)
					break
				}
			}
		}
		matches := exact
		if len(matches) == 0 {
			matches = partial
		}
		picked, err := pickChat(name, matches)
		if err != nil {
			return nil, err
		}
		jid, aliases = picked, matches
	}
	c := &contact{name: name, sendTo: jid, users: map[string]bool{jid.User: true}, resolved: time.Now()}
	for _, alias := range aliases {
		c.users[alias.User] = true
	}
	if jid.Server == types.HiddenUserServer {
		if pn, err := b.client.Store.LIDs.GetPNForLID(ctx, jid); err == nil && !pn.IsEmpty() {
			c.sendTo, c.users[pn.User] = pn, true
		}
	} else if lid, err := b.client.Store.LIDs.GetLIDForPN(ctx, jid); err == nil && !lid.IsEmpty() {
		c.users[lid.User] = true
	}
	b.mu.Lock()
	b.contacts[name] = c
	b.mu.Unlock()
	return c, nil
}

// pickChat chooses the chat among name matches. One person often appears twice, under a
// phone-number JID and under a LID alias; that is one chat, sent to by phone number.
func pickChat(name string, matches []types.JID) (types.JID, error) {
	var phones, lids []types.JID
	for _, m := range matches {
		if m.Server == types.HiddenUserServer {
			lids = append(lids, m)
		} else {
			phones = append(phones, m)
		}
	}
	switch {
	case len(matches) == 0:
		return types.JID{}, fmt.Errorf("no WhatsApp contact named %q; use its phone number instead", name)
	case len(phones) > 1 || len(lids) > 1:
		return types.JID{}, fmt.Errorf("%d contacts match %q; use a more specific name or the phone number",
			len(matches), name)
	case len(phones) == 1:
		return phones[0], nil
	default:
		return lids[0], nil
	}
}

// startPairing opens a linking session unless one is running: a stream of QR codes,
// which is also the session a phone-number code attaches to. WhatsApp closes it after
// about 2.5 minutes; the next request opens a new one.
func (b *bridge) startPairing() error {
	b.pairMu.Lock()
	defer b.pairMu.Unlock()
	if b.client.Store.ID != nil {
		return errors.New("already paired")
	}
	if b.pairing {
		return nil
	}
	b.client.Disconnect()
	qr, err := b.client.GetQRChannel(context.Background())
	if err != nil {
		return err
	}
	if err := b.client.Connect(); err != nil {
		return err
	}
	b.pairing = true
	go func() {
		for evt := range qr {
			b.pairMu.Lock()
			b.qrCode = ""
			if evt.Event == "code" {
				b.qrCode = evt.Code
			}
			b.pairMu.Unlock()
			if evt.Event == "code" {
				log.Print("pairing: new code")
			} else {
				log.Printf("pairing: %s %v", evt.Event, evt.Error)
			}
		}
		b.pairMu.Lock()
		b.pairing, b.qrCode = false, ""
		b.pairMu.Unlock()
	}()
	return nil
}

// currentCode waits up to `wait` for the session's current QR payload.
func (b *bridge) currentCode(wait time.Duration) string {
	for deadline := time.Now().Add(wait); ; time.Sleep(200 * time.Millisecond) {
		b.pairMu.Lock()
		code := b.qrCode
		b.pairMu.Unlock()
		if code != "" || time.Now().After(deadline) {
			return code
		}
	}
}

func (b *bridge) pair(phone string) (string, error) {
	if err := b.startPairing(); err != nil {
		return "", err
	}
	if b.currentCode(15*time.Second) == "" {
		return "", errors.New("linking session did not start; try again")
	}
	return b.client.PairPhone(context.Background(), phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
}

const qrPage = `<!doctype html><meta charset=utf-8><title>Link WhatsApp</title>
<body style="background:#fff;color:#111;font:16px system-ui;text-align:center;padding-top:40px">
<h2>WhatsApp → Settings → Linked devices → Link a device</h2><p>Scan this code. It refreshes by itself.</p>
<img id=q width=360 height=360 alt="">
<p id=s></p>
<script>
async function tick() {
  const s = await (await fetch('/status')).json();
  if (s.paired) { document.getElementById('q').remove(); document.getElementById('s').textContent = 'Linked. You can close this page.'; return; }
  document.getElementById('q').src = '/qr.png?' + Date.now();
  setTimeout(tick, 4000);
}
tick();
</script>`

func reply(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(body)
}

func statusFor(err error) int {
	if errors.Is(err, errNotPaired) {
		return 503
	}
	return 404
}

func fail(w http.ResponseWriter, code int, err error) {
	reply(w, code, map[string]string{"error": err.Error()})
}

func (b *bridge) routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", func(w http.ResponseWriter, r *http.Request) {
		reply(w, 200, map[string]bool{"paired": b.client.Store.ID != nil,
			"connected": b.client.IsConnected(), "logged_in": b.client.IsLoggedIn()})
	})
	mux.HandleFunc("GET /qr", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, qrPage)
	})
	mux.HandleFunc("GET /qr.png", func(w http.ResponseWriter, r *http.Request) {
		if err := b.startPairing(); err != nil {
			fail(w, 409, err)
			return
		}
		code := b.currentCode(15 * time.Second)
		if code == "" {
			fail(w, 503, errors.New("no code yet"))
			return
		}
		png, err := qrcode.Encode(code, qrcode.Medium, 360)
		if err != nil {
			fail(w, 500, err)
			return
		}
		w.Header().Set("Content-Type", "image/png")
		w.Header().Set("Cache-Control", "no-store")
		w.Write(png)
	})
	mux.HandleFunc("POST /pair", func(w http.ResponseWriter, r *http.Request) {
		var req struct{ Phone string }
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || !digits.MatchString(req.Phone) {
			fail(w, 400, errors.New(`send {"phone": "<country code and number, digits only>"}`))
			return
		}
		code, err := b.pair(strings.TrimPrefix(req.Phone, "+"))
		if err != nil {
			fail(w, 409, err)
			return
		}
		log.Printf("pairing code issued; enter it in WhatsApp > Linked devices > Link with phone number")
		reply(w, 200, map[string]string{"code": code})
	})
	mux.HandleFunc("GET /contacts", func(w http.ResponseWriter, r *http.Request) {
		if b.client.Store.ID == nil {
			fail(w, 503, errNotPaired)
			return
		}
		all, err := b.client.Store.Contacts.GetAllContacts(r.Context())
		if err != nil {
			fail(w, 500, err)
			return
		}
		q := strings.ToLower(r.URL.Query().Get("q"))
		var found []map[string]string
		for id, info := range all {
			names := strings.Join([]string{info.FullName, info.PushName, info.BusinessName}, " | ")
			if q == "" || strings.Contains(strings.ToLower(names), q) {
				found = append(found, map[string]string{"jid": id.String(), "names": names})
			}
		}
		reply(w, 200, map[string]any{"contacts": found})
	})
	mux.HandleFunc("POST /revoke", func(w http.ResponseWriter, r *http.Request) {
		var req struct{ Contact, ID string }
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ID == "" {
			fail(w, 400, errors.New(`send {"contact": "...", "id": "..."}`))
			return
		}
		if !b.client.IsLoggedIn() {
			fail(w, 503, errors.New("not connected to WhatsApp"))
			return
		}
		c, err := b.resolve(r.Context(), req.Contact)
		if err != nil {
			fail(w, statusFor(err), err)
			return
		}
		// An empty sender means one of our own messages.
		if _, err := b.client.SendMessage(r.Context(), c.sendTo, b.client.BuildRevoke(c.sendTo, types.EmptyJID, req.ID)); err != nil {
			fail(w, 502, err)
			return
		}
		log.Printf("revoked %s in %s", req.ID, c.name)
		reply(w, 200, map[string]string{"revoked": req.ID})
	})
	mux.HandleFunc("GET /chat", func(w http.ResponseWriter, r *http.Request) {
		c, err := b.resolve(r.Context(), r.URL.Query().Get("contact"))
		if err != nil {
			fail(w, statusFor(err), err)
			return
		}
		b.mu.Lock()
		chat := []message{}
		for _, m := range b.messages {
			if c.has(m) {
				chat = append(chat, m)
			}
		}
		b.mu.Unlock()
		reply(w, 200, map[string]any{"contact": c.sendTo.String(), "messages": chat})
	})
	mux.HandleFunc("POST /send", func(w http.ResponseWriter, r *http.Request) {
		var req struct{ Contact, Text string }
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Text == "" {
			fail(w, 400, errors.New(`send {"contact": "...", "text": "..."}`))
			return
		}
		if !b.client.IsLoggedIn() {
			fail(w, 503, errors.New("not connected to WhatsApp"))
			return
		}
		c, err := b.resolve(r.Context(), req.Contact)
		if err != nil {
			fail(w, statusFor(err), err)
			return
		}
		resp, err := b.client.SendMessage(r.Context(), c.sendTo, &waE2E.Message{Conversation: proto.String(req.Text)})
		if err != nil {
			fail(w, 502, err)
			return
		}
		log.Printf("sent %s to %s", resp.ID, c.name)
		b.record(message{ID: resp.ID, Timestamp: resp.Timestamp.Unix(), Text: req.Text, FromMe: true,
			users: []string{c.sendTo.User}})
		reply(w, 200, map[string]any{"id": resp.ID, "timestamp": resp.Timestamp.Unix()})
	})
	mux.HandleFunc("GET /replies", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		request := q.Get("request")
		since, _ := strconv.ParseInt(q.Get("since"), 10, 64)
		after, _ := strconv.ParseInt(q.Get("after"), 10, 64)
		wait, _ := strconv.Atoi(q.Get("wait"))
		c, err := b.resolve(r.Context(), q.Get("contact"))
		if err != nil {
			fail(w, statusFor(err), err)
			return
		}
		timeout := time.After(time.Duration(min(max(wait, 0), 30)) * time.Second)
		for {
			b.mu.Lock()
			found := b.repliesTo(c, request, since, after)
			changed := b.changed
			b.mu.Unlock()
			if len(found) > 0 {
				reply(w, 200, map[string]any{"messages": found})
				return
			}
			select {
			case <-changed:
			case <-timeout:
				reply(w, 200, map[string]any{"messages": []message{}})
				return
			case <-r.Context().Done():
				return
			}
		}
	})
	return mux
}

func main() {
	dbPath := flag.String("db", "whatsapp.db", "session database (keep private: it holds the device keys)")
	listen := flag.String("listen", "127.0.0.1:8766", "HTTP address; keep on localhost")
	logLevel := flag.String("log", "INFO", "whatsmeow log level: DEBUG, INFO, WARN, ERROR")
	flag.Parse()
	ctx := context.Background()

	store.SetOSInfo("Pi Voice Assistant", [3]uint32{1, 0, 0})
	container, err := sqlstore.New(ctx, "sqlite3",
		"file:"+*dbPath+"?_pragma=foreign_keys(1)&_pragma=busy_timeout(5000)", waLog.Stdout("db", "WARN", false))
	if err != nil {
		log.Fatal(err)
	}
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		log.Fatal(err)
	}
	b := &bridge{client: whatsmeow.NewClient(device, waLog.Stdout("wa", *logLevel, false)),
		changed: make(chan struct{}), contacts: map[string]*contact{}}
	b.client.AddEventHandler(b.onEvent)
	if b.client.Store.ID == nil {
		log.Print(`not paired: POST {"phone": "..."} to /pair and enter the code on your phone`)
	} else if err := b.client.Connect(); err != nil {
		log.Printf("connect: %v", err)
	}
	// whatsmeow reconnects after ordinary drops but gives up if the first connect fails or
	// the post-pairing restart (code 515) can't reach WhatsApp; keep a paired bridge online.
	go func() {
		for range time.Tick(30 * time.Second) {
			b.pairMu.Lock()
			pairing := b.pairing
			b.pairMu.Unlock()
			if b.client.Store.ID != nil && !pairing && !b.client.IsConnected() {
				if err := b.client.Connect(); err != nil {
					log.Printf("reconnect: %v", err)
				}
			}
		}
	}()
	server := &http.Server{Addr: *listen, Handler: b.routes(), ReadHeaderTimeout: 10 * time.Second}
	log.Printf("listening on %s", *listen)
	log.Fatal(server.ListenAndServe())
}
