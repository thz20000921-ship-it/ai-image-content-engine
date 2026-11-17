"""Delete company tasks, generated results and raw-result caches together."""
from contextlib import ExitStack
import json
import re

from .company_workflow import workflow_root, write_json
from .storage import utc_now


class BatchDeleteConflict(ValueError):
    pass


def deleted_batch_path(storage, batch_id):
    if not re.fullmatch(r'[0-9a-f]{32}', batch_id):
        raise ValueError('套图编号无效')
    return workflow_root(storage) / 'deleted-batches' / f'{batch_id}.json'


def deleted_result_files(storage, receipt):
    """Exact app-owned layouts only. A deletion receipt supplies task ownership.

    Do not scan user-selected export folders or recursively follow links. This
    also cleans the previous version's results after their metadata was removed.
    """
    deleted_batch_path(storage, receipt['id'])
    originals = workflow_root(storage) / 'originals'
    output = storage.output_root
    files = set()
    for task_id in receipt['deleted_task_ids']:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', task_id):
            raise ValueError('删除记录中的任务编号无效')
        day = f'{task_id[:4]}-{task_id[4:6]}-{task_id[6:8]}' if re.match(r'^\d{8}', task_id) else None
        locations = [(output, r'\.(?:png|jpe?g|webp)'),
                     (originals, r'\.(?:bin|json)')]
        if day:
            locations += [(output / day, r'\.(?:png|jpe?g|webp)'),
                          (output / 'thumbnails' / day, r'(?:-(?:thumb|sidebar))?\.(?:png|jpe?g|webp)')]
        for directory, suffix in locations:
            pattern = re.compile(re.escape(task_id) + r'-image-[1-9]\d*' + suffix, re.I)
            for path in directory.glob(task_id + '-image-*'):
                if not pattern.fullmatch(path.name):
                    continue
                allowed = originals if directory == originals else output
                if path.is_symlink() or not path.resolve().is_relative_to(allowed.resolve()):
                    raise ValueError('结果文件路径超出程序存储目录，已停止删除')
                if path.is_file():
                    files.add(path)
    return sorted(files)


def purge_deleted_results(storage, receipt):
    paths = deleted_result_files(storage, receipt)
    removed_bytes = sum(path.stat().st_size for path in paths)
    for path in paths:
        path.unlink(missing_ok=True)
    receipt.update(local_images_preserved=False, generated_results_deleted=True,
                   results_purged_at=utc_now())
    write_json(deleted_batch_path(storage, receipt['id']), receipt)
    return {'deleted_files': len(paths), 'freed_bytes': removed_bytes}


def delete_company_batch(ctx, batch):
    """Use the native task deletion locks/index cleanup; never delete external files.

    Native deletion removes app-owned results and raw caches as well as metadata,
    task/sidebar indexes, organizer state and waiting entries.
    A small receipt prevents a stale submit/resume from recreating a deleted set.
    """
    batch_id = batch['id']
    receipt_path = deleted_batch_path(ctx.storage, batch_id)
    path = workflow_root(ctx.storage) / 'batches' / f'{batch_id}.json'
    ids = sorted({row['task_id'] for page in batch.get('pages', [])
                  for row in [page, *page.get('versions', [])]} |
                 {row['task_id'] for revision in batch.get('revisions', []) for row in revision.get('rows', [])})
    for task_id in ids:
        ctx.storage._validate_task_id(task_id)
    with ExitStack() as locks:
        locks.enter_context(ctx.storage._history_organization_lock)
        for task_id in ids:
            locks.enter_context(ctx.storage._task_write_lock(task_id))
        locks.enter_context(ctx.queue_storage.exclusive())
        # Preflight the whole set before removing anything. Queue claims/retries
        # use the same locks and cannot start a task during this operation.
        existing = []
        for task_id in ids:
            if task_id in ctx.active_task_ids or ctx.route_helpers['queue_has_running_task'](task_id):
                raise BatchDeleteConflict('这套仍有图片正在生成，请等生成结束后再删除。')
            try:
                meta = ctx.storage.read_metadata(task_id)
            except FileNotFoundError:
                continue
            if meta.get('params', {}).get('company_page', {}).get('batch_id') != batch_id:
                raise BatchDeleteConflict('任务与套图的归属不一致，已停止删除。')
            existing.append(task_id)
        for task_id in ids:
            if task_id in existing:
                ctx.storage.delete_task(task_id)
            else:
                # A page may have been removed individually or by an interrupted
                # batch delete. Finish its index/organizer cleanup idempotently.
                ctx.storage.task_index.delete(task_id)
                ctx.storage.history_organizer.delete_task_state(task_id)
            ctx.queue_storage.remove_waiting(task_id)
        receipt = {'id': batch_id, 'deleted_at': utc_now(), 'deleted_task_ids': ids,
                   'deleted_count': len(ids)}
        purge_deleted_results(ctx.storage, receipt)
        path.unlink(missing_ok=True)
    return receipt
