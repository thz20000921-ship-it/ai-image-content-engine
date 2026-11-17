/* Use the native destructive task API before removing the set record. Works
   with an already-running earlier company backend, without another restart. */
(() => {
  'use strict';
  async function submit(batchId, api) {
    let batch;
    try { batch = await api(`/api/company-workflow/batches/${batchId}`); }
    catch(error) {
      if (error.status !== 410) throw error;
      return api(`/api/company-workflow/batches/${batchId}`,{method:'DELETE'});
    }
    const present = batch.pages.filter(page => page.status !== 'missing');
    if (present.some(page => ['running','submitting','cancelling'].includes(page.status))) {
      throw new Error('这套仍有图片正在生成，请等生成结束后再删除。');
    }
    const ids = [...new Set(present.map(page => page.task_id))];
    if (ids.length > 12 || ids.some(id => !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(id))) {
      throw new Error('任务编号无效，请刷新后重试。');
    }
    if (ids.length) {
      const result = await api('/api/tasks/delete-batch',{
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_ids:ids})
      });
      if (!Array.isArray(result.deleted) || !Array.isArray(result.skipped) || !Array.isArray(result.failed)) {
        throw new Error('删除结果暂未确认，请刷新任务状态。');
      }
      if (result.skipped.length || result.failed.length || ids.some(id => !result.deleted.includes(id))) {
        const current = await api(`/api/company-workflow/batches/${batchId}`);
        if (current.pages.some(page => page.status !== 'missing')) {
          throw new Error('部分任务尚未删除，整套记录已保留。请等任务结束后再点删除。');
        }
      }
    }
    return api(`/api/company-workflow/batches/${batchId}`,{method:'DELETE'});
  }
  window.CompanyDelete = {submit};
})();
