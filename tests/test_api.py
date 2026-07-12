from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_chat_history_requires_admin_token() -> None:
    client = TestClient(app)
    session = client.post("/chat/sessions").json()

    response = client.get(f"/chat/sessions/{session['session_id']}")

    assert response.status_code == 401


def test_unknown_chat_session_is_rejected_before_agent_call() -> None:
    client = TestClient(app)

    response = client.post("/chat/sessions/unknown/messages", json={"message": "hello"})

    assert response.status_code == 404


def test_chat_message_length_is_limited() -> None:
    client = TestClient(app)
    session = client.post("/chat/sessions").json()

    response = client.post(
        f"/chat/sessions/{session['session_id']}/messages",
        json={"message": "x" * 4001},
    )

    assert response.status_code == 422
