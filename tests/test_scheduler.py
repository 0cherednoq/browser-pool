"""Планировщик: очередь, кандидаты, размещение, лимиты, поколения, вывод из работы."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import override

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,  # pyright: ignore[reportUnknownVariableType] — у hypothesis параметры без аннотаций
)

from browser_pool._core.scheduler import Grant, Scheduler, Waiter
from browser_pool.config import GroupLimit, Limits, Topology
from browser_pool.errors import PoolInvariantError, PoolSaturatedError
from browser_pool.identity import Identity
from browser_pool.snapshot import BrowserState

A, B, C, D = (Identity(key=f"mail:{name}") for name in "abcd")


def scheduler(**topology: int | None) -> Scheduler:
    defaults: dict[str, int | None] = {
        "browsers": 1,
        "pages_per_browser": 4,
        "contexts_per_browser": 4,
    }
    return Scheduler(Topology(**{**defaults, **topology}))  # pyright: ignore[reportArgumentType] — числа топологии


def ask(pool: Scheduler, *candidates: Identity, now: float = 0.0) -> Waiter:
    return pool.submit(candidates, now=now)


def granted(pool: Scheduler, now: float = 0.0) -> list[Grant]:
    return [waiter.grant for waiter in pool.assign(now=now) if waiter.grant is not None]


def only(grants: Sequence[Grant]) -> Grant:
    assert len(grants) == 1
    return grants[0]


# --- выдача и возврат ------------------------------------------------------------------


def test_first_lease_opens_a_context_and_next_reuse_it() -> None:
    pool = scheduler()
    ask(pool, A)
    ask(pool, A)

    first, second = granted(pool)

    assert first.identity.key == second.identity.key == "mail:a"
    assert first.generation == second.generation == 1
    assert first.new_context
    assert not second.new_context
    assert first.lease_id != second.lease_id
    assert [context.active for context in pool.contexts()] == [2]


def test_released_slot_goes_to_the_oldest_waiter() -> None:
    pool = scheduler(pages_per_browser=1)
    ask(pool, A)
    held = only(granted(pool))
    first, second = ask(pool, A), ask(pool, A)
    assert granted(pool) == []

    pool.release(held, now=1.0)
    served = pool.assign(now=1.0)

    assert served == [first]
    assert second in pool.waiting()


def test_release_twice_is_an_invariant_violation() -> None:
    pool = scheduler()
    ask(pool, A)
    grant = only(granted(pool))
    pool.release(grant, now=0.0)

    with pytest.raises(PoolInvariantError):
        pool.release(grant, now=0.0)


# --- кандидаты -------------------------------------------------------------------------


def test_candidate_with_open_context_wins_over_order() -> None:
    pool = scheduler()
    ask(pool, B)
    only(granted(pool))

    ask(pool, A, B)

    assert only(granted(pool)).identity.key == "mail:b"


def test_first_candidate_gets_the_new_context() -> None:
    pool = scheduler()
    ask(pool, C, A, B)

    assert only(granted(pool)).identity.key == "mail:c"


def test_full_candidate_is_skipped_for_the_next_one() -> None:
    pool = scheduler(pages_per_identity=1)
    ask(pool, A)
    only(granted(pool))

    ask(pool, A, B)

    assert only(granted(pool)).identity.key == "mail:b"


@pytest.mark.parametrize(
    ("candidates", "fragment"),
    [((), "кандидат"), ((A, Identity(key="mail:a", payload=1)), "mail:a")],
)
def test_bad_candidate_lists_rejected(candidates: tuple[Identity, ...], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        scheduler().submit(candidates, now=0.0)


# --- размещение и лимиты ---------------------------------------------------------------


def test_new_context_goes_to_the_roomiest_browser() -> None:
    pool = scheduler(browsers=2)
    ask(pool, A)
    ask(pool, A)
    ask(pool, B)

    grants = granted(pool)

    browsers = {grant.identity.key: grant.browser_id for grant in grants}
    assert browsers["mail:a"] != browsers["mail:b"]


def test_browser_page_capacity_is_never_exceeded() -> None:
    pool = scheduler(pages_per_browser=2)
    for _ in range(3):
        ask(pool, A)
    ask(pool, B)

    assert len(granted(pool)) == 2
    assert pool.browsers()[0].active == 2
    assert len(pool.waiting()) == 2


def test_identity_limit_takes_the_strictest_source() -> None:
    pool = scheduler(pages_per_browser=8, pages_per_identity=3)
    strict = Identity(key="mail:a", max_pages=2)
    for _ in range(4):
        ask(pool, strict)

    assert len(granted(pool)) == 2


def test_context_limit_evicts_the_least_recently_used_idle_context() -> None:
    pool = scheduler(contexts_per_browser=2)
    for identity, moment in ((A, 1.0), (B, 2.0)):
        ask(pool, identity, now=moment)
        pool.release(only(granted(pool, now=moment)), now=moment)

    ask(pool, C, now=3.0)
    grant = only(granted(pool, now=3.0))

    assert grant.identity.key == "mail:c"
    closing = pool.take_closures()
    assert [(context.key, context.generation) for context in closing] == [("mail:a", 1)]
    assert {context.key for context in pool.contexts() if not context.retiring} == {
        "mail:b",
        "mail:c",
    }


def test_context_limit_with_every_context_busy_makes_new_identity_wait() -> None:
    pool = scheduler(contexts_per_browser=2)
    ask(pool, A)
    ask(pool, B)
    granted(pool)

    ask(pool, C)

    assert granted(pool) == []
    assert pool.take_closures() == []


def test_unhealthy_browser_gets_no_new_leases() -> None:
    pool = scheduler(browsers=2)
    ask(pool, A)
    on_first = only(granted(pool))
    pool.set_browser_state(on_first.browser_id, BrowserState.quarantined)

    ask(pool, B)
    ask(pool, A)
    grants = granted(pool)

    assert [grant.identity.key for grant in grants] == ["mail:b"]
    assert grants[0].browser_id != on_first.browser_id


# --- вывод из работы и поколения -------------------------------------------------------


def test_retired_context_closes_after_its_last_lease() -> None:
    pool = scheduler()
    ask(pool, A)
    grant = only(granted(pool))

    assert pool.retire("mail:a", generation=grant.generation)
    ask(pool, A)
    assert granted(pool) == []  # дорабатывает только выданное
    assert pool.take_closures() == []

    pool.release(grant, now=1.0)
    (closing,) = pool.take_closures()
    pool.forget(closing.key, generation=closing.generation)
    fresh = only(granted(pool, now=1.0))

    assert fresh.new_context
    assert fresh.generation == grant.generation + 1


def test_retire_of_a_stale_generation_is_ignored() -> None:
    pool = scheduler()
    ask(pool, A)
    grant = only(granted(pool))

    assert not pool.retire("mail:a", generation=grant.generation + 1)
    assert not pool.retire("mail:x", generation=1)
    assert not pool.contexts()[0].retiring


def test_closure_is_handed_out_once() -> None:
    pool = scheduler()
    ask(pool, A)
    grant = only(granted(pool))
    pool.retire("mail:a", generation=grant.generation)
    pool.release(grant, now=0.0)

    assert len(pool.take_closures()) == 1
    assert pool.take_closures() == []


def test_cancelled_waiter_leaves_the_queue() -> None:
    pool = scheduler(pages_per_browser=1)
    ask(pool, A)
    granted(pool)
    waiter = ask(pool, B)

    pool.cancel(waiter)

    assert pool.waiting() == []


# --- инварианты на случайных сценариях -------------------------------------------------

MAIL_CAP = GroupLimit(label="service", value="mail", max_pages=3, max_contexts=2)
IDENTITIES = [
    Identity(
        key=f"acc:{index % 5}",
        variant=("proxy", "direct")[index // 5],
        max_pages=(index % 3) or None,
        labels={"service": "mail" if index % 2 else "shop"},
    )
    for index in range(10)
]


class SchedulerMachine(RuleBasedStateMachine):
    """Случайные заявки, возвраты, вывод из работы, закрытия, отмены — с вариантами и группами."""

    def __init__(self) -> None:
        super().__init__()
        self.pool = Scheduler(
            Topology(browsers=2, pages_per_browser=3, contexts_per_browser=2, pages_per_identity=2),
            limits=Limits(max_waiting=4, groups=(MAIL_CAP,)),
        )
        self.now = 0.0
        self.active: list[Grant] = []
        self.released: set[int] = set()

    def _take(self, grant: Grant | None) -> None:
        if grant is not None:
            assert grant.lease_id not in self.released
            self.active.append(grant)

    @rule(
        picks=st.lists(
            st.sampled_from(IDENTITIES), min_size=1, max_size=3, unique_by=lambda i: i.key
        ),
        priority=st.integers(min_value=0, max_value=2),
    )
    def request(self, picks: list[Identity], priority: int) -> None:
        try:
            waiter = self.pool.request(picks, now=self.now, priority=priority)
        except PoolSaturatedError:
            assert len(self.pool.waiting()) == 4
            return
        self._take(waiter.grant)

    @rule(pick=st.sampled_from(IDENTITIES))
    def request_whole_context(self, pick: Identity) -> None:
        try:
            waiter = self.pool.request([pick], now=self.now, exclusive=True)
        except PoolSaturatedError:
            return
        self._take(waiter.grant)

    @rule()
    def assign(self) -> None:
        self.now += 1
        for waiter in self.pool.assign(now=self.now):
            self._take(waiter.grant)

    @precondition(lambda self: self.active)
    @rule(index=st.integers(min_value=0))
    def release(self, index: int) -> None:
        grant = self.active.pop(index % len(self.active))
        self.pool.release(grant, now=self.now)
        self.released.add(grant.lease_id)

    @precondition(lambda self: self.pool.contexts())
    @rule(index=st.integers(min_value=0))
    def retire(self, index: int) -> None:
        contexts = self.pool.contexts()
        context = contexts[index % len(contexts)]
        self.pool.retire(context.key, generation=context.generation)

    @rule()
    def close(self) -> None:
        for context in self.pool.take_closures():
            assert context.active == 0
            self.pool.forget(context.key, generation=context.generation)

    @rule(waiting=st.integers(min_value=0))
    def abandon(self, waiting: int) -> None:
        queue = self.pool.waiting()
        if queue:
            self.pool.abandon(queue[waiting % len(queue)], now=self.now)

    @invariant()
    def browser_capacity_holds(self) -> None:
        for browser in self.pool.browsers():
            assert 0 <= browser.active <= browser.capacity
            on_browser = [c for c in self.pool.contexts() if c.browser_id == browser.id]
            assert browser.active == sum(context.active for context in on_browser)
            assert sum(not context.retiring for context in on_browser) <= 2

    @invariant()
    def identity_limits_hold(self) -> None:
        for context in self.pool.contexts():
            assert 0 <= context.active <= context.limit

    @invariant()
    def identity_lives_in_one_context(self) -> None:
        keys = [context.key for context in self.pool.contexts()]
        assert len(keys) == len(set(keys))

    @invariant()
    def group_caps_hold(self) -> None:
        mail = [c for c in self.pool.contexts() if ("service", "mail") in c.labels]
        assert sum(context.active for context in mail) <= 3
        assert sum(not context.retiring for context in mail) <= 2

    @invariant()
    def queue_limit_holds(self) -> None:
        assert len(self.pool.waiting()) <= 4

    @invariant()
    def whole_context_lease_is_alone(self) -> None:
        for grant in self.active:
            if grant.exclusive:
                (context,) = [c for c in self.pool.contexts() if c.key == grant.identity.key]
                assert context.generation == grant.generation
                assert context.active == 1

    @invariant()
    def every_active_lease_is_counted(self) -> None:
        assert sum(context.active for context in self.pool.contexts()) == len(self.active)


def test_invariants_hold_on_random_scenarios() -> None:
    run_state_machine_as_test(
        SchedulerMachine, settings=settings(max_examples=150, stateful_step_count=40, deadline=None)
    )


# --- инварианты планировщика с владельцами ---------------------------------------------


def _owner(identity: Identity) -> Hashable:
    return ("direct",) if identity.variant == "direct" else ("identity", identity.key)


class OwnedSchedulerMachine(SchedulerMachine):
    """Те же случайные сценарии, но у браузера есть владелец: «напрямую» — общий, остальные — свои."""

    def __init__(self) -> None:
        super().__init__()
        self.pool = Scheduler(
            Topology(browsers=2, pages_per_browser=3, contexts_per_browser=2, pages_per_identity=2),
            limits=Limits(max_waiting=4, groups=(MAIL_CAP,)),
            owner=_owner,
        )
        self.opened: dict[tuple[str, int], Identity] = {}

    @override
    def _take(self, grant: Grant | None) -> None:
        super()._take(grant)
        if grant is not None and grant.new_context:
            self.opened[grant.identity.key, grant.generation] = grant.identity

    @invariant()
    def browser_serves_one_owner(self) -> None:
        for browser in self.pool.browsers():
            owners = {
                _owner(self.opened[context.key, context.generation])
                for context in self.pool.contexts()
                if context.browser_id == browser.id
            }
            assert len(owners) <= 1


def test_owner_invariants_hold_on_random_scenarios() -> None:
    run_state_machine_as_test(
        OwnedSchedulerMachine,
        settings=settings(max_examples=150, stateful_step_count=40, deadline=None),
    )
