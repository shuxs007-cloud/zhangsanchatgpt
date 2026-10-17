"""Opt-in process-wide guard for offline OpenSMS shell-entry benchmarks."""
import os
from pathlib import Path
import tempfile

from benchmark_statistics_document_offline import isolate_writes

output = os.environ.get("OPENSMS_OFFLINE_BENCHMARK_OUTPUT")
if not output:
    raise RuntimeError("Offline benchmark guard requires an explicit output directory")
tempfile.tempdir = os.environ["TMPDIR"]
isolate_writes(Path(output))
