// wa-bridge links to a WhatsApp account as a companion device (like WhatsApp Web) and
// exposes a small HTTP API on localhost for the voice server: send a text to a contact,
// and long-poll for that contact's replies.
//
//	POST /pair     {"phone": "15551234567"}       -> {"code": "ABCD-EFGH"}  (enter in WhatsApp > Linked devices)
//	GET  /status                                  -> {"paired", "connected", "logged_in"}
//	POST /send     {"contact": "Instinct", "text"} -> {"id", "timestamp"}
//	GET  /replies?contact=&since=&after=&wait=    -> {"messages": [{"seq", "id", "timestamp", "text"}]}
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

type message struct {
	Seq       int64  `json:"seq"`
	ID        string `json:"id"`
	Timestamp int64  `json:"timestamp"`
	Text      string `json:"text"`
	users     []string
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
}

func (b *bridge) onEvent(evt any) {
	switch v := evt.(type) {
	case *events.Message:
		if v.Info.IsFromMe || v.Info.IsGroup {
			return
		}
		text := v.Message.GetConversation()
		if text == "" {
			text = v.Message.GetExtendedTextMessage().GetText()
		}
		if text == "" {
			return
		}
		users := []string{v.Info.Chat.User, v.Info.Sender.User}
		if !v.Info.SenderAlt.IsEmpty() {
			users = append(users, v.Info.SenderAlt.User)
		}
		b.mu.Lock()
		b.seq++
		b.messages = append(b.messages, message{Seq: b.seq, ID: v.Info.ID, Timestamp: v.Info.Timestamp.Unix(),
			Text: text, users: users})
		if len(b.messages) > 200 {
			b.messages = b.messages[len(b.messages)-200:]
		}
		close(b.changed)
		b.changed = make(chan struct{})
		b.mu.Unlock()
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
		switch len(matches) {
		case 0:
			return nil, fmt.Errorf("no WhatsApp contact named %q; use its phone number instead", name)
		case 1:
			jid = matches[0]
		default:
			return nil, fmt.Errorf("%d contacts match %q; use a more specific name or the phone number", len(matches), name)
		}
	}
	c := &contact{name: name, sendTo: jid, users: map[string]bool{jid.User: true}, resolved: time.Now()}
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

func (b *bridge) pair(phone string) (string, error) {
	if b.client.Store.ID != nil {
		return "", errors.New("already paired")
	}
	b.client.Disconnect()
	qr, err := b.client.GetQRChannel(context.Background())
	if err != nil {
		return "", err
	}
	if err := b.client.Connect(); err != nil {
		return "", err
	}
	for evt := range qr {
		if evt.Event != "code" {
			return "", fmt.Errorf("pairing ended: %s", evt.Event)
		}
		code, err := b.client.PairPhone(context.Background(), phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
		go func() {
			for evt := range qr {
				log.Printf("pairing: %s", evt.Event)
			}
		}()
		return code, err
	}
	return "", errors.New("pairing channel closed")
}

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
		reply(w, 200, map[string]any{"id": resp.ID, "timestamp": resp.Timestamp.Unix()})
	})
	mux.HandleFunc("GET /replies", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
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
			found := []message{}
			for _, m := range b.messages {
				if m.Seq > after && m.Timestamp >= since && (c.users[m.users[0]] || c.users[m.users[1]] ||
					(len(m.users) > 2 && c.users[m.users[2]])) {
					found = append(found, m)
				}
			}
			changed := b.changed
			b.mu.Unlock()
			if len(found) > 0 {
				reply(w, 200, map[string]any{"messages": found})
				return
			}
			select {
			case <-changed:
			case <-timeout:
				reply(w, 200, map[string]any{"messages": found})
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
	b := &bridge{client: whatsmeow.NewClient(device, waLog.Stdout("wa", "WARN", false)),
		changed: make(chan struct{}), contacts: map[string]*contact{}}
	b.client.AddEventHandler(b.onEvent)
	if b.client.Store.ID == nil {
		log.Print(`not paired: POST {"phone": "..."} to /pair and enter the code on your phone`)
	} else {
		// whatsmeow reconnects by itself once connected; keep trying until the first success.
		go func() {
			for err := b.client.Connect(); err != nil && !b.client.IsConnected(); err = b.client.Connect() {
				log.Printf("connect: %v; retrying in 15 s", err)
				time.Sleep(15 * time.Second)
			}
		}()
	}
	server := &http.Server{Addr: *listen, Handler: b.routes(), ReadHeaderTimeout: 10 * time.Second}
	log.Printf("listening on %s", *listen)
	log.Fatal(server.ListenAndServe())
}
