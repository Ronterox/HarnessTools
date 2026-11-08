package store

import (
	"database/sql"
	"errors"
	"fmt"
)

type DB struct {
	conn *sql.DB
}

type Note struct {
	ID   int
	Body string
}

var ErrEmpty = errors.New("empty note")

func Open(path string) (*DB, error) {
	conn, err := sql.Open("sqlite3", path)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	return &DB{conn: conn}, nil
}

func (d *DB) Ping() error { return d.conn.Ping() }

func (d *DB) AllNotes() ([]Note, error) {
	rows, err := d.conn.Query("SELECT id, body FROM notes ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Note
	for rows.Next() {
		var n Note
		if err := rows.Scan(&n.ID, &n.Body); err != nil {
			return nil, err
		}
		out = append(out, n)
	}
	return out, nil
}

func (d *DB) Insert(id int, body string) error {
	if body == "" {
		return ErrEmpty
	}
	_, err := d.conn.Exec("INSERT INTO notes (id, body) VALUES (?, ?)", id, body)
	return err
}

func Vacuum(d *DB) {
	d.conn.Exec("VACUUM")
}
