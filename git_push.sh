#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURAZIONE — modifica qui
# ─────────────────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/carmelobattiato/llmjack"
BRANCH="main"
TOKEN_FILE=".llmjack_token"         # token nella cartella progetto, gitignored
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── colori ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}▸${RESET} $*"; }
success() { echo -e "${GREEN}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET} $*"; }
die()     { echo -e "${RED}✗${RESET} $*" >&2; exit 1; }

# ── script dir ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── token ─────────────────────────────────────────────────────────────────────
if [[ -f "$TOKEN_FILE" ]]; then
    GITHUB_TOKEN=$(<"$TOKEN_FILE")
    info "Token caricato da $TOKEN_FILE"
else
    echo -e "${BOLD}GitHub Personal Access Token (sarà salvato in $TOKEN_FILE):${RESET}"
    read -rs GITHUB_TOKEN
    echo
    [[ -z "$GITHUB_TOKEN" ]] && die "Token non fornito."
    echo "$GITHUB_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    success "Token salvato in $TOKEN_FILE"
fi

# ── git init se non esiste ────────────────────────────────────────────────────
if [[ ! -d ".git" ]]; then
    info "Inizializzazione repository git..."
    git init
    git checkout -b "$BRANCH" 2>/dev/null || git branch -M "$BRANCH"
    success "Repository inizializzato."
fi

# ── .gitignore ────────────────────────────────────────────────────────────────
GITIGNORE=".gitignore"
declare -a IGNORE_ENTRIES=(
    "# Runtime data"
    "data/"
    "logs/"
    ""
    "# Token e credenziali"
    ".llmjack_token"
    "*.token"
    "*.pwd"
    "*.password"
    ".env"
    ".env.*"
    ""
    "# Sessioni legacy"
    ".session_id"
    ".chat_url"
    ".chrome_profile/"
    ".deepseek_profile/"
    ".chatgpt_profile/"
    ".claude_profile/"
    ".*_session_id"
    ""
    "# Python"
    "__pycache__/"
    "*.pyc"
    "*.pyo"
    ".venv/"
    "venv/"
    ""
    "# macOS"
    ".DS_Store"
    ""
    "# Backup"
    "*.bak"
    "providers.json.bak"
    ""
    "# Script di deploy locale"
    "git_push.sh"
)

touch "$GITIGNORE"
for entry in "${IGNORE_ENTRIES[@]}"; do
    grep -qxF "$entry" "$GITIGNORE" 2>/dev/null || echo "$entry" >> "$GITIGNORE"
done
success ".gitignore aggiornato."

# ── remote (URL senza token — credenziali passate via helper) ─────────────────
if git remote get-url origin &>/dev/null; then
    git remote set-url origin "$REPO_URL"
else
    git remote add origin "$REPO_URL"
fi

# ── stato attuale ─────────────────────────────────────────────────────────────
echo
info "File modificati:"
git status --short
echo

# ── commit message ────────────────────────────────────────────────────────────
echo -e "${BOLD}Messaggio di commit:${RESET}"
read -r COMMIT_MSG
[[ -z "$COMMIT_MSG" ]] && die "Messaggio di commit vuoto — operazione annullata."

# ── staging + commit ──────────────────────────────────────────────────────────
git add -A
if git diff --cached --quiet; then
    warn "Nessuna modifica da committare — procedo comunque con il push."
else
    git commit -m "$COMMIT_MSG"
fi

# ── push (token nell'URL, bypassa rewrite SSH globale) ───────────────────────
info "Push su $REPO_URL ($BRANCH)..."

# Costruisce URL con token e forza HTTPS ignorando qualsiasi rewrite SSH globale.
PUSH_URL="https://x-token:${GITHUB_TOKEN}@github.com/${REPO_URL#https://github.com/}.git"
git \
    -c "url.https://github.com/.insteadOf=git@github.com:" \
    push -u "$PUSH_URL" HEAD:"$BRANCH"

success "Push completato → ${BOLD}${REPO_URL}${RESET}"
