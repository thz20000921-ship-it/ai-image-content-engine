/* Browser-owned folder permissions. No background native dialogs or arbitrary disk paths. */
(() => {
  'use strict';
  const memory = new Map();
  const supported = () => typeof window.showDirectoryPicker === 'function';
  async function database() {
    return new Promise((resolve,reject) => {
      const request = indexedDB.open('ilab-company-folders',1);
      request.onupgradeneeded = () => request.result.createObjectStore('folders');
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('文件夹记忆暂不可用'));
    });
  }
  async function store(key, value) {
    const db = await database();
    try {
      return await new Promise((resolve,reject) => {
        const tx = db.transaction('folders',value ? 'readwrite' : 'readonly');
        const objectStore = tx.objectStore('folders');
        const request = value ? objectStore.put(value,key) : objectStore.get(key);
        tx.oncomplete = () => resolve(request.result);
        tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error);
      });
    } finally { db.close(); }
  }
  async function remember(key, handle) {
    memory.set(key,handle);
    try { await store(key,handle); return true; } catch { return false; }
  }
  async function recall(key) {
    if (memory.has(key)) return memory.get(key);
    try { return await store(key); } catch { return null; }
  }
  async function pick() {
    if (!supported()) throw new Error('此浏览器不支持文件夹授权，请直接上传图片，完成后下载ZIP。建议使用Edge或Chrome。');
    return window.showDirectoryPicker({id:'ilab-image-set',mode:'readwrite'});
  }
  async function readImages(directory) {
    const files=[];
    for await (const [name,handle] of directory.entries()) {
      if (handle.kind==='file' && /\.(png|jpe?g|webp)$/i.test(name)) files.push(await handle.getFile());
      if (files.length>12) throw new Error('这个文件夹超过12张图片，请改用大上传按钮选择本套图片。');
    }
    if (!files.length) throw new Error('这个文件夹中没有PNG、JPG或WebP图片。');
    files.sort((a,b)=>a.name.localeCompare(b.name,'zh-CN',{numeric:true}));
    return files;
  }
  function folderBase(batch) {
    const raw = batch.title || (batch.company_draft && batch.company_draft.title) || '图文';
    const safe = String(raw).replace(/[\\/:*?"<>|]/g,'').replace(/\s+/g,'').slice(0,30) || '图文';
    const now = new Date();
    const day = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
    return `${day}-${safe}`;
  }
  // 同一天同一套保存两次时自动加 -2、-3，既不覆盖前一次也不报错。
  async function uniqueName(directory, base) {
    for (let n = 1; n <= 99; n++) {
      const name = n === 1 ? base : `${base}-${n}`;
      try { await directory.getDirectoryHandle(name); }
      catch(e) { if (e.name === 'NotFoundError') return name; throw e; }
    }
    throw new Error('同一天同名文件夹太多，请先整理文件夹再保存。');
  }
  async function save(batch, choose = false) {
    let directory = choose ? null : await recall(batch.id);
    if (!directory) directory = await pick();
    if (await directory.queryPermission({mode:'readwrite'}) !== 'granted' && await directory.requestPermission({mode:'readwrite'}) !== 'granted') throw new Error('未获得所选文件夹的写入权限，请重新选择或下载ZIP。');
    await remember(batch.id,directory);
    const pages=batch.pages.filter(p=>p.url).sort((a,b)=>a.page_number-b.page_number);
    if (!pages.length) throw new Error('还没有可以保存的成功图片。');
    // A unique new subfolder prevents overwriting source images or an earlier export.
    const name = await uniqueName(directory, folderBase(batch));
    const output=await directory.getDirectoryHandle(name,{create:true});
    let count=0;
    try {
      for (const page of pages) {
        const response=await fetch(page.url);
        if(!response.ok) throw new Error(`第${page.page_number}页读取失败`);
        const blob=await response.blob();
        const extension=blob.type.includes('jpeg')?'jpg':blob.type.includes('webp')?'webp':'png';
        const file=await output.getFileHandle(`${String(page.page_number).padStart(2,'0')}-第${page.page_number}页.${extension}`,{create:true});
        const writer=await file.createWritable();
        try { await writer.write(blob); await writer.close(); } catch(e) { await writer.abort().catch(()=>{}); throw e; }
        count++;
      }
    } catch(e) { throw new Error(`已保存${count}张，保存中断：${e.message}。原图和程序中的结果均保留，可重新保存。`); }
    return {count,path:`${directory.name}\\${name}`};
  }
  // 批量保存：选一次根目录，每套一个「日期-标题」子文件夹
  async function saveMany(batches, onProgress) {
    const usable = (batches || []).filter(batch => (batch.pages || []).some(page => page.url));
    if (!usable.length) throw new Error('所选套图都还没有可保存的结果。');
    const directory = await pick();
    if (await directory.queryPermission({mode:'readwrite'}) !== 'granted' && await directory.requestPermission({mode:'readwrite'}) !== 'granted') throw new Error('未获得所选文件夹的写入权限，请重新选择或下载ZIP。');
    const items = [];
    for (const [index, batch] of usable.entries()) {
      const pages = batch.pages.filter(page => page.url).sort((a, b) => a.page_number - b.page_number);
      const name = await uniqueName(directory, folderBase(batch));
      const output = await directory.getDirectoryHandle(name, {create: true});
      await remember(batch.id, directory);
      let count = 0;
      for (const page of pages) {
        const response = await fetch(page.url);
        if (!response.ok) throw new Error(`《${name}》第${page.page_number}页读取失败，已保存 ${items.length} 套。`);
        const blob = await response.blob();
        const extension = blob.type.includes('jpeg') ? 'jpg' : blob.type.includes('webp') ? 'webp' : 'png';
        const file = await output.getFileHandle(`${String(page.page_number).padStart(2, '0')}-第${page.page_number}页.${extension}`, {create: true});
        const writer = await file.createWritable();
        try { await writer.write(blob); await writer.close(); } catch (error) { await writer.abort().catch(() => {}); throw error; }
        count += 1;
      }
      items.push({id: batch.id, name, count, path: `${directory.name}\${name}`});
      if (onProgress) onProgress({done: index + 1, total: usable.length, name, count});
    }
    return {root: directory.name, items};
  }
  window.CompanyFiles={supported,pick,readImages,remember,save,saveMany};
})();
