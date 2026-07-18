import asyncio
import logging
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Set, Tuple

from sqlalchemy import and_, or_
from sqlmodel import col, select

from gpustack.config.config import get_global_config
from gpustack.routes.videos import _output_root
from gpustack.schemas.video_generation_task import (
    VIDEO_TASK_TERMINAL_STATES,
    VideoGenerationTask,
    VideoTaskStateEnum,
)
from gpustack.server.db import async_session

logger = logging.getLogger(__name__)

# A day-partition directory is <root>/<feature>/YYYY/MM/DD or
# <root>/inputs/<feature>/YYYY/MM/DD (§7.2 / §7.7).
_DAY_GLOBS = (
    "*/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]",
    "inputs/*/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]",
)

# Engine input-path fields persisted in a task's params (materialized under
# <root>/inputs/...). Their day dirs must be protected too, not just the output.
# Must list EVERY engine path field the facade maps inputs to (routes/videos.py
# _INPUT_FIELDS) — a missing one lets a queued/requeued job's input day dir get
# TTL/watermark evicted, so the re-dispatch fails on a vanished input.
_INPUT_PATH_PARAM_KEYS = (
    "image_path",
    "last_frame_path",
    "image_mask_path",
    "audio_path",
    "video_path",
    # TTS (IndexTTS): reference voice + optional emotion clip.
    "spk_audio_path",
    "emo_audio_path",
    # VACE (video editing) — these engine fields carry no _path suffix. src_video
    # / src_mask are single NFS paths; src_ref_images is comma-separated (handled
    # by the split("," ) below like multi-image image_path).
    "src_video",
    "src_mask",
    "src_ref_images",
    # Music (ACE-Step): cover reference audio / repaint source audio.
    "reference_audio_path",
    "src_audio_path",
)

# DONE tasks stay protected for this long after their last update so watermark
# eviction can't delete a result new-api hasn't fetched yet (the day-granular
# "skip today" floor alone would evict yesterday-23:59 results at 00:05).
_DONE_FETCH_GRACE_HOURS = 6


class VideoStorageJanitor:
    """Reclaims LightX2V NFS output space (§7.6). Leader-only, filesystem +
    DB only (no HTTP). Three protections compose so it never deletes live work:

    1. **Day-partition TTL** — delete whole ``YYYY/MM/DD`` dirs older than
       ``lightx2v_retention_days`` (O(days), no per-file stat). This is what the
       external ``find -mtime`` cron did, but Config-driven and in-process.
    2. **Watermark eviction** — when the output filesystem exceeds the high
       watermark, evict oldest day dirs first down to the low watermark. Time-only
       cron can't do this; it's the guard against the NFS filling before TTL.
    3. **Safety gates** — never touch today's dir (covers the <1h min-age rule),
       and never touch a day dir that still holds a non-terminal task's result.
    """

    def __init__(self, interval: int = 600):
        self._interval = interval

    async def start(self):
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._run_once()
            except Exception as e:
                logger.error(f"Video storage janitor failed: {e}", exc_info=True)

    async def _run_once(self):
        # All filesystem work (glob walks, rmtree, statvfs) targets NFS and runs
        # via to_thread: the janitor shares the API server's event loop, and a
        # blocking rmtree of a large day dir would stall every request.
        root = _output_root()
        if not await asyncio.to_thread(os.path.isdir, root):
            return
        cfg = get_global_config()

        day_dirs = await asyncio.to_thread(self._collect_day_dirs, root)
        if not day_dirs:
            return

        protected = await self._protected_day_dirs()
        today = datetime.now(timezone.utc).date()

        retention = getattr(cfg, "lightx2v_retention_days", None) if cfg else None
        removed = await asyncio.to_thread(
            self._sweep_ttl, day_dirs, protected, today, retention
        )

        # Refresh the working set (drop what TTL already deleted) before the
        # capacity pass.
        remaining = [(p, d) for (p, d) in day_dirs if p not in removed]
        await asyncio.to_thread(
            self._sweep_watermark, root, remaining, protected, today, cfg
        )

    def _collect_day_dirs(self, root: str) -> List[Tuple[Path, date]]:
        """Return (path, date) for every day-partition dir under the root."""
        base = Path(root)
        out: List[Tuple[Path, date]] = []
        for pattern in _DAY_GLOBS:
            for p in base.glob(pattern):
                if not p.is_dir():
                    continue
                try:
                    y, m, d = p.parts[-3:]
                    out.append((p, date(int(y), int(m), int(d))))
                except (ValueError, IndexError):
                    continue
        return out

    async def _protected_day_dirs(self) -> Set[Path]:
        """Day dirs holding a non-terminal task's result OR persisted input —
        never delete these. Covers both the output (nfs_path) and the input
        directories (params): a queued/running/requeued job still needs its
        materialized image/audio inputs when it (re)dispatches, and those live
        under inputs/... which the janitor also sweeps.

        DONE tasks are additionally protected for _DONE_FETCH_GRACE_HOURS after
        their last update, so watermark eviction can't delete results new-api
        hasn't fetched yet."""
        # Compare against enum members (the column persists the member name).
        members = [s for s in VideoTaskStateEnum if s not in VIDEO_TASK_TERMINAL_STATES]
        grace_cutoff = datetime.now(timezone.utc) - timedelta(
            hours=_DONE_FETCH_GRACE_HOURS
        )
        protected: Set[Path] = set()
        async with async_session() as session:
            rows = (
                await session.exec(
                    select(
                        VideoGenerationTask.nfs_path, VideoGenerationTask.params
                    ).where(
                        or_(
                            col(VideoGenerationTask.state).in_(members),
                            and_(
                                col(VideoGenerationTask.state)
                                == VideoTaskStateEnum.DONE,
                                col(VideoGenerationTask.updated_at) >= grace_cutoff,
                            ),
                        )
                    )
                )
            ).all()
        for nfs_path, params in rows:
            # <root>/<feature>/Y/M/D/<user>/<file> → day dir is parent.parent.
            if nfs_path:
                protected.add(Path(nfs_path).parent.parent)
            if isinstance(params, dict):
                for key in _INPUT_PATH_PARAM_KEYS:
                    value = params.get(key)
                    if not isinstance(value, str):
                        continue
                    # image_path may hold multiple comma-separated local paths
                    # (multi-image edit). Protect each one's day dir. Only local
                    # NFS paths (base64-materialized inputs) — URL inputs are
                    # downloaded by the engine, not on our disk.
                    for one in value.split(","):
                        one = one.strip()
                        if one.startswith("/"):
                            protected.add(Path(one).parent.parent)
        return protected

    def _sweep_ttl(
        self,
        day_dirs: List[Tuple[Path, date]],
        protected: Set[Path],
        today: date,
        retention_days,
    ) -> Set[Path]:
        removed: Set[Path] = set()
        if not retention_days or retention_days <= 0:
            return removed
        cutoff = today.toordinal() - int(retention_days)
        for path, d in day_dirs:
            if d.toordinal() >= cutoff or d >= today or path in protected:
                continue
            if self._remove(path):
                removed.add(path)
        if removed:
            logger.info(f"Janitor TTL removed {len(removed)} expired day dir(s)")
        return removed

    def _sweep_watermark(
        self,
        root: str,
        day_dirs: List[Tuple[Path, date]],
        protected: Set[Path],
        today: date,
        cfg,
    ):
        high = getattr(cfg, "lightx2v_storage_high_watermark", None) if cfg else None
        low = getattr(cfg, "lightx2v_storage_low_watermark", None) if cfg else None
        # None (unset/cleared via /config) disables eviction; 0.0 is a valid
        # watermark ("evict everything evictable"), so no falsy check here.
        if high is None or low is None:
            return
        if self._usage_ratio(root) <= high:
            return

        evicted = 0
        # Oldest first; skip today's dir (min-age) and protected dirs.
        for path, d in sorted(day_dirs, key=lambda x: x[1]):
            if d >= today or path in protected:
                continue
            if self._usage_ratio(root) <= low:
                break
            if self._remove(path):
                evicted += 1
        if evicted:
            logger.warning(
                f"Janitor watermark eviction removed {evicted} day dir(s) "
                f"(usage was above {high:.0%})"
            )

    def _usage_ratio(self, root: str) -> float:
        try:
            total, used, _free = shutil.disk_usage(root)
            return used / total if total else 0.0
        except OSError:
            return 0.0

    def _remove(self, path: Path) -> bool:
        try:
            shutil.rmtree(path)
            return True
        except OSError as e:
            logger.warning(f"Janitor failed to remove {path}: {e}")
            return False
