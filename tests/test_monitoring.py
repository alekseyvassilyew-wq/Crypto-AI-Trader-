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
