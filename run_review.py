"""Analyse the proposed schema change and comment the result on the pull request.

By default it calls the public SchemaSafe API, which inspects real MongoDB and CouchDB
databases (synthetic data, read-only) and returns what would really change: affected
records, dependent indexes and validations, and the command that would run (never run).
With api-url empty it falls back to the offline CLI on fixtures (`schemasafe github-report`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENGINES = {"mongodb", "couchdb", "cassandra"}
MARKER = "<!-- schemasafe-analysis -->"
USER_AGENT = "schemasafe-action/1.1"


def _request(url: str, *, method: str = "GET", payload: Any = None, token: str = "",
             timeout: int = 30) -> Any:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else None


def analyse(api_url: str, change: dict[str, str]) -> dict[str, Any]:
    proposal = {"operation": change["operation"]}
    for key in ("field", "target_type", "target_field"):
        if change.get(key):
            proposal[key] = change[key]
    payload = {
        "engine": change["engine"],
        "project_id": "public-demo",
        "resource": change.get("resource") or "demo",
        "proposal": proposal,
    }
    # The API scales to zero: the first call wakes it and its databases (up to a minute).
    for attempt in range(3):
        try:
            return _request(f"{api_url.rstrip('/')}/api/v1/analyses", method="POST",
                            payload=payload, timeout=120)
        except urllib.error.HTTPError as error:
            detail = error.read()[:500].decode("utf-8", "replace")
            if error.code < 500 or attempt == 2:
                raise SystemExit(f"::error::La API respondió {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == 2:
                raise SystemExit(f"::error::No se pudo conectar con la API: {error}") from error
        time.sleep(20)
    raise SystemExit("::error::La API no respondió.")


def comment_markdown(report: dict[str, Any]) -> str:
    proposal = report.get("proposal") or {}
    impact = report.get("impact") or {}
    percentage = report.get("impact_percentage")
    affected = report.get("affected_count")
    measured = "no disponible" if percentage is None else f"{percentage} % ({affected} de {report.get('analyzed_count')})"
    source = {"live": "LIVE — base de datos real conectada (solo lectura)",
              "fixture": "FIXTURE — datos de ejemplo"}.get(report.get("mode"), report.get("mode"))
    change = f"`{proposal.get('operation')}` `{proposal.get('field') or ''}`"
    if proposal.get("target_type"):
        change += f" → `{proposal['target_type']}`"
    if proposal.get("target_field"):
        change += f" → `{proposal['target_field']}`"
    lines = [
        MARKER,
        "## Análisis SchemaSafe (solo lectura)",
        "",
        "| Campo | Valor |",
        "| --- | --- |",
        f"| Motor / recurso | `{report.get('engine')}` / `{report.get('resource')}` |",
        f"| Cambio | {change} |",
        f"| Riesgo | **{report.get('risk')}** |",
        f"| Resultado | {report.get('outcome')} |",
        f"| Registros afectados | {measured} |",
        f"| Procedencia | {source} |",
        f"| Reglas | {', '.join(report.get('rules_applied') or []) or 'ninguna'} |",
        f"| Reporte | `{report.get('id')}` |",
        "",
    ]
    if impact.get("summary"):
        lines += ["### Qué cambia", "", impact["summary"], ""]
    types = impact.get("field_types") or {}
    if types:
        lines += ["Tipos actuales del campo: " + ", ".join(f"`{k}`: {v}" for k, v in types.items()), ""]
    examples = impact.get("affected_examples") or []
    if examples:
        lines += ["| Registro | Valor actual | Después del cambio |", "| --- | --- | --- |"]
        lines += [f"| `{e['record_id']}` | {e['current']} | {e['after']} |" for e in examples]
        lines.append("")
    dependencies = [*(impact.get("dependent_indexes") or []), *(impact.get("dependent_validations") or [])]
    if dependencies:
        lines += ["**Depende del campo:** " + ", ".join(f"`{d}`" for d in dependencies), ""]
    if impact.get("planned_command"):
        lines += ["Comando que se ejecutaría (**no se ejecutó**):", "", "```js",
                  impact["planned_command"], "```", ""]
    for finding in report.get("findings") or []:
        lines.append(f"- **{finding.get('rule_id')}** ({finding.get('severity')}): "
                     f"{finding.get('message')} {finding.get('recommendation')}")
    lines += ["", "El análisis no modificó ninguna base de datos."]
    return "\n".join(lines)


def publish(report: dict[str, Any], body: str, pr_number: str, token: str) -> None:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = os.environ["GITHUB_REPOSITORY"]
    comments = _request(f"{api}/repos/{repo}/issues/{pr_number}/comments?per_page=100", token=token)
    existing = next((c for c in comments if MARKER in (c.get("body") or "")), None)
    if existing:
        _request(f"{api}/repos/{repo}/issues/comments/{existing['id']}", method="PATCH",
                 payload={"body": body}, token=token)
        print(f"Comentario actualizado en el PR #{pr_number}.")
    else:
        _request(f"{api}/repos/{repo}/issues/{pr_number}/comments", method="POST",
                 payload={"body": body}, token=token)
        print(f"Comentario publicado en el PR #{pr_number}.")
    if report.get("risk") != "CRÍTICO":
        return
    proposal = report.get("proposal") or {}
    key = f"{report.get('engine')}/{report.get('resource')}/{proposal.get('operation')}/{proposal.get('field')}"
    issue_marker = f"<!-- schemasafe-critical:{key} -->"
    issues = _request(f"{api}/repos/{repo}/issues?state=open&per_page=100", token=token)
    issue_body = f"{issue_marker}\nEl PR #{pr_number} propone un cambio de riesgo CRÍTICO.\n\n{body}"
    current = next((i for i in issues if issue_marker in (i.get("body") or "")), None)
    if current:
        _request(f"{api}/repos/{repo}/issues/{current['number']}", method="PATCH",
                 payload={"body": issue_body}, token=token)
        print(f"Issue crítica #{current['number']} actualizada.")
    else:
        created = _request(f"{api}/repos/{repo}/issues", method="POST", token=token, payload={
            "title": f"SchemaSafe: cambio CRÍTICO en {key}", "body": issue_body})
        print(f"Issue crítica #{created['number']} abierta.")


def offline(change: dict[str, str], pr_number: str, dry_run: bool) -> int:
    command = ["schemasafe", "github-report", "--engine", change["engine"],
               "--resource", change.get("resource") or "demo", "--operation", change["operation"],
               "--field", change.get("field") or "", "--pr", pr_number]
    if change.get("target_type"):
        command += ["--target-type", change["target_type"]]
    if dry_run:
        command.append("--dry-run")
    print("Ejecutando:", " ".join(command))
    return subprocess.call(command)


def main() -> int:
    change: dict[str, str] = {}
    change_file = Path(os.environ.get("INPUT_CHANGE_FILE") or "schema-change.json")
    if change_file.is_file():
        change = {k: str(v) for k, v in json.loads(change_file.read_text(encoding="utf-8")).items()}
    for key in ("engine", "resource", "operation", "field", "target_type", "target_field"):
        override = os.environ.get(f"INPUT_{key.upper()}", "").strip()
        if override:
            change[key] = override
    if change.get("engine", "").strip() not in ENGINES or not change.get("operation", "").strip():
        print(f"::error::Indica engine (mongodb, couchdb o cassandra) y operation en {change_file} "
              "o como entradas de la Action.")
        return 2
    pr_number = os.environ.get("INPUT_PR_NUMBER", "").strip()
    if not pr_number:
        print("::error::No hay pull request: usa la Action en un evento pull_request o pasa pr-number.")
        return 2
    dry_run = os.environ.get("INPUT_DRY_RUN", "").strip().lower() == "true"
    token = os.environ.get("GITHUB_TOKEN", "")
    if not dry_run and not token:
        print("::error::Falta el token. Deja github-token en su valor por defecto (GITHUB_TOKEN).")
        return 2
    api_url = os.environ.get("INPUT_API_URL", "").strip()
    if not api_url:
        return offline(change, pr_number, dry_run)
    report = analyse(api_url, change)
    body = comment_markdown(report)
    print(body)
    if not dry_run:
        publish(report, body, pr_number, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
