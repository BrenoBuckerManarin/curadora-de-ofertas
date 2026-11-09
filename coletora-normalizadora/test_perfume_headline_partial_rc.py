import sys, json, unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, "/home/ubuntu/.hermes/scripts")
import perfume_manual_normalizer as n

GOOD = '{"options":["A UM","B DOIS","C TRES","D QUATRO","E CINCO"],"headline":"A UM","reason":"x"}'
JUDGE = '{"escolha": 2, "motivo": "ok"}'


def run_with(stdout, rc, accept="1", chain=(("openai-codex", "gpt-5.6-luna"), ("anthropic", "claude-haiku-4-5-20251001"))):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd[cmd.index("--provider") + 1])
        if len(calls) == 1:
            return SimpleNamespace(stdout=stdout, stderr="session_id: x\n", returncode=rc)
        return SimpleNamespace(stdout=GOOD, stderr="", returncode=0)
    cfg = {"PERFUME_HEADLINE_DIRECT_HTTP": "0", "PERFUME_HEADLINE_TIMEOUT": "90",
           "PERFUME_HEADLINE_ACCEPT_PARTIAL_JSON": accept}
    with patch.object(n, "config_value", side_effect=lambda k, d="": cfg.get(k, d)), \
         patch.object(n, "_headline_llm_chain", return_value=list(chain)), \
         patch.object(n, "remaining_headline_seconds", side_effect=lambda x: x), \
         patch.object(n.subprocess, "run", side_effect=fake_run):
        n._HEADLINE_DIAGNOSTICS.attempts = []
        out, err = n.run_headline_llm("p")
    return out, err, calls, n.LAST_HEADLINE_MODEL, n._HEADLINE_DIAGNOSTICS.attempts[-1]["status"]


class T(unittest.TestCase):
    def test_rc1_valid_json_is_accepted_from_primary(self):
        out, err, calls, model, status = run_with(GOOD, 1)
        self.assertEqual(calls, ["openai-codex"]); self.assertEqual(out, GOOD)
        self.assertEqual(model, "openai-codex/gpt-5.6-luna"); self.assertEqual(status, "response_partial_rc")

    def test_rc1_judge_json_is_accepted(self):
        out, _, calls, _, _ = run_with(JUDGE, 1)
        self.assertEqual(calls, ["openai-codex"]); self.assertEqual(out, JUDGE)

    def test_rc1_garbage_falls_back(self):
        out, _, calls, model, status = run_with("Reached maximum iterations", 1)
        self.assertEqual(calls, ["openai-codex", "anthropic"]); self.assertTrue(model.startswith("anthropic/"))
        self.assertEqual(status, "response")

    def test_rc1_too_few_options_falls_back(self):
        _, _, calls, _, _ = run_with('{"options":["SO UMA"]}', 1)
        self.assertEqual(calls, ["openai-codex", "anthropic"])

    def test_http_error_text_falls_back(self):
        _, _, calls, _, _ = run_with('Error: HTTP 401 {"options":["a","b","c"]}', 1)
        self.assertEqual(calls, ["openai-codex", "anthropic"])

    def test_rc0_unchanged(self):
        out, _, calls, _, status = run_with(GOOD, 0)
        self.assertEqual(calls, ["openai-codex"]); self.assertEqual(status, "response")

    def test_kill_switch_restores_strict(self):
        _, _, calls, _, _ = run_with(GOOD, 1, accept="0")
        self.assertEqual(calls, ["openai-codex", "anthropic"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
