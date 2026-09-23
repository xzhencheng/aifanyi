from conftest import post


def new_conv(c):
    response = post(
        c, "/v1/agent/conversations", {"project_id": "project_default", "defaults": {"target_language": "en"}}
    )
    assert response.status_code == 201, response.text
    return response.json()


def chat(c, conv, key="message-one"):
    return post(
        c,
        "/v1/agent/messages",
        {
            "conversation_id": conv["conversation_id"],
            "expected_conversation_version": conv["version"],
            "client_message_id": key,
            "message": "设备处于待机状态。",
            "source_language": "zh-CN",
            "target_language": "en",
            "source_spans": [{"start": 0, "end": 9}],
        },
    )


def test_chat_translation_and_final_sse(env):
    c, runner, _, *_ = env
    conv = new_conv(c)
    accepted = chat(c, conv)
    assert accepted.status_code == 202, accepted.text
    run = accepted.json()
    runner.drain()
    result = c.get(run["run_url"]).json()
    assert result["status"] == "completed", result
    assert result["blocks"][0]["type"] == "translation"
    stream = c.get(run["events_url"])
    assert "event: translation.final" in stream.text
    assert "event: run.completed" in stream.text
    history = c.get(f"/v1/agent/conversations/{conv['conversation_id']}/messages").json()
    assert len(history["items"]) == 2
    assert history["active_run_id"] is None


def test_duplicate_message_and_busy_guard(env):
    c, runner, gateway, *_ = env
    conv = new_conv(c)
    first, second = chat(c, conv), chat(c, conv)
    assert first.json()["run_id"] == second.json()["run_id"]
    busy = chat(c, {**conv, "version": first.json()["conversation_version"]}, "other")
    assert busy.status_code == 409
    runner.drain()
    assert gateway.calls.count("translator") == 1


def test_missing_target_clarifies_without_job(env):
    c, runner, gateway, *_ = env
    conv = new_conv(c)
    run = post(
        c,
        "/v1/agent/messages",
        {
            "conversation_id": conv["conversation_id"],
            "expected_conversation_version": conv["version"],
            "client_message_id": "missing",
            "message": "翻译一下",
        },
    ).json()
    runner.drain()
    status = c.get(run["run_url"]).json()
    assert status["status"] == "awaiting_input", status
    assert status["job_id"] is None
    assert gateway.calls == ["conversation"]


def test_canceled_chat_does_not_reactivate(env):
    c, runner, gateway, *_ = env
    conv = new_conv(c)
    run = chat(c, conv).json()
    state = c.get(run["run_url"]).json()
    r = post(c, run["run_url"] + "/cancel", {"reason": "stop", "expected_run_version": state["version"]})
    assert r.status_code == 202, r.text
    runner.drain()
    assert c.get(run["run_url"]).json()["status"] == "canceled"
    assert gateway.calls == []


def test_files_not_accepted(env):
    c, *_ = env
    result = post(
        c,
        "/v1/translation-jobs",
        {"project_id": "project_default", "target_language": "en", "input": {"kind": "file", "file_id": "x"}},
    )
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "UNSUPPORTED_INPUT_KIND"
    assert c.post("/v1/files").status_code == 404


def test_cross_project_and_unauthenticated_are_blocked(env):
    c, *_ = env
    assert post(c, "/v1/agent/conversations", {"project_id": "other"}).status_code == 404
    c.headers.pop("Authorization")
    assert c.get("/v1/agent/runs/nonexistent").status_code == 401
