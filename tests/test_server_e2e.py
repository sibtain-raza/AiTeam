"""End-to-end test of the web UI backend (server/) against the scripted mock
agent, so it costs no real Claude Code quota and runs instantly like the
rest of tests/. Exercises the same signup -> create run -> live SSE stream
-> completion path a real browser takes.

Regression coverage: this is what caught a genuine bug where
GET /runs/{id}/events took its DB history snapshot *before* subscribing to
the live event broker, silently dropping any event published in that gap
(observed live: product_manager's events vanished because the scripted
agent completed its turn faster than the network round-trip to open the
SSE connection). See server/routes/runs.py for the fix (subscribe first,
dedupe by seq).
"""

import json
import os
import tempfile
import unittest

# A plain sqlite:///:memory: URL gives each connection its own separate
# in-memory database under SQLAlchemy's default pooling, so tables created
# by one connection are invisible to the next — use a throwaway file
# instead, matching how a real run's DB is a single shared file.
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.setdefault("AITEAM_DB_URL", f"sqlite:///{_TMP_DB.name}")

import httpx

from server.app import app
from server.db import Base, engine, init_db
import server.routes.runs as runs_module
from tests.mock_agent import ScriptedClaudeCodeAgent, set_script

BASE_SCRIPT = {
    "product_manager": ["# PRD\nDone.\n"],
    "solution_architect": ["# TECH DESIGN\n## Task Breakdown\nT-1 [FE]\nT-2 [BE]\nT-3 [OPS]\n"],
    "frontend_engineer": ["# FE IMPLEMENTATION\nDone.\n"],
    "backend_engineer": ["# BE IMPLEMENTATION\nDone.\n"],
    "devops_engineer": ["# OPS IMPLEMENTATION\nDone.\n"],
    "qa_engineer": ["# QA REPORT\n## Verdict\nAll good.\nQA_PASS"],
    "uat_reviewer": ["# UAT REPORT\n## Final Summary\nShipped.\nUAT_APPROVED"],
    "release_reporter": ["# FINAL DELIVERY REPORT\nDone.\n"],
}


class ServerE2ETest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # In-memory SQLite is fresh per engine; the module-level `engine` is
        # shared across tests, so wipe and recreate tables around each test
        # rather than relying on process startup (lifespan isn't run here).
        Base.metadata.drop_all(bind=engine)
        init_db()

        set_script(BASE_SCRIPT)
        self._orig_run_pipeline = runs_module.run_pipeline

        async def scripted_run_pipeline(run_id: str, goal: str):
            return await self._orig_run_pipeline(run_id, goal, agent_cls=ScriptedClaudeCodeAgent)

        runs_module.run_pipeline = scripted_run_pipeline

        transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self) -> None:
        runs_module.run_pipeline = self._orig_run_pipeline
        await self.client.aclose()

    async def test_signup_run_and_stream_events(self) -> None:
        r = await self.client.post(
            "/auth/signup", json={"email": "e2e@test.local", "password": "hunter22longenough"}
        )
        self.assertEqual(r.status_code, 201, r.text)
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = await self.client.post(
            "/auth/signup", json={"email": "e2e@test.local", "password": "hunter22longenough"}
        )
        self.assertEqual(r.status_code, 409)

        r = await self.client.get("/runs")
        self.assertIn(r.status_code, (401, 403))

        r = await self.client.post("/runs", json={"goal": "Build a thing."}, headers=headers)
        self.assertEqual(r.status_code, 201, r.text)
        run_id = r.json()["id"]

        r = await self.client.get("/runs", headers=headers)
        self.assertTrue(any(x["id"] == run_id for x in r.json()))

        seen_types: list[str] = []
        seen_sources: list[str] = []
        seen_seqs: list[int] = []
        async with self.client.stream(
            "GET", f"/runs/{run_id}/events", params={"token": token}, timeout=30
        ) as resp:
            self.assertEqual(resp.status_code, 200)
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    if payload:
                        seen_types.append(payload["event_type"])
                        seen_sources.append(payload["source"])
                        seen_seqs.append(payload["seq"])
                if line.startswith("event: end"):
                    break

        self.assertIn("run_completed", seen_types)
        self.assertIn("turn_started", seen_types)
        self.assertIn("product_manager", seen_sources, "regression: PM events dropped by history/subscribe race")
        self.assertIn("release_reporter", seen_sources)
        self.assertEqual(seen_seqs, sorted(set(seen_seqs)), "events must be gap-free, in order, and duplicate-free")

        r = await self.client.get(f"/runs/{run_id}", headers=headers)
        self.assertEqual(r.json()["status"], "completed")

        # Reconnecting after completion must replay history and end, not hang.
        async with self.client.stream(
            "GET", f"/runs/{run_id}/events", params={"token": token}, timeout=10
        ) as resp:
            lines = [line async for line in resp.aiter_lines()]
        self.assertTrue(any(line.startswith("event: end") for line in lines))


if __name__ == "__main__":
    unittest.main()
