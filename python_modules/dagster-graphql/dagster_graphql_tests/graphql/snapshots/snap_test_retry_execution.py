# -*- coding: utf-8 -*-
# snapshottest: v1 - https://goo.gl/zC4yUc
from __future__ import unicode_literals

from snapshottest import Snapshot

snapshots = Snapshot()

snapshots['TestRetryExecution.test_pipeline_reexecution_info_query[sqlite_with_sync_run_launcher_in_process_env] 1'] = [
    'sum_sq_solid.compute'
]

snapshots['TestRetryExecution.test_pipeline_reexecution_info_query[sqlite_with_cli_api_run_launcher_in_process_env] 1'] = [
    'sum_sq_solid.compute'
]
