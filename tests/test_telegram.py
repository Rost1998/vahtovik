"""The telegram channel type's scripts against a local stand-in for the Bot API."""

import json
import os
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills/channels/telegram"


class Api(BaseHTTPRequestHandler):
    updates: list = []
    requests: list = []

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        Api.requests.append({"path": url.path, **query})
        offset = int(query.get("offset", 0))
        self._answer({"ok": True, "result": [u for u in Api.updates if u["update_id"] >= offset]})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        Api.requests.append({"path": self.path, **dict(urllib.parse.parse_qsl(body))})
        self._answer({"ok": True, "result": {"message_id": 99}})

    def _answer(self, data):
        payload = json.dumps(data, separators=(",", ":")).encode()  # as Telegram sends it
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def api(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Api.updates, Api.requests = [], []
    env = {**os.environ, "TELEGRAM_API_URL": f"http://127.0.0.1:{server.server_port}", "TELEGRAM_BOT_TOKEN": "T0K",
           "TELEGRAM_CHAT_ID": "42", "VAHTOVIK_STATE_DIR": str(tmp_path / "state")}
    yield env
    server.shutdown()


def _message(update_id: int, chat: int, text: str | None) -> dict:
    message = {"message_id": update_id * 10, "chat": {"id": chat}, "date": 1700000000,
               "from": {"username": "rost"}}
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def _receive(env) -> dict:
    proc = subprocess.run([str(SKILL / "receive")], env=env, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_receive_lists_new_text_messages_of_the_chat_once(api):
    Api.updates = [_message(1, 42, "Hello"), _message(2, 7, "Other chat"), _message(3, 42, None)]
    first = _receive(api)
    assert first == {"messages": [{"id": "10", "text": "Hello", "from": "rost", "time": "2023-11-14T22:13:20+00:00"}],
                     "error": None}
    Api.updates.append(_message(4, 42, "Again"))
    assert [m["text"] for m in _receive(api)["messages"]] == ["Again"]
    assert [r["offset"] for r in Api.requests] == ["0", "4"]


def test_receive_reports_a_missing_setting(api):
    del api["TELEGRAM_CHAT_ID"]
    assert _receive(api)["error"] == "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set"


def test_send_replies_to_the_message_it_answers(api):
    def send(extra):
        proc = subprocess.run([str(SKILL / "send")], input="Body", env={**api, **extra}, capture_output=True,
                              text=True)
        assert proc.returncode == 0, proc.stderr
        return Api.requests[-1]
    assert "reply_parameters" not in send({})
    request = send({"VAHTOVIK_REPLY_TO": "10", "VAHTOVIK_SUBJECT": "Re"})
    assert json.loads(request["reply_parameters"]) == {"message_id": 10, "allow_sending_without_reply": True}
    assert request["chat_id"] == "42" and request["text"] == "<b>Re</b>\nBody"
