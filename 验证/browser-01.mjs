const task = await taskSpace("通用图文引擎离线工作台验收");
const page = task.page("p1");
await page.goto("http://127.0.0.1:18789");
console.log({taskSpaceId:task.spaceId,page:page.label});
console.log(await page.snapshot({scope:"full_page"}));
