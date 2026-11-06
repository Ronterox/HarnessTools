#!/usr/bin/env bash
# Desktop notification whenever Claude Code stops or needs attention.
# Wired to Stop, StopFailure and Notification hooks in ~/.claude/settings.json.

input=$(cat)
event=$(jq -r '.hook_event_name // empty' <<<"$input")
cwd=$(jq -r '.cwd // empty' <<<"$input")
project=$(basename "${cwd:-$PWD}")
session=$(jq -r '.session_id // empty' <<<"$input")

. "${BASH_SOURCE%/*}/session-badge.sh"

# Per-session icon in the session's badge colour and shape (see session-badge.sh).
session_icon() {
  badge_load "$1" "$2"
  find "$BADGE_DIR" \( -name "*.svg" -o -name "*.ansi" \) -mtime +30 -delete 2>/dev/null

  local body
  case $BADGE_SHAPE in
    0) body='<circle cx="32" cy="32" r="30"/>' ;;
    1) body='<rect x="3" y="3" width="58" height="58" rx="12"/>' ;;
    2) body='<polygon points="32,2 58,17 58,47 32,62 6,47 6,17"/>' ;;
    3) body='<polygon points="32,1 63,32 32,63 1,32"/>' ;;
  esac

  local file="$BADGE_DIR/$1.svg"
  cat >"$file" <<SVG
<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 64 64">
  <g fill="#$BADGE_COLOUR">$body</g>
  <text x="32" y="34" text-anchor="middle" dominant-baseline="central"
        font-family="sans-serif" font-weight="bold" font-size="20"
        fill="#$BADGE_INK">$BADGE_INITIALS</text>
</svg>
SVG
  echo "$file"
}

# True when background agents/shells are still running (see bg-pending). Their
# completion re-invokes Claude, and the Stop after that is the real one.
# Any failure (no transcript, parse error) is false, so we notify as before.
bg_pending() {
  local n
  n=$("${BASH_SOURCE%/*}/bg-pending" "$(jq -r '.transcript_path // empty' <<<"$input")" 2>/dev/null) || return 1
  [[ $n =~ ^[0-9]+$ ]] && (( n > 0 ))
}

urgency=normal
sound=complete
case "$event" in
  Stop)
    # Skip when a Stop hook already forced continuation (avoids duplicate pings).
    [[ $(jq -r '.stop_hook_active // false' <<<"$input") == true ]] && exit 0
    bg_pending && exit 0
    title="Claude finished"
    body=$(jq -r '.last_assistant_message // empty' <<<"$input" | tr '\n' ' ' | cut -c1-200)
    [[ -z $body ]] && body="Task complete — ready for your next prompt."
    ;;
  StopFailure)
    title="Claude stopped with an error"
    body=$(jq -r '.error // .message // "The turn ended because of an error."' <<<"$input" | cut -c1-200)
    urgency=critical
    sound=dialog-warning
    ;;
  Notification)
    type=$(jq -r '.notification_type // empty' <<<"$input")
    body=$(jq -r '.message // "Claude needs your attention."' <<<"$input")
    case "$type" in
      permission_prompt) title="Claude needs permission"; urgency=critical ;;
      idle_prompt)       bg_pending && exit 0; title="Claude is waiting for you" ;;
      *)                 title=$(jq -r '.title // "Claude needs your input"' <<<"$input") ;;
    esac
    sound=message-new-instant
    ;;
  *)
    title="Claude Code"
    body="$event"
    ;;
esac

# Queue before desktop/audio delivery so navigation does not depend on either.
if [[ -n ${TMUX:-} && -n ${TMUX_PANE:-} ]]; then
  "${BASH_SOURCE%/*}/tmux-notifications.sh" enqueue "$session" "$title" "$body" 2>/dev/null || :
fi

icon=dialog-information
[[ -n $session ]] && icon=$(session_icon "$session" "$project")

notify-send -a "Claude Code" -u "$urgency" -i "$icon" \
  "$title — $project" "$body" 2>/dev/null

# Custom sounds: drop any audio files into ~/.claude/sounds/<category>/ and one
# is picked at random. Falls back to the freedesktop system sound if empty.
case "$event" in
  Stop)        category=finished ;;
  StopFailure) category=error ;;
  *)           category=attention ;;
esac
dir="$HOME/.claude/sounds/$category"
snd=$(find "$dir" -maxdepth 1 -type f \
  \( -iname '*.mp3' -o -iname '*.ogg' -o -iname '*.oga' -o -iname '*.wav' \
     -o -iname '*.flac' -o -iname '*.m4a' -o -iname '*.opus' \) 2>/dev/null | shuf -n1)
[[ -z $snd ]] && snd="/usr/share/sounds/freedesktop/stereo/$sound.oga"

if [[ -f $snd ]]; then
  if command -v mpv >/dev/null; then
    (mpv --no-config --no-video --really-quiet --loop-file=no --loop-playlist=no --keep-open=no --length=10 "$snd" &>/dev/null &)
  elif command -v ffplay >/dev/null; then
    (ffplay -nodisp -autoexit -loglevel quiet "$snd" &>/dev/null &)
  else
    (paplay "$snd" &>/dev/null &)
  fi
fi

exit 0
