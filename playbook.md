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

### 4.2 Capturar screenshots REAIS do browser (TODOS os test cases)

**IMPORTANTE:** Screenshots sao do browser Chrome real controlado via MCP DevTools.
Nao sao imagens geradas ou fabricadas.

**REGRA:** Cada screenshot DEVE mostrar AMBOS:
1. O **curl command** gerado pelo Swagger UI
2. A **resposta do servidor** (status code + response body)

Para garantir isso, SEMPRE scroll para o heading "Curl" antes de capturar o screenshot.
A area abaixo do Curl mostra a Request URL e o Server Response, capturando tudo em uma unica imagem.

#### Passo 1: Abrir Swagger UI e instalar fetch interceptor

O Swagger UI NAO envia o token via `Authorization` header mesmo apos autenticar pelo modal.
A solucao e instalar um **fetch interceptor** que injeta o token em todas as requests para a API.

```
Tool: mcp__chrome-devtools__navigate_page
  type: "url"
  url: "http://localhost:8000/docs"

Tool: mcp__chrome-devtools__wait_for
  text: "Authorize"
  timeout: 15000
```

**Instalar fetch interceptor com auth:**
```
Tool: mcp__chrome-devtools__evaluate_script
  function: |
    () => {
      const TOKEN = '<jwt_token_aqui>';
      window._originalFetch = window.fetch.bind(window);
      window.fetch = function(url, opts) {
        opts = opts || {};
        if (typeof url === 'string' && url.includes('localhost:8000/api/')) {
          opts.headers = opts.headers || {};
          if (opts.headers instanceof Headers) {
            opts.headers.set('Authorization', 'Bearer ' + TOKEN);
          } else {
            opts.headers['Authorization'] = 'Bearer ' + TOKEN;
          }
          if (window._interceptNextRequest && window._replaceInUrl) {
            url = url.replace(window._replaceInUrl.from, window._replaceInUrl.to);
            window._interceptNextRequest = false;
          }
        }
        return window._originalFetch(url, opts);
      };
      return 'interceptor installed';
    }
```

**NOTA:** O interceptor suporta dois modos:
- **Auth injection:** Sempre adiciona `Authorization: Bearer <token>` em requests para a API
- **URL replacement:** Quando `window._interceptNextRequest = true` e `window._replaceInUrl = {from, to}`,
  substitui texto na URL antes de enviar. Util para bypassar validacao client-side do Swagger UI.

#### Passo 2: Expandir endpoint e ativar Try it out

```
Tool: mcp__chrome-devtools__take_snapshot
  → Encontrar uid do endpoint desejado e expandir

Tool: mcp__chrome-devtools__take_snapshot
  → Encontrar uid do "Try it out"

Tool: mcp__chrome-devtools__click
  uid: "<Try it out>"
  includeSnapshot: true
```

#### Passo 3: Para CADA test case (happy path + edge cases)

**3a. Preencher parametros:**
```
Tool: mcp__chrome-devtools__fill_form
  elements: [
    { "uid": "<param1>", "value": "{valor1}" },
    { "uid": "<param2>", "value": "{valor2}" }
  ]
```

**3b. (Se necessario) Configurar interceptor para test case especifico:**

Para test cases que precisam bypassar validacao client-side do Swagger UI
(ex: UUID invalido, page=0, page_size > max), usar o URL replacement interceptor:

```
Tool: mcp__chrome-devtools__evaluate_script
  function: |
    () => {
      window._interceptNextRequest = true;
      window._replaceInUrl = { from: 'page_size=20', to: 'page_size=101' };
      return 'interceptor armed';
    }
```

O Swagger UI envia o valor valido do form, mas o interceptor substitui na URL real
antes da request sair. Isso permite testar valores que o Swagger UI bloquearia client-side.

Para test cases sem auth (401), desativar a injecao de token temporariamente:

```
Tool: mcp__chrome-devtools__evaluate_script
  function: |
    () => {
      window.fetch = function(url, opts) {
        return window._originalFetch(url, opts);
      };
      return 'auth disabled';
    }
```

**3c. Executar e aguardar resposta:**
```
Tool: mcp__chrome-devtools__click
  uid: "<Execute>"

Tool: mcp__chrome-devtools__wait_for
  text: "Server response"
  timeout: 30000
```

**3d. Scroll para Curl e capturar screenshot:**

**OBRIGATORIO:** Antes de capturar, scroll para o heading "Curl" para que
o screenshot mostre curl command + response body juntos.

```
Tool: mcp__chrome-devtools__evaluate_script
  function: |
    () => {
      const headings = document.querySelectorAll('h4');
      for (const h of headings) {
        if (h.textContent.trim() === 'Curl') {
          h.scrollIntoView({ block: 'start' });
          return 'scrolled to Curl';
        }
      }
      return 'Curl heading not found';
    }

Tool: mcp__chrome-devtools__take_screenshot
  filePath: "/tmp/qa-evidence/tc-{NNN}-swagger.png"
```

**3e. (Se desativou auth) Reinstalar interceptor com auth para o proximo test case.**

**REGRA DE UIDs:** Sempre usar UIDs do snapshot MAIS RECENTE.
UIDs mudam apos qualquer acao que modifica o DOM.
Nunca reutilizar UIDs de snapshots anteriores.

---

## Fase 5: Upload de Imagens e Report no PR

### 5.1 Upload de screenshots (TODOS os test cases)

Upload CADA screenshot capturado (happy path + edge cases).

**Metodo primario: catbox.moe (zero deps, permanente, sem API key)**

```bash
python tools/qa_swagger.py upload /tmp/qa-evidence/tc-001-swagger.png
python tools/qa_swagger.py upload /tmp/qa-evidence/tc-002-swagger.png
# ... para todos os TCs
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

**IMPORTANTE:** O map `--images` deve conter URLs para TODOS os test cases (happy + edge).

```bash
python tools/qa_swagger.py report \
  --results /tmp/qa-results.json \
  --pr 45 \
  --us US-044 \
  --branch feat/US44 \
  --auth-strategy "Keycloak password grant" \
  --images '{"TC-001": "https://files.catbox.moe/abc.png", "TC-002": "https://files.catbox.moe/def.png", ...}' \
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

# 6. [Claude Code usa MCP DevTools para screenshots de TODOS os TCs - ver Fase 4.2]
#    Cada screenshot deve mostrar curl command + response body

# 7. Upload screenshots (todos os TCs)
python tools/qa_swagger.py upload /tmp/qa-evidence/tc-001-swagger.png
python tools/qa_swagger.py upload /tmp/qa-evidence/tc-002-swagger.png
# ... para todos os TCs

# 8. Gerar report (images com TODOS os TCs)
python tools/qa_swagger.py report \
  --results /tmp/qa-results.json \
  --pr 45 --us US-044 --branch feat/US44 \
  --images '{"TC-001": "https://...", "TC-002": "https://...", ...}' \
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
  SIM → Screenshots reais do Swagger UI (TODOS os TCs: happy + edge)
        Cada screenshot deve mostrar curl command + response body
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
