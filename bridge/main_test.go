package main

import (
	"reflect"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

const (
	instinct = "16508702892"
	lid      = "98765"
	other    = "15550001111"
)

type step struct {
	id       string
	ts       int64
	text     string
	fromMe   bool
	quoted   string
	user     string
	reaction string
	target   string
	audio    bool
}

func chat(steps ...step) *bridge {
	b := &bridge{changed: make(chan struct{}), audio: map[string]*waE2E.AudioMessage{}}
	for _, s := range steps {
		user := s.user
		if user == "" {
			user = instinct
		}
		b.record(message{ID: s.id, Timestamp: s.ts, Text: s.text, FromMe: s.fromMe, Quoted: s.quoted,
			Reaction: s.reaction, Target: s.target, Audio: s.audio, users: []string{user}})
	}
	return b
}

func ids(found []message) []string {
	out := []string{}
	for _, m := range found {
		out = append(out, m.ID)
	}
	return out
}

func TestRepliesTo(t *testing.T) {
	c := &contact{users: map[string]bool{instinct: true, lid: true}}
	cases := []struct {
		name    string
		b       *bridge
		request string
		since   int64
		after   int64
		want    []string
	}{
		{"unquoted replies right after the request", chat(
			step{id: "OLD", ts: 90, text: "earlier answer"},
			step{id: "R", ts: 100, text: "what is due?", fromMe: true},
			step{id: "A", ts: 105, text: "Two things."},
			step{id: "B", ts: 106, text: "Lab and pre-lab.", user: lid}),
			"R", 0, 0, []string{"A", "B"}},
		{"a phone message after the request ends unquoted replies", chat(
			step{id: "R", ts: 100, text: "what is due?", fromMe: true},
			step{id: "P", ts: 101, text: "also, lunch?", fromMe: true},
			step{id: "C", ts: 105, text: "Sushi."},
			step{id: "D", ts: 106, text: "Two things are due.", quoted: "R"}),
			"R", 0, 0, []string{"D"}},
		{"a reply quoting another message is not the answer", chat(
			step{id: "R", ts: 100, text: "what is due?", fromMe: true},
			step{id: "X", ts: 103, text: "Re your photo: nice.", quoted: "PHOTO"},
			step{id: "A", ts: 105, text: "Two things."}),
			"R", 0, 0, []string{"A"}},
		{"an unanswered earlier message takes the next unquoted reply", chat(
			step{id: "P", ts: 95, text: "remind me at 5", fromMe: true},
			step{id: "R", ts: 100, text: "what is due?", fromMe: true},
			step{id: "C", ts: 104, text: "Reminder set."},
			step{id: "D", ts: 106, text: "Two things.", quoted: "R"}),
			"R", 0, 0, []string{"D"}},
		{"an old unanswered message does not block", chat(
			step{id: "P", ts: 10, text: "hello?", fromMe: true},
			step{id: "R", ts: 1000, text: "what is due?", fromMe: true},
			step{id: "A", ts: 1005, text: "Two things."}),
			"R", 0, 0, []string{"A"}},
		{"other chats and non-text messages are ignored", chat(
			step{id: "R", ts: 100, text: "what is due?", fromMe: true},
			step{id: "N", ts: 101, text: "hi", user: other},
			step{id: "IMG", ts: 102, text: ""},
			step{id: "A", ts: 105, text: "Two things."}),
			"R", 0, 0, []string{"A"}},
		{"after skips replies already delivered", chat(
			step{id: "R", ts: 100, text: "what is due?", fromMe: true},
			step{id: "A", ts: 105, text: "Two things."},
			step{id: "B", ts: 106, text: "Lab and pre-lab."}),
			"R", 0, 2, []string{"B"}},
		{"reactions are not replies or turns", chat(
			step{id: "R", ts: 100, text: "what is due?", fromMe: true},
			step{id: "E", ts: 101, reaction: "👀", target: "R"},
			step{id: "L", ts: 102, reaction: "❤️", target: "R", fromMe: true},
			step{id: "A", ts: 105, text: "Two things."}),
			"R", 0, 0, []string{"A"}},
		{"unknown request (bridge restarted) reads from since", chat(
			step{id: "OLD", ts: 90, text: "earlier answer"},
			step{id: "A", ts: 105, text: "Two things."}),
			"GONE", 100, 0, []string{"A"}},
		{"unknown request with nothing new", chat(
			step{id: "OLD", ts: 90, text: "earlier answer"}),
			"GONE", 100, 0, []string{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ids(tc.b.repliesTo(c, tc.request, tc.since, tc.after)); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("got %v, want %v", got, tc.want)
			}
		})
	}
}

func TestPickChat(t *testing.T) {
	pn := types.NewJID(instinct, types.DefaultUserServer)
	alias := types.NewJID("23949492670633", types.HiddenUserServer)
	if got, err := pickChat("Instinct", []types.JID{alias, pn}); err != nil || got != pn {
		t.Errorf("phone number + LID alias: got %v, %v; want %v", got, err, pn)
	}
	if got, err := pickChat("Instinct", []types.JID{alias}); err != nil || got != alias {
		t.Errorf("LID only: got %v, %v", got, err)
	}
	other := types.NewJID(other, types.DefaultUserServer)
	if _, err := pickChat("Instinct", []types.JID{pn, other}); err == nil {
		t.Error("two phone numbers should be ambiguous")
	}
	if _, err := pickChat("Instinct", nil); err == nil {
		t.Error("no match should fail")
	}
}

func TestReactionsTo(t *testing.T) {
	c := &contact{users: map[string]bool{instinct: true}}
	b := chat(
		step{id: "R", ts: 100, text: "what is due?", fromMe: true},
		step{id: "E", ts: 101, reaction: "👀", target: "R"},
		step{id: "X", ts: 102, reaction: "👍", target: "OTHER"},
		step{id: "M", ts: 103, reaction: "👍", target: "R", fromMe: true},
		step{id: "T", ts: 104, reaction: "👍", target: "R"},
		step{id: "N", ts: 105, reaction: "👍", target: "R", user: other},
		step{id: "U", ts: 106, reaction: "", target: "R"}) // reaction removed
	if got := ids(b.reactionsTo(c, "R", 0)); !reflect.DeepEqual(got, []string{"E", "T"}) {
		t.Errorf("got %v, want [E T]", got)
	}
	if got := ids(b.reactionsTo(c, "R", 2)); !reflect.DeepEqual(got, []string{"T"}) {
		t.Errorf("after: got %v, want [T]", got)
	}
}

func TestVoiceNotes(t *testing.T) {
	c := &contact{users: map[string]bool{instinct: true, lid: true}}
	b := chat(
		step{id: "OLD", ts: 90, audio: true},
		step{id: "R", ts: 100, text: "what is due?", fromMe: true},
		step{id: "V1", ts: 105, audio: true},
		step{id: "MINE", ts: 106, audio: true, fromMe: true},
		step{id: "ELSE", ts: 107, audio: true, user: other},
		step{id: "T", ts: 108, text: "also this"},
		step{id: "V2", ts: 110, audio: true, user: lid},
	)
	if got := ids(b.repliesTo(c, "R", 0, 0)); !reflect.DeepEqual(got, []string{"V1"}) {
		t.Errorf("a voice note answers the request: got %v", got)
	}
	if got := ids(b.voiceFrom(c, 100, 0)); !reflect.DeepEqual(got, []string{"V1", "V2"}) {
		t.Errorf("voiceFrom: got %v", got)
	}
	if got := ids(b.voiceFrom(c, 100, 3)); !reflect.DeepEqual(got, []string{"V2"}) {
		t.Errorf("voiceFrom after V1: got %v", got)
	}
}
