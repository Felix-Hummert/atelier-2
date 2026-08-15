from __future__ import annotations

import os
import sys
from pathlib import Path

from atelier2.adapters.systemd_agent_collector import (
    DirectSystemdCollectorBarriers,
    collect_direct_systemd_agent_process,
)
from atelier2.adapters.systemd_generation_records import (
    DirectSystemdGenerationRecords,
    DirectSystemdInvocationId,
)

CRASHED = 86


def main() -> None:
    records = DirectSystemdGenerationRecords(Path(sys.argv[1]))

    def crash_between_started_fsyncs() -> None:
        os._exit(CRASHED)

    collect_direct_systemd_agent_process(
        records,
        DirectSystemdInvocationId(sys.argv[3]),
        launch_credential_path=Path(sys.argv[2]),
        source_envelope_path=Path(sys.argv[2]),
        barriers=DirectSystemdCollectorBarriers(
            after_started_file_fsync=crash_between_started_fsyncs
        ),
    )


if __name__ == "__main__":
    main()
