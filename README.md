# 本机 AI 图文生产引擎

从主题、真实资料和逐页文案生成多页图文，可选参考素材；支持独立任务批量提交、进度查询、PNG/ZIP交付和指定页新版本。

此仓库供代码审核，先阅读 [代码审核说明.md](代码审核说明.md)。包含完整应用源码、已构建的前端静态资源、依赖锁文件、API 文档和离线测试；不上传个人配置、密钥、任务数据、图片结果、源码备份或随包 Python/EXE。

## 从源码启动

需要 Python 3.11 或更新版本。Windows PowerShell 在仓库根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\runtime\app\requirements-webui.txt
.\.venv\Scripts\python.exe .\runtime\app\run_engine.py --data-dir "$env:USERPROFILE\图文引擎审核数据"
```

默认访问 `http://127.0.0.1:8787`，端口冲突时加 `--port 8788`。示例使用独立审核数据目录，不读取现有个人配置；供应商及模型在原有设置界面配置。

本地便携版可双击 `启动图文引擎.cmd`；该脚本需要便携包内的 `runtime/python/python.exe`，GitHub 源码副本请使用上面的启动命令。旧的 `启动复刻图文.cmd` 也已切换到新入口。

## 文档与验证

- `通用引擎使用说明.md`：模式、手动操作、启动、设置迁移与回退。
- `Agent API.md`：本机Bearer接口、输入输出、额度、批量、修订及GoFind对接。
- `示例/agent_client.py`、`示例/原创任务.json`：可直接运行的独立HTTP客户端与完整任务。
- `去业务耦合修改清单.md`、`改造审计与设计.md`：改造范围、兼容决定、权利待确认事项。
- `通用引擎测试报告.md`：20个离线集成用例、本机真实HTTP、独立启动与浏览器检查，以及未验证事项。

```powershell
.\.venv\Scripts\python.exe .\验证\verify_engine.py
.\.venv\Scripts\python.exe .\验证\verify_http.py
.\.venv\Scripts\python.exe .\验证\verify_launcher.py
```

本轮真实模型授权为0元0次，真实生图/视觉模型未验收；测试图片全部明确标记离线模拟。没有修改GoFind或橘子智剪。历史报告中的“未公开发布”描述的是开发验收当时的状态；此源码审核副本按用户后续指令整理。

原有公司版权文件、第三方声明及AGPL字段保留。技术改造不等于产权转移，使用/分发授权与许可冲突仍需确认。
