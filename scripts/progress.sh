#!/usr/bin/env bash
# Watch progress of a phototag pipeline run.
# Usage: scripts/progress.sh [DB_PATH] [LOG_PATH]
# Defaults: data/full.db, data/full.log
set -euo pipefail

readonly DB="${1:-data/full.db}"
readonly LOG="${2:-data/full.log}"
readonly INTERVAL=10

if [[ ! -f "$DB" ]]; then
  echo "DB not found: $DB" >&2
  exit 2
fi

stage() {
  # Look at the actual python child cmdline, not the wrapper shell whose argv
  # contains every stage string at once. Match `python ... phototag.cli <cmd>`
  # or the installed `phototag <cmd>` entrypoint.
  local cmdlines
  cmdlines=$(pgrep -af 'python.*phototag\.cli|/phototag ' 2>/dev/null \
    | grep -vE 'pgrep|grep|/bin/bash' || true)
  if   grep -qE '(phototag\.cli|phototag) +scan'    <<<"$cmdlines"; then echo "scan"
  elif grep -qE '(phototag\.cli|phototag) +embed'   <<<"$cmdlines"; then echo "embed"
  elif grep -qE '(phototag\.cli|phototag) +cluster' <<<"$cmdlines"; then echo "cluster"
  elif grep -qE '(phototag\.cli|phototag) +report'  <<<"$cmdlines"; then echo "report"
  else echo "idle"
  fi
}

last_log_event() {
  [[ -r "$LOG" ]] || { echo "(no log)"; return; }
  tail -n 200 "$LOG" 2>/dev/null \
    | grep -oE '"event"[[:space:]]*:[[:space:]]*"[^"]+"' \
    | tail -n 1 \
    | sed -E 's/.*"([^"]+)"$/\1/'
}

prev_images=""
prev_embeds=""
prev_ts=""

# Compute "+N (R/s)" delta + rate of a counter against its previous sample.
delta_rate() {
  local cur="$1" prev="$2" dt="$3"
  if [[ -z "$prev" || ! "$cur" =~ ^[0-9]+$ || "$dt" -le 0 ]]; then
    echo "—"
    return
  fi
  local d=$((cur - prev))
  awk -v d="$d" -v t="$dt" 'BEGIN{printf "+%d (%.2f/s)", d, d/t}'
}

print_snapshot() {
  local images tag_rows embeddings clusters run_id now dt
  images=$(sqlite3 "$DB" "SELECT COUNT(*) FROM images" 2>/dev/null || echo "?")
  tag_rows=$(sqlite3 "$DB" "SELECT COUNT(*) FROM image_tags" 2>/dev/null || echo "?")
  embeddings=$(sqlite3 "$DB" "SELECT COUNT(*) FROM embeddings" 2>/dev/null || echo "?")
  clusters=$(sqlite3 "$DB" "SELECT COUNT(*) FROM clusters" 2>/dev/null || echo "?")
  run_id=$(sqlite3 "$DB" "SELECT IFNULL(MAX(id), 0) FROM cluster_runs" 2>/dev/null || echo "?")
  now=$(date +%s)
  dt=$(( prev_ts ? now - prev_ts : 0 ))

  printf '[%s] stage=%-7s images=%s [%s]  embeds=%s [%s]  tags=%s clusters=%s run=%s last=%s\n' \
    "$(date +%H:%M:%S)" "$(stage)" \
    "$images"     "$(delta_rate "$images"     "$prev_images" "$dt")" \
    "$embeddings" "$(delta_rate "$embeddings" "$prev_embeds" "$dt")" \
    "$tag_rows" "$clusters" "$run_id" "$(last_log_event)"

  prev_images="$images"
  prev_embeds="$embeddings"
  prev_ts="$now"
}

trap 'echo; echo "stopped."; exit 0' INT

while true; do
  print_snapshot
  if [[ "$(stage)" == "idle" ]] && grep -q '^DONE rc=' "$LOG" 2>/dev/null; then
    echo "pipeline finished: $(grep '^DONE rc=' "$LOG" | tail -n 1)"
    exit 0
  fi
  sleep "$INTERVAL"
done
