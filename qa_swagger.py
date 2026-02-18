#!/usr/bin/env python3
"""QA Swagger Automation Tool.

Script para automacao de testes manuais via Swagger/OpenAPI.
Projetado para ser operado por um AI Agent (Claude Code).

Subcomandos:
  check-env   Valida ambiente (infra, backend, swagger)
  discover    Parseia OpenAPI spec e gera test cases
  run         Executa test cases via HTTP
  upload      Faz upload de imagem e retorna URL
  report      Gera markdown do comentario do PR
  post        Posta comentario no PR via gh CLI

Exemplos:
  python tools/qa_swagger.py check-env --base-url http://localhost:8000
  python tools/qa_swagger.py discover --spec-url http://localhost:8000/openapi.json --paths "GET /api/v1/projects/{project_id}/workflows"
  python tools/qa_swagger.py run --cases /tmp/cases.json --token "Bearer xxx" --base-url http://localhost:8000
  python tools/qa_swagger.py upload /tmp/screenshot.png
  python tools/qa_swagger.py report --results /tmp/results.json --pr 45 --us US-044 --branch feat/US44
  python tools/qa_swagger.py post --pr 45 --body-file /tmp/report.md
"""

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx


@dataclass
class TestCase:
    id: str
    type: str
    method: str
    path: str
    description: str
    path_params: dict = field(default_factory=dict)
    query_params: dict = field(default_factory=dict)
    body: dict | None = None
    headers: dict = field(default_factory=dict)
    expected_status: int = 200
    expected_fields: list[str] = field(default_factory=list)
    validations: list[str] = field(default_factory=list)


@dataclass
class TestResult:
    id: str
    test_case: TestCase
    actual_status: int
    response_body: str
    passed: bool
    notes: str = ""
    screenshot_url: str = ""
    duration_ms: float = 0


def cmd_check_env(args):
    results = {}

    if args.docker_compose:
        proc = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True, text=True, cwd=args.project_root or "."
        )
        if proc.returncode == 0:
            results["docker"] = "UP"
        else:
            proc2 = subprocess.run(
                ["docker-compose", "ps"],
                capture_output=True, text=True, cwd=args.project_root or "."
            )
            results["docker"] = "UP" if proc2.returncode == 0 else "DOWN"

    try:
        resp = httpx.get(f"{args.base_url}/health", timeout=5)
        results["backend"] = "UP" if resp.status_code == 200 else f"STATUS_{resp.status_code}"
    except httpx.ConnectError:
        results["backend"] = "DOWN"

    try:
        resp = httpx.get(f"{args.base_url}{args.openapi_path}", timeout=5)
        if resp.status_code == 200:
            spec = resp.json()
            results["swagger"] = "UP"
            results["endpoints_count"] = len(spec.get("paths", {}))
        else:
            results["swagger"] = f"STATUS_{resp.status_code}"
    except (httpx.ConnectError, json.JSONDecodeError):
        results["swagger"] = "DOWN"

    print(json.dumps(results, indent=2))
    all_up = all(v in ("UP",) for k, v in results.items() if k not in ("endpoints_count",))
    return 0 if all_up else 1


def cmd_discover(args):
    if args.spec_file:
        spec = json.loads(Path(args.spec_file).read_text())
    else:
        resp = httpx.get(args.spec_url, timeout=10)
        spec = resp.json()

    target_paths = None
    if args.paths:
        target_paths = []
        for p in args.paths:
            parts = p.strip().split(" ", 1)
            if len(parts) == 2:
                target_paths.append((parts[0].upper(), parts[1]))
            else:
                target_paths.append((None, parts[0]))

    cases = []
    seq = 1

    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if method.lower() in ("parameters", "servers", "summary", "description"):
                continue

            method_upper = method.upper()

            if target_paths:
                matched = False
                for tm, tp in target_paths:
                    if tp == path and (tm is None or tm == method_upper):
                        matched = True
                        break
                if not matched:
                    continue

            summary = operation.get("summary", f"{method_upper} {path}")
            parameters = operation.get("parameters", [])
            request_body = operation.get("requestBody", {})

            path_params = {}
            query_params = {}
            for param in parameters:
                name = param.get("name", "")
                location = param.get("in", "")
                schema = param.get("schema", {})
                param_type = schema.get("type", "string")
                fmt = schema.get("format", "")

                if location == "path":
                    if fmt == "uuid" or "id" in name.lower():
                        path_params[name] = str(uuid4())
                    elif param_type == "integer":
                        path_params[name] = "1"
                    else:
                        path_params[name] = "test-value"

                elif location == "query":
                    default = schema.get("default")
                    minimum = schema.get("minimum")
                    maximum = schema.get("maximum")
                    if default is not None:
                        query_params[name] = str(default)
                    elif param_type == "integer":
                        query_params[name] = str(minimum or 1)

            cases.append(TestCase(
                id=f"TC-{seq:03d}",
                type="happy_path",
                method=method_upper,
                path=path,
                description=f"{summary} (sucesso)",
                path_params=path_params,
                query_params=query_params,
                expected_status=_expected_success_status(method_upper, operation),
                expected_fields=_extract_response_fields(operation),
            ))
            seq += 1

            cases.append(TestCase(
                id=f"TC-{seq:03d}",
                type="edge_case",
                method=method_upper,
                path=path,
                description=f"{summary} - sem autenticacao (401)",
                path_params=path_params,
                query_params=query_params,
                headers={"__skip_auth__": "true"},
                expected_status=401,
            ))
            seq += 1

            uuid_params = {k: v for k, v in path_params.items() if "id" in k.lower()}
            if uuid_params:
                cases.append(TestCase(
                    id=f"TC-{seq:03d}",
                    type="edge_case",
                    method=method_upper,
                    path=path,
                    description=f"{summary} - ID inexistente (404)",
                    path_params={**path_params, **{k: str(uuid4()) for k in uuid_params}},
                    query_params=query_params,
                    expected_status=404,
                ))
                seq += 1

                cases.append(TestCase(
                    id=f"TC-{seq:03d}",
                    type="edge_case",
                    method=method_upper,
                    path=path,
                    description=f"{summary} - ID invalido (422)",
                    path_params={**path_params, **{k: "not-a-valid-uuid" for k in uuid_params}},
                    query_params=query_params,
                    expected_status=422,
                ))
                seq += 1

            page_params = {k for k in query_params if k in ("page", "page_number")}
            size_params = {k for k in query_params if k in ("page_size", "per_page", "limit")}

            if page_params:
                p_name = next(iter(page_params))
                cases.append(TestCase(
                    id=f"TC-{seq:03d}",
                    type="edge_case",
                    method=method_upper,
                    path=path,
                    description=f"{summary} - paginacao page=0 (422)",
                    path_params=path_params,
                    query_params={**query_params, p_name: "0"},
                    expected_status=422,
                ))
                seq += 1

            if size_params:
                s_name = next(iter(size_params))
                for param in parameters:
                    if param.get("name") == s_name:
                        max_val = param.get("schema", {}).get("maximum")
                        if max_val:
                            cases.append(TestCase(
                                id=f"TC-{seq:03d}",
                                type="edge_case",
                                method=method_upper,
                                path=path,
                                description=f"{summary} - page_size excedido ({max_val + 1}) (422)",
                                path_params=path_params,
                                query_params={**query_params, s_name: str(max_val + 1)},
                                expected_status=422,
                            ))
                            seq += 1

    output = [asdict(c) for c in cases]
    out_path = Path(args.output)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(json.dumps({"total": len(cases), "happy_path": sum(1 for c in cases if c.type == "happy_path"), "edge_cases": sum(1 for c in cases if c.type == "edge_case"), "output": str(out_path)}, indent=2))
    return 0


def cmd_run(args):
    cases_data = json.loads(Path(args.cases).read_text())
    results = []

    for tc_data in cases_data:
        tc = TestCase(**tc_data)

        url_path = tc.path
        for k, v in tc.path_params.items():
            url_path = url_path.replace(f"{{{k}}}", v)
        url = f"{args.base_url}{url_path}"

        headers = {"Content-Type": "application/json"}
        skip_auth = tc.headers.get("__skip_auth__") == "true"
        if args.token and not skip_auth:
            token = args.token if args.token.startswith("Bearer ") else f"Bearer {args.token}"
            headers["Authorization"] = token

        for k, v in tc.headers.items():
            if k != "__skip_auth__":
                headers[k] = v

        start = time.monotonic()
        try:
            with httpx.Client(timeout=30) as client:
                response = client.request(
                    method=tc.method,
                    url=url,
                    params=tc.query_params or None,
                    json=tc.body,
                    headers=headers,
                )
            duration = (time.monotonic() - start) * 1000

            try:
                body = json.dumps(response.json(), indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                body = response.text

            passed = response.status_code == tc.expected_status
            notes = ""

            if passed and tc.expected_fields:
                try:
                    resp_json = response.json()
                    missing = [f for f in tc.expected_fields if f not in resp_json]
                    if missing:
                        passed = False
                        notes = f"Campos ausentes: {missing}"
                except (json.JSONDecodeError, ValueError):
                    passed = False
                    notes = "Resposta nao e JSON valido"

            result = TestResult(
                id=tc.id,
                test_case=tc,
                actual_status=response.status_code,
                response_body=body,
                passed=passed,
                notes=notes,
                duration_ms=round(duration, 1),
            )
        except httpx.ConnectError as e:
            result = TestResult(
                id=tc.id,
                test_case=tc,
                actual_status=0,
                response_body="",
                passed=False,
                notes=f"Erro de conexao: {e}",
            )

        results.append(result)

        status_icon = "PASS" if result.passed else "FAIL"
        print(f"  {status_icon} {tc.id}: {tc.description} -> {result.actual_status} (esperado {tc.expected_status}) [{result.duration_ms}ms]")

    output = []
    for r in results:
        output.append({
            "id": r.id,
            "passed": r.passed,
            "actual_status": r.actual_status,
            "expected_status": r.test_case.expected_status,
            "description": r.test_case.description,
            "type": r.test_case.type,
            "method": r.test_case.method,
            "path": r.test_case.path,
            "response_body": r.response_body,
            "notes": r.notes,
            "screenshot_url": r.screenshot_url,
            "duration_ms": r.duration_ms,
        })

    out_path = Path(args.output)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    print(f"\nResultado: {passed}/{total} passed, {failed} failed")
    print(json.dumps({"total": total, "passed": passed, "failed": failed, "output": str(out_path)}, indent=2))
    return 0 if failed == 0 else 1


def cmd_upload(args):
    filepath = Path(args.file)
    if not filepath.exists():
        print(json.dumps({"error": f"Arquivo nao encontrado: {filepath}"}))
        return 1

    host = args.host or "catbox"

    if host == "catbox":
        proc = subprocess.run(
            ["curl", "-sf", "-F", "reqtype=fileupload", "-F", f"fileToUpload=@{filepath}", "https://catbox.moe/user/api.php"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.startswith("https://"):
            url = proc.stdout.strip()
            print(json.dumps({"url": url, "markdown": f"![{filepath.stem}]({url})", "host": "catbox"}))
            return 0
        print(json.dumps({"error": f"Upload falhou: {proc.stderr or proc.stdout}", "host": "catbox"}))
        return 1

    elif host == "imgbb":
        api_key = args.api_key
        if not api_key:
            print(json.dumps({"error": "imgbb requer --api-key"}))
            return 1
        import base64
        with open(filepath, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()
        resp = httpx.post(
            "https://api.imgbb.com/1/upload",
            data={"key": api_key, "image": encoded},
            timeout=60,
        )
        if resp.status_code == 200:
            url = resp.json()["data"]["display_url"]
            print(json.dumps({"url": url, "markdown": f"![{filepath.stem}]({url})", "host": "imgbb"}))
            return 0
        print(json.dumps({"error": f"Upload falhou: {resp.text}", "host": "imgbb"}))
        return 1

    elif host == "github":
        repo = args.repo
        branch = args.github_branch or "assets"
        if not repo:
            print(json.dumps({"error": "github requer --repo"}))
            return 1
        import base64
        with open(filepath, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()
        filename = f"qa/{args.pr or 'misc'}/{filepath.name}"
        proc = subprocess.run(
            ["gh", "api", "--method", "PUT", f"/repos/{repo}/contents/{filename}",
             "-f", f"message=qa: {filepath.stem}",
             "-f", f"content={encoded}",
             "-f", f"branch={branch}"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            url = f"https://raw.githubusercontent.com/{repo}/{branch}/{filename}"
            print(json.dumps({"url": url, "markdown": f"![{filepath.stem}]({url})", "host": "github"}))
            return 0
        print(json.dumps({"error": f"Upload falhou: {proc.stderr}", "host": "github"}))
        return 1

    print(json.dumps({"error": f"Host desconhecido: {host}. Use: catbox, imgbb, github"}))
    return 1


def cmd_report(args):
    results = json.loads(Path(args.results).read_text())

    images = {}
    if args.images:
        images = json.loads(args.images)

    happy = [r for r in results if r["type"] == "happy_path"]
    edge = [r for r in results if r["type"] == "edge_case"]
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed

    lines = []
    lines.append(f"## QA: Validacao de Endpoints - {args.us or 'N/A'}")
    lines.append("")
    lines.append(f"**Data:** {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**PR:** #{args.pr} | **Branch:** `{args.branch or 'N/A'}`")
    lines.append(f"**Auth:** {args.auth_strategy or 'N/A'}")
    lines.append("")
    lines.append("### Resumo")
    lines.append("")
    lines.append("| Tipo | Total | Pass | Fail |")
    lines.append("|------|-------|------|------|")
    hp = sum(1 for r in happy if r["passed"])
    ep = sum(1 for r in edge if r["passed"])
    lines.append(f"| Happy Path | {len(happy)} | {hp} | {len(happy) - hp} |")
    lines.append(f"| Edge Cases | {len(edge)} | {ep} | {len(edge) - ep} |")
    lines.append(f"| **Total** | **{total}** | **{passed}** | **{failed}** |")
    lines.append("")
    lines.append("---")
    lines.append("")

    if happy:
        lines.append("### Happy Path")
        lines.append("")
        for r in happy:
            icon = "PASS" if r["passed"] else "FAIL"
            lines.append(f"<details>")
            lines.append(f"<summary>{r['id']}: {r['description']} - {icon}</summary>")
            lines.append("")
            lines.append(f"**Request:** `{r['method']} {r['path']}`")
            lines.append("")
            lines.append(f"**Response:** `{r['actual_status']}`")
            body = r.get("response_body", "")
            if body:
                truncated = _truncate_json(body, 40)
                lines.append("```json")
                lines.append(truncated)
                lines.append("```")
            img_url = images.get(r["id"]) or r.get("screenshot_url", "")
            if img_url:
                lines.append("")
                lines.append(f"![{r['id']}]({img_url})")
            if r.get("notes"):
                lines.append(f"\n> {r['notes']}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    if edge:
        lines.append("### Edge Cases")
        lines.append("")
        for r in edge:
            icon = "PASS" if r["passed"] else "FAIL"
            lines.append(f"<details>")
            lines.append(f"<summary>{r['id']}: {r['description']} - {icon}</summary>")
            lines.append("")
            lines.append(f"**Request:** `{r['method']} {r['path']}`")
            lines.append(f"**Response:** `{r['actual_status']}` (esperado: `{r['expected_status']}`)")
            body = r.get("response_body", "")
            if body:
                truncated = _truncate_json(body, 20)
                lines.append("```json")
                lines.append(truncated)
                lines.append("```")
            img_url = images.get(r["id"]) or r.get("screenshot_url", "")
            if img_url:
                lines.append("")
                lines.append(f"![{r['id']}]({img_url})")
            if r.get("notes"):
                lines.append(f"\n> {r['notes']}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    lines.append("---")

    markdown = "\n".join(lines)
    out_path = Path(args.output)
    out_path.write_text(markdown)
    print(json.dumps({"output": str(out_path), "size": len(markdown)}))
    return 0


def cmd_post(args):
    if args.body_file:
        body = Path(args.body_file).read_text()
    elif args.body:
        body = args.body
    else:
        print(json.dumps({"error": "--body ou --body-file obrigatorio"}))
        return 1

    proc = subprocess.run(
        ["gh", "pr", "comment", str(args.pr), "--body", body],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode == 0:
        print(json.dumps({"status": "posted", "pr": args.pr}))
        return 0
    print(json.dumps({"error": proc.stderr, "pr": args.pr}))
    return 1


def _expected_success_status(method: str, operation: dict) -> int:
    responses = operation.get("responses", {})
    for code in ("200", "201", "204"):
        if code in responses:
            return int(code)
    if method == "POST":
        return 201
    if method == "DELETE":
        return 204
    return 200


def _extract_response_fields(operation: dict) -> list[str]:
    responses = operation.get("responses", {})
    for code in ("200", "201"):
        resp = responses.get(code, {})
        content = resp.get("content", {})
        json_schema = content.get("application/json", {}).get("schema", {})
        props = json_schema.get("properties", {})
        if props:
            return list(props.keys())
    return []


def _truncate_json(body: str, max_lines: int) -> str:
    lines = body.split("\n")
    if len(lines) <= max_lines:
        return body
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines)} linhas total)"


def main():
    parser = argparse.ArgumentParser(description="QA Swagger Automation Tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_env = sub.add_parser("check-env", help="Valida ambiente")
    p_env.add_argument("--base-url", required=True)
    p_env.add_argument("--openapi-path", default="/openapi.json")
    p_env.add_argument("--docker-compose", action="store_true")
    p_env.add_argument("--project-root", default=None)

    p_disc = sub.add_parser("discover", help="Gera test cases a partir do OpenAPI spec")
    p_disc.add_argument("--spec-url", default=None)
    p_disc.add_argument("--spec-file", default=None)
    p_disc.add_argument("--paths", nargs="*", help='Filtrar endpoints: "GET /api/v1/..."')
    p_disc.add_argument("--output", default="/tmp/qa-test-cases.json")

    p_run = sub.add_parser("run", help="Executa test cases")
    p_run.add_argument("--cases", required=True)
    p_run.add_argument("--token", default=None)
    p_run.add_argument("--base-url", required=True)
    p_run.add_argument("--output", default="/tmp/qa-results.json")

    p_up = sub.add_parser("upload", help="Upload de imagem")
    p_up.add_argument("file")
    p_up.add_argument("--host", default="catbox", choices=["catbox", "imgbb", "github"])
    p_up.add_argument("--api-key", default=None)
    p_up.add_argument("--repo", default=None)
    p_up.add_argument("--pr", default=None)
    p_up.add_argument("--github-branch", default="assets")

    p_rep = sub.add_parser("report", help="Gera markdown do relatorio")
    p_rep.add_argument("--results", required=True)
    p_rep.add_argument("--pr", required=True)
    p_rep.add_argument("--us", default=None)
    p_rep.add_argument("--branch", default=None)
    p_rep.add_argument("--auth-strategy", default=None)
    p_rep.add_argument("--images", default=None, help="JSON map: {tc_id: url}")
    p_rep.add_argument("--output", default="/tmp/qa-report.md")

    p_post = sub.add_parser("post", help="Posta comentario no PR")
    p_post.add_argument("--pr", required=True, type=int)
    p_post.add_argument("--body", default=None)
    p_post.add_argument("--body-file", default=None)

    args = parser.parse_args()

    commands = {
        "check-env": cmd_check_env,
        "discover": cmd_discover,
        "run": cmd_run,
        "upload": cmd_upload,
        "report": cmd_report,
        "post": cmd_post,
    }
    sys.exit(commands[args.command](args))


if __name__ == "__main__":
    main()
