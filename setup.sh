#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_MD="$HOME/.claude/CLAUDE.md"
MARKER="## Swagger QA Automation"

if ! command -v python3 &>/dev/null; then
  echo "ERRO: python3 nao encontrado"
  exit 1
fi

echo "Instalando dependencias..."
pip install -q -r "$REPO_DIR/requirements.txt"

if [ ! -f "$CLAUDE_MD" ]; then
  mkdir -p "$HOME/.claude"
  touch "$CLAUDE_MD"
fi

if grep -qF "$MARKER" "$CLAUDE_MD" 2>/dev/null; then
  echo "CLAUDE.md ja possui referencia ao Swagger QA Automation. Pulando."
else
  cat >> "$CLAUDE_MD" << BLOCK

---

# GLOBAL TOOLS

$MARKER

Ferramenta para automacao de testes manuais via Swagger UI. Agnostica ao projeto.

- **Script:** \`$REPO_DIR/qa_swagger.py\`
- **Playbook:** \`$REPO_DIR/playbook.md\`

**Quando usar:** Quando o usuario pedir para testar endpoints manualmente, gerar evidencias de QA, ou postar resultados de teste no PR.

**Como usar:** Ler o playbook PRIMEIRO. Ele contem o fluxo completo (6 fases): validar ambiente, autenticar, gerar test cases, executar testes, capturar screenshots reais do browser (MCP DevTools), upload de imagens e postar report no PR.

**Subcomandos do script:**
- \`check-env\` - Validar ambiente (backend, swagger, docker)
- \`discover\` - Parsear OpenAPI e gerar test cases automaticamente
- \`run\` - Executar test cases via HTTP
- \`upload\` - Upload de screenshots (catbox.moe, imgbb, github)
- \`report\` - Gerar report markdown
- \`post\` - Postar report como comentario no PR via gh CLI
BLOCK
  echo "Referencia adicionada em $CLAUDE_MD"
fi

echo "Setup concluido."
