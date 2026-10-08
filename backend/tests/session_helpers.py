"""Helpers for explicit two-phase lesson progression in API tests."""


def confirm_advance(client, session_id):
    prepared = client.post(f"/api/sessions/{session_id}/advance/prepare")
    assert prepared.status_code == 200, prepared.text
    response = client.post(f"/api/sessions/{session_id}/advance/confirm", json={
        "source_index": prepared.json()["source_index"],
        "source_fen": prepared.json()["source_fen"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def confirm_reveal(client, session_id):
    preview = client.post(f"/api/sessions/{session_id}/reveal/prepare")
    assert preview.status_code == 200, preview.text
    response = client.post(f"/api/sessions/{session_id}/reveal/confirm",
                           json={"expected_fen": preview.json()["fen"]})
    assert response.status_code == 200, response.text
    return response.json()
