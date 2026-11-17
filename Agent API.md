# 本机图文生产 API v1

默认地址 `http://127.0.0.1:8787`。JSON UTF-8。服务不开放公网，不需要合并 GoFind 源码。

## 启动与调用授权

运行 `启动图文引擎.cmd`（原 `启动复刻图文.cmd` 也已接入新入口）。
供应商继续在原来的「API 与系统设置」配置。分析模型继续在工作台「参考与品牌素材 → 更多设置」配置。

```powershell
.\runtime\python\python.exe .\runtime\app\run_engine.py --port 8788 --data-dir "D:\你的个人数据目录"
```

数据目录优先级：`--data-dir` > `ILAB_CONJURE_DATA_DIR` > `%USERPROFILE%\复刻图文-Data`。
直接使用旧 exe/`portable_webui_app.py` 时仍遵循原有环境变量和 `runtime/data` 后备位置。
新入口不会更换已有供应商密钥文件。`IMAGE_ENGINE_PORT` 可设置端口；8787 被占用时报告冲突，不结束旧程序。

所有 `/api/engine` 调用需要：

```http
Authorization: Bearer <本机 Agent 令牌>
```

令牌文件：`<数据目录>/webui-outputs/source-data/company-workflow/agent-token.txt`。
它是本机调用凭证，与供应商 API Key 不同。不要写进源码、URL、日志、示例输出或报告。
可用 `IMAGE_ENGINE_TOKEN` 设置本机调用令牌。工作台通过同源会话获取它，不在页面持久化存储。
原有 Host、Origin、请求体大小保护仍生效。远程地址或跨站请求会被拒绝。

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/engine/status` | 模型配置状态、模式、限制、已声明价格、升级提示；不返回 Key |
| POST | `/api/engine/assets` | multipart `images`，返回素材 ID；不计费 |
| POST | `/api/engine/plan` | 根据任务事实本地组织页面，不调用模型、不计费 |
| POST | `/api/engine/analyze` | 可选结构化视觉/内容分析，单独明确授权一次调用 |
| POST | `/api/engine/estimate` | 输出页数、初始请求数、总授权范围和可用价格信息；不计费 |
| POST | `/api/engine/jobs` | 提交一套创作任务 |
| GET | `/api/engine/jobs` | 最近100套引擎任务 |
| GET | `/api/engine/jobs/{task_id}` | 当前版本、每页状态、成功/失败数、错误、额度及真实结果路径 |
| GET | `/api/engine/jobs/{task_id}/pages/{number}/image` | 下载当前版本 PNG；`?version=1` 下载旧版本 |
| GET | `/api/engine/jobs/{task_id}/download` | 完整套图 ZIP，未完成时返回409 |
| POST | `/api/engine/jobs/{task_id}/revisions` | 只重生成指定页，使用独立修订额度 |
| POST | `/api/engine/jobs/{task_id}/retry` | 显式重试失败页，使用原剩余额度 |
| POST | `/api/engine/jobs/{task_id}/cancel` | 取消未完成页，保留成功结果及版本 |
| POST | `/api/engine/jobs/{task_id}/resume` | 恢复未提交完、未调用或已有原图缓存的页 |
| POST | `/api/engine/jobs/{task_id}/pages/{number}/text-review` | 对实际抄录/OCR文字与准确文案做校核记录 |
| POST | `/api/engine/batches` | 一次接收多个独立任务 |
| GET | `/api/engine/batches/{batch_id}` | 查询整批及各套状态 |
| GET | `/api/engine/batches/{batch_id}/download` | 每套独立子目录的完整批量 ZIP |

FastAPI 的本机 `/docs` 和 `/openapi.json` 提供全部字段类型。业务错误使用 HTTP 400/409，结构错误使用422，缺少令牌401，本机/来源限制403。写盘中断保留已有清单，不得换ID盲目重发。

## 单套请求

完整可运行示例见 `示例/原创任务.json`。主要字段：

| 字段 | 含义 |
|---|---|
| `task_id` | 调用方稳定唯一 ID，1～96位字母、数字、下划线、点、连字符，首位为字母或数字 |
| `mode` | `original` 无参考原创；`inspired` 参考启发原创；`edit` 自有/获权页面编辑 |
| `project` / `theme` / `audience` / `purpose` | 项目、主题、受众、用途；主题必填 |
| `product.name/facts/boundaries` | 真实产品名称、已确认事实列表、不可宣称/未确认内容边界 |
| `title` / `key_points` / `requirements` | 标题、已确认关键点、本次明确要求 |
| `content_mode` | `final` 逐字执行已确认文案；`brief` 根据所给事实组织页面 |
| `page_count` | brief 没有 pages 时的输出页数，1～12 |
| `pages` | 从1连续编号，包含角色、标题、正文、画面要求和本页素材 ID |
| `references` | 所有允许使用的素材及来源、权利、用途，最多24个 |
| `visual_rules/transferable_methods/avoid` | 全套风格、可借鉴的通用手法、需要避开的内容 |
| `generation` | 原有供应商/模型绑定、尺寸、质量、可选模型原生参数 |
| `brand` | 可选本地 Logo，默认关闭 |
| `authorization` | 是否获准执行、图片请求上限、可选金额上限/币种 |

`final` 必须提供 `pages`，每页至少有标题或正文。提供 pages 时实际输出页数以 pages 为准；`page_count` 不会把已有最终文案扩成更多页。brief 的本地规划只使用所给事实，不能代替创意/产品审核；需要更充分规划可单独使用已配置的分析模型，再检查并提交 final。

```json
{
  "task_id": "gofind-topic-001",
  "mode": "original",
  "theme": "已确定的新主题",
  "product": {"name": "真实产品", "facts": ["已确认的事实"], "boundaries": ["不得宣称未验证的效果"]},
  "content_mode": "final",
  "pages": [
    {"page_number": 1, "role": "cover", "title": "准确标题", "copy": "准确正文", "visual": "原创信息图"},
    {"page_number": 2, "role": "steps", "title": "真实流程", "copy": "已经确认的步骤"}
  ],
  "references": [],
  "generation": {"size": "1024x1536", "quality": "low"},
  "authorization": {"approved": true, "max_image_requests": 2}
}
```

示例中的 `approved: true` 表示调用方已经取得这两次请求的实际费用授权；不能把 API 令牌的存在视作费用授权。本轮开发没有获得真实请求授权，未向真实模型发送此请求。

### 素材与编辑

通过 multipart 导入文件，取返回的 `assets[].id`：

```python
with open("reference.png", "rb") as f:
    result = client.post("/api/engine/assets", files=[("images", ("reference.png", f, "image/png"))])
asset_id = result.json()["assets"][0]["id"]
```

然后在任务中声明：

```json
{
  "references": [
    {"asset_id": "<64位素材ID>", "source": "来源或素材说明", "rights": "inspiration", "purpose": "reference"},
    {"asset_id": "<真实截图ID>", "source": "自有产品截图", "rights": "owned", "purpose": "product_material"}
  ]
}
```

- `rights` 为 `inspiration`、`owned`、`licensed`。编辑母版只能使用 owned/licensed。
- 第三方启发素材不用于去水印再包装。原始行业主题不能覆盖新主题或产品事实。
- inspired 默认只向第一页附带第一张参考，其余页独立原创；需要更多素材时，在当前页 `asset_ids` 明确列出，最多4个。不默认把所有参考发送给每页。
- edit 必须为每个输出页明确选择授权母版（第一页可默认第一张）；没有指定其他页母版时拒绝请求。
- 产品截图等素材也通过 `pages[].asset_ids` 指定。真实素材优先；不得生成假截图冒充实测。
- 一张参考封面可以生成5页；多个来源也可共同提供全套方向。素材数量、单套页数、套数互相独立。
- 原来的本地文件/文件夹选择与 `/api/company-workflow/link` 导入保留。链接导入仅在平台正常允许访问时工作，遇到登录/验证码/访问限制停止。source URL 本身不会触发生图引擎抓取。

品牌：

```json
{"brand": {"enabled": true, "logo_asset_id": "<自有Logo素材ID>", "rights_confirmed": true, "width_ratio": 0.14}}
```

按原比例在本地右下角合成，保留原颜色与透明度；模型不画 Logo。默认不使用旧公司 Logo。原始未叠加图片继续缓存。

### 模型与费用

`generation.provider_id`、`model_id`（本地模型目录ID）、`binding_id` 可明确选择；未指定时使用当前供应商的唯一/默认可生成绑定。有多个绑定而无法判定时返回400，不擅自选择第三方服务。
远程模型名、Base URL、Key 继续从原有供应商设置读取并写入任务执行快照；不会把 Key 保存到任务或返回结果。
GPT Images 使用 `size/quality`，`n=1`；其他原有模型可在 `generation.parameters` 传入当前模型目录支持的参数。未知参数会被原有解析器拒绝。其他供应商的真实兼容性本轮未验证。

每套最多12页；每批最多50套；每页最多4个附件；每套最多24个素材。导入单图和总图片大小继续使用原有校验（HTTP 请求192MiB、任务素材128MiB，另有单图像素/大小上限）。单次ZIP最大512MiB。

默认每页一次请求，不自动付费重试。`max_image_requests` 是硬请求上限，包含失败及结果不确定的调用。每次发送前持久化预占，重启不清零；极端写盘中断可能保守多计一次，不会因此多发请求。

供应商未配置价格上限时 `/status` 与 `/estimate` 明确显示未知，只支持次数额度。不能由本项目保证实际货币费用。可由本机操作者在工作流个人 settings.json 的 `pricing` 填写经过核对、适用于所用供应商/模型/尺寸/质量的单次最高价格：

```json
{"pricing": {"per_request_ceiling": 0.5, "currency": "CNY"}}
```

该信息是操作者声明，不是自动查询得到的报价。提交 `authorization.max_cost/currency` 时，必须有此价格且“请求上限×单次价格上限”不超过金额授权，否则拒绝。真实结算以供应商为准，价格不确定时使用明确次数授权并先在供应商核价。

### 分析

```json
{"analysis_id": "analysis-topic-001", "job": "<上面的完整任务对象>", "approved": true, "max_analysis_requests": 1}
```

实际 `job` 必须为对象。`/api/engine/analyze` 使用原有分析模型配置；一次调用读取最多12张参考或只读取任务文字。返回 sources（来源、实际内容、角色、通用方法、不能复制的内容）、pages、visual_rules、missing_facts、warnings。
来源只采信任务声明；最终主题、受众和 final 文案由程序保留，模型不能覆盖。分析结果需审核后带回创作任务，不隐式触发图片生成。
相同 analysis_id 重发复用结果，内容不同409；异常/中断不自动重试，避免重复费用。

## 查询、修订、重试与恢复

任务返回 `job_id`（调用方ID）、`id/batch_id`（内部套图ID）、`pages[].task_id/version/versions`、完成/失败计数、错误、预算使用、PNG下载地址、绝对输出路径及ZIP地址。
状态包括 queued、running、completed、partial_failed、failed、cancelled、preparing。completed 仅表示文件已生成，不等于文字及产品真实性已验收。每页保留准确文案、文件解码/比例检查和校核状态。

只改第3页标题：

```python
response = client.post("/api/engine/jobs/gofind-topic-001/revisions", json={
    "revision_id": "fix-page-3-title-v1",
    "pages": [{"page_number": 3, "title": "新的准确标题"}],
    "authorization": {"approved": True, "max_image_requests": 1}
})
response.raise_for_status()
```

只提供要修改的字段；其他文案与页面继续保留。可同时修改 copy/visual/asset_ids，或在 `references` 中加入新截图。只生成列出的页面，其他页不计费、不替换。旧版本可 `?version=1` 下载。
如果原始产品事实有误，可同时提供完整的新 `product`（name/facts/boundaries）以及需要纠正的页面文案，新的生成以修正事实为准；调用方需明确列出全部受影响页面，未列出的历史成品继续保留。
`instructions` 表达额外修订要求；整个套图统一风格要提交新的 `visual_rules` 并明确列出全部受影响页面及相应额度。
修订使用独立、明确的新额度，不偷偷提高原额度。相同 revision_id 幂等，不同内容409。

失败页重试：

```json
{"request_id": "retry-page-3-once", "page_numbers": [3], "acknowledge_uncertain_charge": true}
```

原额度不足返回409，未发新请求；已有原始结果缓存时本地恢复不消耗次数。重试ID也幂等；不能反复换ID无限消耗额度。
取消不会撤销已发送上游请求的可能费用。恢复未发送页和缓存页；已发出但中断且无缓存的页标失败，需核对供应商记录后显式重试。已保存而未完整入队的任务可沿用原请求ID重发或调用 resume。

校核文字：

```json
{"observed_text": "从实际图片人工抄录或外部OCR得到的标题及正文"}
```

返回 matched/mismatch，忽略换行空格。方法标为 caller_supplied_transcription，这不是内置 OCR，不能把粘贴预期文案当作图片正确的证明。关键错字、数字、品牌和产品事实仍需人工/真实视觉模型核对；可仅重生成出错页。

## 多套批量生产

```json
{
  "batch_id": "campaign-001",
  "jobs": ["<第一套完整Job对象>", "<第二套完整Job对象>"],
  "authorization": {"approved": true, "max_image_requests": 9}
}
```

实际 jobs 是对象列表，每套有自己的 task_id、主题、pages、references、authorization。
例如第一套5页、第二套4页，总初始请求9次。批量授权与单套授权同时生效，任一额度达到上限就停止新增请求。
提交前可对各 Job 调用 estimate，相加得到预期请求数；提交接口先校验全部模型计划和额度，再释放到现有队列，整套依次执行。
同一素材/链接可供不同主题复用；计费防重基于任务ID和内容指纹，不再基于来源链接。相同ID内容不同返回409；删除过的任务ID返回410，不能用删除后重发绕过幂等。

## GoFind 对接约定

GoFind 提供稳定任务ID、模式、主题/受众/用途、真实产品事实和边界、final/brief状态、逐页文案或规划目标、可用素材及权利、生成绑定、品牌选项和已经获得的费用额度。
本引擎返回任务/页进度、错误、请求额度使用、每页版本与下载路径、整套/批量ZIP、待审核项目。
GoFind 负责选题、客户研究、营销策略、费用授权和平台发布；本引擎负责理解、执行、生成、基本检查与文件交付。
