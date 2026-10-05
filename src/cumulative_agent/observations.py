"""Read-only provider observations, with no automatic model/budget expansion."""
import asyncio
import json
import math
import re
from uuid import uuid5, NAMESPACE_URL
from datetime import datetime, timezone

from .domain import TaskCreate, now


def numeric(value):
    number=float(value)
    if not math.isfinite(number) or number<0:raise ValueError('Invalid provider quantity')
    return number


class ResourceObserver:
    def __init__(self,repo,settings,client):self.repo,self.settings,self.client=repo,settings,client

    def schedule(self,tasks):
        if not self.settings.resource_auto_sync or not self.settings.openrouter_api_key:return
        if self.repo.get_setting('paused',False) or not self.repo.get_setting('background_enabled',True):return
        backlog=self.repo.one("SELECT COUNT(*) n FROM tasks WHERE type!='user' AND status IN ('inbox','ready','running','blocked')")['n']
        if backlog>=self.settings.limits.max_background_backlog:return
        current=datetime.now(timezone.utc).timestamp()
        previous=self.repo.get_setting('resource_sync_scheduled',0)
        if current-previous<self.settings.resource_sync_seconds:return
        tasks.create(TaskCreate(title='Synchronize OpenRouter resource metadata',type='resource',handler='resource_sync',required_capabilities=[],max_cost=0,max_attempts=1,
                               idempotency_key=f'resource-sync:{int(current//self.settings.resource_sync_seconds)}'))
        self.repo.set_setting('resource_sync_scheduled',current)

    async def refresh(self):
        if not self.settings.openrouter_api_key:
            return json.dumps({'status':'not_configured','source':'OpenRouter API','credentials_required':True})
        try:
            observed_started=now()
            key_response,model_response=await asyncio.gather(
                self.client.get('https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+self.settings.openrouter_api_key},timeout=15),
                self.client.get('https://openrouter.ai/api/v1/models',timeout=15))
            key_response.raise_for_status();model_response.raise_for_status()
            key=key_response.json()['data'];catalog=model_response.json()['data']
            raw_rate=key.get('rate_limit');rate=None
            if isinstance(raw_rate,dict) and re.fullmatch(r'\d+[smh]',str(raw_rate.get('interval',''))):
                rate={'requests':int(numeric(raw_rate['requests'])),'interval':raw_rate['interval']}
            reset=key.get('limit_reset')
            if reset not in ('daily','weekly','monthly'):reset=None
            allowed={m.id for m in self.repo.models() if m.provider=='openrouter'}
            models={}
            for entry in catalog:
                if entry['id'] not in allowed:continue
                maximum=entry.get('top_provider',{}).get('max_completion_tokens')
                models[entry['id']]={'input_price':numeric(entry['pricing']['prompt'])*1_000_000,
                    'output_price':numeric(entry['pricing']['completion'])*1_000_000,'context_size':int(numeric(entry['context_length'])),
                    'max_output_tokens':None if maximum is None else int(numeric(maximum))}
            observation={'source':'https://openrouter.ai/api/v1/key + /models','status':'observed',
                'key_limit_remaining':None if key.get('limit_remaining') is None else numeric(key['limit_remaining']),
                'key_limit':None if key.get('limit') is None else numeric(key['limit']),
                'key_limit_reset':reset,'rate_limit':rate,'models':models,
                'catalog_checked':True,'automatic_model_enable':False,'observed_at':observed_started,'completed_at':now()}
            with self.repo.transaction() as db:
                db.execute('INSERT INTO resource_observations VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET payload=excluded.payload,observed_at=excluded.observed_at',
                           ('openrouter',json.dumps(observation),observation['observed_at']))
            # Public catalog discovery produces bounded proposals, never enables a model.
            from .tasks import TaskManager
            room=max(0,self.settings.limits.max_background_backlog-self.repo.one("SELECT COUNT(*) n FROM tasks WHERE type!='user' AND status IN ('inbox','ready','running','blocked')")['n'])
            if self.settings.limits.minimum_expected_value<=1:
                for entry in [e for e in catalog if e['id'] not in allowed and e['id'].endswith(':free')][:min(3,room)]:
                    if numeric(entry['pricing']['prompt']) or numeric(entry['pricing']['completion']):continue
                    identifier=str(uuid5(NAMESPACE_URL,'https://openrouter.ai/models/'+entry['id']))
                    source='https://openrouter.ai/api/v1/models'
                    self.repo.execute('INSERT INTO discoveries VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET verified_at=excluded.verified_at',
                        (identifier,'openrouter',entry['id'],'Zero catalog token price; verify provider terms and suitability before enabling','Account-specific quotas/rate limits not inferred',1,source,now(),now()))
                    TaskManager(self.repo).create(TaskCreate(title='Evaluate free model: '+entry['id'][:450],type='resource',status='inbox',max_cost=0,
                        description=f'Catalog source: {source}. Advertised token prices: zero. Quotas and usage conditions need verification. Model remains disabled.',
                        idempotency_key='discovery:'+identifier))
            self.repo.event('resources_observed',provider='openrouter',configured_models_observed=len(models))
            return json.dumps(observation)
        except asyncio.CancelledError:raise
        except Exception as error:
            old=self.repo.one("SELECT payload FROM resource_observations WHERE provider='openrouter'")
            data=json.loads(old['payload']) if old else {'source':'OpenRouter API','models':{},'observed_at':None}
            data.update(status='refresh_failed',last_error_type=type(error).__name__,last_attempt_at=now())
            self.repo.execute('INSERT INTO resource_observations VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET payload=excluded.payload',('openrouter',json.dumps(data),data.get('observed_at')))
            self.repo.event('resource_sync_failed',provider='openrouter',error_type=type(error).__name__)
            return json.dumps({'status':'refresh_failed','error_type':type(error).__name__,'prior_observation_retained':bool(old)})


def provider_budget_remaining(repo,provider):
    row=repo.one('SELECT payload FROM resource_observations WHERE provider=?',(provider,))
    if not row:return None
    data=json.loads(row['payload']);remaining=data.get('key_limit_remaining')
    if remaining is None:return None
    # Deduct all calls/reservations since the observation started; observations may
    # lag provider billing. This intentionally errs towards lower availability.
    since=data.get('observed_at')
    used=repo.one("SELECT COALESCE(SUM(COALESCE(charged_cost,reserved_cost)),0) n FROM task_runs WHERE provider=? AND (started_at>=? OR finished_at>=? OR status='running')",(provider,since,since))['n']
    return max(0,remaining-used)
