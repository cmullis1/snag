import asyncio
from pathlib import Path
from typing import Callable

from snag.jobs.models import ChoiceKind, Extras, FormatChoice, Job, JobSpec, JobState


def make_spec(url: str, dest: Path, **overrides) -> JobSpec:
    values = dict(
        url=url, title="Fake Video", site="youtube", video_id="abc123",
        choice=FormatChoice(ChoiceKind.VIDEO, "299", "140", 1080, "mp4"),
        extras=Extras(), dest_dir=dest,
    )
    values.update(overrides)
    return JobSpec(**values)


async def wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.02)


class Recorder:
    """Listener that records every (job id, state, percent) it is told about."""

    def __init__(self):
        self.events: list[tuple[str, JobState, float | None]] = []

    def __call__(self, job: Job) -> None:
        self.events.append((job.id, job.state, job.percent))

    def states(self, job_id: str) -> list[JobState]:
        result: list[JobState] = []
        for event_id, state, _ in self.events:
            if event_id == job_id and (not result or result[-1] != state):
                result.append(state)
        return result
