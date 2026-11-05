from types import SimpleNamespace

from src.trader.monitoring import poll_external_events


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_render_events_baseline_then_new_failure_is_reported_once(monkeypatch):
    settings = SimpleNamespace(
        render_api_key="test-key",
        render_service_id="srv-test",
        github_token="",
        github_repository="",
    )
    state = {}
    payloads = [
        [{"event": {"id": "evt-old", "type": "server_restarted", "details": {}}}],
        [
            {"event": {"id": "evt-new", "type": "server_failed", "details": {"message": "process exited"}}},
            {"event": {"id": "evt-build", "type": "build_ended", "details": {"buildStatus": "succeeded"}}},
            {"event": {"id": "evt-old", "type": "server_restarted", "details": {}}},
        ],
        [
            {"event": {"id": "evt-new", "type": "server_failed", "details": {"message": "process exited"}}},
            {"event": {"id": "evt-old", "type": "server_restarted", "details": {}}},
        ],
    ]

    def fake_get(*args, **kwargs):
        return Response(payloads.pop(0))

    monkeypatch.setattr("src.trader.monitoring.requests.get", fake_get)

    assert poll_external_events(settings, state) == []
    events = poll_external_events(settings, state)
    assert len(events) == 2
    failed = next(item for item in events if item["title"] == "SERVER FAILED")
    succeeded = next(item for item in events if item["title"] == "BUILD ENDED")
    assert failed["source"] == "Render"
    assert failed["status"] == "FAILED"
    assert "process exited" in failed["detail"]
    assert succeeded["status"] == "RECOVERED"
    assert poll_external_events(settings, state) == []


def test_missing_credentials_disables_external_polling():
    settings = SimpleNamespace(
        render_api_key="",
        render_service_id="",
        github_token="",
        github_repository="",
    )
    assert poll_external_events(settings, {}) == []

def test_public_github_monitor_reports_new_run_completed_between_polls(monkeypatch):
    settings = SimpleNamespace(
        render_api_key="",
        render_service_id="",
        github_token="",
        github_repository="alekseyvassilyew-wq/Crypto-AI-Trader-",
    )
    state = {}
    payloads = [
        {"workflow_runs": [
            {"id": 1, "status": "completed", "conclusion": "success",
             "created_at": "2026-11-04T10:00:00Z", "name": "CI",
             "head_branch": "main", "head_sha": "aaaa", "html_url": "https://example.test/1"}
        ]},
        {"workflow_runs": [
            {"id": 2, "status": "completed", "conclusion": "failure",
             "created_at": "2026-11-04T10:02:00Z", "name": "CI",
             "head_branch": "main", "head_sha": "bbbb", "html_url": "https://example.test/2"},
            {"id": 1, "status": "completed", "conclusion": "success",
             "created_at": "2026-11-04T10:00:00Z", "name": "CI",
             "head_branch": "main", "head_sha": "aaaa", "html_url": "https://example.test/1"},
        ]},
        {"workflow_runs": [
            {"id": 2, "status": "completed", "conclusion": "failure",
             "created_at": "2026-11-04T10:02:00Z", "name": "CI",
             "head_branch": "main", "head_sha": "bbbb", "html_url": "https://example.test/2"}
        ]},
    ]

    def fake_get(*args, **kwargs):
        return Response(payloads.pop(0))

    monkeypatch.setattr("src.trader.monitoring.requests.get", fake_get)

    assert poll_external_events(settings, state) == []
    events = poll_external_events(settings, state)
    assert len(events) == 1
    assert events[0]["source"] == "GitHub Actions"
    assert events[0]["status"] == "FAILURE"
    assert events[0]["url"] == "https://example.test/2"
    assert poll_external_events(settings, state) == []


def test_github_monitor_reports_run_that_transitions_to_completed(monkeypatch):
    settings = SimpleNamespace(
        render_api_key="",
        render_service_id="",
        github_token="",
        github_repository="owner/repo",
    )
    state = {}
    payloads = [
        {"workflow_runs": [
            {"id": 10, "status": "completed", "conclusion": "success",
             "created_at": "2026-11-04T10:00:00Z", "name": "CI"}
        ]},
        {"workflow_runs": [
            {"id": 11, "status": "in_progress", "conclusion": None,
             "created_at": "2026-11-04T10:01:00Z", "name": "CI"}
        ]},
        {"workflow_runs": [
            {"id": 11, "status": "completed", "conclusion": "success",
             "created_at": "2026-11-04T10:01:00Z", "name": "CI"}
        ]},
    ]

    def fake_get(*args, **kwargs):
        return Response(payloads.pop(0))

    monkeypatch.setattr("src.trader.monitoring.requests.get", fake_get)

    assert poll_external_events(settings, state) == []
    assert poll_external_events(settings, state) == []
    events = poll_external_events(settings, state)
    assert len(events) == 1
    assert events[0]["status"] == "SUCCESS"

def test_render_deploy_success_is_reported_as_recovery(monkeypatch):
    settings = SimpleNamespace(
        render_api_key="test-key",
        render_service_id="srv-test",
        github_token="",
        github_repository="",
    )
    state = {}
    payloads = [
        [{"event": {"id": "evt-old", "type": "server_restarted", "details": {}}}],
        [
            {"event": {"id": "evt-deploy", "type": "deploy_ended",
                       "details": {"deployStatus": "succeeded", "status": 2}}},
            {"event": {"id": "evt-old", "type": "server_restarted", "details": {}}},
        ],
    ]

    def fake_get(*args, **kwargs):
        return Response(payloads.pop(0))

    monkeypatch.setattr("src.trader.monitoring.requests.get", fake_get)

    assert poll_external_events(settings, state) == []
    events = poll_external_events(settings, state)
    assert len(events) == 1
    assert events[0]["title"] == "DEPLOY ENDED"
    assert events[0]["status"] == "RECOVERED"
    assert "succeeded" in events[0]["detail"]



def test_github_monitor_detects_new_run_with_same_timestamp_as_baseline(monkeypatch):
    settings = SimpleNamespace(
        render_api_key="",
        render_service_id="",
        github_token="",
        github_repository="owner/repo",
    )
    state = {}
    payloads = [
        {"workflow_runs": [
            {"id": 20, "status": "completed", "conclusion": "success",
             "created_at": "2026-11-04T10:00:00Z", "name": "CI"}
        ]},
        {"workflow_runs": [
            {"id": 21, "status": "completed", "conclusion": "failure",
             "created_at": "2026-11-04T10:00:00Z", "name": "CI"},
            {"id": 20, "status": "completed", "conclusion": "success",
             "created_at": "2026-11-04T10:00:00Z", "name": "CI"},
        ]},
    ]

    def fake_get(*args, **kwargs):
        return Response(payloads.pop(0))

    monkeypatch.setattr("src.trader.monitoring.requests.get", fake_get)

    assert poll_external_events(settings, state) == []
    events = poll_external_events(settings, state)
    assert len(events) == 1
    assert events[0]["status"] == "FAILURE"


from src.trader.monitoring import format_monitor_status


def test_monitor_status_reports_missing_configuration_without_secrets():
    settings = SimpleNamespace(
        render_api_key="",
        render_service_id="",
        github_repository="owner/repo",
        telegram_group_chat_id=0,
    )
    report = format_monitor_status(settings, {})
    assert "<b>RENDER API</b>" in report
    assert "⚪ <b>НЕ НАСТРОЕН</b>" in report
    assert "<b>GITHUB ACTIONS</b>" in report
    assert "🟢 <b>НАСТРОЕН</b>" in report
    assert "<b>Telegram-группа</b>" in report
    assert "⚪ <b>НЕ НАСТРОЕНА</b>" in report
    assert "API-ключи скрыты" in report


def test_monitor_status_reports_last_success_and_errors():
    settings = SimpleNamespace(
        render_api_key="secret-value",
        render_service_id="srv-test",
        github_repository="owner/repo",
        telegram_group_chat_id=-100123,
    )
    report = format_monitor_status(settings, {
        "render_event_ids": ["a", "b"],
        "render_last_error": "Timeout",
        "github_status": {"1": "completed"},
        "github_last_success_at": "2026-11-04T14:00:00+00:00",
        "last_poll_at": "2026-11-04T14:01:00+00:00",
        "last_events_count": 2,
        "last_alert_delivery_at": "2026-11-04T14:01:00+00:00",
    })
    assert "сохранено событий — 2" in report
    assert "Timeout" in report
    assert "отслеживается запусков — 1" in report
    assert "🆕 Новых событий: <b>2</b>" in report
    assert "Последняя подтверждённая отправка Telegram" in report
    assert "secret-value" not in report



def test_health_state_persists_without_advancing_failed_delivery_cursors():
    from src.trader.monitoring import merge_monitor_health_state

    persisted = {
        "render_event_ids": ["old-render"],
        "github_status": {"1": "in_progress"},
        "last_poll_at": "old-poll",
        "last_alert_delivery_at": "old-delivery",
        "render_last_error": "Timeout",
    }
    observed = {
        "render_event_ids": ["new-render", "old-render"],
        "github_status": {"2": "completed"},
        "render_last_success_at": "2026-11-04T15:00:00+00:00",
        "github_last_success_at": "2026-11-04T15:00:01+00:00",
    }

    merged = merge_monitor_health_state(persisted, observed)

    assert merged["render_last_success_at"] == observed["render_last_success_at"]
    assert merged["github_last_success_at"] == observed["github_last_success_at"]
    assert "render_last_error" not in merged
    assert merged["render_event_ids"] == ["old-render"]
    assert merged["github_status"] == {"1": "in_progress"}
    assert merged["last_poll_at"] == "old-poll"
    assert merged["last_alert_delivery_at"] == "old-delivery"


def test_health_state_keeps_api_error_and_previous_success():
    from src.trader.monitoring import merge_monitor_health_state

    persisted = {"github_last_success_at": "yesterday"}
    observed = {"github_last_error": "Timeout"}
    merged = merge_monitor_health_state(persisted, observed)

    assert merged["github_last_success_at"] == "yesterday"
    assert merged["github_last_error"] == "Timeout"



def test_monitor_status_shows_last_success_and_latest_api_error_together():
    settings = SimpleNamespace(
        render_api_key="key",
        render_service_id="service",
        github_repository="owner/repo",
        telegram_group_chat_id=-100123,
    )
    report = format_monitor_status(settings, {
        "render_last_success_at": "2026-11-04T15:00:00+00:00",
        "render_last_error": "Timeout",
        "github_last_success_at": "2026-11-04T15:00:01+00:00",
        "last_alert_delivery_at": "2026-11-04T15:00:02+00:00",
    })

    assert "Render API: НАСТРОЕН" in report
    assert "🕒 Последняя успешная проверка: <code>2026-11-04T15:00:00+00:00</code>" in report
    assert "Последняя ошибка API: <code>Timeout</code>" in report
    assert "🕒 Последняя успешная проверка: <code>2026-11-04T15:00:01+00:00</code>" in report
    assert "Последняя подтверждённая отправка: <code>2026-11-04T15:00:02+00:00</code>" in report



def test_backup_health_transition_alerts_only_on_changes():
    from src.trader.monitoring import backup_health_transition

    assert backup_health_transition(None, "fresh") is None
    assert backup_health_transition("fresh", "fresh") is None
    assert backup_health_transition("fresh", "stale") == "failure"
    assert backup_health_transition("stale", "stale") is None
    assert backup_health_transition("stale", "corrupt") == "failure"
    assert backup_health_transition("corrupt", "fresh") == "recovered"
    assert backup_health_transition(None, "missing") == "failure"



def test_monitor_status_escapes_api_error_and_shows_dashboard_sections():
    settings = SimpleNamespace(
        render_api_key="secret",
        render_service_id="service",
        github_repository="owner/repo",
        telegram_group_chat_id=-100123,
    )
    report = format_monitor_status(settings, {
        "render_last_error": "<bad & unsafe>",
        "render_event_ids": ["1", "2", "3"],
        "github_status": {"1": "completed"},
        "last_poll_at": "2026-11-05T13:00:00+00:00",
        "last_events_count": 3,
    })
    assert "🛰 <b>МОНИТОРИНГ ВНЕШНИХ СЕРВИСОВ</b>" in report
    assert "🟠 <b>ТРЕБУЕТСЯ ВНИМАНИЕ</b>" in report
    assert "Сохранено событий: <b>3</b>" in report
    assert "Отслеживается запусков: <b>1</b>" in report
    assert "&lt;bad &amp; unsafe&gt;" in report
    assert "<bad & unsafe>" not in report
    assert "secret" not in report
