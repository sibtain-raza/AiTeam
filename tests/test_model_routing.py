"""Unit tests for per-role model routing (LOOPER_CODE_MODEL_<AGENT_NAME>).

Pure constructor logic — no SDK. Precedence: per-role env var > global
LOOPER_CODE_MODEL > DEFAULT_MODEL.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from looper.claude_code_agent import DEFAULT_MODEL, ClaudeCodeAgent


def make_agent(name: str, cwd: Path) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        name=name, description="test", system_prompt="test", cwd=cwd, max_turns=3
    )


class ModelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cwd = Path(tempfile.mkdtemp(prefix="looper_model_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.cwd, ignore_errors=True)

    def _clean_env(self) -> dict:
        return {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("LOOPER_CODE_MODEL")
        }

    def test_default_when_nothing_set(self) -> None:
        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            self.assertEqual(make_agent("qa_engineer", self.cwd)._model, DEFAULT_MODEL)

    def test_global_override(self) -> None:
        env = {**self._clean_env(), "LOOPER_CODE_MODEL": "opus"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(make_agent("qa_engineer", self.cwd)._model, "opus")

    def test_per_role_override_beats_global(self) -> None:
        env = {
            **self._clean_env(),
            "LOOPER_CODE_MODEL": "sonnet",
            "LOOPER_CODE_MODEL_RELEASE_REPORTER": "haiku",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(make_agent("release_reporter", self.cwd)._model, "haiku")
            # ...and only that role — others keep the global.
            self.assertEqual(make_agent("qa_engineer", self.cwd)._model, "sonnet")


if __name__ == "__main__":
    unittest.main()
