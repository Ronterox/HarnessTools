package store

import "testing"

func TestInsertEmpty(t *testing.T) {
	d := &DB{}
	if err := d.Insert(1, ""); err != ErrEmpty {
		t.Fatalf("got %v", err)
	}
	checkSchema(t)
}
