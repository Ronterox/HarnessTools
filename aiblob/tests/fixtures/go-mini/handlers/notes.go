package handlers

import (
	"html/template"
	"net/http"
	"strconv"

	"example.com/notes/store"
)

var tmpl = template.Must(template.ParseFiles("templates/notes.html"))

type Handler struct {
	db *store.DB
}

func New(db *store.DB) *Handler {
	return &Handler{db: db}
}

func (h *Handler) ListNotes(w http.ResponseWriter, r *http.Request) {
	notes, err := h.db.AllNotes()
	if err != nil {
		writeError(w, err)
		return
	}
	Hits++
	tmpl.Execute(w, notes)
}

func (h *Handler) CreateNote(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.Atoi(r.FormValue("id"))
	if err != nil {
		http.Error(w, "bad id", http.StatusBadRequest)
		return
	}
	if err := h.db.Insert(id, r.FormValue("body")); err != nil {
		writeError(w, err)
		return
	}
	WriteJSON(w, map[string]int{"id": id})
}
