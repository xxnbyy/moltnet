import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server import Adapter, MAX_LINE, TOOLS, handle, serve


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / ".moltnet").mkdir()
        (root / ".moltnet/config.json").write_text("{}")
        cli = root / "cli"
        cli.touch()
        self.adapter = Adapter(cli, root, "local", "doubao", "general")

    def test_scoped_read_uses_argument_array(self):
        with patch("server.subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout = 0, '{"messages": []}'
            self.adapter.call("moltnet_read", {"limit": 12})
            argv = run.call_args.args[0]
            self.assertIn("room:general", argv)
            self.assertIn("doubao", argv)
            self.assertEqual(argv[-2:], ["--limit", "12"])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_invalid_tool_arguments_never_run(self):
        for name, args in [("exec", {}), ("moltnet_read", {"room": "other"}),
                           ("moltnet_read", {"limit": True}), ("moltnet_read", {"limit": 101}),
                           ("moltnet_send", {"text": " "}), ("moltnet_send", {"text": "ok", "reply_to": "x;cmd"})]:
            with self.subTest(name=name, args=args), patch.object(self.adapter, "command") as run:
                with self.assertRaises(ValueError):
                    self.adapter.call(name, args)
                run.assert_not_called()

    def test_send_file_keeps_unicode_and_metadata_then_cleans_up(self):
        text = '豆包说："hello world"\n第二行；$HOME；' + chr(96) + 'literal'
        state = {"id": "discussion_1", "active": True, "expected_agent": "doubao", "expected_reply_to": "msg_1"}
        path = None

        def command(action, extra=()):
            nonlocal path
            if action == "discussion":
                return {"discussion": state}
            self.assertEqual(action, "send")
            self.assertEqual(extra[:4], ["--reply-to", "msg_1", "--discussion", "discussion_1"])
            path = extra[-1]
            self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
            if os.name != "nt":
                self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            return {"accepted": True, "message_id": "msg_2"}

        with patch.object(self.adapter, "command", side_effect=command):
            result = self.adapter.call("moltnet_send", {"text": text, "reply_to": "msg_1", "discussion_id": "discussion_1"})
        self.assertTrue(result["accepted"])
        self.assertFalse(Path(path).exists())

    def test_stopped_wrong_turn_and_missing_metadata_block_send(self):
        state = {"id": "discussion_1", "active": True, "expected_agent": "doubao", "expected_reply_to": "msg_1"}
        for update, args in [({"active": False}, {"discussion_id": "discussion_1", "reply_to": "msg_1"}),
                             ({"expected_agent": "grok"}, {"discussion_id": "discussion_1", "reply_to": "msg_1"}),
                             ({}, {}), ({}, {"discussion_id": "discussion_1", "reply_to": "wrong"})]:
            with self.subTest(update=update, args=args):
                with patch.object(self.adapter, "command", return_value={"discussion": state | update}) as run:
                    with self.assertRaises(ValueError):
                        self.adapter.call("moltnet_send", {"text": "reply", **args})
                    self.assertEqual(run.call_count, 1)

    def test_process_error_does_not_disclose_config_or_token(self):
        with patch("server.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "TOKEN=do-not-disclose"
            with self.assertRaisesRegex(RuntimeError, "command failed") as error:
                self.adapter.call("moltnet_read", {})
            self.assertNotIn("do-not-disclose", str(error.exception))


class ProtocolTests(unittest.TestCase):
    def test_stdio_handshake_tools_notifications_and_invalid_json(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "unknown"},
        ]
        source = io.StringIO("\n".join(map(json.dumps, messages)) + "\ninvalid\n")
        output = io.StringIO()
        serve(None, source, output)
        results = list(map(json.loads, output.getvalue().splitlines()))
        self.assertEqual(len(results), 5)
        self.assertEqual(results[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(results[1]["result"]["tools"], TOOLS)
        self.assertEqual(results[3]["error"]["code"], -32601)
        self.assertEqual(results[4]["error"]["code"], -32700)

    def test_oversized_frame_and_malformed_requests(self):
        output = io.StringIO()
        serve(None, io.StringIO("a" * (MAX_LINE + 1)), output)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(handle([], None)["error"]["code"], -32600)
        self.assertEqual(handle({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, None)["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
