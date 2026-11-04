"""Optional external platform event polling for Telegram notifications."""
from __future__ import annotations

import logging
import requests

log = logging.getLogger(__name__)
TIMEOUT = (4, 10)


def _get_json(url: str, headers: dict[str, str]):
    response = requests.get(url, headers=headers, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def poll_external_events(settings, state: dict) -> list[dict[str, str]]:
    """Return newly observed status transitions; first poll establishes baseline."""
    events: list[dict[str, str]] = []

    if settings.render_api_key and settings.render_service_id:
        try:
            payload = _get_json(
                f"https://api.render.com/v1/services/{settings.render_service_id}/deploys?limit=10",
                {"Authorization": f"Bearer {settings.render_api_key}", "Accept": "application/json"},
            )
            rows = payload if isinstance(payload, list) else payload.get("data", [])
            previous = state.get("render_status")
            current = dict(previous or {})
            for row in rows if isinstance(rows, list) else []:
                deploy = row.get("deploy", row) if isinstance(row, dict) else {}
                deploy_id = str(deploy.get("id") or "")
                if not deploy_id:
                    continue
                status = str(deploy.get("status") or deploy.get("state") or "unknown").lower()
                old_status = previous.get(deploy_id) if isinstance(previous, dict) else None
                if old_status is not None and old_status != status and status in {
                    "live", "deactivated", "build_failed", "update_failed", "canceled", "pre_deploy_failed"
                }:
                    commit = deploy.get("commit") or {}
                    events.append({
                        "source": "Render",
                        "status": status.upper(),
                        "title": f"Деплой {deploy_id[:12]}",
                        "detail": str(commit.get("message") or deploy.get("trigger") or "Изменение состояния деплоя"),
                        "url": str(deploy.get("dashboardUrl") or ""),
                    })
                current[deploy_id] = status
            state["render_status"] = current
        except Exception as exc:
            log.warning("Render event polling failed: %s", type(exc).__name__)

    if settings.github_token and settings.github_repository:
        try:
            payload = _get_json(
                f"https://api.github.com/repos/{settings.github_repository}/actions/runs?per_page=10",
                {"Authorization": f"Bearer {settings.github_token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            )
            rows = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
            previous = state.get("github_status")
            current = dict(previous or {})
            for run in rows if isinstance(rows, list) else []:
                run_id = str(run.get("id") or "")
                if not run_id:
                    continue
                status = str(run.get("status") or "").lower()
                conclusion = str(run.get("conclusion") or "").lower()
                old_status = previous.get(run_id) if isinstance(previous, dict) else None
                if old_status not in (None, "completed") and status == "completed":
                    events.append({
                        "source": "GitHub Actions",
                        "status": (conclusion or "completed").upper(),
                        "title": str(run.get("name") or "Workflow"),
                        "detail": f"Ветка: {run.get('head_branch') or '—'}; commit: {str(run.get('head_sha') or '')[:10]}",
                        "url": str(run.get('html_url') or ""),
                    })
                current[run_id] = status
            state["github_status"] = current
        except Exception as exc:
            log.warning("GitHub event polling failed: %s", type(exc).__name__)

    return events
