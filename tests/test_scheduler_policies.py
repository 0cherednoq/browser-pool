"""Планировщик: варианты, приоритеты, предел очереди, групповые потолки, отмена."""

from __future__ import annotations

import pytest

from browser_pool._core.scheduler import Grant, Scheduler, Waiter
from browser_pool.config import Backoff, GroupLimit, Limits, Recovery, Topology
from browser_pool.errors import PoolSaturatedError
from browser_pool.identity import Identity

PROXY = Identity(key="mail:a", variant="proxy")
DIRECT = Identity(key="mail:a", variant="direct")


def scheduler(*, limits: Limits | None = None, **topology: int | None) -> Scheduler:
    defaults: dict[str, int | None] = {
        "browsers": 1,
        "pages_per_browser": 4,
        "contexts_per_browser": 4,
    }
    return Scheduler(
        Topology(**{**defaults, **topology}),  # pyright: ignore[reportArgumentType] — числа топологии
        limits=limits or Limits(),
    )


def lease(pool: Scheduler, identity: Identity, *, now: float = 0.0) -> Grant:
    pool.submit([identity], now=now)
    served = pool.assign(now=now)
    assert len(served) == 1
    assert served[0].grant is not None
    return served[0].grant


def mailbox(name: str, **kwargs: object) -> Identity:
    return Identity(key=f"mail:{name}", labels={"service": "mail"}, **kwargs)  # pyright: ignore[reportArgumentType] — поля identity


# --- варианты --------------------------------------------------------------------------


def test_idle_context_switches_variant_in_place_with_new_generation() -> None:
    pool = scheduler()
    first = lease(pool, PROXY)
    pool.release(first, now=1.0)

    switched = lease(pool, DIRECT, now=2.0)

    assert switched.new_context
    assert switched.generation == first.generation + 1
    assert switched.browser_id == first.browser_id
    assert pool.variant_switches == 1


def test_busy_context_holds_its_variant_for_the_waiting_one() -> None:
    pool = scheduler()
    held = lease(pool, PROXY)
    wants_direct = pool.submit([DIRECT], now=1.0)
    wants_proxy = pool.submit([PROXY], now=2.0)

    # Места у варианта proxy есть, но старшая заявка ждёт direct: proxy больше не выдаётся.
    assert pool.assign(now=2.0) == []

    pool.release(held, now=3.0)
    served = pool.assign(now=3.0)

    assert served == [wants_direct]
    assert wants_direct.grant is not None
    assert wants_direct.grant.new_context
    assert wants_proxy in pool.waiting()


def test_hold_is_lifted_when_nobody_waits_for_the_other_variant() -> None:
    pool = scheduler()
    lease(pool, PROXY)
    wants_direct = pool.submit([DIRECT], now=1.0)
    wants_proxy = pool.submit([PROXY], now=2.0)
    assert pool.assign(now=2.0) == []

    pool.cancel(wants_direct)

    assert pool.assign(now=3.0) == [wants_proxy]


# --- приоритеты ------------------------------------------------------------------------


def test_higher_priority_is_served_first_and_equal_priority_is_fifo() -> None:
    pool = scheduler(pages_per_browser=1)
    held = lease(pool, Identity(key="mail:x"))
    low = pool.submit([Identity(key="mail:y")], now=1.0)
    urgent = pool.submit([Identity(key="mail:z")], now=2.0, priority=10)
    also_low = pool.submit([Identity(key="mail:w")], now=3.0)

    assert pool.waiting() == [urgent, low, also_low]

    pool.release(held, now=4.0)
    assert pool.assign(now=4.0) == [urgent]


# --- предел очереди --------------------------------------------------------------------


def test_request_over_queue_limit_is_rejected_at_once() -> None:
    pool = scheduler(pages_per_browser=1, limits=Limits(max_waiting=1))
    lease(pool, Identity(key="mail:x"))
    waiting = pool.request([Identity(key="mail:y")], now=1.0)
    assert waiting.grant is None

    with pytest.raises(PoolSaturatedError):
        pool.request([Identity(key="mail:z")], now=2.0)

    assert pool.waiting() == [waiting]


def test_queue_limit_does_not_reject_what_can_be_served_now() -> None:
    pool = scheduler(limits=Limits(max_waiting=0))

    waiter = pool.request([Identity(key="mail:x")], now=0.0)

    assert waiter.grant is not None
    assert pool.waiting() == []


# --- групповые потолки -----------------------------------------------------------------


def test_group_page_cap_leaves_room_for_other_groups() -> None:
    cap = GroupLimit(label="service", value="mail", max_pages=2)
    pool = scheduler(limits=Limits(groups=(cap,)))
    for name in "abc":
        pool.submit([mailbox(name)], now=0.0)
    pool.submit([Identity(key="shop:1", labels={"service": "shop"})], now=0.0)

    served = pool.assign(now=0.0)

    keys = sorted(waiter.grant.identity.key for waiter in served if waiter.grant is not None)
    assert keys == ["mail:a", "mail:b", "shop:1"]


def test_group_context_cap_evicts_the_coldest_idle_member() -> None:
    cap = GroupLimit(label="service", value="mail", max_contexts=2)
    pool = scheduler(limits=Limits(groups=(cap,)))
    for name, moment in (("a", 1.0), ("b", 2.0)):
        pool.release(lease(pool, mailbox(name), now=moment), now=moment)

    fresh = lease(pool, mailbox("c"), now=3.0)

    assert fresh.identity.key == "mail:c"
    assert [context.key for context in pool.take_closures()] == ["mail:a"]


def test_group_context_cap_with_busy_members_makes_new_member_wait() -> None:
    cap = GroupLimit(label="service", value="mail", max_contexts=1)
    pool = scheduler(limits=Limits(groups=(cap,)))
    lease(pool, mailbox("a"))

    pool.submit([mailbox("b")], now=1.0)

    assert pool.assign(now=1.0) == []


def test_identity_bringing_new_labels_respects_the_groups_it_joins() -> None:
    # Найдено hypothesis: простаивающий контекст сменил вариант, а новая identity принесла метку
    # другой группы — и в группе стало контекстов больше потолка.
    cap = GroupLimit(label="service", value="mail", max_contexts=1)
    pool = scheduler(limits=Limits(groups=(cap,)))
    lease(pool, mailbox("a"))
    shop = Identity(key="mail:b", variant="proxy", labels={"service": "shop"})
    pool.release(lease(pool, shop), now=1.0)

    pool.submit([Identity(key="mail:b", variant="direct", labels={"service": "mail"})], now=2.0)

    assert pool.assign(now=2.0) == []
    mail = [context for context in pool.contexts() if ("service", "mail") in context.labels]
    assert len(mail) == 1


# --- отмена ----------------------------------------------------------------------------


def test_abandoning_a_granted_waiter_returns_its_lease() -> None:
    # Гонка: заявке выдали аренду, но ожидавшую задачу в тот же момент отменили.
    pool = scheduler(pages_per_browser=1)
    waiter = pool.request([Identity(key="mail:x")], now=0.0)
    assert waiter.grant is not None
    next_one = pool.submit([Identity(key="mail:y")], now=1.0)

    pool.abandon(waiter, now=2.0)

    assert pool.assign(now=2.0) == [next_one]


def test_abandoning_a_waiting_waiter_just_removes_it() -> None:
    pool = scheduler(pages_per_browser=1)
    lease(pool, Identity(key="mail:x"))
    waiter = pool.submit([Identity(key="mail:y")], now=1.0)

    pool.abandon(waiter, now=2.0)

    assert pool.waiting() == []


# --- учёт по ключу и цена выдачи ---------------------------------------------------------


def test_identity_status_is_not_kept_once_there_is_nothing_to_remember() -> None:
    scheduler = Scheduler(Topology(browsers=1, pages_per_browser=4))

    for index in range(50):  # одноразовые identity: открылись, отработали, закрылись
        identity = Identity(key=f"anon:{index}")
        waiter = scheduler.request([identity], now=float(index))
        assert waiter.grant is not None
        scheduler.open_succeeded(identity.key)
        scheduler.release(waiter.grant, now=float(index))
        scheduler.retire(identity.key, generation=waiter.grant.generation)
        for context in scheduler.take_closures():
            scheduler.forget(context.key, generation=context.generation)

    assert scheduler.remembered_identities == frozenset()


def test_expired_pause_and_finished_failure_streak_are_forgotten() -> None:
    recovery = Recovery(open_failure_backoff=Backoff(initial=10.0, maximum=100.0, jitter=0.0))
    scheduler = Scheduler(Topology(browsers=1), recovery=recovery)
    scheduler.cool_down("mail:paused", until=50.0)
    scheduler.open_failed("mail:failed", now=0.0)  # пауза до 10
    scheduler.block("mail:banned", reason="бан")

    scheduler.assign(now=60.0)
    assert scheduler.remembered_identities == {
        "mail:failed",
        "mail:banned",
    }  # серия неудач ещё помнится
    assert scheduler.open_failed("mail:failed", now=60.0) == 80.0  # и вторая пауза длиннее: 20

    scheduler.assign(now=300.0)  # после конца паузы прошло больше самой длинной паузы
    assert scheduler.remembered_identities == {"mail:banned"}
    scheduler.unblock("mail:banned")
    assert scheduler.remembered_identities == frozenset()


def test_generations_never_repeat_across_identities() -> None:
    scheduler = Scheduler(Topology(browsers=1, pages_per_browser=4))

    first = scheduler.request([Identity(key="mail:a")], now=0.0).grant
    second = scheduler.request([Identity(key="mail:b")], now=0.0).grant

    assert first is not None
    assert second is not None
    assert first.generation < second.generation


def test_full_pool_does_not_scan_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = Scheduler(Topology(browsers=2, pages_per_browser=2, pages_per_identity=1))
    identities = [Identity(key=f"mail:{index}") for index in range(50)]
    holders = [scheduler.request(identities, now=0.0) for _ in range(4)]  # вся ёмкость занята
    for _ in range(20):
        scheduler.request(identities, now=0.0)
    tried = 0
    original = Scheduler._try  # pyright: ignore[reportPrivateUsage]

    def counting(self: Scheduler, waiter: Waiter, now: float) -> Grant | None:
        nonlocal tried
        tried += 1
        return original(self, waiter, now)

    monkeypatch.setattr(Scheduler, "_try", counting)

    assert scheduler.assign(now=1.0) == []
    assert tried == 0  # свободных вкладок нет — очередь не просматривается

    grant = holders[0].grant
    assert grant is not None
    scheduler.release(grant, now=2.0)
    served = scheduler.assign(now=2.0)

    assert len(served) == 1
    assert tried == 1  # освободилась одна вкладка — её получил первый в очереди, дальше не смотрели
