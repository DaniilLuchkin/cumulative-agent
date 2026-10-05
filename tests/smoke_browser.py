"""Opt-in browser acceptance: real API, desktop/mobile, signed Telegram bridge.

Requires development-only Playwright and Chromium, plus loopback sockets.
Provider calls and real Telegram traffic are disabled.
"""
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import yaml
from playwright.sync_api import expect, sync_playwright

from cumulative_agent.domain import TaskCreate
from cumulative_agent.repository import Repository
from cumulative_agent.tasks import TaskManager


def main():
    root = Path(__file__).resolve().parents[1]
    screenshots = root / "docs" / "screenshots"
    screenshots.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        token = secrets.token_urlsafe(32)
        bot_token = "123456:synthetic-browser-test-token"
        database = str(temporary / "agent.sqlite3")
        config = temporary / "config.yaml"
        config.write_text(yaml.safe_dump({"database": database,
            "providers": {"openrouter": {"balance":4.32,"requests_per_minute":20},"chatgpt_codex":{}},
            "models": [{"id":"example/coding-model","provider":"openrouter","enabled":True,"capabilities":["general","coding"],
                        "input_price":.15,"output_price":.6,"quota_remaining":72000,"quota_limit":100000,
                        "resets_at":(datetime.now(ZoneInfo('UTC'))+timedelta(minutes=45)).isoformat(),"requests_per_minute":10,
                        "tokens_per_minute":20000,"context_size":128000},
                       {"id":"example/free-model","provider":"openrouter","enabled":True,"quota_remaining":None}],
            "limits": {"max_background_backlog": 0,"daily_cost_limit":2,"background_cost_limit":.25}}))
        repo = Repository(database)
        repo.set_setting("paused", True)
        tasks = TaskManager(repo)
        local_now = datetime.now(ZoneInfo("Europe/Moscow"))
        tasks.create(TaskCreate(title="Проверить job-assistant на баги", description="Проверить обработку ошибок и подготовить отчёт.", priority=30, status="inbox", deadline=(local_now-timedelta(days=1)).replace(hour=18,minute=0,second=0,microsecond=0)))
        tasks.create(TaskCreate(title="Обновить документацию проекта", description="Собрать решения по архитектуре и инструкции запуска.", priority=20, status="inbox", deadline=local_now.replace(hour=18,minute=0,second=0,microsecond=0)))
        tasks.create(TaskCreate(title="Спланировать следующую неделю", priority=10, status="inbox", deadline=(local_now+timedelta(days=1)).replace(hour=18,minute=0,second=0,microsecond=0)))
        tasks.create(TaskCreate(title="Разобрать заметки", description="Личное напоминание: остаётся во входящих.", priority=0, status="inbox"))
        completed = tasks.create(TaskCreate(title="Подключить список задач", status="inbox"))
        tasks.control(completed.id, "complete")
        tasks.create(TaskCreate(title="Отчёт по затратам моделей", type="optimization", handler="usage_review", required_capabilities=[]))
        repo.close()
        env = dict(os.environ, PYTHONPATH=str(root/"src"), AGENT_CONFIG=str(config), AGENT_API_TOKEN=token, TELEGRAM_BOT_TOKEN=bot_token, TELEGRAM_OWNER_IDS="123", OPENROUTER_API_KEY="synthetic-resource-test-key")
        env.pop("AGENT_DATABASE", None)
        # Disable the poller while keeping Mini App HMAC verification enabled.
        launcher = temporary / "server.py"
        launcher.write_text("from cumulative_agent.api import create_app\nfrom cumulative_agent.config import load_settings\nfrom cumulative_agent.service import Service\nimport uvicorn\ns=load_settings()\nservice=Service(s)\nuvicorn.run(create_app(s,service=service,start_worker=False),host='127.0.0.1',port="+str(port)+",access_log=False)\n")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{port}"
        with (temporary / "server.log").open("w+") as log:
            process = subprocess.Popen([sys.executable,str(launcher)],cwd=root,env=env,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:
                        opener.open(base+"/health",timeout=1).close(); break
                    except (OSError,urllib.error.URLError):
                        if process.poll() is not None: raise RuntimeError("Browser fixture server failed")
                        time.sleep(.05)
                else: raise TimeoutError("Readiness timeout")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH","/usr/bin/chromium"),headless=True,args=["--no-sandbox","--disable-dev-shm-usage"])
                    context = browser.new_context(viewport={"width":1280,"height":900},timezone_id="Europe/Moscow",locale="ru-RU")
                    context.route("https://telegram.org/**",lambda route: route.fulfill(status=200,content_type="text/javascript",body=""))
                    page = context.new_page()
                    errors = []
                    page.on("pageerror",lambda error: errors.append(str(error)))
                    page.goto(base+"/app")
                    page.get_by_label("Токен доступа",exact=True).fill(token)
                    page.get_by_role("button",name="Открыть задачи",exact=True).click()
                    expect(page.locator("#workspace")).to_be_visible()
                    expect(page.locator(".task-card")).to_have_count(4)
                    expect(page.locator(".task-title").first).to_have_text("Проверить job-assistant на баги")
                    page.screenshot(path=str(screenshots/"tasks-desktop.png"),full_page=True)
                    page.get_by_role('button',name='Ресурсы',exact=True).click()
                    expect(page.get_by_role('heading',name='Ресурсы',exact=True)).to_be_visible()
                    expect(page.locator('#resources-content [data-provider-id="openrouter"]')).to_contain_text('OpenRouter')
                    expect(page.locator('[data-model-id="example/coding-model"]')).to_contain_text('72\u00a0000')
                    expect(page.locator('[data-model-id="example/free-model"]')).to_contain_text('Не отслеживается')
                    expect(page.locator('[data-provider-id="chatgpt_codex"]')).to_contain_text('Нет адаптера')
                    page.locator('[data-model-id="example/coding-model"] summary').click()
                    expect(page.locator('[data-model-id="example/coding-model"]')).to_contain_text('128\u00a0000')
                    expect(page.locator('#add-task')).not_to_be_visible()
                    expect(page.locator('#task-panel')).not_to_be_visible()
                    assert 'synthetic-resource-test-key' not in page.locator('#workspace').inner_text()
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(screenshots/'resources-desktop.png'),full_page=True)
                    page.get_by_role('button',name='Обновить',exact=True).click()
                    expect(page.locator('#resources-status')).to_contain_text('На ')
                    expect(page.locator('[data-model-id="example/coding-model"] details')).to_have_attribute('open','')
                    page.locator('[data-view="active"]').click()
                    expect(page.locator('.task-card')).to_have_count(4)
                    page.get_by_role("button",name="Новая задача",exact=True).click()
                    page.get_by_label("Что нужно сделать?",exact=True).fill("Моя новая задача")
                    page.get_by_label("Детали",exact=False).fill("Контекст <img src=x onerror=alert(1)> остаётся текстом.")
                    page.get_by_label("Приоритет",exact=True).select_option("20")
                    page.get_by_label("Дедлайн",exact=False).fill("2026-10-05T15:45")
                    page.get_by_label("Выполнение",exact=True).select_option("inbox")
                    page.get_by_role("button",name="Создать задачу",exact=True).click()
                    expect(page.get_by_role("button",name="Открыть задачу: Моя новая задача",exact=True)).to_be_visible()
                    request = urllib.request.Request(base+"/api/task-list?search="+urllib.parse.quote("Моя новая задача"),headers={"Authorization":"Bearer "+token})
                    with opener.open(request) as response: created = json.load(response)["items"][0]
                    assert created["priority"] == 20 and created["status"] == "inbox"
                    assert datetime.fromisoformat(created["deadline"]).astimezone(ZoneInfo("UTC")).hour == 12
                    page.get_by_role("button",name="Открыть задачу: Моя новая задача",exact=True).click()
                    expect(page.locator("#editor")).to_be_visible()
                    page.get_by_label("Приоритет",exact=True).select_option("custom")
                    page.get_by_label("Числовой приоритет",exact=True).fill("77")
                    page.get_by_role("button",name="Без срока",exact=True).click()
                    page.get_by_role("button",name="Сохранить",exact=True).click()
                    expect(page.locator(".task-title").first).to_have_text("Моя новая задача")
                    page.get_by_role("button",name="Отметить выполненной: Моя новая задача",exact=True).click()
                    expect(page.get_by_role("button",name="Открыть задачу: Моя новая задача",exact=True)).to_have_count(0)
                    page.locator('[data-view="done"]').click()
                    expect(page.get_by_role("button",name="Открыть задачу: Моя новая задача",exact=True)).to_be_visible()
                    page.get_by_role("button",name="Вернуть в список: Моя новая задача",exact=True).click()
                    expect(page.get_by_role("button",name="Открыть задачу: Моя новая задача",exact=True)).to_have_count(0)
                    page.locator('[data-view="active"]').click()
                    page.get_by_role("searchbox",name="Поиск задач").fill("НОВАЯ")
                    expect(page.locator(".task-card")).to_have_count(1)
                    page.get_by_role("searchbox",name="Поиск задач").fill("")
                    expect(page.locator(".task-card")).to_have_count(5)
                    assert page.locator("#workspace img").count() == 0
                    # An older save response must not close a newly opened draft.
                    page.get_by_role("button",name="Открыть задачу: Моя новая задача",exact=True).click()
                    page.locator("#task-description").fill("Настроить сроки и порядок выполнения задач.")
                    held = []
                    def hold_save(route):
                        if route.request.method == "PATCH":
                            held.append((route, route.fetch()))
                        else:
                            route.continue_()
                    task_url = base+"/api/tasks/"+created["id"]
                    page.route(task_url,hold_save)
                    page.get_by_role("button",name="Сохранить",exact=True).click()
                    for _ in range(100):
                        if held: break
                        page.wait_for_timeout(20)
                    assert held
                    page.get_by_role("button",name="Закрыть",exact=True).click()
                    page.get_by_role("button",name="Новая задача",exact=True).click()
                    page.get_by_label("Что нужно сделать?",exact=True).fill("Не потерять новый черновик")
                    held[0][0].fulfill(response=held[0][1])
                    expect(page.locator("#toast")).to_have_text("Задача сохранена")
                    expect(page.locator("#editor")).to_be_visible()
                    expect(page.get_by_label("Что нужно сделать?",exact=True)).to_have_value("Не потерять новый черновик")
                    page.unroute(task_url,hold_save)
                    page.get_by_role("button",name="Закрыть",exact=True).click()
                    fields={"auth_date":str(int(time.time())),"user":json.dumps({"id":123,"first_name":"Test"}),"query_id":"browser-test"}
                    secret=hmac.new(b"WebAppData",bot_token.encode(),hashlib.sha256).digest()
                    fields["hash"]=hmac.new(secret,"\n".join(f"{k}={v}" for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
                    bridge="window.Telegram={WebApp:{initData:"+json.dumps(urlencode(fields))+",colorScheme:'light',ready(){},expand(){},onEvent(event,fn){(this.events??={})[event]=fn},BackButton:{onClick(fn){this.handler=fn},show(){this.visible=true},hide(){this.visible=false}}}};"
                    mobile=browser.new_context(viewport={"width":390,"height":844},timezone_id="Europe/Moscow",locale="ru-RU",is_mobile=True,has_touch=True)
                    mobile.route("https://telegram.org/**",lambda route: route.fulfill(status=200,content_type="text/javascript",body=bridge))
                    phone=mobile.new_page(); phone.on("pageerror",lambda error: errors.append(str(error)))
                    phone.goto(base+"/app")
                    expect(phone.locator("#workspace")).to_be_visible()
                    expect(phone.locator(".task-card")).to_have_count(5)
                    assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    phone.screenshot(path=str(screenshots/"tasks-mobile.png"),full_page=True)
                    phone.get_by_role('button',name='Ресурсы',exact=True).click()
                    expect(phone.locator('#resource-panel')).to_be_visible()
                    expect(phone.locator('[data-model-id="example/coding-model"]')).to_be_visible()
                    assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    phone.evaluate('window.scrollTo(0,0)')
                    phone.screenshot(path=str(screenshots/'resources-mobile.png'),full_page=True)
                    phone.locator('[data-view="active"]').click()
                    expect(phone.locator('#task-panel')).to_be_visible()
                    phone.get_by_role("button",name="Новая задача",exact=True).click()
                    assert phone.evaluate("window.Telegram.WebApp.BackButton.visible")
                    phone.evaluate("window.Telegram.WebApp.BackButton.handler()")
                    expect(phone.locator("#editor")).not_to_be_visible()
                    phone.get_by_role("button",name="Новая задача",exact=True).click()
                    phone.get_by_label("Что нужно сделать?",exact=True).fill("Новое напоминание")
                    phone.get_by_label("Приоритет",exact=True).select_option("30")
                    phone.get_by_label("Выполнение",exact=True).select_option("inbox")
                    phone.get_by_role("button",name="Завтра",exact=True).click()
                    phone.get_by_label('Повторять',exact=True).select_option('daily')
                    phone.get_by_label('Напомнить до срока',exact=True).select_option('60')
                    phone.screenshot(path=str(screenshots/"task-editor-mobile.png"),full_page=True)
                    assert phone.locator("#editor").bounding_box()["width"] == 390
                    phone.get_by_role("button",name="Создать задачу",exact=True).click()
                    expect(phone.get_by_role("button",name="Открыть задачу: Новое напоминание",exact=True)).to_be_visible()
                    request=urllib.request.Request(base+'/api/task-list?search='+urllib.parse.quote('Новое напоминание'),headers={'Authorization':'Bearer '+token})
                    with opener.open(request) as response: reminder=json.load(response)['items'][0]
                    assert reminder['recurrence']=='daily' and reminder['task_timezone']=='Europe/Moscow' and reminder['notify_before_minutes']==60
                    phone.get_by_role('button',name='Открыть задачу: Новое напоминание',exact=True).click()
                    expect(phone.get_by_text('История задачи',exact=True)).to_be_visible()
                    phone.get_by_role('button',name='Отменить выполнение',exact=True).click()
                    phone.locator('[data-view="cancelled"]').click()
                    phone.get_by_role('button',name='Открыть задачу: Новое напоминание',exact=True).click()
                    phone.get_by_role('button',name='Повторить новой задачей',exact=True).click()
                    phone.locator('[data-view="active"]').click()
                    expect(phone.get_by_role('button',name='Открыть задачу: Новое напоминание',exact=True)).to_be_visible()
                    phone.evaluate("window.Telegram.WebApp.colorScheme='dark';window.Telegram.WebApp.events.themeChanged()")
                    expect(phone.locator("body")).to_have_attribute("data-theme","dark")
                    phone.set_viewport_size({"width":320,"height":720})
                    assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    phone.get_by_role('button',name='Ресурсы',exact=True).click()
                    expect(phone.locator('[data-provider-id="openrouter"]')).to_be_visible()
                    assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    phone.evaluate('window.scrollTo(0,0)')
                    phone.screenshot(path=str(screenshots/'resources-dark-mobile.png'),full_page=True)
                    phone.get_by_role('button',name='Сверить с OpenRouter API',exact=True).click()
                    expect(phone.locator('#toast')).to_have_text('Задача сверки поставлена в очередь')
                    assert not errors, errors
                    browser.close()
            finally:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                assert process.returncode in (0,-signal.SIGTERM)
        print(json.dumps({"desktop":"passed","mobile":"passed","resources":"passed","telegram_signed_login":"passed","create_edit_complete_reopen":"passed","cancel_retry_recurrence_history":"passed","resource_sync_queue":"passed","late_save_draft":"passed","telegram_back_button":"passed","dark_theme":"passed","timezone":"Europe/Moscow","javascript_errors":0,"screenshots":str(screenshots)}))


if __name__ == "__main__":
    main()
