# 电子管家（Butler）项目上下文快照

> 生成时间：本文档为对话存档，记录截至目前的项目理解、已解决问题、踩过的坑、
> 以及下一步核心目标。用于会话中断/换人接手时快速恢复上下文。

---

## 1. 项目定位

一个私人"电子管家" AI Agent：多模态输入（文字/语音/图片）、加密记忆、
用户画像构建、按语气和时机智能提醒、兼具"电子监督"（对拖延/沉默的追问）能力。

**核心硬约束（用户明确提出，指导了几乎所有架构决策）**：

> 这个系统后续可能会不停替换模型选型，所以要给出迭代的空间，同时要保证
> 迭代前后数据安全且不丢失。

由此确立的设计原则：**文本是真相，向量是缓存，schema 有版本，模型是插件。**

## 2. 硬件与技术分层

- 当前机器：RTX 3060 12GB VRAM、i5-12490F（6c/12t）、16GB 单通道 DDR4-2667、
  多块 1TB 盘（Samsung 980、NVMe D 盘、WD Elements 外置）。偶尔用来打游戏，
  因此必须支持本地/云端模型的实时切换，且**不能靠硬编码游戏进程名**，而是
  通用的资源占用监控（排除管家自身进程后，其余软件占用了多少 VRAM/RAM/CPU）。
- 经济型 vs 性能型（"经济自由之后"）技术选型分层已在早期方案中给出，
  当前实现聚焦经济型：本地 Ollama（Qwen2.5-14B/7B）+ DeepSeek 云端兜底。
- 全程加密：Fernet 对称加密 + PBKDF2 派生密钥，主密码存 Windows 密钥链
  （keyring），或环境变量/交互输入兜底。

## 3. 目录结构（`D:\butler\`）

```
butler/
├── config.yaml        静态参数（路径/阈值/语气/提醒策略/vision/speech）
├── models.yaml        模型注册表（迭代入口，backend/kind/model/dim/context/cost）
├── .env.example       密钥模板（DEEPSEEK_API_KEY / BARK_* / TELEGRAM_* / 主密码）
├── requirements.txt   依赖（纯 ASCII 注释，见下方 bug）
├── run.py             CLI 入口：repl / serve / status / doctor
├── data/              加密数据 + 向量 + 备份（.gitignore 排除，勿提交）
└── src/
    ├── config.py       配置加载（.env + config.yaml + models.yaml）
    ├── crypto.py        Fernet 加密 + 主密钥获取（env→keyring→交互）
    ├── resource_monitor.py  实时 VRAM/RAM/CPU 压力评级（排除自身进程）
    ├── scheduler_model.py   压力→模型角色 映射 + 防抖 + 可用性回退链
    ├── llm.py / registry.py 后端抽象（Ollama/DeepSeek/OpenAI兼容）+ 角色注册表
    ├── memory.py        加密 SQLite（真相源）+ Chroma 向量（可重建缓存）
    ├── versioning.py     schema 迁移（PRAGMA user_version）+ 嵌入模型变更检测
    ├── backup.py         快照/回滚（迁移前自动备份，保留最近 10 份）
    ├── profile.py        用户画像（加密 JSON，additive 迁移）
    ├── settings.py        运行时可调设置（与 config.yaml 静态默认分离，前端友好）
    ├── reminder.py         提醒/监督引擎（APScheduler tick）
    ├── notify.py           推送（Bark → Telegram → 控制台兜底）
    ├── speech.py         【本轮新增】语音转文字（faster-whisper，本地 CPU）
    ├── vision.py         【本轮新增】图像理解（压缩预处理 + MiniCPM-V via Ollama）
    ├── api.py             FastAPI 层（前端后端，含多模态上传端点）
    ├── static/
    │   └── index.html      【本轮新增】单文件前端（原生 JS，零构建）
    └── butler.py           主控编排（串联以上所有模块）
└── tests/                  【本轮新增】永久测试套件（隔离环境，绝不碰真实 data/）
    ├── isolation.py         共享夹具：临时目录重定向 + 一次性密码 + check/report
    ├── run_all.py           子进程逐个跑各测试文件，--fast 跳过依赖真实模型的项
    ├── test_migrate.py       schema v1→v2 迁移安全性
    ├── test_backup.py        WAL + 快照/恢复正确性
    ├── test_reminder2.py     提醒退避 / 配额 / 拖延画像
    ├── test_smoke.py         API 全端点 30 项冒烟
    └── test_due.py           due_at 时间幻觉修复 + 真实对话复测
```

## 4. 已完成并验证的能力

### 4.1 基础架构（早期已完成）
- 环境自检 `python run.py doctor`：依赖检查、配置加载、资源快照、后端可用性。
- 资源监控：实时 VRAM/RAM/CPU 占用（排除管家自身及其子进程），映射到
  low/medium/high/critical 四级压力。
- 模型调度：压力等级 → 角色（chat_large/chat_small/chat_cloud）+ 冷却防抖 +
  可用性回退链（大模型不可用就退小模型，都不行退云端）。
- 加密：Fernet 加密/解密往返验证通过；**直接 grep 原始文件字节确认无明文泄露**。
- 记忆/任务/画像 CRUD 全部验证可用。
- 嵌入模型迁移安全机制：换 embed 模型 → 自动快照 → 从加密真相源重嵌入 →
  校验向量数量与文本数量严格一致 → 原子切换 collection → 旧 collection 保留可回滚。

### 4.2 可调设置 + HTTP API（上一轮完成）
- `src/settings.py`：10 个可调字段（免打扰起止、每日提醒上限、检查频率、
  默认提前量、监督开关、沉默问候阈值、语气、是否用 emoji、管家称呼），
  每个字段带类型/范围/选项/标签/分组元数据，持久化到 `data/settings.json`
  （明文，非敏感），与 `config.yaml` 的静态默认分离。
- `GET /settings/schema` 让前端无需硬编码表单即可自动渲染滑块/开关/下拉。
- FastAPI 层（`src/api.py`）：`/health` `/status` `/settings` `/settings/schema`
  `/chat` `/tasks`(CRUD) —— 全部通过 TestClient 端到端冒烟测试
  （含 PUT /settings 故意传非法值验证校验生效）。
- `tone_hint`（对话中学到的语气偏好）统一路由到 `Settings`，不再和
  `Profile` 分裂存储两份。

### 4.3 多模态输入（本轮新增，刚完成）
- **语音**：`src/speech.py` 的 `Transcriber` 类，faster-whisper 本地转写，
  默认 CPU + int8（避免和聊天/视觉模型抢 GPU 显存），惰性加载模型
  （首次调用才占内存），`transcribe_bytes()` 直接吃字节流（BytesIO），
  无需落地临时文件。
- **图片**：`src/vision.py` 的 `VisionHelper` 类，Pillow 预处理
  （转 RGB、按 `vision.max_dimension`=1280 等比压缩长边、转 JPEG），
  再 base64 送 Ollama 的 MiniCPM-V（`kind: vision` 角色，走 `/api/chat`
  的 `images` 字段）。
- **关键设计**：语音转文字 / 图片转描述后，**都只是生成一段文字，
  再喂给已有的 `Butler.chat()` 管线**（`chat_with_voice()` /
  `chat_with_image()`），因此记忆写入、画像更新、任务提取、语气化回复
  对多模态输入**零特殊逻辑复用**，不用重复实现一遍。
- API 新增端点：`POST /input/voice`（form-data `file`）、
  `POST /input/image`（form-data `file` + 可选 `text`）。
- `/status` 新增 `speech_available` 字段；`vision` 的可用性已包含在
  原有 `backends` 字典里（复用 registry 的角色可用性检查）。
- 依赖：`faster-whisper`、`pillow`、`python-multipart`（FastAPI 文件上传
  必需）——已加入 `requirements.txt` 并 `pip install` 成功。

### 4.4 前端页面 + API 补齐（本轮新增）
- **`src/static/index.html`**：单文件前端（原生 HTML/CSS/JS，零构建零依赖），
  由 FastAPI 以 `StaticFiles(html=True)` 挂载在 `/`。四个标签页：
  - **聊天**：气泡式对话、Enter 发送 / Shift+Enter 换行、输入框自适应高度；
    图片上传（可附文字）；`MediaRecorder` 录音上传；请求期间锁输入避免并发
    压垮本地模型；刷新后自动从 `/history` 恢复历史。
  - **任务**：待办/已完成切换、新建（内容+分类+截止时间）、完成/撤销/改内容/
    删除；逾期红色标注；`datetime-local` 输入自动补本地时区转 ISO。
  - **设置**：完全由 `/settings/schema` 驱动渲染（int→滑块、bool→开关、
    choice→下拉、str→文本框），前端不硬编码任何字段 —— 后端加一项设置，
    前端自动出现，不用改前端代码。
  - **状态**：显存/内存/CPU 占用条、压力等级、当前角色、各后端可用性。
  - 顶栏常驻模型/压力/待办数，20 秒轮询 `/status`。
- **API 补齐（做前端时发现的缺口）**：
  - `GET /history?limit=N` —— 新增。从 episodes 真相源解析
    `[用户] x\n[管家] y` 还原对话回合，前端刷新不再丢历史。
  - `PATCH /tasks/{id}` —— 修好 `content` 字段（原先声明了却完全不处理），
    并支持 `status=pending` 撤销完成；非法 status / 空 content 返回 400。
  - `DELETE /tasks/{id}` —— 原先在 api.py 里写裸 SQL，已收敛到
    `memory.delete_task()`（项目其余部分所有 SQL 都在 memory.py）。
  - `memory.py` 新增 `recent_episodes()` / `reopen_task()` /
    `update_task_content()` / `delete_task()`。
  - `app.state.butler` 暴露实例，供测试与未来路由复用。
- **验证**：隔离环境（临时 config.yaml 重定向所有 `paths.*` + 一次性主密码）
  下 30 项冒烟测试全通过，含静态挂载未遮蔽 API 路由、schema 元数据完整性、
  任务 CRUD 全流程、`/history` 解析、多模态端点参数校验；另跑通真实
  qwen2.5:14b 端到端对话（记忆写入 + 任务提取 + 历史回读）。

### 4.5 提醒/监督引擎修正（本轮）
做完前端后审查了此前没细看的 `reminder.py` / `profile.py` / `versioning.py`，
用实测（而非读代码）发现提醒与画像两条链路都是坏的，见 bug #13～#15。修正后：
- **逾期追问有退避**：阶梯 `(1, 3, 6, 12, 24)` 小时，靠 schema v2 新列
  `tasks.last_reminded_at` 驱动。实测 5 次连续 tick 只发 2 条（每任务 1 条），
  修复前是 8 条打满配额。
- **配额跨重启**：从 `events` 表实时统计，不再用内存计数。
- **拖延画像在"完成任务"时记账**，而非"发提醒"时；首次接上
  `record_reliable()`，拖延分从只有 0.3/1.0 两档变成真正的比例值。
- **schema v1→v2 迁移已实测**：造真 v1 库（含加密数据）迁移后，加密载荷
  逐字节完好、老行新列为 NULL、反复执行幂等。真实库当前是空的 v1，
  下次启动自动升级，无风险。

### 4.6 WAL + 备份加固、永久测试套件（本轮新增）
- `memory.py` 打开 `PRAGMA journal_mode=WAL` + `synchronous=NORMAL`，提升
  APScheduler 后台线程与 API 请求线程并发读写时的稳健性。
- 配套修复 `backup.py`：WAL 模式下最近事务可能还没落回主 `.db` 文件，原来
  `shutil.copy2()` 裸复制会漏数据；改用 SQLite 官方 `conn.backup()` API。
  `restore()` 额外清理恢复后残留的 `-wal`/`-shm`，且清理时机放在
  pre_restore 快照**之后**（快照本身会重新生成 -wal，顺序反了会白清）。
  同时把此前漏备份的 `settings.json` 补上。
- 新增 `tests/` 永久测试套件（此前都是一次性临时脚本，本轮收纳进项目）：
  `isolation.py` 提供共享夹具——把 `config.yaml` 里所有 `paths.*` 正则替换
  重定向到临时目录、设一次性 `BUTLER_MASTER_PASSWORD`，**绝不触碰真实
  `D:/butler/data`**；`run_all.py` 用子进程逐个跑各测试文件（每个套件持有
  独立的 config 单例 + SQLite 连接，同进程跑会打架），`--fast` 跳过依赖真实
  Ollama 模型的 `test_due.py` 复测项。5 个套件全部通过。

## 5. 遇到的 Bug 与修复（按时间顺序）

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | `pip install` 报 `UnicodeDecodeError: 'gbk' codec can't decode byte 0x80` | `requirements.txt` 里的中文注释在 GBK locale 的 Windows 上被 pip 用 GBK 解码失败 | 改成纯 ASCII 注释 |
| 2 | 控制台打印 `✓`/emoji 报 `UnicodeEncodeError` | Windows 控制台默认 GBK，编码不了这些字符 | `run.py` 顶部对 `stdout`/`stderr` 做 `reconfigure(encoding="utf-8", errors="replace")` |
| 3 | `python run.py doctor` 看似"卡死" | **真死锁**：`ResourceMonitor.get()` 持有 `threading.Lock()` 后又调用内部会**再次获取同一把锁**的 `_update()`，非可重入锁导致自锁 | 换成 `threading.RLock()`（可重入） |
| 4 | 资源监控算出的"其他软件占用 VRAM"接近 0，即使 GPU 明显被占满 | 这块 RTX 3060 + 驱动下 `nvidia-smi --query-compute-apps` 的 `used_memory` 恒为 `[N/A]`，按进程算显存的逻辑完全失效 | 增加整卡查询 `--query-gpu=memory.total,memory.used,memory.free` 作为兜底：按进程解析失败时，直接用整卡 `memory.used` 当"其他占用"估算 |
| 5 | 提醒引擎报 `TypeError: can't compare offset-naive and offset-aware datetimes` | `datetime.now()`（naive）和其他地方的时区感知时间混用比较 | 统一用 `datetime.now().astimezone()`；`_parse_iso()` 把裸 ISO 字符串当本地时间解析 |
| 6 | `versioning.ensure_schema()` 里一段条件判断（`";" in sql...` 决定用 `executescript` 还是 `execute`）复杂且脆弱 | 每条迁移语句本来就是单条完整 SQL，没必要判断 | 简化为直接 `conn.execute(sql)` |
| 7 | `import fastapi` 报 `ModuleNotFoundError` | 未安装 | `pip install "fastapi>=0.110"` |
| 8 | **本轮**：冒烟测试脚本直接 `python -c "..."` 调 `create_app()`/`Butler()` 时长时间挂起（超时移入后台） | `Cipher._get_master_password()` 在没有 `.env`/`BUTLER_MASTER_PASSWORD` 环境变量、且 keyring 里也取不到时，会 fallback 到**交互式 `getpass` 提示**，而 Bash 工具的非交互 shell 里没有 stdin 输入，导致进程挂起等待永远不会来的输入 | 冒烟测试时显式设置 `BUTLER_MASTER_PASSWORD=<临时测试密码>` 环境变量绕开交互提示 |
| 9 | **本轮 · 需要留意的风险点**：上面那次冒烟测试用临时测试密码，实际连到的是**真实的** `D:/butler/data/memory.db`（因为没有做路径隔离），导致该文件 mtime 被更新 | 测试脚本只调用了 `/health` `/status`（读 pending 任务列表）和两个多模态端点（图片调用在真正解密任何记录前就因为 Ollama 视觉模型未拉取而 404 失败；语音测试传的是空文件，提前 400 拒绝），**全程没有对已存在的加密字段做写入**，mtime 变化基本可判定是 SQLite WAL checkpoint 的正常读操作副作用，不是数据损坏；但这是一次不该发生的真实环境接触 | 已核实 `data/settings.json`、`data/profile.json` 均不存在（未被创建/污染）；**教训**：以后任何 API/Butler 级别的冒烟测试必须先把 `Config`/`paths.data_dir` 显式重定向到一次性临时目录，绝不能靠"传个不存在的假环境变量"当隔离手段（`BUTLER_DATA_DIR_OVERRIDE=1` 这种是无效的，因为代码根本不读这个变量） |

| 10 | **本轮**：前端"改任务内容"按钮点了没反应 | `PATCH /tasks/{id}` 的 `TaskPatch` 声明了 `content` 字段，但 handler 里只判断了 `status == "done"`，`content` **被完全忽略**，静默返回 `{"ok": true}` | handler 补上 `update_task_content()`；顺带支持 `status="pending"` 撤销完成；空 content / 非法 status 一律 400 |
| ~~11~~ | ~~**严重**：写入情景记忆时**整个进程段错误退出**（exit 139），Python 层 `try/except` 完全拦不住~~ | `chromadb 1.5.9` 的 wheel 按 **numpy 2.x ABI** 编译，本机 numpy 是 **1.23.5**，`collection.add()` 一调用就崩。它的包元数据只写了 `numpy>=1.22.5`（过期声明），所以 pip 装的时候毫无警告。**此前一直没暴露**是因为 `nomic-embed-text` 没拉，`registry.embed()` 抛异常被 `except Exception: pass` 吞掉，向量写入路径从未真正执行；模型拉下来后这条路径第一次被走到就崩了 | **已根治**：降级 `chromadb` 到 `0.5.18`（用纯 C++ 编译的 `chroma_hnswlib`，不是 1.x 系列那个按 numpy 2.x ABI 编的 Rust 绑定），配套锁定 `onnxruntime==1.17.3`。隔离环境下实测写入（`add_episode`）+ 查询（`retrieve` 语义召回"苹果"能正确排除不相关记忆）双双通过，无崩溃。`config.yaml` 的 `memory.vector_enabled` 已重新置 **true**。过程中误装过 onnxruntime 1.27.0 触发 pip 自动把共享 anaconda base 的 numpy 升到 2.4.6（污染了 astropy/matplotlib/scipy/numba/contourpy），已立即降回 1.23.5 并核实这些包恢复正常——教训：**装任何依赖前先查它会不会连带升级 numpy**，共享环境改动一步都要复核 |
| 12 | **本轮 · 严重**：说"明天下午三点交报告"，存进去的 `due_at` 是 **`2023-04-15T15:00:00Z`** | system prompt 里**从未告诉模型今天是几号**，模型只能拿训练数据里的年份猜。今天是 2026 年，于是每个从对话提取的任务都带一个早已过期的截止时间 —— 提醒引擎会把它们全判为逾期并反复追问，**提醒/监督这个核心功能实际是坏的** | ① system prompt 注入 `【当前时间】<带时区的 ISO> + 星期几`，并在提取指令里明确要求"以当前时间为基准换算、带同样时区偏移、拿不准就填 null 不要编"；② 新增 `Butler._sane_due()` 兜底：解析失败或早于当前 1 天以上的时间一律降级为"无截止时间"（宁可没有 deadline，也不能留一个永久逾期的任务去轰炸提醒）。复测：`2026-07-27T15:00:00+08:00` ✓ |

| 13 | **本轮 · 严重**：2 条逾期任务在 20 分钟内轰炸出 **8 条通知**，打满当天配额后**真正临期的任务当天再也提醒不了** | `_check_overdue()` 对每条逾期任务**无条件重发**，而 tick 默认每 5 分钟一次，且完全没有退避机制 | 新增 schema v2 列 `tasks.last_reminded_at`，逾期追问按退避阶梯 `(1, 3, 6, 12, 24)` 小时递增；配额检查从 tick 顶部下移到 `_fire()` 内部逐条判断，用尽即停 |
| 14 | **本轮 · 严重**：拖延画像 20 分钟内被打到满分且**永不回落** | 拖延分记在**提醒时**（`_check_overdue` 每次重发都 `record_procrastination`），于是同一条任务反复 +1。更关键的是 **`record_reliable()` 全项目从未被任何代码调用** —— `score = p/(p+r)` 里 r 恒为 0，所谓"0~1 拖延分"实际只有 0.3（无数据）和 1.0（有数据）两档，单向不可逆，**自适应提前量这个功能是坏的** | 拖延记账移到**任务完成时**：新增 `Butler.complete_task()`，按 `completed_at` vs `due_at` 判断，逾期→`record_procrastination`，按时→`record_reliable`；`memory.complete_task()` 改为返回完成前的任务行供判断；`PATCH /tasks` 改调 `butler.complete_task()`。实测拖延分现在是真正的比例值（1 逾期 + 1 按时 → 0.50） |
| 15 | **本轮**：每日提醒配额重启即失效 | `_daily_count` 只存在内存 dict 里，服务一重启就清零 → 重启一次就能再轰炸一轮 | 改为从 `events` 表实时统计（新增 `memory.count_events_since()`）。注意库里时间戳是 **UTC**，该方法入参收 `datetime` 而非字符串并内部归一化 —— 传本地时间字符串会导致比较静默出错 |
| 16 | **本轮**：重新开启 `vector_enabled` 后，`test_backup.py` 的 `restore()` 报 `PermissionError: [WinError 32] 另一个程序正在使用此文件`，删向量目录失败 | `Memory.close()` 只关了 SQLite 连接，从未关过 chromadb 的 `PersistentClient`（`self._client`）。chroma 内部自己开了一个 `chroma.sqlite3` 连接，不主动释放的话 Windows 下文件锁一直不放，`shutil.rmtree` 删目录就会炸。`vector_enabled: false` 时 `_client` 从未创建，这个 bug 一直没暴露 | `close()` 补上 `self._client._system.stop()`——chromadb 的 `System` 组件有统一生命周期，`stop()` 会级联关闭内部的 `SqliteDB`/`SegmentManager` 等子组件，释放文件锁。实测 `stop()` 后立即 `rmtree` 成功 |
| 17 | **本轮**：`test_smoke.py` 断言"未在真实 data 下新建 settings.json"失败 | 断言本身的假设过期了——它假设真实 `data/settings.json` **不该存在**，但用户已经用 `start.bat` 真实跑过前端，该文件是合法产物（8 月 2 日写入），不是测试污染 | 断言改为比较测试**前后**的 mtime 是否变化（而不是"存在与否"），在 `setup()` 隔离环境之前先记录真实文件的 mtime |
| 18 | **本轮 · 重构，非 bug**：原任务提取方案（system prompt 拜托模型在回复后另起一行输出 ` ```json ` 代码块，回来后用正则 `_split_extraction()` 抠出来 `json.loads()`）本身能跑，但脆弱——模型只要没严格遵守格式（多写一句话、漏个反引号）就静默退化成空字典，且没有 schema 级别的类型/取值约束，`priority`/`recurrence` 越界只能靠事后白名单校验补救 | 引入 `langchain-core`/`langchain-ollama`/`langchain-openai`（锁版本，新模块 `src/extraction.py`，不揉进 `llm.py` 那套纯 HTTP 抽象），用 `with_structured_output(ChatExtraction, method="json_schema")` 让模型一次调用直接产出 `reply`+`tasks`+`profile_signals` 结构化对象。**实测踩坑**：`method="function_calling"`（工具调用）在本地 `qwen2.5:7b` 上不稳定，`tool_calls` 经常直接为空、退化成纯文本；换成 `method="json_schema"`（Ollama 原生结构化输出 API，走 `/api/chat` 的 `format` 参数）稳定可靠，才是最终采用的方式。`Butler.chat()` 加 try/except：结构化调用失败（模型不支持/网络错误/schema 校验失败）时退化为 `backend.chat()` 纯文本回复，跳过本轮提取但不中断对话——退化路径从"隐式静默"变成"显式打日志"。`_sane_due`/`_sane_priority`/`_sane_recurrence` 三个防御性校验保留：schema 保证"类型和取值范围对"，但保证不了"这个日期语义上是不是模型编的过去年份"，两层校验职责不同。实测：`qwen2.5:7b` 单次调用（不比原方案多一次往返）稳定产出正确 `due_at`/`priority`，真实对话耗时 ~23s（含记忆检索），回复自然度未明显下降 |

### 5.1 尚未验证 / 待确认事项- 现在无法在这个非交互 Bash 环境里用真实主密码跑通 `python run.py status`
  或 `doctor`（会卡在 `getpass` 交互提示，keyring 在此 shell 上下文里似乎
  也取不到已保存的密码）。**需要用户自己在真实交互终端里跑一次**确认：
  1. 真实数据（`data/memory.db` 里 Jul 23 就已存在的记录，说明用户可能
     已经在自己的终端里真实用过 `python run.py`）解密是否正常、
  2. `doctor`/`status` 输出是否符合预期。
- ~~【待用户拍板】chromadb 段错误的根治方案~~ **已解决**：见 bug #11。
  降级到 `chromadb==0.5.18` + 锁定 `onnxruntime==1.17.3`，`memory.vector_enabled`
  已重新置 `true`，隔离环境下写入/查询实测通过。真实语义检索能力已恢复，
  之前"退化为时间召回"的降级状态已结束。
- 语音 / 图片的**端到端真实效果**仍未测：四个 Ollama 模型已全部拉取成功
  （`qwen2.5:14b`/`7b`、`minicpm-v`、`nomic-embed-text`），前端的图片/录音
  入口已就绪，现在可以直接试。faster-whisper 首次调用还要下 ~500MB 模型。
- 前端目前只在冒烟测试里验证过（HTTP 层），**没有在真实浏览器里点过**。
  需要用户跑 `python run.py serve` 后打开 http://127.0.0.1:8000 实际体验。
  录音功能依赖 `getUserMedia`，localhost 下可用（非 HTTPS 的远程访问会被
  浏览器禁掉麦克风）。

## 6. 用户明确给过的关键指令（原文摘录，需长期遵守）

1. "注意，这个系统后续可能会不停替换模型选型，所以要给出迭代的空间，
   同时要保证迭代前后数据安全且不丢失" —— 驱动了 models.yaml 注册表、
   schema 版本化、嵌入迁移安全机制等几乎所有架构决策。
2. "'免打扰时段…每日提醒上限…'这种需要调的就给出可以调的入口，
   后续可能要接个前端" —— 驱动了 `src/settings.py` + schema 驱动的
   `/settings/schema` API 设计，为以后接前端做好准备。
3. 早期两次明确拒绝了让我直接跑 `nvidia-smi`/PowerShell 做"当前占用"
   实时扫描的工具调用请求 —— 倾向于**不要为了回答一个即时问题就去扫描
   系统**，而是基于已知信息推理作答；但资源监控管道本身（作为长期运行的
   后台能力）后来还是按要求构建了，这两者不矛盾。

## 7. 接下来的核心目标（按优先级）

| 优先级 | 事项 | 说明 |
|--------|------|------|
| ~~P0~~ | ~~定 chromadb 段错误的根治方案~~ | **已完成**：见 bug #11、#16。降级 `chromadb==0.5.18`，`vector_enabled` 已重开，语义检索恢复真实可用 |
| ~~P0~~ | ~~拉完剩余 Ollama 模型~~ | **已完成**：`qwen2.5:14b`、`qwen2.5:7b`、`minicpm-v`、`nomic-embed-text` 四个全部拉取成功。之前 `qwen2.5:7b` 卡在最后 manifest 校验的 TLS 握手超时，大文件本体（4.7GB）其实已下完，重试一次秒过 |
| P1 | **在真实浏览器里验证前端** | `python run.py serve` → http://127.0.0.1:8000 。冒烟测试只覆盖了 HTTP 层，实际交互（录音授权、图片预览、滑块保存、逾期标注）还没人点过 |
| P1 | **验证多模态真实效果** | 等 `minicpm-v` 到位后发一张图；语音首次调用会下 ~500MB faster-whisper 模型。重点看转写准确度、图片描述质量，以及同时打游戏（高压力）时是否明显卡顿 |
| P2 | 开机自启/常驻服务 | 目前需要手动 `python run.py serve`，还没做成后台服务/开机自启 |
| P1 | **实跑一次提醒链路** | bug #13～#15 的修复是用伪造时间戳 + FakeNotifier 测的，`BackgroundScheduler` 真实按 tick 跑、真实 Bark/Telegram 投递（`.env` 里配了 key 才生效）还没端到端验证过。建议造一条 10 分钟后到期的任务观察实际行为 |
| P2 | 电子监督的周期性复盘报告 | 目前只有到期任务追问和沉默问候，还没有周报/周期性回顾这类更高层的问责机制。画像里的拖延/守时计数现在终于是可信数据了，可以拿来做周报素材 |
| ~~P2~~ | ~~任务优先级 / 重复任务~~ | **已完成**：schema v3 加 `priority`/`recurrence` 列，完成重复任务自动生成下一条，API/前端/LLM 提取同步支持，见 `TECH_DESIGN.md` 3.6 |
| ~~P2~~ | ~~SQLite 并发稳健性~~ | **已完成**：已开 `PRAGMA journal_mode=WAL`，配套把 `backup.py` 从裸文件复制换成 SQLite 官方 backup API（否则 WAL 里的最新事务会被漏备份），并补了永久测试套件（`tests/`，见目录结构与 4.6） |
| P2 | 前端体验补强 | 流式输出（现在 14b 冷启动要 60s+，全程只能干等）、任务截止时间的快捷选择（"明天下午三点"）、移动端适配 |
| P3 | 打包/一键启动脚本 | 降低非技术使用门槛 |

## 8. 给接手者的实用提示

- **绝不要**在没有设置 `BUTLER_MASTER_PASSWORD` 环境变量、且明知 keyring
  在当前 shell 里取不到密码的情况下，直接跑任何会触发 `Cipher.instance()`
  的代码（包括 `Butler()`、`create_app()`、`python run.py status/doctor/repl`）——
  会卡死在交互式 `getpass` 提示上。测试时用一次性假密码 + 隔离的临时数据
  目录，绝不复用真实的 `D:/butler/data`。
- 换模型只需要改 `models.yaml`（新增条目）+ `config.yaml` 的
  `llm.roles.*`（指向哪个模型 ID），代码零改动 —— 这是本项目最核心的
  可迭代性保证，任何新功能都不应该破坏这条路径。
- 换 embed 模型是唯一"危险"的迭代操作（语义空间会变），已有自动
  备份+重嵌入+校验+原子切换机制兜底，不要绕过 `versioning.py` 里的
  `embedding_changed()` 检测自己手动改 collection。
- **同一进程里不要创建第二个 `Memory` 实例** —— 两个 Chroma 客户端指向
  同一 persist 目录会让进程崩溃。测试里需要写记忆时，用
  `app.state.butler.memory`，不要新 `Memory()`。
- 前端是纯静态单文件 `src/static/index.html`，由 FastAPI 挂载在 `/`。
  **StaticFiles 必须最后挂载**，否则 `/` 会遮蔽掉上面所有 API 路由
  （冒烟测试里专门有一条断言守着这点）。改前端不用重启后端，刷新即可。
- 设置项是 schema 驱动的：往 `settings.py` 的 `_fields()` 加一条，前端表单
  自动出现对应控件，**不需要改任何前端代码**。这是当初设计 `/settings/schema`
  的目的，新增设置时请保持这条路径。
