package main

import (
	"reflect"
	"testing"

	"go.mau.fi/whatsmeow/types"
)

const (
	instinct = "16508702892"
	lid      = "98765"
	other    = "15550001111"
)

type step struct {
	id     string
	ts     int64
	text   string
	fromMe bool
	quoted string
	user   string
}

func chat(steps ...step) *bridge {
	b := &bridge{changed: make(chan struct{})}
	for _, s := range steps {
		user := s.user
		if user == "" {
			user = instinct
		}
		b.record(message{ID: s.id, Timestamp: s.ts, Text: s.text, FromMe: s.fromMe, Quoted: s.quoted,
			users: []string{user}})
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
