import random
import threading
import time

from celery import Celery
from sqlalchemy import select, update

from .callbacks import DELAYS, CallbackRetry, deliver
from .config import get_settings
from .conversation import ConversationWorker, finish_run
from .db import Database, Job, Record, Work, now, uid
from .errors import DomainError
from .models import Gateway
from .translations import set_status
from .workflow import Workflow

settings = get_settings()
celery = Celery("aifanyi", broker=settings.redis_url)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_schedule={"dispatch": {"task": "aifanyi.dispatch", "schedule": 5.0}},
)


class Runner:
    def __init__(self, db, settings, gateway=None):
        self.db, self.settings = db, settings
        self.gateway = gateway or Gateway(db, settings)

    def pending(self, limit=50):
        with self.db.session() as s:
            return list(
                s.scalars(
                    select(Work.id)
                    .where(
                        Work.status.in_(["pending", "sending", "retry_wait"]),
                        Work.available_at <= now(),
                        Work.lease_until < now(),
                    )
                    .order_by(Work.available_at)
                    .limit(limit)
                )
            )

    def run(self, id):
        token = uid("lease")
        with self.db.session() as s:
            n = s.execute(
                update(Work)
                .where(
                    Work.id == id,
                    Work.status.in_(["pending", "sending", "retry_wait"]),
                    Work.lease_until < now(),
                    Work.available_at <= now(),
                )
                .values(
                    status="sending",
                    lease_token=token,
                    lease_until=now() + self.settings.lease_seconds,
                    attempts=Work.attempts + 1,
                )
            ).rowcount
            if not n:
                return False
            work = s.get(Work, id)
        stop = threading.Event()

        def fence():
            with self.db.session() as s:
                live = s.get(Work, id)
                if live.lease_token != token or live.lease_until < now():
                    raise DomainError("LEASE_LOST", "任务租约已失效。")

        def heartbeat():
            while not stop.wait(20):
                try:
                    with self.db.session() as s:
                        s.execute(
                            update(Work)
                            .where(Work.id == id, Work.lease_token == token)
                            .values(lease_until=now() + self.settings.lease_seconds)
                        )
                except Exception:
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        state, error, data = "delivered", None, work.data
        retry_after = 0
        try:
            if work.kind == "job":
                Workflow(self.db, self.settings, self.gateway, fence).execute(work.target)
            elif work.kind == "run":
                ConversationWorker(self.db, self.settings, self.gateway, fence).execute(work.target)
            elif work.kind == "callback":
                data = {**work.data, **deliver(self.db, self.settings, work)}
            else:
                raise DomainError("UNKNOWN_WORK", "未知任务类型。")
        except DomainError as exc:
            error = exc.code
            if exc.code == "LEASE_LOST":
                return False
            state = "dead"
            self.fail_target(work, exc.code, token)
        except Exception as exc:
            retry_after = exc.retry_after if isinstance(exc, CallbackRetry) else 0
            # No raw provider responses, secrets or business text in operator error messages.
            error = "EXECUTION_ERROR"
            if work.kind == "callback" and work.attempts <= len(DELAYS):
                state = "retry_wait"
            else:
                state = "dead"
                self.fail_target(work, error, token)
        finally:
            stop.set()
            thread.join(timeout=1)
        delay = max(DELAYS[min(work.attempts - 1, 5)], retry_after)
        delay += random.uniform(0, delay * 0.1)
        with self.db.session() as s:
            s.execute(
                update(Work)
                .where(Work.id == id, Work.lease_token == token)
                .values(
                    status=state,
                    last_error=error,
                    data=data,
                    lease_until=0,
                    available_at=now() + (delay if state == "retry_wait" else 0),
                )
            )
        return True

    def fail_target(self, work, code, token):
        with self.db.session() as s:
            lease = s.get(Work, work.id, with_for_update=True)
            if lease.lease_token != token or lease.lease_until < now():
                return
            if work.kind == "job":
                job = s.get(Job, work.target)
                if job and job.status in {"queued", "running"}:
                    set_status(
                        s, job, "failed", {"code": code, "message": "执行失败，请检查服务配置和审计记录。"}
                    )
            elif work.kind == "run":
                run = s.get(Record, work.target)
                if run:
                    finish_run(
                        s,
                        run,
                        [{"type": "error", "code": code, "text": "执行失败，请检查服务配置。"}],
                        "failed",
                    )

    def drain(self, maximum=100):
        count = 0
        while count < maximum:
            ids = self.pending()
            if not ids:
                break
            acquired = sum(int(self.run(id)) for id in ids[: maximum - count])
            count += acquired
            if not acquired:
                break
        return count


@celery.task(name="aifanyi.execute")
def execute(id):
    runner = Runner(Database(settings.database_url), settings)
    runner.run(id)


@celery.task(name="aifanyi.dispatch")
def dispatch():
    runner = Runner(Database(settings.database_url), settings)
    for id in runner.pending():
        execute.delay(id)


def poll(settings):
    runner = Runner(Database(settings.database_url), settings)
    while True:
        runner.drain()
        time.sleep(1)
