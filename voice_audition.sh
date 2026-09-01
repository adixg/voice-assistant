#!/usr/bin/env bash
# Audition every VITS speaker one by one: print the name, play two lines in that
# voice, ask you for a rating, move to the next.  Ratings go to a CSV so you can
# see the best ones later.
#
#   ./voice_audition.sh [trilingual|japanese]     # start / resume the audition
#   ./voice_audition.sh rank [trilingual|japanese]  # list what you've rated, best first
#
# At the prompt: a number 1-9 (optionally + a note), or
#   <enter> or r = replay      s = skip (no rating)      q = quit (resume later)
#
# Resuming skips voices you already rated.  Clips are kept under voice_audition/.
# Override the spoken lines with $AUDITION_TEXT (do NOT name it LINES: that is the terminal height).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UMA_PY="${UMA_PY:-$HOME/anaconda3/envs/uma-tts/bin/python}"
TTS="$HERE/tts_cli.py"
DEVICE="${TTS_DEVICE:-cpu}"
AUDITION_TEXT="${AUDITION_TEXT:-Hey, it's really good to hear your voice again. I was just thinking about you, and I can't wait to see you tonight.}"

die() { echo "error: $*" >&2; exit 1; }

# ---- args -------------------------------------------------------------------
mode=audition
if [ "${1:-}" = "rank" ]; then mode=rank; shift; fi
model="${1:-trilingual}"
case "$model" in trilingual|japanese) ;; *) die "model must be 'trilingual' or 'japanese'" ;; esac
lang=en
mdir="$HERE/voice_audition/$model"
csv="$HERE/voice_audition/${model}_ratings.csv"

[ -x "$UMA_PY" ] || die "no uma-tts python at $UMA_PY (set \$UMA_PY)"
[ -f "$TTS" ]    || die "no tts_cli.py next to this script"
command -v ffplay >/dev/null || die "ffplay not on PATH"

# ---- rank mode ------------------------------------------------------------
if [ "$mode" = rank ]; then
  [ -f "$csv" ] || die "nothing rated yet for $model"
  printf '%5s  %4s  %-30s  %s\n' rating id name note
  printf -- '-----  ----  ------------------------------  ----\n'
  # last rating per id wins, then sort by rating desc, then by id
  awk -F'\t' 'NR>1 {last[$1]=$0} END {for (k in last) print last[k]}' "$csv" \
    | sort -t$'\t' -k3,3nr -k1,1n \
    | awk -F'\t' '{printf "%5s  %4s  %-30s  %s\n", $3, $1, $2, $4}'
  echo "----- $(( $(wc -l < "$csv") - 1 )) rated -----"
  exit 0
fi

# ---- audition mode ------------------------------------------------------------
mkdir -p "$mdir"
[ -f "$csv" ] || printf 'id\tname\trating\tnote\tts\n' > "$csv"

echo "loading the $model model (once)..."
# one resident TTS process we feed speaker by speaker
coproc SYNTH { "$UMA_PY" "$TTS" --serve -m "$model" -l "$lang" --device "$DEVICE" 2>/dev/null; }
read -r hello <&"${SYNTH[0]}"                       # "ready"
[ "$hello" = "ready" ] || die "TTS worker didn't start"

# speaker list: "<id>\t<name>"
mapfile -t SPEAKERS < <("$UMA_PY" "$TTS" --list-speakers -m "$model" 2>/dev/null \
                        | sed -E 's/^ *([0-9]+) +(.*)$/\1\t\2/')
total=${#SPEAKERS[@]}
[ "$total" -gt 0 ] || die "no speakers listed"
echo "$total voices.  lines: \"$AUDITION_TEXT\""
echo

done_count=0
for row in "${SPEAKERS[@]}"; do
  id=${row%%$'\t'*}
  name=${row#*$'\t'}
  wav="$mdir/$(printf '%03d' "$id").wav"

  if grep -q "^$id"$'\t' "$csv"; then
    done_count=$((done_count + 1)); continue
  fi

  # synth these two lines in this speaker's voice
  printf '%s\t%s\t%s\n' "$wav" "$id" "$AUDITION_TEXT" >&"${SYNTH[1]}"
  read -r resp <&"${SYNTH[0]}"
  if [ "$resp" != "$wav" ]; then
    echo "[$id] $name -- synth failed: $resp" >&2
    continue
  fi

  while :; do
    printf '\n[%d/%d]  id %s  =  %s\n' "$((done_count + 1))" "$total" "$id" "$name"
    ffplay -nodisp -autoexit -loglevel error "$wav" </dev/null
    printf '   rate 1-9  (enter/r replay, s skip, q quit): ' >&2
    { [ -r /dev/tty ] && read -r ans </dev/tty 2>/dev/null; } || read -r ans || { echo; ans=q; }
    rating=${ans%% *}; note=""
    [ "$ans" != "$rating" ] && note=${ans#* }
    case "$rating" in
      ""|r|R) continue ;;
      s|S) echo "   skipped."; break ;;
      q|Q) echo; echo "stopped -- rerun to resume, or: $0 rank $model"; exit 0 ;;
      [1-9])
        printf '%s\t%s\t%s\t%s\t%s\n' "$id" "$name" "$rating" "${note//$'\t'/ }" \
          "$(date +%FT%T)" >> "$csv"
        done_count=$((done_count + 1))
        echo "   saved: $rating${note:+  ($note)}"
        break ;;
      *) echo "   ? a digit 1-9, or r / s / q" >&2 ;;
    esac
  done
done

echo
echo "all $total done.  ranking:"
exec "$0" rank "$model"
