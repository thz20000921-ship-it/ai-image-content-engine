/* Company workspace r4. Credentials remain in the native local settings service. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  if (!$('companyApp')) return;
  let pages = [], provider = {}, batches = [], selectedId = null, initialized = false;
  let busy = false, submitted = false, batchId = null, loading = false, polls = 0;
  let historySignature = '', resultSignature = '';
  let sourceSelectionId = '', sourceFolder = '', picking = false, exporting = false;
  let undoAction = null;
  let sourceDirectory = null;
  let lastPlanText = '';
  let engineToken = '', engineJobId = null, revisionTarget = null;
  async function engineApi(path, options = {}) {
    if (!engineToken) engineToken = (await api('/api/engine/session', {headers:{'X-Requested-With':'image-engine-webui'}})).token;
    return api('/api/engine' + path, {...options, headers:{...options.headers, Authorization:'Bearer ' + engineToken}});
  }
  const postJson = value => ({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
  const lines = value => value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  function contentPages() {
    const text = $('enginePageCopy').value;
    if (!text.trim()) return [];
    return text.split(/\r?\n---\r?\n/).map((block, index) => {
      const rows = block.trim().split(/\r?\n/);
      return {page_number:index+1, role:index ? 'content' : 'cover', title:rows.shift(), copy:rows.join('\n')};
    });
  }
  async function jobFromForm() {
    const selected = pages.filter(page => page.selected);
    let references = [];
    if (selected.length) {
      const form = new FormData(); selected.forEach(page => form.append('images',page.file,page.file.name));
      const uploaded = (await engineApi('/assets',{method:'POST',body:form})).assets;
      references = uploaded.map(asset => ({asset_id:asset.id,source:'本机用户导入',rights:$('engineRights').value,purpose:'reference'}));
    }
    let brand = {enabled:false};
    const logo = $('engineBrand').files[0];
    if (logo) {
      if (!$('engineBrandRights').checked) throw new Error('请确认本次 Logo 使用授权。');
      const form = new FormData(); form.append('images',logo,logo.name);
      brand = {enabled:true,logo_asset_id:(await engineApi('/assets',{method:'POST',body:form})).assets[0].id,rights_confirmed:true};
    }
    engineJobId ||= 'ui-' + makeId();
    const content = contentPages();
    if ($('engineMode').value === 'edit') content.forEach((page,index) => { if (references[index]) page.asset_ids = [references[index].asset_id]; });
    return {task_id:engineJobId,mode:$('engineMode').value,theme:$('engineTheme').value.trim(),
      audience:$('engineAudience').value,product:{name:$('engineProduct').value,facts:lines($('engineFacts').value)},
      title:content[0]?.title || $('engineTheme').value,content_mode:$('engineContentMode').value,
      page_count:Number($('enginePageCount').value),pages:content,references,brand,
      requirements:[$('companyChanges').value,$('companyPrompt').value,$('companyCopyRules').value].filter(Boolean).join('\n\n'),
      visual_rules:lines($('engineVisual').value),
      generation:{size:'1024x1536',quality:$('companyQuality').value,binding_id:$('engineBinding').value},
      authorization:{approved:$('engineApproved').checked,max_image_requests:Number($('engineBudget').value)}};
  }
  async function refreshEngineProvider() {
    const state = await engineApi('/status');
    const active = state.providers.find(p => p.id === state.active_provider_id);
    const current = $('engineBinding').value;
    $('engineBinding').replaceChildren();
    for(const binding of (active?.bindings || []).filter(b=>b.operations.includes('generate'))) {
      const option=element('option',binding.remote_model_id); option.value=binding.id; $('engineBinding').append(option);
    }
    if ([...$('engineBinding').options].some(o=>o.value===current)) $('engineBinding').value=current;
    updateProvider({ready:!!active?.ready && !!active?.bindings?.length});
  }
  const selectedBatches = new Set();
  let exportingMany = false;
  let selectMode = false;
  let deleteAllArmed = false;
  let deleteAllTimer = 0;
  let deletingMany = false;
  let retrying = false;
  const deletingIds = new Set();
  const makeId = () => crypto.randomUUID().replaceAll('-', '');
  const element = (tag, text, cls) => { const el = document.createElement(tag); if (text) el.textContent = text; if (cls) el.className = cls; return el; };
  const message = (text, error = false) => { $('companyMessage').textContent = text; $('companyMessage').classList.toggle('cw-error', error); };
  function toast(text, undo = null) {
    $('companyToastText').textContent = text; undoAction = undo;
    $('companyToastUndo').hidden = !undo; $('companyToast').hidden = false;
  }
  $('companyToastClose').onclick = () => { $('companyToast').hidden = true; undoAction = null; };
  $('companyToastUndo').onclick = async () => { const action = undoAction; if (!action) return; $('companyToastUndo').disabled = true; try { await action(); $('companyToast').hidden = true; } catch(e) { message(e.message,true); } finally { $('companyToastUndo').disabled = false; } };
  async function api(path, options) {
    const response = await fetch(path, options);
    let value;
    try { value = await response.json(); } catch { throw new Error('本机服务暂时没有响应，请确认软件仍在运行。'); }
    if (!response.ok) { const d = value.detail; const error = new Error(typeof d === 'string' ? d : d?.message || '操作未完成，请查看任务记录。'); error.status = response.status; throw error; }
    return value;
  }
  const statusName = {queued:'等待生成',staged:'排队中（等前一套跑完）',running:'正在生成',completed:'已完成，待校核',failed:'生成失败',partial_failed:'生成失败',cancelled:'已取消',paused:'升级后已暂停',missing:'记录已删除'};
  const failed = page => ['failed','partial_failed'].includes(page.status);
  function refreshCount() {
    const count = contentPages().length || Number($('enginePageCount').value);
    $('companyReferenceCount').textContent = `${pages.filter(p=>p.selected).length} 张素材 · 输出页数独立`;
    $('companyCount').textContent = submitted ? '这套已提交；可以查看结果或新建下一套' : `预计 ${count} 页 / ${count} 次请求；上限 ${$('engineBudget').value} 次；自动付费重试0次。单价请核对供应商。`;
    $('companyPrepare').disabled = busy || submitted || retrying || !count || count>12 || !provider.ready || !$('engineTheme').value.trim() || !$('engineApproved').checked || Number($('engineBudget').value)<count;
    $('companyPrepare').textContent = busy ? '正在提交…' : submitted ? '这套已提交' : count ? `生成整套 · ${count} 张` : '生成整套';
    $('companyUploadLabel').textContent = pages.length ? '继续添加素材' : '上传参考图或真实素材';
    $('companyUpload').classList.toggle('cw-upload-compact', !!pages.length);
    ['companyFiles','companyUpload','companyChanges','companyPrompt','companyQuality','companyOpacity','companyLink','companyLinkFetch','companyAnalyze','companyCopyRules'].forEach(id => $(id).disabled = busy || submitted);
    ['engineMode','engineBinding','engineTheme','engineAudience','engineProduct','engineFacts','enginePageCopy','engineContentMode','enginePageCount','engineVisual','engineRights','engineBrand','engineBrandRights','engineBudget','engineApproved','enginePlan'].forEach(id=>$(id).disabled=busy || submitted);
    $('companyNew').disabled = busy || retrying;
    $('companySave').disabled = busy;
    $('companyBrowserUpload').disabled = busy || submitted || picking;
    $('companyUpload').disabled ||= picking;
    $('companySourceFolder').textContent = sourceDirectory ? `结果保存到「${sourceDirectory.name}」内的新文件夹` : pages.length ? '首次保存时，选择一次原参考图文件夹' : '支持多选，或直接拖入图片';
  }
  function invalidate() {
    if (busy || submitted) return;
    batchId = null;
    engineJobId = null;
    refreshCount();
  }
  function renderPages() {
    const positions = new Map([...$('companyPages').children].map(el => [el.dataset.pageId,el.getBoundingClientRect()]));
    $('companyPages').replaceChildren();
    $('companyEmpty').hidden = !!pages.length;
    pages.forEach((page, index) => {
      const li = element('li', '', 'cw-page');
      page.uid ||= makeId(); li.dataset.pageId = page.uid;
      const image = element('img'); image.src = page.url; image.alt = `参考第${index+1}页`; image.draggable = false;
      const label = element('label'); const check = element('input'); check.type = 'checkbox'; check.checked = page.selected; check.disabled = busy || submitted;
      check.addEventListener('change', () => { page.selected = check.checked; invalidate(); });
      label.append(check, document.createTextNode(`素材 ${index+1}`));
      const controls = element('div','','cw-page-controls');
      [['↑','上移',-1],['↓','下移',1],['×','移除',0]].forEach(([text, title, offset]) => {
        const button = element('button',text); button.type = 'button'; button.title = `${title}第${index+1}页`; button.setAttribute('aria-label',button.title);
        button.disabled = busy || submitted || (offset !== 0 && (index+offset < 0 || index+offset >= pages.length));
        button.onclick = () => { if (offset) [pages[index],pages[index+offset]] = [pages[index+offset],pages[index]]; else { URL.revokeObjectURL(page.url); pages.splice(index,1); } invalidate(); renderPages(); };
        controls.append(button);
      });
      li.title = page.file.name; li.append(image,label,controls); $('companyPages').append(li);
    });
    refreshCount();
    if (!matchMedia('(prefers-reduced-motion: reduce)').matches) for (const li of $('companyPages').children) {
      const before = positions.get(li.dataset.pageId), after = li.getBoundingClientRect();
      if (before && (before.x !== after.x || before.y !== after.y)) li.animate([{transform:`translate(${before.x-after.x}px,${before.y-after.y}px)`},{transform:'none'}],{duration:200,easing:'cubic-bezier(.2,.8,.2,1)'});
    }
  }
  function updateProvider(value) {
    provider = value;
    if (!provider.ready) message('先在左下角「API 与系统设置」配置生图供应商、模型与 API Key。',true);
    refreshCount();
  }
  async function loadSettings() {
    const settings = await api('/api/company-workflow');
    $('companyPrompt').value = settings.prompt; $('companyOpacity').value = String(settings.opacity);
    $('companyPreview').href = `/api/company-workflow/preview?opacity=${settings.opacity}`;
    $('companyDownloadDir').value = settings.download_dir || '';
    $('companyCopyRules').value = (settings.copy_rules || []).join(String.fromCharCode(10));
    $('companyAnalysisBase').value = settings.analysis_base_url || '';
    $('companyAnalysisModel').value = settings.analysis_model || '';
    $('companyAnalysisKey').placeholder = settings.analysis_ready ? '密钥已保存（留空表示不修改）' : '密钥';
    $('companyAnalysisProtocol').value = settings.analysis_protocol || 'chat_completions';
    if (settings.migration?.notice) message(settings.migration.notice);
    await refreshEngineProvider();
  }
  function dateLabel(batch) {
    return new Date(batch.created_at).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
  }
  function renderHistory() {
    const signature = JSON.stringify([selectedId,[...deletingIds],[...selectedBatches],exportingMany,selectMode,batches.map(b => [b.id,b.changes,b.pages.map(p => p.status)])]);
    if (signature === historySignature) return;
    historySignature = signature;
    const nav = $('companyHistory'); nav.replaceChildren();
    if (!batches.length) { nav.append(element('p','还没有制作记录','cw-hint')); return; }
    for (const batch of batches) {
      const row = element('div','','cw-history-row'); row.classList.toggle('is-active',batch.id === selectedId);
      const count = batch.pages.filter(p => p.url).length;
      const button = element('button','','cw-history-item'); button.type = 'button'; button.classList.toggle('is-active',batch.id === selectedId); button.setAttribute('aria-pressed',String(batch.id === selectedId));
      const label = batch.title || batch.changes?.trim() || '按基础指令改编';
      const thumbSrc = (batch.pages.find(p => p.url) || {}).url || (batch.pages[0] || {}).reference_thumbnail_url;
      if (thumbSrc) {
        const thumb = element('span','','cw-history-thumb');
        const img = element('img'); img.src = thumbSrc; img.alt = ''; img.loading = 'lazy';
        thumb.append(img);
        button.append(thumb);
      }
      const text = element('span','','cw-history-text');
      text.append(
        element('strong',label),
        element('span',
          `${dateLabel(batch)} · ${count === batch.request_count ? `${batch.request_count} 页` : `${count}/${batch.request_count} 页`}`,
          count === batch.request_count ? 'cw-history-meta is-done' : 'cw-history-meta')
      );
      button.append(text);
      button.onclick = () => { selectedId = batch.id; renderHistory(); renderResults(); };
      const remove = element('button','','cw-history-delete'); remove.type = 'button';
      remove.disabled = deletingIds.has(batch.id);
      remove.title = '删除整套记录和程序内生图文件，已下载图片保留'; remove.setAttribute('aria-label',`删除记录：${label}`);
      const svg = document.createElementNS('http://www.w3.org/2000/svg','svg'); svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('width','17'); svg.setAttribute('height','17'); svg.setAttribute('aria-hidden','true');
      const path = document.createElementNS(svg.namespaceURI,'path'); path.setAttribute('d','M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7'); path.setAttribute('fill','none'); path.setAttribute('stroke','currentColor'); path.setAttribute('stroke-width','1.6'); path.setAttribute('stroke-linecap','round'); path.setAttribute('stroke-linejoin','round'); svg.append(path); remove.append(svg);
      remove.onclick = async () => {
        if (busy || deletingIds.has(batch.id)) return;
        deletingIds.add(batch.id); renderHistory();
        try {
          await CompanyDelete.submit(batch.id,api);
          const clearEditor = batchId === batch.id;
          if (clearEditor) resetComposer();
          if (selectedId === batch.id) selectedId = null;
          batches = batches.filter(item => item.id !== batch.id);
          renderHistory(); renderResults();
          toast('整套记录和程序内生图文件已删除，已下载的图片保留');
          try { await refreshBatches(); } catch { message('整套及单图历史已删除；列表暂时无法刷新，请稍后点击刷新。',true); }
        } catch(error) { message(error.message,true); toast(error.message); }
        finally { deletingIds.delete(batch.id); renderHistory(); }
      };
      if (selectMode) {
        const check = element('input','','cw-history-check'); check.type = 'checkbox';
        check.checked = selectedBatches.has(batch.id);
        check.setAttribute('aria-label',`选择：${label}`);
        check.onchange = () => { if (check.checked) selectedBatches.add(batch.id); else selectedBatches.delete(batch.id); disarmDeleteAll(); updateSelectBar(); renderHistory(); };
        row.append(check);
      }
      row.append(button,remove); nav.append(row);
    }
  }
  function compare(batch, page) {
    $('companyCompareTitle').textContent = `第 ${page.page_number} 页 · 原图与结果`;
    const source = $('companyCompareSource'), result = $('companyCompareResult');
    source.hidden = !page.reference_url; if (page.reference_url) source.src = page.reference_url; else source.removeAttribute('src');
    result.hidden = !page.url; if (page.url) result.src = page.url; else result.removeAttribute('src');
    $('companyCompareDownload').hidden = !page.url;
    if (page.url) { $('companyCompareDownload').href = page.url; $('companyCompareDownload').download = `第${page.page_number}页.png`; }
    $('companyCompareOriginal').hidden = !page.original_available;
    $('companyCompareOriginal').href = `/api/company-workflow/tasks/${page.task_id}/original`;
    $('companyCompare').showModal();
  }
  async function runQueueAction(action) {
    if (retrying || busy) return;
    retrying = true; refreshCount(); renderResults();
    try { await action(); }
    catch(error) { message(error.message,true); toast(error.message); }
    finally {
      try { await refreshBatches(); }
      catch { message('任务状态暂时无法刷新，请稍后点刷新核对，勿重复提交。',true); }
      retrying = false; refreshCount(); renderResults();
    }
  }
  function retryPages(batch, targets) {
    return runQueueAction(async () => {
      if (batch.engine_schema) {
        if (!confirm(`只重试 ${targets.length} 个失败页，可能再次计费，仍受任务授权总额度限制。确认继续？`)) return;
        await engineApi(`/jobs/${batch.external_id}/retry`,postJson({request_id:makeId(),page_numbers:targets.map(p=>p.page_number),acknowledge_uncertain_charge:true}));
        message('失败页已提交，成功页保留；达到额度上限时不会再次请求。');
        return;
      }
      const result = await CompanyRetry.submit(batch.id,targets,api,(count,total) => message(`正在提交失败页重试：${count}/${total}`));
      let summary = `已提交 ${result.accepted.length} 页重试。`;
      if (result.skipped.length) summary += ` ${result.skipped.length} 页状态已变化或不可重试，已跳过。`;
      if (result.uncertain.length) summary += ` 第 ${result.uncertain.join('、')} 页提交结果未确认，已停止后续提交；请刷新核对，勿立即重复操作。`;
      if (result.unsubmitted.length) summary += ` ${result.unsubmitted.length} 页未提交。`;
      if (result.error) summary += ` ${result.error}`;
      message(summary,!!result.error); toast(summary);
    });
  }
  function renderResults() {
    const batch = batches.find(b => b.id === selectedId);
    const signature = JSON.stringify([batch || null,retrying]);
    if (signature === resultSignature) return;
    resultSignature = signature;
    const container = $('companyBatches'); container.replaceChildren(); $('companyBatchActions').replaceChildren();
    container.classList.remove('is-refresh');
    void container.offsetWidth;          // 强制重排以重播动画
    container.classList.add('is-refresh');
    $('companyDownload').hidden = true; $('companyZipDownload').hidden = true; $('companySaveAs').hidden = true; $('companyLegacyWarning').hidden = true;
    $('companyProgress').hidden = !batch; $('companyResultsEmpty').hidden = !!batch;
    $('companyResultsTitle').textContent = batch ? `${batch.request_count} 页图文` : '整套结果';
    $('companyResultSubtitle').textContent = batch ? `${dateLabel(batch)} · ${batch.title || batch.changes?.trim() || '按已保存的基础指令改编'}` : '完成一页，出现在这里。整套结果一起保存。';
    $('companyResultSubtitle').classList.remove('is-open');
    $('companySubtitleMore').textContent = '展开';
    $('companySubtitleMore').hidden = ($('companyResultSubtitle').textContent || '').length < 60;
    if (!batch) { renderPageChips(null); return; }
    const successes = batch.pages.filter(p => p.url).length;
    const failures = batch.pages.filter(failed).length;
    const running = batch.pages.filter(p => p.status === 'running').length;
    const queued = batch.pages.filter(p => p.status === 'queued').length;
    const settled = batch.pages.filter(p => !['queued','staged','running'].includes(p.status)).length;
    $('companyProgressLabel').textContent = `已完成 ${successes} / ${batch.request_count} 张`;
    $('companyProgressStatus').textContent = batch.submission_state === 'waiting_turn' ? '排队中：前面那套跑完就自动开始' : running || queued ? `${running} 张生成中，${queued} 张等待` : failures ? `${failures} 张失败，可批量重试` : successes === batch.request_count ? '整套已完成' : '部分记录已取消或删除';
    $('companyProgressBar').max = batch.request_count; $('companyProgressBar').value = settled;
    renderPageChips(batch);
    $('companyLegacyWarning').hidden = !!batch.engine_schema || batch.reference_strategy === 'target_only';
    if (successes) {
      const a = $('companyDownload'); a.hidden = false; a.disabled = exporting;
      a.textContent = exporting ? '正在保存…' : successes === batch.request_count ? `保存整套 · ${successes} 张` : `保存已完成 · ${successes} 张`;
      a.onclick = () => saveLocal(batch);
      $('companyZipDownload').hidden = false; $('companyZipDownload').href = `/api/company-workflow/batches/${batch.id}/download`;
      $('companySaveAs').hidden = false; $('companySaveAs').disabled = exporting; $('companySaveAs').onclick = () => saveLocal(batch,true);
    }
    if (batch.submission_state === 'waiting_turn') {
      $('companyBatchActions').append(element('p','排队中：前面还有一套在生成，这套会自动开始，不用重复提交。','cw-hint'));
    } else if (batch.submission_state !== 'submitted') {
      const resume = element('button','继续提交未入队页面','cw-secondary'); resume.type = 'button';
      resume.disabled = retrying || busy;
      resume.onclick = () => runQueueAction(async () => {
        if (batch.engine_schema) await engineApi(`/jobs/${batch.external_id}/resume`,{method:'POST'});
        else await api(`/api/company-workflow/batches/${batch.id}/resume`,{method:'POST'});
        message('未入队页面已继续提交，已有结果会保留。');
      });
      $('companyBatchActions').append(element('p','这套提交曾中断，已保存的结果仍然保留。','cw-error'),resume);
    }
    if (batch.engine_schema && batch.pages.some(p=>['queued','staged','running'].includes(p.status))) {
      const cancel = element('button','取消未完成页面','cw-secondary'); cancel.type='button';
      cancel.onclick=()=>runQueueAction(()=>engineApi(`/jobs/${batch.external_id}/cancel`,{method:'POST'}));
      $('companyBatchActions').append(cancel);
    }
    const targets = CompanyRetry.plan(batch);
    if (targets.length) {
      const retryAll = element('button',retrying ? '正在提交重试…' : `重试全部失败页 · ${targets.length} 张`,'cw-secondary');
      retryAll.type = 'button'; retryAll.disabled = retrying || busy;
      retryAll.onclick = () => retryPages(batch,targets);
      $('companyBatchActions').append(retryAll,element('span','成功页保留，也可在下方单页重试','cw-hint'));
    }
    batch.pages.forEach((page, index) => {
      const card = element('article','','cw-result'); card.dataset.page = String(page.page_number);
      card.dataset.state = page.status || 'queued';
      const head = element('div','','cw-result-head');
      head.append(element('span',`第 ${page.page_number} 页${page.version ? ` · v${page.version}` : ''}`,'cw-result-badge'),
                  element('span',statusName[page.status] || page.status,
                    failed(page) ? 'cw-result-status cw-error' : page.status === 'completed' ? 'cw-result-status cw-success' : 'cw-result-status'));
      const preview = element('button','','cw-result-preview'); preview.type = 'button'; preview.setAttribute('aria-label',`查看第${page.page_number}页原图与结果`);
      const url = page.url || page.reference_thumbnail_url;
      if (url) { const img = element('img'); img.src = url; img.alt = `第${page.page_number}页${page.url ? '结果' : '参考图'}`; img.loading = 'lazy'; preview.append(img); }
      if (!page.url) preview.append(element('span',statusName[page.status] || '等待生成','cw-result-state'));
      preview.onclick = () => compare(batch,page);
      const controls = element('div','','cw-result-controls');
      const view = element('button','对照原图','cw-text-button'); view.type = 'button'; view.onclick = () => compare(batch,page); controls.append(view);
      if (page.url) { const a = element('a','下载','cw-text-button'); a.href = page.url; a.download = `第${page.page_number}页.png`; controls.append(a); }
      if (page.url && batch.engine_schema) {
        const edit = element('button','修改此页','cw-text-button'); edit.type='button';
        edit.disabled = batch.pages.some(p=>['queued','staged','running'].includes(p.status));
        edit.onclick=()=>{
          revisionTarget={batch,page,id:makeId()};
          $('engineRevisionTitle').textContent=`修改第${page.page_number}页 · 新版本`;
          $('engineRevisionHeading').value=page.content.title;
          $('engineRevisionCopy').value=page.content.copy;
          $('engineRevisionVisual').value=page.content.visual;
          $('engineRevisionMessage').textContent='';
          $('engineRevision').showModal();
        };
        const review = element('button','校核文字','cw-text-button'); review.type='button';
        review.onclick=async()=>{
          const expected=[page.content.title,page.content.copy].filter(Boolean).join('\n');
          const observed=prompt('请先查看图片，再粘贴实际看到的完整标题与正文。填写预期文案不能证明图片正确。', '');
          if(observed===null) return;
          try {
            const result=await engineApi(`/jobs/${batch.external_id}/pages/${page.page_number}/text-review`,postJson({observed_text:observed}));
            toast(result.status==='matched' ? '录入文字与预期文案一致；请同时核对产品事实。' : '录入文字与预期文案不一致，可修改此页重生成。');
          } catch(e){message(e.message,true);}
        };
        controls.append(edit,review);
      }
      if (failed(page) && !page.url) {
        const retry = element('button',page.original_available ? '重做水印' : '重试本页','cw-text-button'); retry.type = 'button';
        retry.disabled = retrying || busy;
        retry.onclick = () => retryPages(batch,[{task_id:page.task_id,page_number:page.page_number,original_available:!!page.original_available}]);
        controls.append(retry);
      }
      card.append(preview,head,controls);
      if (page.url && batch.engine_schema) card.append(element('p','请核对图中文字、数字与产品事实；文件生成成功不代表内容已验收。','cw-hint'));
      if (page.error) { const details = element('details','','cw-result-error'); details.append(element('summary','查看失败原因'),element('p',page.error)); card.append(details); }
      card.style.setProperty('--i', String(index));
      card.classList.add('is-entering');
      container.append(card);
    });
  }
  // 页码格子：一眼看出哪页完成、哪页在跑、哪页挂了
  function renderPageChips(batch) {
    const bar = $('companyPageChips');
    bar.replaceChildren();
    if (!batch) return;
    for (const page of batch.pages) {
      const li = document.createElement('li');
      li.className = 'cw-chip';
      li.dataset.state = page.status || 'queued';
      li.textContent = String(page.page_number);
      li.title = `第 ${page.page_number} 页 · ${statusName[page.status] || page.status}`;
      li.tabIndex = 0;
      li.setAttribute('role', 'button');
      li.setAttribute('aria-label', li.title);
      const jump = () => {
        const card = document.querySelector(`.cw-result[data-page="${page.page_number}"]`);
        if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      };
      li.onclick = jump;
      li.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); jump(); } };
      bar.append(li);
    }
  }

  function updateSelectBar() {
    const bar = $('companySelectBar');
    if (!bar) return;
    bar.hidden = !(selectMode && batches.length);
    $('companySelectMode').classList.toggle('is-on', selectMode);
    $('companySelectMode').textContent = selectMode ? '退出多选' : '多选';
    $('companySelectAll').checked = batches.length > 0 && batches.every(batch => selectedBatches.has(batch.id));
    const count = selectedBatches.size;
    const button = $('companyDownloadSelected');
    button.disabled = !count || exportingMany || deletingMany;
    button.textContent = exportingMany ? '保存中…' : count ? `下载 ${count}` : '下载';
    const removeAll = $('companyDeleteSelected');
    removeAll.disabled = !count || exportingMany || deletingMany;
    removeAll.textContent = deletingMany ? '删除中…' : deleteAllArmed ? `确认删除 ${count}` : count ? `删除 ${count}` : '删除';
    removeAll.classList.toggle('is-armed', deleteAllArmed);
  }
  function disarmDeleteAll() {
    if (!deleteAllArmed) return;
    deleteAllArmed = false;
    clearTimeout(deleteAllTimer);
    updateSelectBar();
  }
  $('companySelectMode').onclick = () => {
    selectMode = !selectMode;
    if (!selectMode) { selectedBatches.clear(); disarmDeleteAll(); }
    renderHistory(); updateSelectBar();
  };
  $('companySelectAll').onchange = () => {
    if ($('companySelectAll').checked) batches.forEach(batch => selectedBatches.add(batch.id));
    else selectedBatches.clear();
    renderHistory(); updateSelectBar();
  };
  $('companyDownloadSelected').onclick = async () => {
    if (exportingMany) return;
    const chosen = batches.filter(batch => selectedBatches.has(batch.id));
    if (!chosen.length) { message('先勾选要下载的套图。',true); return; }
    if (!CompanyFiles.supported()) { message('当前浏览器不能直接写文件夹，请改用 Edge 或 Chrome。',true); return; }
    exportingMany = true; updateSelectBar(); renderHistory();
    try {
      const result = await CompanyFiles.saveMany(chosen, ({done,total,name,count}) => message(`正在保存 ${done}/${total}：${name}（${count} 张）`));
      const total = result.items.reduce((sum,item) => sum + item.count, 0);
      message(`已保存 ${result.items.length} 套、共 ${total} 张到「${result.root}」：${result.items.map(item => item.name).join('、')}`);
      toast(`已保存 ${result.items.length} 套图文`);
    } catch(e) { message(e.message,true); toast(e.message); }
    finally { exportingMany = false; updateSelectBar(); renderHistory(); }
  };
  $('companyDeleteSelected').onclick = async () => {
    if (deletingMany || exportingMany) return;
    const chosen = batches.filter(batch => selectedBatches.has(batch.id));
    if (!chosen.length) { message('先勾选要删除的套图。',true); return; }
    if (!deleteAllArmed) {
      // 两步确认：第一次点把按钮变成“确认删除 N 套”，4 秒后自动复位
      deleteAllArmed = true; updateSelectBar();
      clearTimeout(deleteAllTimer);
      deleteAllTimer = setTimeout(disarmDeleteAll, 4000);
      message(`再点一次就删除这 ${chosen.length} 套（记录 + 程序内生图），已下载的图片会保留。`,true);
      return;
    }
    disarmDeleteAll();
    deletingMany = true; updateSelectBar(); renderHistory();
    let done = 0;
    const failed = [];
    try {
      for (const batch of chosen) {
        try {
          await CompanyDelete.submit(batch.id, api);
          done += 1;
          selectedBatches.delete(batch.id);
          if (batchId === batch.id) resetComposer();
          if (selectedId === batch.id) selectedId = null;
        } catch (error) {
          failed.push(`${(batch.title || batch.changes || batch.id).slice(0,12)}：${error.message}`);
        }
        message(`正在删除 ${done + failed.length}/${chosen.length} …`);
      }
      await refreshBatches();
      if (failed.length) {
        message(`已删除 ${done} 套，${failed.length} 套没删掉（可能正在生成）：${failed[0]}`,true);
        toast(`已删除 ${done} 套，${failed.length} 套失败`);
      } else {
        message(`已删除 ${done} 套。记录和程序内生图已清除，已下载的图片保留。`);
        toast(`已删除 ${done} 套图文`);
      }
    } catch (error) { message(error.message,true); toast(error.message); }
    finally { deletingMany = false; updateSelectBar(); renderHistory(); renderResults(); }
  };
  window.addEventListener('keydown',event => {
    if (!document.body.classList.contains('company-mode')) return;
    if ((event.ctrlKey || event.metaKey) && event.key === 'a' && !event.target.closest('input,textarea')) {
      event.preventDefault();
      batches.forEach(batch => selectedBatches.add(batch.id));
      renderHistory(); updateSelectBar();
    }
  },true);

  function renderQueue(state) {
    const summary = (state && state.summary) || {};
    const running = summary.running_count || 0;
    const waiting = summary.waiting_count || 0;
    const channels = summary.usable_channel_count || summary.channel_count || 0;
    $('companyQueueRunning').textContent = running;
    $('companyQueueWaiting').textContent = waiting;
    $('companyQueueChannels').textContent = channels;
    const total = running + waiting;
    $('companyQueueFill').style.width = (channels ? Math.min(100, running / channels * 100) : 0) + '%';
    $('companyQueueHint').textContent = !total
      ? '队列空闲，准备内容后提交。'
      : waiting ? `正在生成 ${running} 张，还有 ${waiting} 张排队。` : `正在生成 ${running} 张，即将完成。`;
  }

  async function refreshBatches() {
    if (loading) return;
    loading = true;
    try {
      const data = await api('/api/company-workflow/batches'); batches = data.batches;
      for (const id of [...selectedBatches]) if (!batches.some(batch => batch.id === id)) selectedBatches.delete(id);
      updateSelectBar();
      if (submitted && batchId && !batches.some(batch => batch.id === batchId)) resetComposer();
      if (!initialized) { selectedId = batches[0]?.id || null; initialized = true; }
      renderHistory(); renderResults();
    } finally { loading = false; }
  }
  function addFiles(files, selection = null) {
    if (busy || submitted) return;
    if (pages.length + files.length > 12) { message('每套最多12张，请分套上传。',true); return; }
    if (files.some(file => !/\.(png|jpe?g|webp)$/i.test(file.name))) { message('请选择 PNG、JPG 或 WebP 图片。',true); return; }
    let added = 0;
    const wasEmpty = !pages.length;
    for (const file of files) {
      if (pages.some(p => p.file.name === file.name && p.file.size === file.size && p.file.lastModified === file.lastModified)) continue;
      pages.push({file,url:URL.createObjectURL(file),selected:true}); added++;
    }
    if (added) { sourceSelectionId = ''; sourceFolder = ''; sourceDirectory = wasEmpty && selection ? selection.directory : null; }
    invalidate(); renderPages(); message(added ? '核对页面顺序，然后填写本次修改要求。' : '这些图片已在本套中，无需重复添加。');
  }
  // Keep the OS file dialog directly attached to the browser's click gesture.
  // A server-owned PowerShell dialog can be hidden behind the browser indefinitely.
  $('companyUpload').onclick = () => { if (!busy && !submitted) $('companyFiles').click(); };
  $('companyBrowserUpload').textContent = '选择参考图文件夹';
  $('companyBrowserUpload').onclick = async () => {
    if (busy || submitted) return;
    try { const directory=await CompanyFiles.pick(); const files=await CompanyFiles.readImages(directory); addFiles(files,{directory}); }
    catch(e) { if(e.name!=='AbortError') message(e.message,true); }
  };
  $('companyFiles').onchange = event => { addFiles([...event.target.files]); event.target.value = ''; };

  $('companyCopyRulesReset').onclick = async () => {
    if (busy) return;
    try {
      const settings = await api('/api/company-workflow');
      $('companyCopyRules').value = (settings.copy_rules_shipped || []).join(String.fromCharCode(10));
      message('已载入出厂规则，点“保存指令与水印设置”后生效。');
    } catch(e) { message(e.message,true); }
  };

  $('companyDownloadDirPick').onclick = async () => {
    if (busy) return;
    $('companyDownloadDirPick').disabled = true;
    try {
      const result = await api('/api/company-workflow/download-dir',{method:'POST'});
      $('companyDownloadDir').value = result.download_dir;
      message(`取图会保存到 ${result.download_dir}`);
    } catch(e) { if (e.status !== 409) message(e.message,true); }
    finally { $('companyDownloadDirPick').disabled = false; }
  };

  async function fetchLinkImages() {
    const text = $('companyLink').value.trim();
    if (!text) { message('先粘贴小红书或抖音的分享内容和链接。',true); $('companyLink').focus(); return; }
    if (busy || submitted) return;
    $('companyLinkFetch').disabled = true; $('companyLinkFetch').textContent = '正在取图…';
    message('正在用本机浏览器打开链接，取图和下载大约需要 20～60 秒，请稍等。');
    try {
      const data = await api('/api/company-workflow/link',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
      const files = [];
      for (const image of data.images) {
        const response = await fetch(image.url);
        if (!response.ok) throw new Error(`第 ${image.name} 张读取失败，请重新取图。`);
        const blob = await response.blob();
        files.push(new File([blob],image.name,{type:blob.type || 'image/webp'}));
      }
      addFiles(files);
      $('companyLink').value = '';
      const clipped = data.truncated ? `。原文共 ${data.total} 张，超过 12 张，已保留前 12 页` : '';
      message(`已取到 ${data.count} 张（${data.title}），保存目录：${data.folder}${clipped}`);
    } catch(e) { message(e.message,true); }
    finally { $('companyLinkFetch').disabled = false; $('companyLinkFetch').textContent = '从链接取图'; }
  }
  $('companyLinkFetch').onclick = fetchLinkImages;

  $('companyAnalyze').onclick = async () => {
    if (busy || submitted) return;
    const selected = pages.filter(page => page.selected);
    if (!selected.length) { message('先取图或上传参考图，再点读图预填。',true); return; }
    if (!confirm(`分析将调用一次已配置的视觉模型，可能产生费用，不自动重试。确认本次分析授权？`)) return;
    $('companyAnalyze').disabled = true; $('companyAnalyze').textContent = '正在读图…';
    message(`正在分析 ${selected.length} 张素材的信息表达与原创规划，请勿重复提交。`);
    try {
      const body = new FormData();
      selected.forEach(page => body.append('reference_images',page.file,page.file.name));
      body.set('analysis_id',makeId()); body.set('confirmed','true');
      const context=await jobFromForm(); body.set('context',JSON.stringify(context));
      const data = await api('/api/company-workflow/analyze',{method:'POST',body});
      if (!$('engineVisual').value.trim()) $('engineVisual').value=(data.analysis.visual_rules || []).join('\n');
      if ($('engineContentMode').value==='brief' && !contentPages().length && data.pages?.length) {
        $('enginePageCopy').value=data.pages.map(p=>[p.title,p.copy].filter(Boolean).join('\n')).join('\n---\n');
        $('enginePageCount').value=String(data.pages.length);
      }
      lastPlanText=data.summary;
      message(`结构化分析完成（${data.model}，${data.elapsed} 秒）。已保留确认文案，补充空白风格与页面规划。请校核资料与文案后生成。`);
      invalidate();
    } catch(e) { message(e.message,true); }
    finally { $('companyAnalyze').disabled = false; $('companyAnalyze').textContent = '读图预填'; }
  };
  $('companyLink').onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); fetchLinkImages(); } };
  const dropzone = $('companyUpload');
  ['dragenter','dragover'].forEach(name => dropzone.addEventListener(name,event => { event.preventDefault(); if (!busy && !submitted) dropzone.classList.add('is-dragging'); }));
  dropzone.addEventListener('dragleave',() => dropzone.classList.remove('is-dragging'));
  dropzone.addEventListener('drop',event => { event.preventDefault(); event.stopPropagation(); dropzone.classList.remove('is-dragging'); addFiles([...event.dataTransfer.files]); });
  function resetComposer() {
    pages.forEach(page => URL.revokeObjectURL(page.url)); pages = []; submitted = false; batchId = null; engineJobId = null;
    sourceSelectionId = ''; sourceFolder = ''; sourceDirectory = null;
    $('companyFiles').value = ''; $('companyChanges').value = ''; lastPlanText = '';
    ['engineTheme','engineAudience','engineProduct','engineFacts','enginePageCopy','engineVisual','engineBrand'].forEach(id=>$(id).value='');
    $('engineApproved').checked=false; $('engineBudget').value='0'; $('engineBrandRights').checked=false;
    $('companyUpload').classList.remove('is-dragging'); renderPages(); message('');
  }
  $('companySubtitleMore').onclick = () => {
    const open = $('companyResultSubtitle').classList.toggle('is-open');
    $('companySubtitleMore').textContent = open ? '收起' : '展开';
  };
  $('companyNew').onclick = () => {
    if (busy || retrying) return;
    resetComposer(); selectedId = null; initialized = true;
    renderHistory(); renderResults(); $('companyUpload').focus();
  };
  ['companyPrompt','companyChanges'].forEach(id => $(id).addEventListener('input',invalidate));
  ['engineMode','engineBinding','engineTheme','engineAudience','engineProduct','engineFacts','enginePageCopy','engineContentMode','enginePageCount','engineVisual','engineRights','engineBrand','engineBrandRights','engineBudget','engineApproved'].forEach(id=>$(id).addEventListener('input',invalidate));
  $('enginePlan').onclick=async()=>{
    try {
      const job=await jobFromForm(); job.content_mode='brief'; job.pages=[];
      const result=await engineApi('/plan',postJson(job));
      $('enginePageCopy').value=result.job.pages.map(p=>[p.title,p.copy].filter(Boolean).join('\n')).join('\n---\n');
      message('已按所给事实组织页面，未调用模型、未产生费用。请核对并补足各页文案。'); invalidate();
    } catch(e){message(e.message,true);}
  };
  $('companyQuality').onchange = invalidate;
  $('companyOpacity').onchange = () => { invalidate(); $('companyPreview').href = `/api/company-workflow/preview?opacity=${$('companyOpacity').value}`; };
  $('companySave').onclick = async () => { try { await api('/api/company-workflow',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:$('companyPrompt').value,opacity:Number($('companyOpacity').value),download_dir:$('companyDownloadDir').value,
        copy_rules:$('companyCopyRules').value.split(String.fromCharCode(10)).map(line => line.trim()).filter(Boolean),
        analysis_base_url:$('companyAnalysisBase').value,analysis_model:$('companyAnalysisModel').value,analysis_protocol:$('companyAnalysisProtocol').value,
        ...($('companyAnalysisKey').value.trim() ? {analysis_api_key:$('companyAnalysisKey').value.trim()} : {})})}); message('基础指令、水印、保存目录和识别模型已保存。'); } catch(e) { message(e.message,true); } };
  async function submitBatch() {
    if (busy || submitted || retrying) return;
    busy = true; renderPages();
    try {
      const job=await jobFromForm();
      message('正在保存这一套，请勿重复提交。');
      const batch = await engineApi('/jobs',postJson(job)); submitted = true; selectedId = batch.id; batchId=batch.id;
      if (sourceDirectory) await CompanyFiles.remember(batch.id,sourceDirectory);
      message('整套已提交，结果会按页码自动出现。'); await refreshBatches();
    } catch(e) { throw new Error(`${e.message} 若连接中断，请先查看最近制作；原样重试沿用同一提交编号。`); }
    finally { busy = false; renderPages(); }
  }
  $('companyPrepare').onclick = async () => {
    if ($('companyPrepare').disabled) return;
    if (!$('companyPrompt').value.trim()) { message('请在更多设置里填写基础指令。',true); return; }
    try { await submitBatch(); }
    catch(e) { message(e.message,true); toast(e.message); }
  };
  $('companyCompareClose').onclick = () => $('companyCompare').close();
  $('engineRevisionClose').onclick=()=>$('engineRevision').close();
  $('engineRevisionSubmit').onclick=async()=>{
    if(!revisionTarget) return;
    const {batch,page,id}=revisionTarget;
    $('engineRevisionSubmit').disabled=true;
    try {
      await engineApi(`/jobs/${batch.external_id}/revisions`,postJson({revision_id:id,
        pages:[{page_number:page.page_number,title:$('engineRevisionHeading').value,copy:$('engineRevisionCopy').value,visual:$('engineRevisionVisual').value}],
        authorization:{approved:true,max_image_requests:1}}));
      $('engineRevision').close(); await refreshBatches(); toast(`仅第${page.page_number}页已提交新版本。`);
    } catch(e){$('engineRevisionMessage').textContent=e.message;}
    finally{$('engineRevisionSubmit').disabled=false;}
  };

  // 点遮罩空白处关闭；点对话框内部的留白（padding）不关闭，避免误触。
  const compareDialog = $('companyCompare');
  compareDialog.addEventListener('click', event => {
    const box = compareDialog.getBoundingClientRect();
    const outside = event.clientX < box.left || event.clientX > box.right
                 || event.clientY < box.top || event.clientY > box.bottom;
    if (outside) compareDialog.close();
  });
  compareDialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); compareDialog.close(); }
  });
  async function saveLocal(batch, chooseFolder = false) {
    if (exporting) return;
    if (!CompanyFiles.supported()) { $('companyZipDownload').click(); toast('当前浏览器不支持直接写文件夹，已改用ZIP下载。可使用Edge或Chrome直接保存。'); return; }
    exporting = true; $('companyDownload').disabled = true; $('companyDownload').textContent = '正在保存…';
    try {
      const result = await CompanyFiles.save(batch,chooseFolder);
      toast(`已保存 ${result.count} 张图片到 ${result.path}，原图和之前的结果已保留。`);
    } catch(e) { if(e.name!=='AbortError') { message(e.message,true); toast(e.message); } }
    finally { exporting = false; resultSignature = ''; renderResults(); }
  }
  $('companyRefresh').onclick = () => refreshBatches().catch(e => message(e.message,true));
  // 独立版：单图工具入口已移除，这两个按钮不存在时不要中断整个工作台脚本
  const nativeEntry = $('companyNative');
  if (nativeEntry) {
    nativeEntry.onclick = async () => {
      try { await window.__codexImageWebUI?.methods?.refreshTasksAfterDeletion?.(); }
      catch { message('单图任务暂时无法刷新，请稍后重试。',true); return; }
      document.body.classList.remove('company-mode');
    };
  }
  const workbenchEntry = $('companyWorkflowOpen');
  if (workbenchEntry) workbenchEntry.onclick = () => { document.body.classList.add('company-mode'); refreshBatches().catch(e => message(e.message,true)); };
  $('companySettings').onclick = () => $('generationProviderSettingsButton').click();
  window.addEventListener('keydown',event => {
    if (!document.body.classList.contains('company-mode')) return;
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); event.stopImmediatePropagation(); if (!document.querySelector('dialog[open]') && !$('companyPrepare').disabled) $('companyPrepare').click(); }
  },true);

  let batchRunning = false;
  function batchRow(text) {
    const li = element('li');
    const status = element('em', '排队中');
    const label = element('span', text);
    li.append(status, label);
    $('companyBatchList').append(li);
    return { li, status };
  }
  function setRow(row, text, state) {
    row.status.textContent = text;
    row.li.classList.toggle('is-done', state === 'done');
    row.li.classList.toggle('is-error', state === 'error');
  }
  $('companyBatchStart').onclick = async () => {
    if (batchRunning) return;
    let payload;
    try {
      payload = JSON.parse($('companyBatchLinks').value);
      if (!Array.isArray(payload.jobs) || !payload.jobs.length || payload.jobs.length > 50) throw new Error('需提交1～50套独立任务。');
      const count = payload.jobs.reduce((sum,job)=>sum+(job.pages?.length || job.page_count || 5),0);
      if (!confirm(`本次 ${payload.jobs.length} 套，共 ${count} 页，预计 ${count} 次生图请求。批量授权上限 ${payload.authorization?.max_image_requests || 0} 次，不自动付费重试。确认文案、供应商费用与额度后继续？`)) return;
      batchRunning = true; $('companyBatchStart').disabled = true;
      const result = await engineApi('/batches',postJson(payload));
      $('companyBatchList').replaceChildren();
      result.jobs.forEach(job=>{const row=batchRow(job.title || job.job_id);setRow(row,job.status,'done');});
      message(`已接收 ${result.jobs.length} 套独立任务，共 ${result.expected_pages} 页，按套排队。`);
      await refreshBatches();
    } catch(e) {message(e.message,true);}
    finally {batchRunning=false;$('companyBatchStart').disabled=false;}
  };

  // ===== 可拖拽分栏：拖动改变宽度，双击复位，宽度记在本机 =====
  (() => {
    const MIN = { nav: 220, composer: 380 };
    const MAX = { nav: 380, composer: 680 };
    const DEFAULT = { nav: 240, composer: 0 };
    const app = $('companyApp');
    const workspace = document.querySelector('.cw-main > .cw-workspace');
    const splitterVars = { nav: '--cw-nav-w', composer: '--cw-composer-w' };
    if (!app || !workspace) return;

    const read = key => {
      const stored = localStorage.getItem('cw-width-' + key);
      if (stored === null) return null;
      const raw = Number(stored);
      return Number.isFinite(raw) && raw >= MIN[key] && raw <= MAX[key] ? raw : null;
    };
    const apply = (key, value) => {
      const target = key === 'nav' ? app : workspace;
      const clamped = Math.round(Math.max(MIN[key], Math.min(MAX[key], value)));
      target.style.setProperty(splitterVars[key], clamped + 'px');
      try { localStorage.setItem('cw-width-' + key, String(clamped)); } catch { /* 本机记忆不可用也不影响拖动 */ }
    };
    const currentWidth = key => {
      const element = key === 'nav' ? app.querySelector('.cw-sidebar') : app.querySelector('.cw-composer');
      return element ? element.getBoundingClientRect().width : DEFAULT[key];
    };

    // 没有记忆值时不要写入内联变量，交给 CSS 的自适应宽度
    for (const key of ['nav', 'composer']) {
      const saved = read(key);
      if (saved !== null) apply(key, saved);
    }

    for (const handle of document.querySelectorAll('.cw-splitter')) {
      const key = handle.dataset.splitter;
      if (!MIN[key]) continue;
      let startX = 0, startWidth = 0, dragging = false;

      handle.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        dragging = true; startX = event.clientX; startWidth = currentWidth(key);
        try { handle.setPointerCapture(event.pointerId); } catch { /* 捕获失败也能拖 */ }
        handle.classList.add('is-dragging');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
      });
      handle.addEventListener('pointermove', event => {
        if (!dragging) return;
        event.preventDefault();
        apply(key, startWidth + (event.clientX - startX));
      });
      const stop = () => {
        if (!dragging) return;
        dragging = false;
        handle.classList.remove('is-dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      };
      handle.addEventListener('pointerup', stop);
      handle.addEventListener('pointercancel', stop);
      handle.addEventListener('lostpointercapture', stop);
      handle.addEventListener('dblclick', () => {
        // 双击复位：清掉记忆值，交还 CSS 的自适应宽度
        localStorage.removeItem('cw-width-' + key);
        (key === 'nav' ? app : workspace).style.removeProperty(key === 'nav' ? '--cw-nav-w' : '--cw-composer-w');
      });
      handle.addEventListener('keydown', event => {
        const step = event.shiftKey ? 40 : 12;
        if (event.key === 'ArrowLeft') { event.preventDefault(); apply(key, currentWidth(key) - step); }
        if (event.key === 'ArrowRight') { event.preventDefault(); apply(key, currentWidth(key) + step); }
      });
    }
  })();

  Promise.all([loadSettings(),refreshBatches()]).catch(e => message(e.message,true));
  setInterval(async () => {
    if (document.hidden) return;
    try {
      await refreshBatches();
      try { renderQueue(await api('/api/queue')); } catch { /* 队列接口偶发失败不影响主流程 */ }
      if (++polls % 4 === 0) await refreshEngineProvider();
    }
    catch(e) { message(e.message,true); }
  },5000);
})();
