"""Optional Render and GitHub event polling for Telegram notifications."""
from __future__ import annotations

import json
import logging
import requests

log = logging.getLogger(__name__)
TIMEOUT = (4, 10)

RENDER_FAILURE_EVENTS = {
    "server_failed", "server_hardware_failure", "service_suspended",
    "service_disk_usage_high", "image_pull_failed", "artifact_fetch_failed",
}
RENDER_RECOVERY_EVENTS = {
    "server_restarted", "server_available", "service_resumed",
    "service_disk_usage_recovered",
}
RENDER_TERMINAL_EVENTS = {"build_ended", "deploy_ended", "initial_deploy_hook_ended"}


def _get_json(url: str, headers: dict[str, str], params=None):
    response = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def _render_detail(details) -> str:
    if isinstance(details, str):
        return details[:900]
    if isinstance(details, dict):
        # Keep alerts useful without dumping large or sensitive response bodies.
        preferred = ("message", "reason", "deployStatus", "buildStatus", "status", "exitCode")
        parts = [f"{key}: {details[key]}" for key in preferred if details.get(key) is not None]
        if not parts:
            parts = [json.dumps(details, ensure_ascii=False, default=str)[:900]]
        return "; ".join(parts)[:900]
    return str(details or "Render service event")[:900]


def format_monitor_status(settings, state: dict) -> str:
    """Build a safe owner-facing health summary without exposing credentials."""
    render_ready = bool(getattr(settings, "render_api_key", "") and getattr(settings, "render_service_id", ""))
    github_ready = bool(getattr(settings, "github_repository", ""))
    telegram_ready = bool(getattr(settings, "telegram_group_chat_id", 0))
    lines = [
        "🛰 МОНИТОРИНГ ВНЕШНИХ СЕРВИСОВ",
        f"Render API: {'ПОДКЛЮЧЁН' if render_ready else 'НЕ НАСТРОЕН'}",
        f"GitHub Actions: {'ПОДКЛЮЧЁН' if github_ready else 'НЕ НАСТРОЕН'}",
        f"Telegram-группа: {'ПОДКЛЮЧЕНА' if telegram_ready else 'НЕ НАСТРОЕНА'}",
    ]
    if render_ready:
        saved = state.get("render_event_ids")
        lines.append(f"Render: сохранено событий — {len(saved) if isinstance(saved, list) else 0}")
        if state.get("render_last_error"):
            lines.append(f"Последняя ошибка Render API: {state['render_last_error']}")
        elif state.get("render_last_success_at"):
            lines.append(f"Последняя успешная проверка Render: {state['render_last_success_at']}")
        else:
            lines.append("Последняя успешная проверка Render: ещё не зафиксирована")
    if github_ready:
        tracked = state.get("github_status")
        lines.append(f"GitHub: отслеживается запусков — {len(tracked) if isinstance(tracked, dict) else 0}")
        if state.get("github_last_error"):
            lines.append(f"Последняя ошибка GitHub API: {state['github_last_error']}")
        elif state.get("github_last_success_at"):
            lines.append(f"Последняя успешная проверка GitHub: {state['github_last_success_at']}")
        else:
            lines.append("Последняя успешная проверка GitHub: ещё не зафиксирована")
    if state.get("last_poll_at"):
        lines.append(f"Последний завершённый цикл мониторинга: {state['last_poll_at']}")
        lines.append(f"Новых событий в последнем цикле: {int(state.get('last_events_count') or 0)}")
    if state.get("last_alert_delivery_at"):
        lines.append(f"Последняя отправка уведомления: {state['last_alert_delivery_at']}")
    lines.append("API-ключи не отображаются. Доставка считается успешной только после ответа Telegram.")
    return "\n".join(lines)


def poll_external_events(settings, state: dict) -> list[dict[str, str]]:
    """Return newly observed events; caller persists state to SQLite."""
    events: list[dict[str, str]] = []

    if settings.render_api_key and settings.render_service_id:
        try:
            payload = _get_json(
                f"https://api.render.com/v1/services/{settings.render_service_id}/events",
                {"Authorization": f"Bearer {settings.render_api_key}", "Accept": "application/json"},
                params={"limit": 100},
            )
            rows = payload if isinstance(payload, list) else []
            seen = state.get("render_event_ids")
            known = set(seen if isinstance(seen, list) else [])
            current = []
            for row in rows:
                event = row.get("event", row) if isinstance(row, dict) else {}
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("id") or "")
                if not event_id:
                    continue
                current.append(event_id)
                event_type = str(event.get("type") or "").lower()
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                is_terminal = event_type in RENDER_TERMINAL_EVENTS
                if seen is not None and event_id not in known and (
                    event_type in RENDER_FAILURE_EVENTS | RENDER_RECOVERY_EVENTS or is_terminal
                ):
                    if is_terminal:
                        result = str(details.get("buildStatus") or details.get("status") or "").lower()
                        failed = result not in {"succeeded", "success", "2"}
                    else:
                        failed = event_type in RENDER_FAILURE_EVENTS
                    events.append({
                        "source": "Render",
                        "status": "FAILED" if failed else "RECOVERED",
                        "title": event_type.replace("_", " ").upper(),
                        "detail": _render_detail(details),
                        "url": "",
                    })
            # First poll establishes a baseline and avoids replaying old history.
            # Persist a bounded window so restarts do not duplicate notifications.
            state["render_event_ids"] = list(dict.fromkeys(current + (seen or [])))[:300]
            from datetime import datetime, timezone
            state["render_last_success_at"] = datetime.now(timezone.utc).isoformat()
            state.pop("render_last_error", None)
        except Exception as exc:
            state["render_last_error"] = type(exc).__name__
            log.warning("Render event polling failed: %s", type(exc).__name__)

    if settings.github_repository:
        try:
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if settings.github_token:
                headers["Authorization"] = "Bearer " + settings.github_token
            payload = _get_json(
                "https://api.github.com/repos/" + settings.github_repository + "/actions/runs",
                headers, params={"per_page": 20},
            )
            rows = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
            previous = state.get("github_status")
            current = {}
            baseline = state.get("github_baseline_created_at")
            latest_created_at = baseline
            for run in rows if isinstance(rows, list) else []:
                run_id = str(run.get("id") or "")
                if not run_id:
                    continue
                status = str(run.get("status") or "").lower()
                conclusion = str(run.get("conclusion") or "").lower()
                created_at = str(run.get("created_at") or "")
                old_status = previous.get(run_id) if isinstance(previous, dict) else None
                is_new_after_baseline = bool(baseline and created_at and created_at >= baseline)
                completed_now = status == "completed" and (
                    old_status not in (None, "completed") or
                    (old_status is None and is_new_after_baseline)
                )
                if completed_now:
                    detail = "Branch: {}; commit: {}".format(run.get("head_branch") or "—", str(run.get("head_sha") or "")[:10])
                    events.append({
                        "source": "GitHub Actions",
                        "status": (conclusion or "completed").upper(),
                        "title": str(run.get("name") or "Workflow"),
                        "detail": detail,
                        "url": str(run.get("html_url") or ""),
                    })
                current[run_id] = status
                if created_at and (latest_created_at is None or created_at > latest_created_at):
                    latest_created_at = created_at
            state["github_baseline_created_at"] = latest_created_at
            state["github_status"] = current
            from datetime import datetime, timezone
            state["github_last_success_at"] = datetime.now(timezone.utc).isoformat()
            state.pop("github_last_error", None)
        except Exception as exc:
            state["github_last_error"] = type(exc).__name__
            log.warning("GitHub event polling failed: %s", type(exc).__name__)
    return events
