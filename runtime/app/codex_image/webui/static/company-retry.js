/* Manual retries reuse the existing task API and queue; no automatic image calls. */
(() => {
  'use strict';
  const eligible = page => ['failed','partial_failed'].includes(page.status) && !page.url;
  const plan = batch => batch.pages.filter(eligible).map(page => ({
    task_id:page.task_id, page_number:page.page_number, original_available:!!page.original_available
  }));

  async function submit(batchId, targets, api, progress = () => {}) {
    if (!targets.length || targets.length > 12 || new Set(targets.map(p => p.task_id)).size !== targets.length) {
      throw new Error('重试页数无效，请刷新后重新选择。');
    }
    // Recheck the pages selected by this click. Newly failed pages wait for another click.
    const data = await api('/api/company-workflow/batches');
    const batch = data.batches.find(item => item.id === batchId);
    if (!batch) throw new Error('这条制作记录已移除，请刷新后核对任务状态。');
    const current = new Map(batch.pages.map(page => [page.task_id,page]));
    const result = {accepted:[],skipped:[],unsubmitted:[],uncertain:[],error:''};
    for (const [index,target] of targets.entries()) {
      const page = current.get(target.task_id);
      if (!page || !eligible(page) || (target.original_available && !page.original_available)) {
        result.skipped.push(target.page_number); continue;
      }
      try {
        await api(`/api/tasks/${page.task_id}/retry-failed`,{
          method:'POST',headers:{'Content-Type':'application/json'},body:'{}'
        });
        result.accepted.push(page.page_number);
        progress(result.accepted.length,targets.length);
      } catch(error) {
        // A second tab may already have queued/completed this page. The task API locks it.
        if (error.status === 409 || error.status === 404) {
          result.skipped.push(page.page_number);
          continue;
        }
        result.error = error.message;
        if (!error.status || error.status >= 500) result.uncertain.push(page.page_number);
        else result.unsubmitted.push(page.page_number);
        result.unsubmitted.push(...targets.slice(index+1).map(item => item.page_number));
        // Stop on an ambiguous response: never replay a potentially accepted paid request.
        break;
      }
    }
    return result;
  }
  window.CompanyRetry = {plan,submit};
})();
