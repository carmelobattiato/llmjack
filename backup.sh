#!/bin/bash
PROJECT=$(basename "$(cd "$(dirname "$0")" && pwd)")
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="$(dirname "$0")/../Backup"
ARCHIVE="$BACKUP_DIR/${PROJECT}_${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"

tar -czf "$ARCHIVE" \
  --exclude="data/qwen_profile" \
  --exclude="data/deepseek_profile" \
  --exclude="data/chatgpt_profile" \
  --exclude="data/claude_profile" \
  --exclude=".chrome_profile" \
  --exclude=".deepseek_profile" \
  --exclude=".chatgpt_profile" \
  --exclude=".claude_profile" \
  --exclude="__pycache__" \
  --exclude="*.pyc" \
  --exclude="clients/__pycache__" \
  -C "$(dirname "$0")/.." "$PROJECT"

FILE_COUNT=$(tar -tzf "$ARCHIVE" | grep -v '/$' | wc -l | tr -d ' ')
DIR_COUNT=$(tar -tzf "$ARCHIVE" | grep '/$' | wc -l | tr -d ' ')

# dimensione sorgente (escluse cartelle ignorate) — compatibile macOS
SOURCE_BYTES=$(find "$(dirname "$0")" \
  -not -path "*/data/*_profile/*" \
  -not -path "*/.chrome_profile/*" \
  -not -path "*/.deepseek_profile/*" \
  -not -path "*/.chatgpt_profile/*" \
  -not -path "*/.claude_profile/*" \
  -not -path "*/__pycache__/*" \
  -not -name "*.pyc" \
  -type f | xargs stat -f%z 2>/dev/null | awk '{s+=$1} END{print s+0}')

ARCHIVE_BYTES=$(stat -f%z "$ARCHIVE")

# formato leggibile
human() { awk -v b="$1" 'BEGIN{
  if(b<1024) printf "%d B", b
  else if(b<1048576) printf "%.1f KB", b/1024
  else printf "%.1f MB", b/1048576
}'; }

SOURCE_HR=$(human "$SOURCE_BYTES")
ARCHIVE_HR=$(human "$ARCHIVE_BYTES")

if [ "$SOURCE_BYTES" -gt 0 ]; then
  RATIO=$(awk -v a="$ARCHIVE_BYTES" -v s="$SOURCE_BYTES" \
    'BEGIN{printf "%.1f%%", (1 - a/s)*100}')
else
  RATIO="n/a"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Backup: $(basename "$ARCHIVE")"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "  %-16s %s\n" "File:" "$FILE_COUNT"
printf "  %-16s %s\n" "Cartelle:" "$DIR_COUNT"
printf "  %-16s %s\n" "Sorgente:" "$SOURCE_HR"
printf "  %-16s %s\n" "Archivio:" "$ARCHIVE_HR"
printf "  %-16s %s\n" "Compressione:" "$RATIO"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Salvato in: $ARCHIVE"
