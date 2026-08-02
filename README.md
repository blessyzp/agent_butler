# 电子管家 Butler

私人信息托管 Agent：多模态输入 · 加密记忆 · 用户画像 · 语气化提醒与监督。
本地优先（Ollama + RTX 3060 12GB），资源紧张/游戏时自动切云端，数据全程加密。

## 一键启动

Windows 下装好 Ollama + Python 依赖后，双击：

```
start.bat
```

自动完成：拉起 Ollama（若未运行）→ 启动后端 → 打开浏览器 `http://127.0.0.1:8000`。
首次运行若密钥链里还没存过主密码，会在窗口里等你输入一次（用于加密本地数据）。
关闭该窗口即停止服务。

浏览器界面四个标签页：**聊天 / 任务 / 设置 / 状态**，详见下方「前端界面」。

## 技术栈

| 层 | 技术 |
|----|------|
| 后端框架 | FastAPI + uvicorn |
| 本地大模型 | Ollama（Qwen2.5-14B/7B 对话，nomic-embed-text 嵌入，MiniCPM-V 视觉） |
| 云端兜底 | DeepSeek（OpenAI 兼容接口） |
| 加密 | cryptography（Fernet + PBKDF2），密钥存 Windows 密钥链（keyring） |
| 结构化存储 | SQLite（WAL 模式，加密字段） |
| 向量检索 | ChromaDB（可选，当前因 numpy ABI 冲突默认关闭，退化为时间召回） |
| 任务调度 | APScheduler（提醒/监督后台 tick） |
| 语音转写 | faster-whisper（本地 CPU） |
| 图像预处理 | Pillow |
| 前端 | 原生 HTML/CSS/JS 单文件，零构建零依赖，由 FastAPI StaticFiles 挂载 |
| 测试 | 自建轻量断言框架（`tests/isolation.py`），子进程隔离运行 |

## 架构

```
输入 → Butler 主控
        ├─ ResourceMonitor  实时 VRAM/RAM/CPU（排除自身）→ 压力评级
        ├─ ModelScheduler   压力 → 模型角色 + 防抖 + 可用性回退
        ├─ Registry+LLM     models.yaml 定义模型，代码只认接口（可插拔）
        ├─ Memory           加密SQLite(真相源) + Chroma向量(可重建)
        ├─ Profile          加密画像，随对话演化
        └─ ReminderEngine   智能时机 + 语气化催办 + 监督闭环 → Notifier
```

**数据安全三原则**：文本是真相、向量是缓存、schema 有版本。
换模型 = 改 `models.yaml`/`config.yaml` + 自动迁移，绝不丢数据。

## 安装

```bash
# 1. 依赖
cd /d/butler
pip install -r requirements.txt

# 2. 安装 Ollama（Windows）: https://ollama.com/download/windows
#    装好后拉模型：
ollama pull qwen2.5:14b        # 主力（约9GB VRAM）
ollama pull qwen2.5:7b         # 轻量备用
ollama pull nomic-embed-text   # 向量嵌入
ollama pull minicpm-v          # 图像理解（多模态-图片）

# 3. 配置密钥（可选，用于云端兜底/推送）
cp .env.example .env           # 然后填入 DEEPSEEK_API_KEY / BARK_KEY 等

# 4. 自检
python run.py doctor
```

## 使用

```bash
python run.py            # 进入对话（后台自动跑资源监控 + 提醒）
python run.py serve      # 启动 HTTP API（供前端接入）
python run.py status     # 查看资源/模型/待办状态
python run.py doctor     # 环境自检
```

对话中命令：`/status` 查看状态，`/quit` 退出。

## 前端界面

`python run.py serve` 后打开 `http://127.0.0.1:8000`，四个标签页：

- **聊天**：气泡式对话，Enter 发送；支持上传图片（可附文字）、录音输入；
  请求期间锁输入避免并发压垮本地模型；刷新页面自动从 `/history` 恢复历史。
- **任务**：新建（内容/分类/截止时间）、完成/撤销、改内容、删除；逾期红色标注。
- **设置**：完全由 `/settings/schema` 驱动渲染，新增设置项无需改前端代码。
- **状态**：显存/内存/CPU 占用条、压力等级、当前模型、各后端可用性，20 秒轮询。

## 可调设置（运行时可改，前端友好）

用户可调项与开发者静态配置分离：静态默认在 `config.yaml`，用户覆盖持久化到
`data/settings.json`，改完即时生效。每个字段带元数据（类型/范围/选项/标签/分组），
前端调 `GET /settings/schema` 即可自动渲染滑块/开关/下拉，无需硬编码表单。

当前可调项：免打扰起止、每日提醒上限、检查频率、默认提前量、监督开关、
沉默问候阈值、语气、是否用 emoji、管家称呼。新增一项 = 往 `src/settings.py`
的 `_fields()` 加一条，校验与前端自动生效。

## HTTP API（前端后端）

```bash
python run.py serve      # 默认 http://127.0.0.1:8000 ，交互式文档 /docs
```

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/status` | 资源/模型/待办状态 |
| GET  | `/settings` | 当前设置值 |
| GET  | `/settings/schema` | 字段元数据（渲染表单用） |
| PUT  | `/settings` | 批量更新（body: `{"patch": {...}}`） |
| POST | `/chat` | 对话（body: `{"message": "..."}`） |
| POST | `/input/voice` | 上传音频（form-data: `file`）→ 转写文字 + 对话回复 |
| POST | `/input/image` | 上传图片（form-data: `file`，可选 `text`）→ 图片描述 + 对话回复 |
| GET/POST/PATCH/DELETE | `/tasks` | 待办增删改查 |

> 仅绑定 localhost。远程访问请走 WireGuard/内网穿透，勿直接公网暴露。

## 多模态输入

- **语音**：本地 faster-whisper 转写（默认跑 CPU，不占 GPU 显存），转写文本
  直接走 `chat()` 原有流程 —— 记忆、画像、任务提取全部复用，零特殊逻辑。
  模型大小/设备/语言见 `config.yaml` 的 `speech` 段。
- **图片**：先用 Pillow 压缩（限制长边像素，见 `vision.max_dimension`），
  再送本地 MiniCPM-V（Ollama）生成中文描述，描述文本同样并入 `chat()` 流程。
- 两者都只是"生成一段文字，喂给现有对话管线"，所以任务提取、语气回复、
  记忆写入、画像更新对多模态输入同样生效，无需重复实现。
- 依赖为可选安装：`pip install faster-whisper pillow`（已在 requirements.txt，
  未安装时对应功能报错提示，不影响纯文本功能）。

## 更换模型（迭代）

**换聊天模型**：编辑 `models.yaml` 加条目 → 改 `config.yaml` 的 `llm.roles`。零改代码。

**换嵌入模型（⚠ 关键）**：嵌入模型决定向量语义空间。
1. 在 `models.yaml` 新增嵌入模型（注意 `dim` 维度）
2. 改 `config.yaml` 的 `llm.roles.embed` 指向新模型
3. 下次启动自动：**备份 → 从加密真相源重嵌入到新集合 → 校验 → 切换**，旧集合保留可回滚

## 安全说明

- 所有敏感字段（任务/画像/对话）用 Fernet 加密后落盘；主密钥存 Windows 密钥链
- 向量库文档也以密文存储；建议再对 `data/` 目录开 BitLocker
- `.env`、`data/`、`*.db` 已在 `.gitignore`，切勿提交
- 迁移前自动快照至 `data/backups/`，保留最近 10 份

## 目录

```
butler/
├── config.yaml        参数（路径/阈值/语气/提醒策略）
├── models.yaml        模型注册表（迭代入口）
├── .env               密钥（自建，勿提交）
├── run.py             CLI 入口
├── data/              加密数据 + 向量 + 备份（勿提交）
└── src/
    ├── config.py      配置加载
    ├── crypto.py      加密 + 主密钥
    ├── resource_monitor.py  资源感知
    ├── scheduler_model.py   模型调度
    ├── llm.py / registry.py 后端抽象 + 注册表
    ├── memory.py      加密记忆 + 嵌入迁移
    ├── versioning.py  schema 迁移 + 嵌入兼容判定
    ├── backup.py      快照/回滚
    ├── profile.py     用户画像
    ├── reminder.py    提醒/监督引擎
    ├── notify.py      推送
    ├── static/index.html  前端（单文件，零构建）
    └── butler.py      主控编排
├── tests/             隔离环境测试套件（不碰真实 data/）
└── start.bat          一键启动脚本
```

## 测试

```bash
python tests/run_all.py          # 全量（含依赖真实 Ollama 模型的复测）
python tests/run_all.py --fast   # 跳过依赖真实模型的项，更快
```

每个测试套件在独立子进程中运行，用临时目录重定向 `paths.*`，绝不接触真实
`data/` 目录。
