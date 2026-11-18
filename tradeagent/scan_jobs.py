"""Batch-Scan mit Fortschritt (Direktive 5A).

Der Scan über die USDT-Perp-Universe läuft in Batches. Nach jedem Symbol und
jedem Batch wird ein ScanProgress aktualisiert, den die API per Polling
ausliefert. Ein Symbolfehler stoppt den Scan nicht, er wird gezählt und gemeldet.
Die Signal-Engine wird hier nur aufgerufen, nicht verändert.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 20
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 100
MAX_KEPT_JOBS = 10
MAX_REPORTED_FAILURES = 100

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

ScanOne = Callable[[str], Optional[Any]]
Finalize = Callable[[List[Any], List[str]], Dict[str, Any]]


def clamp_batch_size(value: Optional[int]) -> int:
    try:
        size = int(value) if value is not None else DEFAULT_BATCH_SIZE
    except (TypeError, ValueError):
        size = DEFAULT_BATCH_SIZE
    return max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, size))


def make_batches(symbols: Sequence[str], batch_size: Optional[int]) -> List[List[str]]:
    size = clamp_batch_size(batch_size)
    items = list(symbols)
    return [items[i:i + size] for i in range(0, len(items), size)]


@dataclass
class ScanProgress:
    job_id: str
    total: int
    batch_size: int
    batch_count: int
    status: str = STATUS_RUNNING
    processed: int = 0
    failed: int = 0
    signals_found: int = 0
    batches_done: int = 0
    current_batch: List[str] = field(default_factory=list)
    failed_symbols: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def begin_batch(self, batch: Sequence[str]) -> None:
        with self._lock:
            self.current_batch = list(batch)

    def record(self, symbol: str, ok: bool, found: bool = False) -> None:
        with self._lock:
            self.processed += 1
            if not ok:
                self.failed += 1
                if len(self.failed_symbols) < MAX_REPORTED_FAILURES:
                    self.failed_symbols.append(symbol)
            elif found:
                self.signals_found += 1

    def end_batch(self) -> None:
        with self._lock:
            self.batches_done += 1
            self.current_batch = []

    def finish(self, status: str, result: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
        with self._lock:
            self.status = status
            self.result = result
            self.error = error
            self.current_batch = []
            self.finished_at = time.time()

    def to_dict(self, include_result: bool = True) -> Dict[str, Any]:
        with self._lock:
            if self.total > 0:
                percent = round(100.0 * self.processed / self.total, 1)
            else:
                percent = 0.0 if self.status == STATUS_RUNNING else 100.0
            end = self.finished_at or time.time()
            return {
                "job_id": self.job_id,
                "status": self.status,
                "total": self.total,
                "processed": self.processed,
                "failed": self.failed,
                "signals_found": self.signals_found,
                "percent": percent,
                "batch_size": self.batch_size,
                "batch_count": self.batch_count,
                "batches_done": self.batches_done,
                "current_batch": list(self.current_batch),
                "failed_symbols": list(self.failed_symbols),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_s": round(end - self.started_at, 1),
                "error": self.error,
                "result": self.result if include_result else None,
            }


OnFinish = Callable[[ScanProgress], None]


def run_batched_scan(
    symbols: Sequence[str],
    scan_one: ScanOne,
    progress: ScanProgress,
) -> Tuple[List[Any], List[str]]:
    """Scannt alle Symbole batchweise. Gibt (Signale, fehlgeschlagene Symbole) zurück."""
    signals: List[Any] = []
    failed: List[str] = []
    for batch in make_batches(symbols, progress.batch_size):
        progress.begin_batch(batch)
        for symbol in batch:
            try:
                sig = scan_one(symbol)
            except Exception as exc:  # Fail-safe: ein Symbol-Fehler stoppt nicht alle
                failed.append(symbol)
                progress.record(symbol, ok=False)
                logger.warning("[scan] %s Fehler: %s", symbol, exc)
                continue
            if sig is not None:
                signals.append(sig)
            progress.record(symbol, ok=True, found=sig is not None)
        progress.end_batch()
    return signals, failed


class ScanJobBusyError(RuntimeError):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__("Scan " + job_id + " läuft bereits.")


class ScanJobManager:
    """Hält laufende und letzte Scan-Jobs. Es läuft höchstens ein Job gleichzeitig."""

    def __init__(self, max_kept: int = MAX_KEPT_JOBS) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, ScanProgress] = {}
        self._order: List[str] = []
        self._active: Optional[str] = None
        self._max_kept = max(1, max_kept)

    def start(
        self,
        symbols: Sequence[str],
        batch_size: Optional[int],
        scan_one: ScanOne,
        finalize: Finalize,
        on_finish: Optional[OnFinish] = None,
        run_async: bool = True,
    ) -> ScanProgress:
        items = list(symbols)
        size = clamp_batch_size(batch_size)
        with self._lock:
            if self._active is not None:
                raise ScanJobBusyError(self._active)
            progress = ScanProgress(
                job_id=uuid.uuid4().hex,
                total=len(items),
                batch_size=size,
                batch_count=len(make_batches(items, size)),
            )
            self._jobs[progress.job_id] = progress
            self._order.append(progress.job_id)
            self._active = progress.job_id
            self._prune_locked()

        def work() -> None:
            try:
                signals, failed = run_batched_scan(items, scan_one, progress)
                progress.finish(STATUS_DONE, result=finalize(signals, failed))
            except Exception as exc:
                logger.exception("[scan] Job %s abgebrochen", progress.job_id)
                progress.finish(STATUS_FAILED, error=type(exc).__name__ + ": " + str(exc))
            if on_finish is not None:
                try:
                    on_finish(progress)
                except Exception:
                    logger.exception("[scan] on_finish für Job %s fehlgeschlagen", progress.job_id)
            with self._lock:
                if self._active == progress.job_id:
                    self._active = None

        if not run_async:
            work()
            return progress

        try:
            threading.Thread(target=work, name="scan-" + progress.job_id[:8], daemon=True).start()
        except Exception as exc:
            progress.finish(STATUS_FAILED, error=type(exc).__name__ + ": " + str(exc))
            with self._lock:
                if self._active == progress.job_id:
                    self._active = None
            raise
        return progress

    def get(self, job_id: str) -> Optional[ScanProgress]:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> Optional[ScanProgress]:
        with self._lock:
            return self._jobs.get(self._active) if self._active else None

    def _prune_locked(self) -> None:
        while len(self._order) > self._max_kept:
            oldest = next((j for j in self._order if j != self._active), None)
            if oldest is None:
                return
            self._order.remove(oldest)
            self._jobs.pop(oldest, None)