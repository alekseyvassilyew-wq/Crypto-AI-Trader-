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
