import hashlib
import hmac
import json
import time
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from cumulative_agent.api import create_app
from cumulative_agent.config import Settings
from cumulative_agent.domain import Conflict, Model, TaskCreate, TaskPatch
from cumulative_agent.service import Service
from cumulative_agent.web_auth import InvalidTelegramData, verify_init_data


BOT_TOKEN = "123456:synthetic-test-token"


def signed_data(owner=123, issued=None, **extra):
    fields = {"user": json.dumps({"id": owner, "first_name": "Test"}, separators=(",", ":")),
              "auth_date": str(int(time.time()) if issued is None else issued), "query_id": "test-query", **extra}
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class AuthTests(unittest.TestCase):
    def test_official_hmac_validates_user_and_optional_signature_field(self):
        self.assertEqual(verify_init_data(signed_data(signature="optional-ed25519-value"), BOT_TOKEN, 3600), 123)

    def test_tampered_user_signature_duplicates_and_missing_fields_rejected(self):
        valid = signed_data()
        for data in (valid.replace("123", "456"), valid + "&user=anything", "user=%7B%7D", valid + "&evil%0Akey=value"):
            with self.subTest(data=data[:20]), self.assertRaises(InvalidTelegramData):
                verify_init_data(data, BOT_TOKEN, 3600)

    def test_stale_future_and_wrong_bot_rejected(self):
        for data, token in ((signed_data(issued=int(time.time()) - 3601), BOT_TOKEN),
                            (signed_data(issued=int(time.time()) + 100), BOT_TOKEN), (signed_data(), "different-token")):
            with self.assertRaises(InvalidTelegramData):
                verify_init_data(data, token, 3600)

    def test_https_url_is_required_and_credentials_forbidden(self):
        for value in ("http://example.com/app", "https://user:pass@example.com/app", "https://example.com/app?token=secret"):
            with self.assertRaises(ValueError):
                Settings(telegram_webapp_url=value)
        self.assertEqual(Settings(telegram_webapp_url="https://example.com/app").telegram_webapp_url, "https://example.com/app")


class MiniAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(database=":memory:", api_token="test-api-token-at-least-24-characters", telegram_bot_token=BOT_TOKEN, telegram_owner_ids=[123])
        self.service = Service(self.settings, providers={})
        self.app = create_app(self.settings, service=self.service, start_worker=False)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.headers = {"Authorization": "Telegram " + signed_data()}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)

    async def create(self, **data):
        response = await self.client.post("/api/tasks", headers=self.headers, json={"title": "Example", "status": "inbox", **data})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_telegram_owner_auth_works_without_browser_api_token(self):
        self.settings.api_token = ""
        response = await self.client.get("/api/auth/me", headers=self.headers)
        self.assertEqual(response.json(), {"method": "telegram", "owner_id": 123})
        task = await self.create(title="From Mini App")
        self.assertEqual(task["title"], "From Mini App")

    async def test_artifact_download_requires_auth_and_rejects_outside_paths_and_symlinks(self):
        task_data = await self.create()
        task = self.service.repo.task(task_data['id'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'artifacts'; root.mkdir()
            self.service.coding.root = root
            patch = root / 'changes.patch'; patch.write_text('safe patch')
            identifier = self.service.coding.artifact(task, None, 'patch', patch)
            url = '/api/artifacts/' + identifier
            self.assertEqual((await self.client.get(url)).status_code, 401)
            download = await self.client.get(url, headers=self.headers)
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.text, 'safe patch')
            detail = (await self.client.get('/api/tasks/' + task.id, headers=self.headers)).json()
            self.assertNotIn('path', detail['artifacts'][0])
            outside = Path(directory) / 'secret'; outside.write_text('must not download')
            link = root / 'link'; link.symlink_to(outside)
            for path in (outside, link):
                forbidden = self.service.coding.artifact(task, None, 'report', path)
                response = await self.client.get('/api/artifacts/' + forbidden, headers=self.headers)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn('must not download', response.text)

    async def test_unauthorized_forged_and_expired_data_cannot_read_or_write(self):
        cases = [(signed_data(owner=999), 403), (signed_data().replace("123", "456"), 401),
                 (signed_data(issued=int(time.time()) - 4000), 401)]
        for data, expected in cases:
            headers = {"Authorization": "Telegram " + data}
            self.assertEqual((await self.client.get("/api/task-list", headers=headers)).status_code, expected)
            self.assertEqual((await self.client.post("/api/tasks", headers=headers, json={"title": "forged"})).status_code, expected)
        self.assertEqual(len(self.service.repo.tasks()), 0)

    async def test_shell_framing_sdk_and_assets_allowed_but_api_not_embeddable(self):
        shell = await self.client.get("/app")
        self.assertEqual(shell.status_code, 200)
        self.assertIn("https://telegram.org/js/telegram-web-app.js", shell.text)
        self.assertNotIn("X-Frame-Options", shell.headers)
        self.assertIn("https://web.telegram.org", shell.headers["Content-Security-Policy"])
        self.assertEqual((await self.client.get("/app/assets/app.js")).status_code, 200)
        api = await self.client.get("/api/task-list", headers=self.headers)
        self.assertEqual(api.headers["X-Frame-Options"], "DENY")
        self.assertNotIn(BOT_TOKEN, shell.text)

    async def test_patch_deadline_priority_and_explicit_clear_persist(self):
        task = await self.create()
        updated = await self.client.patch("/api/tasks/" + task["id"], headers=self.headers, json={"priority": 30, "deadline": "2026-10-03T15:30:00+03:00", "expected_updated_at": task["updated_at"]})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["priority"], 30)
        self.assertEqual(datetime.fromisoformat(updated.json()["deadline"]).hour, 15)
        cleared = await self.client.patch("/api/tasks/" + task["id"], headers=self.headers, json={"deadline": None})
        self.assertIsNone(cleared.json()["deadline"])
        self.assertEqual(self.service.repo.task(task["id"]).priority, 30)
        self.assertIn("task_updated", [e["kind"] for e in self.service.repo.all("SELECT * FROM system_events")])

    async def test_bad_patch_fields_naive_datetime_and_stale_edit_rejected(self):
        task = await self.create()
        for data in ({"deadline": "2026-10-02T15:30:00"}, {"priority": 1001}, {"priority": None}, {"title": "   "}, {"max_cost": 100}, {"description": None}):
            response = await self.client.patch("/api/tasks/" + task["id"], headers=self.headers, json=data)
            self.assertEqual(response.status_code, 422, response.text)
        self.service.tasks.update(task["id"], TaskPatch(priority=10))
        stale = await self.client.patch("/api/tasks/" + task["id"], headers=self.headers, json={"priority": 30, "expected_updated_at": task["updated_at"]})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.service.repo.task(task["id"]).priority, 10)

    async def test_running_task_content_is_fixed_but_deadline_can_change(self):
        task = await self.create()
        self.service.repo.execute("UPDATE tasks SET status='running' WHERE id=?", (task["id"],))
        response = await self.client.patch("/api/tasks/" + task["id"], headers=self.headers, json={"title": "replacement"})
        self.assertEqual(response.status_code, 409)
        response = await self.client.patch("/api/tasks/" + task["id"], headers=self.headers, json={"priority": 30, "deadline": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.repo.task(task["id"]).title, "Example")

    async def test_manual_completion_reopens_as_reminder_without_agent_run(self):
        task = await self.create()
        completed = await self.client.post("/api/tasks/" + task["id"] + "/control", headers=self.headers, json={"action": "complete"})
        self.assertEqual(completed.json()["status"], "done")
        self.assertTrue(completed.json()["completed_manually"])
        self.assertEqual(self.service.metrics.snapshot()["runs"], 0)
        reopened = await self.client.post("/api/tasks/" + task["id"] + "/control", headers=self.headers, json={"action": "reopen"})
        self.assertEqual(reopened.json()["status"], "inbox")
        self.assertFalse(reopened.json()["completed_manually"])

    async def test_manual_completion_cannot_bypass_approval_or_running_state(self):
        proposal = await self.create(proposal_action="merge")
        running = await self.create()
        self.service.repo.execute("UPDATE tasks SET status='running' WHERE id=?", (running["id"],))
        for task in (proposal, running):
            response = await self.client.post("/api/tasks/" + task["id"] + "/control", headers=self.headers, json={"action": "complete"})
            self.assertEqual(response.status_code, 400)

    async def test_pagination_sort_priority_and_deadline_offset_order(self):
        first = await self.create(title="A", priority=0)
        earlier = await self.create(title="B", priority=30, deadline="2026-10-03T01:00:00+03:00")
        later = await self.create(title="C", priority=10, deadline="2026-10-02T23:00:00Z")
        response = await self.client.get("/api/task-list?sort=deadline&limit=1", headers=self.headers)
        page = response.json()
        self.assertEqual(page["items"][0]["id"], earlier["id"])
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["next_offset"], 1)
        page = (await self.client.get("/api/task-list?sort=deadline&offset=1", headers=self.headers)).json()
        self.assertEqual([t["id"] for t in page["items"]], [later["id"], first["id"]])

    async def test_search_is_unicode_case_insensitive_and_literal_percent(self):
        await self.create(title="Проверить профиль 100%")
        await self.create(title="Проверить профиль 100x")
        response = await self.client.get("/api/task-list", params={"search": "ПРОФИЛЬ 100%"}, headers=self.headers)
        self.assertEqual(response.json()["total"], 1)

    async def test_today_uses_client_timezone_and_filters_completed_background(self):
        start = "2026-10-02T00:00:00+03:00"
        end = "2026-10-03T00:00:00+03:00"
        local_today = await self.create(deadline="2026-10-01T22:00:00Z")
        await self.create(deadline="2026-10-01T20:00:00Z")
        await self.create(type="maintenance", deadline="2026-10-01T22:00:00Z")
        response = await self.client.get("/api/task-list", params={"due": "today", "day_start": start, "day_end": end}, headers=self.headers)
        self.assertEqual([t["id"] for t in response.json()["items"]], [local_today["id"]])
        await self.client.post("/api/tasks/" + local_today["id"] + "/control", headers=self.headers, json={"action": "complete"})
        response = await self.client.get("/api/task-list", params={"due": "today", "day_start": start, "day_end": end}, headers=self.headers)
        self.assertEqual(response.json()["total"], 0)

    async def test_quota_validator_is_synchronous_and_rejects_naive_reset(self):
        response = await self.client.post("/api/resources/quota/missing", headers=self.headers, json={"remaining": 1, "resets_at": "2026-10-02T00:00:00"})
        self.assertEqual(response.status_code, 422)

    async def test_bot_app_button_and_menu_only_for_private_owner(self):
        self.settings.telegram_webapp_url = "https://example.com/app"
        calls = []
        async def call(method, payload):
            calls.append((method, payload)); return True
        self.service.telegram.call = call
        update = {"update_id": 7, "message": {"from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": "/app"}}
        await self.service.telegram.handle(update)
        self.assertEqual([method for method, _ in calls], ["setChatMenuButton", "sendMessage"])
        self.assertEqual(calls[1][1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"], "https://example.com/app")
        update["message"]["from"]["id"] = 999
        self.assertFalse(await self.service.telegram.handle(update))
        self.assertEqual(len(calls), 2)

    async def test_resources_protected_and_report_unknowns_without_secrets(self):
        self.assertEqual((await self.client.get('/api/resources')).status_code, 401)
        self.settings.openrouter_api_key = 'synthetic-openrouter-secret'
        model = Model(id='example/unknown-quota',quota_remaining=None,enabled=True)
        self.service.repo.execute('INSERT INTO models VALUES(?,?)',(model.id,model.model_dump_json()))
        data = await self.client.get('/api/resources',headers=self.headers)
        self.assertEqual(data.status_code, 200)
        self.assertIsNone(data.json()['models'][0]['quota_remaining'])
        self.assertEqual(data.json()['budgets']['daily']['limit'], 0)
        self.assertEqual(data.json()['budgets']['daily']['remaining'], 0)
        self.assertEqual(data.json()['execution_limits']['max_concurrent_agents'], 1)
        for value in (BOT_TOKEN, self.settings.api_token, self.settings.openrouter_api_key):
            self.assertNotIn(value,data.text)
        self.assertEqual((await self.client.get('/app/assets/resources.mjs')).status_code, 200)

    async def test_resources_budget_includes_running_reservations_before_midnight(self):
        task = await self.create()
        self.settings.limits.daily_cost_limit = 1
        self.settings.limits.background_cost_limit = .5
        yesterday = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        self.service.repo.execute("INSERT INTO task_runs(id,task_id,status,started_at,decision,reserved_cost,background) VALUES(?,?,'running',?,'{}',.2,1)",('reserved',task['id'],yesterday))
        data = (await self.client.get('/api/resources',headers=self.headers)).json()
        self.assertAlmostEqual(data['budgets']['daily']['remaining'],.8)
        self.assertAlmostEqual(data['budgets']['background']['remaining'],.3)
        self.assertAlmostEqual(data['budgets']['daily']['reserved'],.2)
        self.service.repo.execute("UPDATE task_runs SET status='done',charged_cost=.1,started_at=? WHERE id='reserved'",(datetime.now(timezone.utc).isoformat(),))
        data = (await self.client.get('/api/resources',headers=self.headers)).json()
        self.assertAlmostEqual(data['budgets']['daily']['remaining'],.9)
        self.assertEqual(data['budgets']['daily']['reserved'],0)

    async def test_resources_exposes_only_registered_executable_adapters(self):
        self.service.repo.execute("INSERT INTO providers VALUES('unavailable',?)",(json.dumps({'balance':None,'requests_per_minute':5}),))
        self.service.repo.execute("INSERT INTO providers VALUES('registered',?)",(json.dumps({'balance':4.32,'requests_per_minute':10}),))
        self.service.providers['registered'] = object()
        data = (await self.client.get('/api/resources',headers=self.headers)).json()
        providers = {p['id']:p for p in data['providers']}
        self.assertFalse(providers['unavailable']['adapter_available'])
        self.assertTrue(providers['registered']['adapter_available'])
        self.assertEqual(providers['registered']['balance'],4.32)


if __name__ == "__main__":
    unittest.main()
