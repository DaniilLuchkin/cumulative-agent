import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from cumulative_agent.api import create_app
from cumulative_agent.config import Settings, load_settings
from cumulative_agent.ownership import claim_telegram_owner, telegram_owner_ids
from cumulative_agent.repository import Repository
from cumulative_agent.service import Service
from test_miniapp import BOT_TOKEN, signed_data


def update(owner=123, text="/start", kind="private", **sender):
    return {"update_id": owner, "message": {"from": {"id": owner, **sender}, "chat": {"id": owner, "type": kind}, "text": text}}


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(database=":memory:", api_token="synthetic-browser-token-at-least-24", telegram_bot_token=BOT_TOKEN, telegram_claim_first_user=True)
        self.service = Service(self.settings, providers={})
        self.sent = []
        async def call(method, payload):
            self.sent.append((method, payload))
            return True
        self.service.telegram.call = call

    async def asyncTearDown(self):
        await self.service.stop()

    async def test_first_private_text_claims_and_second_user_cannot_act_or_receive(self):
        self.assertTrue(await self.service.telegram.handle(update()))
        self.assertEqual(telegram_owner_ids(self.service.repo, self.settings), {123})
        self.assertIn("Вы назначены владельцем", self.sent[0][1]["text"])
        self.assertFalse(await self.service.telegram.handle(update(999, "/pause")))
        self.assertFalse(self.service.repo.get_setting("paused", False))
        with self.assertRaises(ValueError):
            await self.service.telegram.send(999, "no")
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.service.resources.snapshot()["credential_readiness"]["telegram"])

    async def test_bot_group_nontext_invalid_id_and_empty_text_cannot_claim(self):
        for item in (update(kind="group"), update(is_bot=True), update(text="  "), update(text=None), update(owner=True), update(owner=-1)):
            self.assertFalse(await self.service.telegram.handle(item))
        self.assertEqual(telegram_owner_ids(self.service.repo, self.settings), set())
        self.assertTrue(self.service.resources.snapshot()["telegram_ownership"]["awaiting_first_user"])

    async def test_bootstrap_is_opt_in_and_explicit_allowlist_overrides_claim(self):
        self.settings.telegram_claim_first_user = False
        self.assertFalse(await self.service.telegram.handle(update()))
        self.settings.telegram_claim_first_user = True
        await self.service.telegram.handle(update())
        self.settings.telegram_owner_ids = [456]
        self.assertFalse(await self.service.telegram.handle(update()))
        self.assertTrue(await self.service.telegram.handle(update(456)))
        self.assertEqual(self.service.repo.get_setting("telegram_claimed_owner"), 123)

    async def test_polling_starts_without_preconfigured_owner_in_claim_mode(self):
        self.service.telegram.start()
        self.assertTrue(self.service.telegram.enabled)
        # Stop before the zero-delay timer can launch mocked polling.
        await self.service.telegram.stop()

    async def test_api_never_claims_but_accepts_the_persisted_chat_owner(self):
        app = create_app(self.settings, service=self.service, start_worker=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                headers = {"Authorization": "Telegram " + signed_data()}
                self.assertEqual((await client.get("/api/auth/me", headers=headers)).status_code, 403)
                self.assertEqual(telegram_owner_ids(self.service.repo, self.settings), set())
                await self.service.telegram.handle(update())
                response = await client.get("/api/auth/me", headers=headers)
                self.assertEqual(response.json(), {"method": "telegram", "owner_id": 123})
                self.assertEqual((await client.get("/api/resources", headers={"Authorization": "Telegram " + signed_data(owner=999)})).status_code, 403)


class OwnershipPersistenceTests(unittest.TestCase):
    def test_claim_survives_reopen_and_disabled_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "agent.sqlite3")
            repo = Repository(path)
            settings = Settings(telegram_claim_first_user=True)
            self.assertTrue(claim_telegram_owner(repo, settings, 123))
            repo.close()
            repo = Repository(path)
            self.assertEqual(telegram_owner_ids(repo, Settings()), {123})
            self.assertFalse(claim_telegram_owner(repo, settings, 999))
            self.assertEqual(len(repo.all("SELECT * FROM system_events WHERE kind='telegram_owner_claimed'")), 1)
            repo.close()

    def test_two_connections_claim_exactly_one_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "agent.sqlite3")
            repos = [Repository(path), Repository(path)]
            barrier = threading.Barrier(2)
            results = []
            def claim(index):
                barrier.wait()
                results.append(claim_telegram_owner(repos[index], Settings(telegram_claim_first_user=True), 123 + index))
            threads = [threading.Thread(target=claim, args=(i,)) for i in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(telegram_owner_ids(repos[0], Settings()), telegram_owner_ids(repos[1], Settings()))
            for repo in repos: repo.close()

    def test_env_switch_accepts_only_explicit_boolean(self):
        with patch.dict("os.environ", {"TELEGRAM_CLAIM_FIRST_USER": "true"}):
            self.assertTrue(load_settings().telegram_claim_first_user)
        with patch.dict("os.environ", {"TELEGRAM_CLAIM_FIRST_USER": "typo"}):
            with self.assertRaises(ValueError): load_settings()
