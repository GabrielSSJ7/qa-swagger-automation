# Swagger QA Automation Playbook

Playbook para automacao completa de testes manuais via Swagger UI.
Projetado para ser executado por um AI Agent (Claude Code) sem intervencao humana.
Agnostico ao projeto - funciona com qualquer API REST que exponha OpenAPI/Swagger.

---

## Setup

```bash
git clone git@github.com:GabrielSSJ7/qa-swagger-automation.git ~/code/qa-swagger-automation
cd ~/code/qa-swagger-automation
chmod +x setup.sh
./setup.sh
```

O script `setup.sh`:
1. Instala dependencias (`httpx`)
2. Adiciona automaticamente a referencia no `~/.claude/CLAUDE.md` com os caminhos absolutos do repo clonado

Apos rodar, Claude Code tera acesso global a ferramenta em qualquer projeto.

---

## Arquitetura

```
Claude Code (orquestrador)
  |
  |-- tools/qa_swagger.py        → Execucao programatica (HTTP, parse, report)
  |-- MCP DevTools (browser)     → Screenshots REAIS do Chrome (Swagger UI)
  |-- gh CLI                     → Postar comentario no PR
  |-- curl / catbox.moe          → Upload de imagens (permanente, sem API key)
```

### Separacao de Responsabilidades

| Quem | Faz o que |
|------|-----------|
| **Script** (`qa_swagger.py`) | Parsear OpenAPI, gerar test cases, executar HTTP requests, validar respostas, gerar markdown, upload de imagens, postar no PR |
| **Claude Code** (via MCP DevTools) | Abrir browser, navegar no Swagger UI, autenticar, preencher formularios, executar requests no Swagger, tirar screenshots REAIS do browser |
| **Claude Code** (orquestracao) | Decidir auth strategy, obter IDs existentes, substituir placeholders nos test cases, coordenar fluxo |

### Dependencias

| Ferramenta | Instalacao | Necessaria? |
|------------|-----------|-------------|
| Python 3.10+ | Ja disponivel | Sim |
| httpx | `pip install httpx` | Sim (geralmente ja instalado em projetos FastAPI) |
| MCP DevTools | Plugin Chrome DevTools do Claude Code | Sim (para screenshots do browser) |
| gh CLI | `brew install gh` / `apt install gh` | Sim (para postar no PR) |
| curl | Ja disponivel | Sim (para upload de imagens) |
| images-upload-cli | `pip install images-upload-cli` | Opcional (alternativa polida ao curl para uploads) |

---

## Fase 0: Inputs

Antes de iniciar, o agente DEVE coletar:

| Input | Como obter | Exemplo |
|-------|------------|---------|
| PR_NUMBER | `gh pr list --head $(git branch --show-current) --json number -q '.[0].number'` | `45` |
| REPO | `gh repo view --json nameWithOwner -q '.nameWithOwner'` | `Owner/repo-name` |
| BRANCH | `git branch --show-current` | `feat/US44` |
| US_CODE | Extrair da branch ou do contexto | `US-044` |
| BASE_URL | CLAUDE.md, .env ou convencao da framework | `http://localhost:8000` |
| DOCS_PATH | Convencao: FastAPI=`/docs`, Spring=`/swagger-ui` | `/docs` |

---

## Fase 1: Validacao do Ambiente

```bash
python tools/qa_swagger.py check-env \
  --base-url http://localhost:8000 \
  --docker-compose \
  --project-root codebases/backend
```

**Output:** JSON com status de cada servico.

**Decisoes:**
- `backend: DOWN` → Subir backend, re-executar check-env
- `swagger: DOWN` → Backend nao serve OpenAPI, abortar
- Todos `UP` → Prosseguir

**MCP DevTools:**
```
Tool: mcp__chrome-devtools__list_pages
```
- OK → Modo completo (browser screenshots)
- Erro → Modo fallback (apenas payloads JSON, sem screenshots)

---

## Fase 2: Autenticacao

O agente Claude Code deve tentar as estrategias na ordem abaixo.
Avancar para a proxima se a atual falhar.

### Estrategia A: OAuth2 Password Grant (Keycloak/OIDC)
```bash
curl -sf -X POST "{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token" \
  -d "grant_type=password&client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}&username={USER}&password={PASS}" \
  | jq -r '.access_token'
```

### Estrategia B: Client Credentials
```bash
curl -sf -X POST "{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}" \
  | jq -r '.access_token'
```

### Estrategia C: Endpoint de Login da aplicacao
```bash
curl -sf -X POST "{BASE_URL}/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"{EMAIL}","password":"{PASS}"}' \
  | jq -r '.access_token // .token'
```

### Estrategia D: Script local de teste
```
Glob: **/generate_token* OR **/create_test_token*
Grep: "jwt.encode" OR "create_access_token"
```

### Estrategia E: Sem auth disponivel
- Documentar limitacao no report
- Testar apenas: endpoints publicos + verificar 401 em protegidos

### Validar Token
```bash
curl -sf -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "{BASE_URL}/api/v1/{QUALQUER_ENDPOINT_PROTEGIDO}"
```
- `200-299` → Valido
- `401` → Invalido, re-gerar
- `403` → Valido mas role insuficiente

---

## Fase 3: Descoberta e Geracao de Test Cases

### 3.1 Gerar test cases automaticamente

```bash
python tools/qa_swagger.py discover \
  --spec-url http://localhost:8000/openapi.json \
  --paths "GET /api/v1/projects/{project_id}/workflows" \
  --output /tmp/qa-test-cases.json
```

O script gera automaticamente:
- **Happy path** para cada endpoint (status de sucesso)
- **Edge cases**: 401 sem auth, 404 ID inexistente, 422 ID invalido, 422 paginacao invalida

### 3.2 Claude Code enriquece os test cases

O agente DEVE ajustar o JSON gerado antes de executar:

1. **Substituir UUIDs placeholder** por IDs reais (buscar via listagem primeiro)
2. **Adicionar test cases extras** que o script nao consegue inferir:
   - 403 com role invalida (precisa de segundo token)
   - Multi-tenancy isolation (precisa de segundo tenant)
   - Soft delete (precisa deletar recurso primeiro)
3. **Ajustar expected_fields** se necessario

### 3.3 Heuristicas de Edge Case por Tipo

| Tipo | Edge Cases |
|------|-----------|
| GET (listagem) | `401` `403` `page=0→422` `page=-1→422` `page_size=max+1→422` `page=99999→items=[]` |
| GET (detalhe) | `401` `403` `uuid4()→404` `"not-uuid"→422` `outro_tenant→404` |
| POST | `401` `403` `body vazio→422` `campo ausente→422` `tipo invalido→422` `duplicata→409` `valido→201` |
| PUT | `401` `403` `id inexistente→404` `body vazio→422` `valido→200` |
| DELETE | `401` `403` `id inexistente→404` `valido→204` `re-delete→404` |

---

## Fase 4: Execucao dos Testes

### 4.1 Executar test cases via script

```bash
python tools/qa_swagger.py run \
  --cases /tmp/qa-test-cases.json \
  --token "Bearer eyJ..." \
  --base-url http://localhost:8000 \
  --output /tmp/qa-results.json
```

O script executa cada test case via httpx, valida o status code e campos esperados,
e salva os resultados com os payloads de resposta.

### 4.2 Capturar screenshots REAIS do browser (happy path)

**IMPORTANTE:** Screenshots sao do browser Chrome real controlado via MCP DevTools.
Nao sao imagens geradas ou fabricadas.

#### Passo 1: Abrir Swagger UI

```
Tool: mcp__chrome-devtools__navigate_page
  type: "url"
  url: "http://localhost:8000/docs"

Tool: mcp__chrome-devtools__wait_for
  text: "Authorize"
  timeout: 15000
```

#### Passo 2: Autenticar no Swagger

```
Tool: mcp__chrome-devtools__take_snapshot
  → Encontrar uid do botao "Authorize"

Tool: mcp__chrome-devtools__click
  uid: "<uid do Authorize>"
  includeSnapshot: true
  → No snapshot retornado, encontrar uid do input de token

Tool: mcp__chrome-devtools__fill
  uid: "<uid do input>"
  value: "Bearer eyJ..."

Tool: mcp__chrome-devtools__click
  uid: "<uid do Authorize dentro do modal>"

Tool: mcp__chrome-devtools__click
  uid: "<uid do Close>"
```

#### Passo 3: Para CADA test case happy path

**3a. Expandir endpoint:**
```
Tool: mcp__chrome-devtools__evaluate_script
  function: |
    () => {
      const blocks = document.querySelectorAll('.opblock-summary');
      for (const b of blocks) {
        if (b.textContent.includes('{PATH}') && b.textContent.includes('{METHOD}')) {
          b.scrollIntoView({ behavior: 'smooth', block: 'center' });
          b.click();
          return 'expanded';
        }
      }
      return 'not found';
    }
```

**3b. Try it out + preencher parametros:**
```
Tool: mcp__chrome-devtools__take_snapshot
  → Encontrar uid do "Try it out"

Tool: mcp__chrome-devtools__click
  uid: "<Try it out>"
  includeSnapshot: true

Tool: mcp__chrome-devtools__fill_form
  elements: [
    { "uid": "<param1>", "value": "{valor1}" },
    { "uid": "<param2>", "value": "{valor2}" }
  ]
```

**3c. Executar e capturar:**
```
Tool: mcp__chrome-devtools__click
  uid: "<Execute>"

Tool: mcp__chrome-devtools__wait_for
  text: "Response body"
  timeout: 30000

Tool: mcp__chrome-devtools__take_screenshot
  filePath: "/tmp/qa-evidence/tc-001-swagger.png"
```

**3d. Limpar para proximo:**
```
Tool: mcp__chrome-devtools__evaluate_script
  function: |
    () => {
      const btn = document.querySelector('.btn-clear');
      if (btn) { btn.click(); return 'cleared'; }
      return 'no button';
    }
```

**REGRA DE UIDs:** Sempre usar UIDs do snapshot MAIS RECENTE.
UIDs mudam apos qualquer acao que modifica o DOM.
Nunca reutilizar UIDs de snapshots anteriores.

---

## Fase 5: Upload de Imagens e Report no PR

### 5.1 Upload de screenshots

**Metodo primario: catbox.moe (zero deps, permanente, sem API key)**

```bash
python tools/qa_swagger.py upload /tmp/qa-evidence/tc-001-swagger.png
```

Retorna JSON:
```json
{"url": "https://files.catbox.moe/abc123.png", "markdown": "![tc-001](https://...)"}
```

**Alternativas disponiveis:**

| Host | Comando | Requer API key? | Permanente? |
|------|---------|-----------------|-------------|
| catbox.moe | `qa_swagger.py upload FILE` | Nao | Sim |
| imgbb | `qa_swagger.py upload FILE --host imgbb --api-key KEY` | Sim (gratis) | Sim |
| github (assets branch) | `qa_swagger.py upload FILE --host github --repo OWNER/REPO --pr 45` | Nao (usa gh auth) | Sim |
| images-upload-cli | `imgup -h catbox -f markdown FILE` | Depende do host | Depende |

### 5.2 Gerar report

```bash
python tools/qa_swagger.py report \
  --results /tmp/qa-results.json \
  --pr 45 \
  --us US-044 \
  --branch feat/US44 \
  --auth-strategy "Keycloak password grant" \
  --images '{"TC-001": "https://files.catbox.moe/abc123.png"}' \
  --output /tmp/qa-report.md
```

### 5.3 Postar no PR

```bash
python tools/qa_swagger.py post --pr 45 --body-file /tmp/qa-report.md
```

---

## Fluxo Completo (Copiar e Colar)

```bash
# 1. Validar ambiente
python tools/qa_swagger.py check-env --base-url http://localhost:8000 --docker-compose

# 2. [Claude Code obtem token - ver Fase 2]

# 3. Gerar test cases
python tools/qa_swagger.py discover \
  --spec-url http://localhost:8000/openapi.json \
  --paths "GET /api/v1/projects/{project_id}/workflows" \
  --output /tmp/qa-test-cases.json

# 4. [Claude Code edita /tmp/qa-test-cases.json - substitui IDs placeholder por reais]

# 5. Executar testes
python tools/qa_swagger.py run \
  --cases /tmp/qa-test-cases.json \
  --token "$TOKEN" \
  --base-url http://localhost:8000 \
  --output /tmp/qa-results.json

# 6. [Claude Code usa MCP DevTools para screenshots do browser - ver Fase 4.2]

# 7. Upload screenshots
python tools/qa_swagger.py upload /tmp/qa-evidence/tc-001-swagger.png

# 8. Gerar report
python tools/qa_swagger.py report \
  --results /tmp/qa-results.json \
  --pr 45 --us US-044 --branch feat/US44 \
  --images '{"TC-001": "https://files.catbox.moe/abc123.png"}' \
  --output /tmp/qa-report.md

# 9. Postar no PR
python tools/qa_swagger.py post --pr 45 --body-file /tmp/qa-report.md
```

---

## Arvore de Decisao

```
[check-env] Ambiente OK?
  NAO → Subir servicos → Re-executar check-env
  SIM ↓

[auth] Token disponivel?
  SIM → Testes completos (happy + edge com auth)
  NAO → Testes limitados (401 + endpoints publicos)
  ↓

[discover] Endpoints identificados?
  SIM → qa_swagger.py discover --paths "..."
  NAO → qa_swagger.py discover (todos os endpoints)
  ↓

[enrich] Claude Code enriquece test cases
  Substituir IDs placeholder por reais
  Adicionar edge cases que o script nao infere (403, multi-tenancy)
  ↓

[run] Executar test cases via script
  qa_swagger.py run --cases ... --token ...
  ↓

[MCP DevTools] Browser disponivel?
  SIM → Screenshots reais do Swagger UI (happy path)
  NAO → Pular screenshots, usar apenas payloads JSON
  ↓

[upload] Upload screenshots
  qa_swagger.py upload (catbox.moe | imgbb | github)
  ↓

[report] Gerar e postar no PR
  qa_swagger.py report → qa_swagger.py post
```

---

## Seletores CSS do Swagger UI (Referencia)

| Elemento | Seletor |
|----------|---------|
| Botao Authorize (topo) | `.btn.authorize` |
| Input de token no modal | `.auth-container input[type="text"]` |
| Authorize dentro do modal | `.auth-btn-wrapper .btn.modal-btn.auth.authorize` |
| Close do modal | `.auth-btn-wrapper .btn-done` |
| Summary do endpoint | `.opblock-summary` |
| Try it out | `.try-out__btn` |
| Inputs de parametros | `.parameters input` |
| Botao Execute | `.execute-wrapper .btn.execute` |
| Response body | `.responses-inner .microlight` |
| Response status | `.responses-inner .response-col_status` |
| Botao Clear | `.btn-clear` |
