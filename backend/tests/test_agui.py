from __future__ import annotations

from tests.helpers import createTestClient


class FakeAgUiAgentService:
    async def run(self, request):
        yield {"type": "RUN_STARTED", "threadId": request.threadId, "runId": request.runId}
        yield {"type": "STEP_STARTED", "stepName": "deep_thinking"}
        yield {"type": "TOOL_CALL_START", "toolCallId": "tool-1", "toolCallName": "search_content"}
        yield {"type": "TOOL_CALL_ARGS", "toolCallId": "tool-1", "delta": "{\"query\":\"香薰机 电商图\"}"}
        yield {"type": "TOOL_CALL_END", "toolCallId": "tool-1"}
        yield {
            "type": "TOOL_CALL_RESULT",
            "messageId": "msg-tool",
            "toolCallId": "tool-1",
            "content": "{\"status\":\"success\",\"results\":[]}",
            "role": "tool",
        }
        yield {"type": "RUN_FINISHED", "threadId": request.threadId, "runId": request.runId, "result": {"status": "ok"}}


def testAgUiRunRouteStreamsSseEvents() -> None:
    with createTestClient() as client:
        client.app.state.agUiAgentService = FakeAgUiAgentService()
        response = client.post(
            "/api/v1/agui/run",
            json={
                "threadId": "thread-1",
                "runId": "run-1",
                "messages": [{"id": "msg-1", "role": "user", "content": "生成香薰机电商图"}],
                "state": {},
                "tools": [],
                "context": [],
                "forwardedProps": {},
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "RUN_STARTED" in response.text
    assert "TOOL_CALL_RESULT" in response.text
