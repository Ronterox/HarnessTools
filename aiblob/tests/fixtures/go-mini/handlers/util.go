package handlers

import (
	"encoding/json"
	"net/http"
)

// Hits counts the requests served.
var Hits int

// WriteJSON is the helper every handler answers with.
func WriteJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, err error) {
	http.Error(w, err.Error(), http.StatusInternalServerError)
}
