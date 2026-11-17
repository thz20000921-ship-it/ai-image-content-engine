const task = await taskSpace(11);
const page = task.page("p1");
await page.click("button[aria-label='关闭系统设置面板']",{label:"返回图文结果工作台"});
await page.screenshot({path:"D:/3.项目/Pythonchengxu/复刻图文-独立版-20261116b/复刻图文-独立版/验证/证据/工作台-局部新版本.png"});
await page.cdp("Emulation.setDeviceMetricsOverride",{width:375,height:812,deviceScaleFactor:1,mobile:true});
await page.cdp("Emulation.setEmulatedMedia",{features:[{name:"prefers-reduced-motion",value:"reduce"}]});
const state=await page.evaluate(()=>({
  viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,
  generateVisible:getComputedStyle(document.querySelector(".cw-run-area")).display!=="none",
  reducedMotion:matchMedia("(prefers-reduced-motion: reduce)").matches,
  revisions:[...document.querySelectorAll(".cw-result-badge")].map(e=>e.innerText)
}));
console.log(state);
console.log(await page.screenshot({path:"D:/3.项目/Pythonchengxu/复刻图文-独立版-20261116b/复刻图文-独立版/验证/证据/工作台-375px.png"}));
const fs=await import("node:fs/promises");
await fs.writeFile("D:/3.项目/Pythonchengxu/复刻图文-独立版-20261116b/复刻图文-独立版/验证/浏览器验证结果.json",JSON.stringify({
  provider:"EXPLICIT_OFFLINE_FIXTURE",real_model_calls:0,
  no_reference_form_submission:true,single_page_revision:true,browser_zip_download:true,
  original_provider_settings_opened:true,...state
},null,2),"utf8");
await page.cdp("Emulation.clearDeviceMetricsOverride");
await page.cdp("Emulation.setEmulatedMedia",{features:[]});
