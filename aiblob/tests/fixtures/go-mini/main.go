package main

import (
	"log"
	"net/http"
	"os"

	"example.com/notes/handlers"
	"example.com/notes/store"
)

var addr = ":8080"

func init() {
	log.SetFlags(0)
}

func main() {
	db, err := store.Open(os.Getenv("DB_PATH"))
	if err != nil {
		log.Fatal(err)
	}
	h := handlers.New(db)
	mux := http.NewServeMux()
	mux.HandleFunc("/notes", h.ListNotes)
	mux.HandleFunc("POST /notes", h.CreateNote)
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		if err := db.Ping(); err != nil {
			http.Error(w, "down", http.StatusServiceUnavailable)
			return
		}
		handlers.WriteJSON(w, map[string]string{"status": "ok"})
	})
	mux.Handle("/static/", http.StripPrefix("/static/", http.FileServer(http.Dir("static"))))
	go store.Vacuum(db)
	setupMetrics()
	log.Fatal(http.ListenAndServe(addr, logRequests(mux)))
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		handlers.Hits++
		next.ServeHTTP(w, r)
	})
}
