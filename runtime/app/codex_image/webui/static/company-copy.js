/* Local prompt preparation only. No model analysis or additional API request. */
(() => {
  'use strict';
  let policy;
  const loading = fetch('/api/company-workflow/copy-rules', {cache:'no-store'})
    .then(response => {
      if (!response.ok) throw new Error('标题与编号规则加载失败，请刷新页面。');
      return response.json();
    }).then(value => {
      if (typeof value.marker !== 'string' || !Array.isArray(value.rules) || !value.rules.length ||
          value.rules.some(rule => typeof rule !== 'string')) throw new Error('标题与编号规则无效，请刷新页面。');
      policy = value;
    }).catch(() => {});

  async function compose(master, total, selected) {
    await loading;
    if (!policy) throw new Error('标题与编号规则未就绪，请刷新后重试。');
    if (!Number.isInteger(total) || total < 1 || total > 12 || !Array.isArray(selected) || !selected.length ||
        selected.some((page, i) => !Number.isInteger(page) || page < 1 || page > total || (i && page <= selected[i-1]))) {
      throw new Error('本套页序无效，请重新核对参考图。');
    }
    const clean = master.split('\n\n' + policy.marker)[0].trimEnd();
    const rows = selected.map((page, i) => page === 1
      ? `上传第1页 → 成品第${i+1}页，封面，不占正文主序号。`
      : `上传第${page}页 → 成品第${i+1}页；若为单个知识点内容页，原有跨页主序号改为${String(i+1-(selected.includes(1)?1:0)).padStart(2,'0')}；若为总结页或多知识点页，按上述例外处理。`);
    const result = `${clean}\n\n${policy.marker}\n${policy.rules.join('\n')}\n【本次保留页清单：程序计数，不能沿用参考图旧数字】\n上传${total}页，本次保留${selected.length}页、输出${selected.length}张图；保留上传页码：${selected.join('、')}。未选页不计入本次成品。技巧总数尚未由程序识别，不得将图片张数当作技巧数。\n${rows.join('\n')}`;
    if (result.length > 8000) throw new Error('基础指令过长，请精简后重试；需为标题和编号规则预留空间。');
    return result;
  }
  window.CompanyCopy = {compose};
})();
