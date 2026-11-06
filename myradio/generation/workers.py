"""The worker kinds the engine knows, and the shared worker types."""
from __future__ import annotations

from .worker_drama import DramaHttpWorker
from .worker_fixture import FixtureWorker, fixtures_allowed
from .worker_yue2 import Yue2CliWorker
from .worker_yue2_http import Yue2HttpWorker
from .workers_base import (EVIDENCE, GenerationRequest, ProbeResult, Worker, WorkerContext, WorkerOutput)

KINDS = {
    DramaHttpWorker.kind: DramaHttpWorker,
    Yue2CliWorker.kind: Yue2CliWorker,
    Yue2HttpWorker.kind: Yue2HttpWorker,
    FixtureWorker.kind: FixtureWorker,
}

__all__ = ["DramaHttpWorker", "Yue2CliWorker", "Yue2HttpWorker", "FixtureWorker", "fixtures_allowed", "KINDS", "EVIDENCE", "GenerationRequest",
           "ProbeResult", "Worker", "WorkerContext", "WorkerOutput"]
