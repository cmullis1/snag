"""JobQueue's queue-wide state for the UI (subscribe_state) and holding new starts for the updater."""
import asyncio
import time
import uuid

import pytest

from snag.jobs.models import JobState
from snag.jobs.queue import QueueState
from tests.helpers import make_spec, wait_for


class StateRecorder:
    def __init__(self):
        self.states: list[QueueState] = []

    def __call__(self, state: QueueState) -> None:
        self.states.append(state)

    def values(self, name: str) -> list:
        """The successive distinct values of one field."""
        result = []
        for state in self.states:
            value = getattr(state, name)
            if not result or result[-1] != value:
                result.append(value)
        return result


def watch(queue) -> StateRecorder:
    recorder = StateRecorder()
    queue.subscribe_state(recorder)
    return recorder


async def test_the_initial_state(queue_env):
    queue = queue_env.make_queue(max_concurrent=3)
    assert queue.state == QueueState(
        paused=False, held=False, throttled=False, throttled_until=None, current_limit=3, running=0, waiting=0,
    )


async def test_pause_and_resume_are_reported(queue_env):
    queue = queue_env.make_queue()
    states = watch(queue)
    queue.pause()
    queue.pause()  # no change, nothing reported
    queue.resume()
    assert [s.paused for s in states.states] == [True, False]


async def test_a_limit_change_is_reported(queue_env):
    queue = queue_env.make_queue(max_concurrent=5)
    states = watch(queue)
    queue.set_max_concurrent(2)
    queue.set_max_concurrent(2)
    assert [s.current_limit for s in states.states] == [2]


async def test_jobs_starting_and_ending_are_reported(queue_env):
    queue = queue_env.make_queue(max_concurrent=1)
    queue.pause()
    states = watch(queue)
    first = queue.add(make_spec("fake://success", queue_env.dest))
    queue.add(make_spec("fake://success", queue_env.dest))
    assert [(s.running, s.waiting) for s in states.states] == [(0, 1), (0, 2)]
    queue.resume()
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert first.state is JobState.DONE
    counts = [(s.running, s.waiting) for s in states.states]
    # One job ending and the next one starting in its place is a single change.
    assert counts[2:] == [(1, 1), (1, 0), (0, 0)]
    assert queue.state.running == queue.state.waiting == 0


async def test_every_reported_state_is_a_change(queue_env):
    queue = queue_env.make_queue(max_concurrent=2)
    states = watch(queue)
    jobs = [queue.add(make_spec("fake://success", queue_env.dest)) for _ in range(3)]
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert all(job.state is JobState.DONE for job in jobs)
    assert all(a != b for a, b in zip(states.states, states.states[1:]))
    assert states.states[-1] == queue.state


async def test_throttling_and_its_end_are_reported_on_time(queue_env):
    queue = queue_env.make_queue(max_concurrent=3, throttle_pause=0.4, throttle_reduced=0.6)
    states = watch(queue)
    before = time.time()
    job = queue.add(make_spec(f"fake://throttle-once/{uuid.uuid4().hex}", queue_env.dest))
    await wait_for(lambda: queue.throttled)
    throttled = next(s for s in states.states if s.throttled)
    # Wall-clock seconds, so the UI can show when downloads start again.
    assert before + 0.4 <= throttled.throttled_until <= time.time() + 0.4
    assert throttled.current_limit == 1 and throttled.waiting == 1
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    # The end of the reduced period is reported although nothing is waiting by then.
    await wait_for(lambda: queue.state.current_limit == 3, timeout=5)
    await wait_for(lambda: states.states[-1].current_limit == 3, timeout=1)
    assert states.values("throttled") == [False, True, False]
    assert [s.throttled_until for s in states.states if not s.throttled] == [None] * sum(
        1 for s in states.states if not s.throttled
    )
    assert states.values("current_limit") == [3, 1, 3]


async def test_the_end_of_a_throttle_is_reported_while_paused(queue_env):
    queue = queue_env.make_queue(throttle_pause=0.3, throttle_reduced=0.3)
    states = watch(queue)
    queue.add(make_spec(f"fake://throttle-once/{uuid.uuid4().hex}", queue_env.dest))
    await wait_for(lambda: queue.throttled)
    queue.pause()  # nothing starts, but the state still changes when the pause ends
    await wait_for(lambda: states.states[-1].current_limit == 5 and not states.states[-1].throttled, timeout=5)
    assert states.states[-1].waiting == 1


async def test_a_state_listener_that_raises_does_not_stop_the_others(queue_env, caplog):
    queue = queue_env.make_queue()

    def broken(state):
        raise RuntimeError("boom")

    queue.subscribe_state(broken)
    states = watch(queue)
    queue.pause()
    assert [s.paused for s in states.states] == [True]
    assert "A queue state listener failed" in caplog.text


async def test_a_change_made_by_a_state_listener_reaches_everyone_in_order(queue_env):
    queue = queue_env.make_queue()
    queue.subscribe_state(lambda state: state.paused and queue.resume())
    states = watch(queue)
    queue.pause()
    assert [s.paused for s in states.states] == [True, False]


# --- holding new starts for the updater ---

async def test_wait_running_done_returns_at_once_with_nothing_running(queue_env):
    queue = queue_env.make_queue()
    queue.pause()
    queue.add(make_spec("fake://success", queue_env.dest))  # waiting jobs don't count
    await asyncio.wait_for(queue.wait_running_done(), 1)


async def test_hold_lets_running_jobs_finish_and_starts_nothing_new(queue_env):
    queue = queue_env.make_queue(max_concurrent=1)
    states = watch(queue)
    running = queue.add(make_spec("fake://medium", queue_env.dest))
    await wait_for(lambda: running.state is JobState.DOWNLOADING)
    queue.hold()
    assert queue.held and not queue.paused
    later = queue.add(make_spec("fake://success", queue_env.dest))
    queue.set_max_concurrent(3)  # still nothing new starts
    await asyncio.wait_for(queue.wait_running_done(), 15)
    assert running.state is JobState.DONE
    await asyncio.sleep(0.2)
    assert later.state is JobState.WAITING
    queue.release()
    assert not queue.held
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert later.state is JobState.DONE
    assert states.values("held") == [False, True, False]


async def test_release_does_not_undo_the_users_pause(queue_env):
    queue = queue_env.make_queue()
    queue.pause()
    queue.hold()
    queue.release()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.sleep(0.2)
    assert queue.paused and job.state is JobState.WAITING


async def test_resume_does_not_undo_a_hold(queue_env):
    queue = queue_env.make_queue()
    queue.hold()
    queue.pause()
    queue.resume()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.sleep(0.2)
    assert queue.held and job.state is JobState.WAITING
    queue.release()
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE


async def test_holds_nest(queue_env):
    queue = queue_env.make_queue()
    queue.hold()
    queue.hold()
    queue.release()
    assert queue.held
    queue.release()
    assert not queue.held
    with pytest.raises(RuntimeError):
        queue.release()
