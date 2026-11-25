package main

import "testing"

func TestExtractAgyPrompt(t *testing.T) {
	cases := []struct {
		input      string
		wantPrompt string
		wantOK     bool
	}{
		{"!agy summarize this chat", "summarize this chat", true},
		{"  !AGY\nwhat happened today?  ", "what happened today?", true},
		{"!agy", "", false},
		{"!agy   ", "", false},
		{"!agyother foo", "", false},
		{"hello !agy foo", "", false},
		{"", "", false},
	}

	for _, tc := range cases {
		gotPrompt, gotOK := extractAgyPrompt(tc.input)
		if gotOK != tc.wantOK || gotPrompt != tc.wantPrompt {
			t.Errorf("extractAgyPrompt(%q) = (%q, %v), want (%q, %v)", tc.input, gotPrompt, gotOK, tc.wantPrompt, tc.wantOK)
		}
	}
}
