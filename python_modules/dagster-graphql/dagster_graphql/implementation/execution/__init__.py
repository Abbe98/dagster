import asyncio
import os
import sys
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional, Sequence, Tuple, Union

# re-exports
import dagster._check as check
from dagster._core.events import EngineEventData
from dagster._core.instance import DagsterInstance
from dagster._core.storage.captured_log_manager import CapturedLogManager
from dagster._core.storage.compute_log_manager import ComputeIOType, ComputeLogFileData
from dagster._core.storage.pipeline_run import DagsterRunStatus
from dagster._core.workspace.permissions import Permissions
from dagster._utils.error import serializable_error_info_from_exc_info
from starlette.concurrency import (
    run_in_threadpool,  # can provide this indirectly if we dont want starlette dep in dagster-graphql
)

from ..utils import (
    assert_permission,
    assert_permission_for_location,
    capture_error,
)
from .backfill import (
    cancel_partition_backfill as cancel_partition_backfill,
    create_and_launch_partition_backfill as create_and_launch_partition_backfill,
    resume_partition_backfill as resume_partition_backfill,
)

if TYPE_CHECKING:
    from dagster_graphql.schema.logs.compute_logs import GrapheneComputeLogFile
    from dagster_graphql.schema.pipelines.subscription import (
        GraphenePipelineRunLogsSubscriptionFailure,
        GraphenePipelineRunLogsSubscriptionSuccess,
    )
    from dagster_graphql.schema.util import ResolveInfo


def _force_mark_as_canceled(instance: DagsterInstance, run_id):
    from ...schema.pipelines.pipeline import GrapheneRun
    from ...schema.roots.mutation import GrapheneTerminateRunSuccess

    reloaded_record = check.not_none(instance.get_run_record_by_id(run_id))

    if not reloaded_record.dagster_run.is_finished:
        message = (
            "This pipeline was forcibly marked as canceled from outside the execution context. The "
            "computational resources created by the run may not have been fully cleaned up."
        )
        instance.report_run_canceled(reloaded_record.dagster_run, message=message)
        reloaded_record = check.not_none(instance.get_run_record_by_id(run_id))

    return GrapheneTerminateRunSuccess(GrapheneRun(reloaded_record))


def terminate_pipeline_execution(graphene_info: "ResolveInfo", run_id, terminate_policy):
    from ...schema.errors import GrapheneRunNotFoundError
    from ...schema.pipelines.pipeline import GrapheneRun
    from ...schema.roots.mutation import (
        GrapheneTerminateRunFailure,
        GrapheneTerminateRunPolicy,
        GrapheneTerminateRunSuccess,
    )

    check.str_param(run_id, "run_id")

    instance = graphene_info.context.instance

    record = instance.get_run_record_by_id(run_id)

    force_mark_as_canceled = (
        terminate_policy == GrapheneTerminateRunPolicy.MARK_AS_CANCELED_IMMEDIATELY
    )

    if not record:
        assert_permission(graphene_info, Permissions.TERMINATE_PIPELINE_EXECUTION)
        return GrapheneRunNotFoundError(run_id)

    run = record.dagster_run
    graphene_run = GrapheneRun(record)

    location_name = (
        run.external_pipeline_origin.location_name if run.external_pipeline_origin else None
    )

    if location_name:
        assert_permission_for_location(
            graphene_info, Permissions.TERMINATE_PIPELINE_EXECUTION, location_name
        )
    else:
        assert_permission(graphene_info, Permissions.TERMINATE_PIPELINE_EXECUTION)

    can_cancel_run = run.status == DagsterRunStatus.STARTED or run.status == DagsterRunStatus.QUEUED

    valid_status = not run.is_finished and (force_mark_as_canceled or can_cancel_run)

    if not valid_status:
        return GrapheneTerminateRunFailure(
            run=graphene_run,
            message="Run {run_id} could not be terminated due to having status {status}.".format(
                run_id=run.run_id, status=run.status.value
            ),
        )

    if force_mark_as_canceled:
        try:
            if instance.run_coordinator and can_cancel_run:
                instance.run_coordinator.cancel_run(run_id)
        except:
            instance.report_engine_event(
                (
                    "Exception while attempting to force-terminate run. Run will still be marked as"
                    " canceled."
                ),
                pipeline_name=run.pipeline_name,
                run_id=run.run_id,
                engine_event_data=EngineEventData(
                    error=serializable_error_info_from_exc_info(sys.exc_info()),
                ),
            )
        return _force_mark_as_canceled(instance, run_id)

    if instance.run_coordinator and can_cancel_run and instance.run_coordinator.cancel_run(run_id):
        return GrapheneTerminateRunSuccess(graphene_run)

    return GrapheneTerminateRunFailure(
        run=graphene_run, message="Unable to terminate run {run_id}".format(run_id=run.run_id)
    )


@capture_error
def delete_pipeline_run(graphene_info: "ResolveInfo", run_id: str):
    from ...schema.errors import GrapheneRunNotFoundError
    from ...schema.roots.mutation import GrapheneDeletePipelineRunSuccess

    instance = graphene_info.context.instance

    run = instance.get_run_by_id(run_id)
    if not run:
        return GrapheneRunNotFoundError(run_id)

    location_name = (
        run.external_pipeline_origin.location_name if run.external_pipeline_origin else None
    )
    if location_name:
        assert_permission_for_location(
            graphene_info, Permissions.DELETE_PIPELINE_RUN, location_name
        )
    else:
        assert_permission(graphene_info, Permissions.DELETE_PIPELINE_RUN)

    instance.delete_run(run_id)

    return GrapheneDeletePipelineRunSuccess(run_id)


def get_chunk_size() -> int:
    return int(os.getenv("DAGIT_EVENT_LOAD_CHUNK_SIZE", "10000"))


async def gen_events_for_run(
    graphene_info: "ResolveInfo",
    run_id: str,
    after_cursor: Optional[str] = None,
) -> AsyncIterator[
    Union[
        "GraphenePipelineRunLogsSubscriptionFailure",
        "GraphenePipelineRunLogsSubscriptionSuccess",
    ]
]:
    from ...schema.pipelines.pipeline import GrapheneRun
    from ...schema.pipelines.subscription import (
        GraphenePipelineRunLogsSubscriptionFailure,
        GraphenePipelineRunLogsSubscriptionSuccess,
    )
    from ..events import from_event_record

    check.str_param(run_id, "run_id")
    after_cursor = check.opt_str_param(after_cursor, "after_cursor")
    instance = graphene_info.context.instance
    record = instance.get_run_record_by_id(run_id)

    if not record:
        yield GraphenePipelineRunLogsSubscriptionFailure(
            missingRunId=run_id,
            message="Could not load run with id {}".format(run_id),
        )
        return

    run = record.dagster_run

    dont_send_past_records = False
    # special sigil cursor that signals to start watching for updates only after the current point in time
    if after_cursor == "HEAD":
        dont_send_past_records = True
        after_cursor = None

    chunk_size = get_chunk_size()
    # load the existing events in chunks
    has_more = True
    while has_more:
        # run the fetch in a thread since its sync
        connection = await run_in_threadpool(
            instance.get_records_for_run,
            run_id=run_id,
            cursor=after_cursor,
            limit=chunk_size,
        )
        if not dont_send_past_records:
            yield GraphenePipelineRunLogsSubscriptionSuccess(
                run=GrapheneRun(record),
                messages=[
                    from_event_record(record.event_log_entry, run.pipeline_name)
                    for record in connection.records
                ],
                hasMorePastEvents=connection.has_more,
                cursor=connection.cursor,
            )
        has_more = connection.has_more
        after_cursor = connection.cursor

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue[Tuple[Any, Any]] = asyncio.Queue()

    def _enqueue(event, cursor):
        loop.call_soon_threadsafe(queue.put_nowait, (event, cursor))

    # watch for live events
    instance.watch_event_logs(run_id, after_cursor, _enqueue)
    try:
        while True:
            event, cursor = await queue.get()
            yield GraphenePipelineRunLogsSubscriptionSuccess(
                run=GrapheneRun(record),
                messages=[from_event_record(event, run.pipeline_name)],
                hasMorePastEvents=False,
                cursor=cursor,
            )
    finally:
        instance.end_watch_event_logs(run_id, _enqueue)


async def gen_compute_logs(
    graphene_info: "ResolveInfo",
    run_id: str,
    step_key: str,
    io_type: ComputeIOType,
    cursor: Optional[str] = None,
) -> AsyncIterator[Optional["GrapheneComputeLogFile"]]:
    from ...schema.logs.compute_logs import from_compute_log_file

    check.str_param(run_id, "run_id")
    check.str_param(step_key, "step_key")
    check.inst_param(io_type, "io_type", ComputeIOType)
    check.opt_str_param(cursor, "cursor")
    instance = graphene_info.context.instance

    obs = instance.compute_log_manager.observable(run_id, step_key, io_type, cursor)

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue[ComputeLogFileData] = asyncio.Queue()

    def _enqueue(new_event):
        loop.call_soon_threadsafe(queue.put_nowait, new_event)

    obs(_enqueue)
    is_complete = False
    try:
        while not is_complete:
            update = await queue.get()
            yield from_compute_log_file(update)
            is_complete = obs.is_complete
    finally:
        obs.dispose()


async def gen_captured_log_data(
    graphene_info: "ResolveInfo", log_key: Sequence[str], cursor: Optional[str] = None
):
    from ...schema.logs.compute_logs import from_captured_log_data

    instance = graphene_info.context.instance

    compute_log_manager = instance.compute_log_manager
    if not isinstance(compute_log_manager, CapturedLogManager):
        return

    subscription = compute_log_manager.subscribe(log_key, cursor)

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue[ComputeLogFileData] = asyncio.Queue()

    def _enqueue(new_event):
        loop.call_soon_threadsafe(queue.put_nowait, new_event)

    subscription(_enqueue)
    is_complete = False
    try:
        while not is_complete:
            update = await queue.get()
            yield from_captured_log_data(update)  # type: ignore
            is_complete = subscription.is_complete
    finally:
        subscription.dispose()


@capture_error
def wipe_assets(graphene_info: "ResolveInfo", asset_keys):
    from ...schema.roots.mutation import GrapheneAssetWipeSuccess

    instance = graphene_info.context.instance
    instance.wipe_assets(asset_keys)
    return GrapheneAssetWipeSuccess(assetKeys=asset_keys)
