import httpx

from .agents import LocalAgents
from .chat import ChatStore
from .config import Settings
from .coding import CodingWorkflow
from .metrics import Metrics
from .planner import Planner
from .providers import OpenRouter
from .repository import Repository
from .resources import ResourceManager
from .reminders import Reminders
from .observations import ResourceObserver
from .routing import Router
from .scheduler import Scheduler
from .tasks import Memory, ProjectRegistry, TaskManager
from .telegram import Telegram
from .worker import Worker


class Service:
    def __init__(self, settings: Settings, providers=None, client=None):
        self.settings = settings
        self.repo = Repository(settings.database)
        self.client = client or httpx.AsyncClient(follow_redirects=False)
        self.resources = ResourceManager(self.repo, settings)
        self.projects = ProjectRegistry(self.repo, settings)
        self.memory = Memory(self.repo)
        self.tasks = TaskManager(self.repo)
        self.providers = providers if providers is not None else ({"openrouter": OpenRouter(settings.openrouter_api_key, self.client)} if settings.openrouter_api_key else {})
        self.router = Router(self.repo, settings, set(self.providers))
        self.scheduler = Scheduler(self.repo, self.resources, self.router, self.memory, self.projects)
        self.coding = CodingWorkflow(self.repo, settings, self.projects, self.tasks)
        self.scheduler.coding = self.coding
        self.chat = ChatStore(self.repo, settings.chat_history_bytes)
        self.scheduler.chat = self.chat
        self.planner = Planner(self.repo, settings, self.tasks, self.projects)
        self.agents = LocalAgents(self.repo, settings, self.projects)
        self.observer = ResourceObserver(self.repo, settings, self.client)
        self.agents.observer = self.observer
        self.worker = Worker(self.repo, settings, self.scheduler, self.planner, self.agents, self.providers)
        self.worker.coding = self.coding
        self.coding.should_yield = self.worker.should_yield
        self.agents.should_yield = self.worker.should_yield
        self.tasks.wake = self.worker.wake
        self.tasks.cancel_running = self.worker.request_cancel
        self.reminders = Reminders(self.repo, settings, self.tasks)
        self.worker.reminders = self.reminders
        self.worker.observer = self.observer
        self.telegram = Telegram(self.repo, settings, self.tasks, self.resources, self.projects, self.client)
        self.telegram.reminders = self.reminders
        self.telegram.chat, self.telegram.worker = self.chat, self.worker
        self.metrics = Metrics(self.repo)

    async def start(self):
        self.worker.start()
        self.telegram.start()

    async def stop(self):
        await self.telegram.stop()
        await self.worker.stop()
        await self.client.aclose()
        self.repo.close()
