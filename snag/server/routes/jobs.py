"""Downloads, batches (playlists) and the queue.

POST /api/jobs        {url, format_id, audio_id?, extras?, dest_dir?, cookies?}  -> 201 Job
POST /api/jobs/quick  {url, cookies?}                                            -> 201 Job
POST /api/jobs/batch  {url, entries: [index], quality: {kind: "quick", height: int | null} |
                       {kind: "mp3"} | {kind: "best_audio"}, extras?, dest_dir?, cookies?}
                                                                       -> 201 {batch_id, jobs: [Job]}
GET  /api/jobs                                   -> {jobs: [Job]}, oldest first
POST /api/jobs/{id}/cancel                       -> Job (a waiting job is cancelled at once; a running
                                                    one is stopping, and turns cancelled in a job event)
POST /api/jobs/{id}/retry  {dest_dir?}           -> Job
POST /api/jobs/{id}/clear                        -> {cleared: [id]}   a done, failed or cancelled job
                                                    (a done job's file stays); waiting or running -> 409
POST /api/jobs/{id}/open-folder                  -> {opened: path}    Explorer with a done job's file
                                                    selected, or else (the file gone, or the job not
                                                    done) its folder; a done job's folder gone -> 404,
                                                    an unfinished job's folder not made yet -> 409
POST /api/jobs/clear-finished                    -> {cleared: [id]}   (done and cancelled jobs)
GET  /api/batches/{id}                           -> BatchSummary {total, done, failed, cancelled, pending}
POST /api/batches/{id}/retry-failed              -> {jobs: [Job]}     (the jobs retried)
POST /api/queue/pause, /api/queue/resume; GET /api/queue  -> Queue

Every download is looked up first (cached: the UI asks by the address it looked up, so this is that
lookup), so its title, site, video id and channel are the lookup's, and it is queued with the
lookup's page URL. A picked dest_dir (a full path on a drive or a
network share; see snag.store.settings.folder_problem) is remembered (remembered_dirs), so the
startup sweep looks there for leftover working folders. Invalid extras -> 400 invalid_extras (see
snag.server.schemas.parse_extras). Unknown job or batch -> 404; a job in the wrong state for the
action -> 409."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from snag.info.models import FormatRow, PlaylistInfo, VideoInfo
from snag.jobs.models import ChoiceKind, Extras, FormatChoice, Job, JobSpec, JobState
from snag.jobs.naming import playlist_dest
from snag.server.routes import bad_request, conflict, invalid_extras, json_body, not_found, sentence
from snag.server.routes.lookup import core_of, look_up, look_up_video, take_cookies, url_of
from snag.server.routes.settings import remember_folder
from snag.server.schemas import (
    InvalidExtras, batch_summary_json, job_json, parse_extras, parse_job_request, queue_state_json,
)
from snag.store.settings import ALLOWED_HEIGHTS, default_dest, validate_folder

if TYPE_CHECKING:
    from snag.app import SnagCore

router = APIRouter()


def created(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse(data, status_code=201)


def checked_folder(value: Any) -> Path | None:
    """A dest_dir from a request: None, or a folder Snag can save to; anything else -> 400."""
    if value is None:
        return None
    try:
        return validate_folder(value)
    except ValueError as error:
        raise bad_request(str(error)) from None


async def picked_folder(request: Request, value: Any) -> Path | None:
    """checked_folder, and the folder is remembered."""
    folder = checked_folder(value)
    if folder is not None:
        await remember_folder(request, str(folder))
    return folder


def _extras(value: Any) -> Extras:
    try:
        return parse_extras(value)
    except InvalidExtras as problem:
        raise invalid_extras(problem.message, problem.details) from None


def estimated_size(info: VideoInfo, body: dict[str, Any]) -> int | None:
    """What the chosen row will download, for the free-space check: the row's size, with another
    audio track's size in place of its paired one when audio_id overrides it. None when unknown."""
    formats = info.formats
    row = {r.format_id: r for r in (*formats.video_all, *formats.audio)}.get(body.get("format_id"))
    if row is None or row.size is None:
        return None
    audio_id = body.get("audio_id")
    if audio_id is None:
        return row.size
    tracks: dict[str, FormatRow] = {r.format_id: r for r in formats.audio_tracks}
    chosen = tracks.get(audio_id)
    if chosen is None or chosen.size is None:
        return None
    if row.audio_id is None:
        return row.size + chosen.size
    paired = tracks.get(row.audio_id)
    if paired is None or paired.size is None:
        return None
    return row.size - paired.size + chosen.size


def quick_estimate(info: VideoInfo, max_height: int | None) -> int | None:
    """The size of the best simple-view row yt-dlp would pick under the height limit (a guide)."""
    for row in info.formats.video_simple:
        if max_height is None or (row.height is not None and row.height <= max_height):
            return row.size
    return None


def spec_for(info: VideoInfo, choice: FormatChoice, extras: Extras, dest: Path, size: int | None) -> JobSpec:
    return JobSpec(
        url=info.url, title=info.title, site=info.site, video_id=info.video_id, choice=choice, extras=extras,
        dest_dir=dest, channel=info.channel, estimated_size=size,
    )


# ----- adding downloads -----

@router.post("/api/jobs")
async def add_job(request: Request) -> JSONResponse:
    core = core_of(request)
    body = await json_body(request)
    info = await look_up_video(core, body)
    try:
        choice, extras, dest_dir = parse_job_request(body, info)
    except InvalidExtras as problem:
        raise invalid_extras(problem.message, problem.details) from None
    except ValueError as error:
        raise bad_request(str(error)) from None
    if dest_dir is not None:
        dest = await picked_folder(request, str(dest_dir))
    else:
        dest = default_dest(core.settings.get(), choice)
    job = core.queue.add(spec_for(info, choice, extras, dest, estimated_size(info, body)))
    return created(job_json(job))


@router.post("/api/jobs/quick")
async def add_quick_job(request: Request) -> JSONResponse:
    """The quick-download default (right-click, the YouTube button): best video up to the setting's
    height, the quick extras, the video folder."""
    core = core_of(request)
    body = await json_body(request)
    info = await look_up_video(core, body)
    settings = core.settings.get()
    choice = FormatChoice(ChoiceKind.QUICK, height=settings.quick_max_height)
    spec = spec_for(info, choice, settings.quick_extras, Path(settings.video_dir),
                    quick_estimate(info, settings.quick_max_height))
    return created(job_json(core.queue.add(spec)))


def _quality(value: Any) -> FormatChoice:
    if not isinstance(value, dict):
        raise bad_request('quality must be {"kind": "quick", "height": ...}, {"kind": "mp3"} or {"kind": "best_audio"}.')
    kind = value.get("kind")
    if kind == "mp3":
        return FormatChoice(ChoiceKind.MP3)
    if kind == "best_audio":
        return FormatChoice(ChoiceKind.BEST_AUDIO)
    if kind == "quick":
        height = value.get("height")
        if height is not None and (type(height) is not int or height not in ALLOWED_HEIGHTS):
            raise bad_request(f"The height must be null (the best there is) or one of {list(ALLOWED_HEIGHTS)}.")
        return FormatChoice(ChoiceKind.QUICK, height=height)
    raise bad_request("quality's kind must be quick, mp3 or best_audio.")


def _picked_entries(value: Any, info: PlaylistInfo) -> list:
    by_index = {entry.index: entry for entry in info.entries}
    if not isinstance(value, list) or not value:
        raise bad_request("Tick at least one video (entries must list their indexes).")
    if any(type(index) is not int or index not in by_index for index in value):
        raise bad_request("Some of those videos aren't in this playlist. Look it up again.")
    if len(set(value)) != len(value):
        raise bad_request("Each video may be ticked only once.")
    return [by_index[index] for index in value]


@router.post("/api/jobs/batch")
async def add_batch(request: Request) -> JSONResponse:
    """One job per ticked playlist entry, all in one batch, saved in a folder named after the playlist
    inside the picked folder (or the video or audio folder)."""
    core = core_of(request)
    body = await json_body(request)
    url = url_of(body)
    info = await look_up(core, url, take_cookies(core, url, body))
    if not isinstance(info, PlaylistInfo):
        raise bad_request("This link isn't a playlist.")
    entries = _picked_entries(body.get("entries"), info)
    choice = _quality(body.get("quality"))
    extras = _extras(body.get("extras"))
    base = await picked_folder(request, body.get("dest_dir")) or default_dest(core.settings.get(), choice)
    dest = playlist_dest(base, info.title)
    batch_id = core.queue.new_batch_id()
    count = len(info.entries)
    specs = [
        JobSpec(
            url=entry.url, title=entry.title, site=info.site, video_id=entry.video_id, choice=choice,
            extras=extras, dest_dir=dest, batch_id=batch_id, playlist_index=entry.index, playlist_count=count,
            playlist_item=entry.item_index,
        )
        for entry in entries
    ]
    jobs = core.queue.add_batch(specs)
    return created({"batch_id": batch_id, "jobs": [job_json(job) for job in jobs]})


# ----- managing jobs -----

@router.get("/api/jobs")
async def list_jobs(request: Request) -> dict[str, Any]:
    return {"jobs": [job_json(job) for job in core_of(request).queue.jobs]}


def _no_such_job():
    return not_found("There's no such download. It may have been cleared.")


def _job(core: SnagCore, job_id: str) -> Job:
    job = core.queue.get(job_id)
    if job is None:
        raise _no_such_job()
    return job


@router.post("/api/jobs/clear-finished")
async def clear_finished(request: Request) -> dict[str, Any]:
    queue = core_of(request).queue
    before = [job.id for job in queue.jobs]
    queue.clear_finished()
    remaining = {job.id for job in queue.jobs}
    return {"cleared": [job_id for job_id in before if job_id not in remaining]}


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    core = core_of(request)
    job = _job(core, job_id)
    try:
        await core.queue.cancel(job_id)
    except KeyError:  # cleared meanwhile
        raise _no_such_job() from None
    return job_json(core.queue.get(job_id) or job)


@router.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str, request: Request) -> dict[str, Any]:
    core = core_of(request)
    body = await json_body(request, required=False)
    _job(core, job_id)
    folder = checked_folder(body.get("dest_dir"))
    try:
        job = core.queue.retry(job_id, folder)
    except KeyError:
        raise _no_such_job() from None
    except ValueError as error:
        raise conflict(sentence(str(error))) from None
    if folder is not None:
        await remember_folder(request, str(folder))
    return job_json(job)


@router.post("/api/jobs/{job_id}/clear")
async def clear_job(job_id: str, request: Request) -> dict[str, Any]:
    core = core_of(request)
    _job(core, job_id)
    try:
        await core.queue.clear(job_id)
    except KeyError:  # cleared meanwhile
        raise _no_such_job() from None
    except ValueError as error:
        raise conflict(sentence(str(error))) from None
    return {"cleared": [job_id]}


def _job_target(job: Job) -> Path:
    """What Explorer shows for a job: a done job's file, or else its folder. Raises 404 when a done
    job's folder is gone and 409 when an unfinished job's folder isn't made yet."""
    if job.state is JobState.DONE:
        if job.file_path is not None and job.file_path.is_file():
            return job.file_path
        folder = job.file_path.parent if job.file_path is not None else job.spec.dest_dir
        if folder.is_dir():
            return folder
        raise not_found(f"The folder {folder} isn't there anymore.")
    if job.spec.dest_dir.is_dir():
        return job.spec.dest_dir
    raise conflict("This download hasn't made its folder yet.")


@router.post("/api/jobs/{job_id}/open-folder")
async def open_job_folder(job_id: str, request: Request) -> dict[str, Any]:
    job = _job(core_of(request), job_id)
    # Off the loop: the checks may touch a slow or sleeping drive, and Explorer takes a moment to start.
    target = await asyncio.to_thread(_job_target, job)
    try:
        await asyncio.to_thread(request.app.state.platform.open_in_explorer, target)
    except FileNotFoundError:  # gone between the check and Explorer
        raise not_found(f"{target} isn't there anymore.") from None
    return {"opened": str(target)}


# ----- batches -----

def _summary(core: SnagCore, batch_id: str):
    summary = core.queue.batch_summary(batch_id)
    if summary.total == 0:
        raise not_found("There's no such batch of downloads.")
    return summary


@router.get("/api/batches/{batch_id}")
async def batch_summary(batch_id: str, request: Request) -> dict[str, Any]:
    return batch_summary_json(_summary(core_of(request), batch_id))


@router.post("/api/batches/{batch_id}/retry-failed")
async def retry_failed(batch_id: str, request: Request) -> dict[str, Any]:
    core = core_of(request)
    _summary(core, batch_id)
    return {"jobs": [job_json(job) for job in core.queue.retry_failed(batch_id)]}


# ----- the queue -----

@router.post("/api/queue/pause")
async def pause(request: Request) -> dict[str, Any]:
    queue = core_of(request).queue
    queue.pause()
    return queue_state_json(queue.state)


@router.post("/api/queue/resume")
async def resume(request: Request) -> dict[str, Any]:
    queue = core_of(request).queue
    queue.resume()
    return queue_state_json(queue.state)


@router.get("/api/queue")
async def queue_state(request: Request) -> dict[str, Any]:
    return queue_state_json(core_of(request).queue.state)
