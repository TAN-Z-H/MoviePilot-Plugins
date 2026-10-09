# OneBot v11 通知（MoviePilot V3 插件）

通过 [OneBot v11](https://github.com/botuniverse/onebot-11) 协议（[NapCat](https://napneko.github.io/) 实现）
把 MoviePilot 的通知推送到 QQ 私聊与群聊。插件订阅宿主通知事件，因此**入库整理、
下载、订阅、站点、媒体服务器、插件、系统错误**等场景的通知都会一并转发。

- 插件 ID：`OneBotMsg`
- 源码目录：`plugins.v3/onebotmsg/`
- 测试目录：`tests/v3/onebotmsg/`
- 版本：`1.0.0`，要求 `MoviePilot >= 3.0.0`
- 依赖：无（仅使用 Python 标准库与宿主已提供的 pydantic / fastapi）

---

## 1. 前置条件：配置 NapCat

1. 部署 NapCat 并登录机器人 QQ。
2. 打开 NapCat 的 **WebUI → 网络配置**，启用 **HTTP 服务器**：
   - 主机：`0.0.0.0`（若 MoviePilot 与 NapCat 同容器/同主机，`127.0.0.1` 也可）
   - 端口：默认 `3000`
   - **Token（Access Token）**：建议设置，并与插件配置保持一致
3. 记录访问地址，例如 `http://127.0.0.1:3000`。

> 插件只使用 OneBot 的 **正向 HTTP** 能力：`POST http://<host>:<port>/<action>`，
> 请求体为动作参数，鉴权头为 `Authorization: Bearer <token>`。
> 反向 WebSocket / HTTP 上报（接收消息）不在本插件范围内。

### 是否需要额外放行 IP

如果 MoviePilot 以 Docker 部署而 NapCat 在宿主机，请把地址写成
`http://<宿主机IP>:3000`，并确认 `3000` 端口可从容器内访问。

---

## 2. 安装

插件按 MoviePilot 插件仓约定落位：

```
MoviePilot-Plugins/
├── plugins.v3/onebotmsg/
│   ├── __init__.py        # 插件主类 OneBotMsg
│   ├── onebot.py          # OneBot v11 客户端
│   ├── pyproject.toml     # 依赖清单（本插件无额外依赖）
│   └── README.md          # 本文档
├── tests/v3/onebotmsg/
│   └── test_plugin.py     # 回归测试
└── package.v3.json        # 索引，须登记 OneBotMsg 条目
```

索引中的 `version` 必须与插件类的 `plugin_version` 一致（均为 `1.0.0`），
且 `history` 首项为 `v1.0.0`。

### 2.1 本地目录加载

在 MoviePilot 的 `.env` 中配置本地插件仓库路径，便于边改边测：

```dotenv
PLUGIN_LOCAL_REPO_PATHS=/path/to/MoviePilot-Plugins
PLUGIN_AUTO_RELOAD=true
DEBUG=true
```

随后在 **设定 → 插件市场** 中安装「OneBot v11 通知」。

---

## 3. 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 总开关。未启用时不接收任何通知 |
| OneBot 接口地址 | NapCat HTTP 服务器地址，例如 `http://127.0.0.1:3000` |
| Access Token | 与 NapCat 保持一致，未设置则留空 |
| 默认推送目标 | 每行一个目标；**裸数字按私聊发送**，群号必须加前缀 |
| 用户定向推送 | 按 MoviePilot 用户名定向，未命中的用户回落默认目标 |
| 推送的消息类型 | 勾选需要转发的场景；**未勾选任何场景时不推送通知** |
| 附带海报图片 | 通知带图时以 OneBot `image` 段发送海报 |
| 附带链接 | 在正文末尾追加通知链接 |
| 群消息 @ 用户 | 群消息在正文前插入 `at` 消息段 |
| 标题前缀 | 给所有通知标题加统一前缀，例如 `[MoviePilot]` |
| 单条消息最大长度 | 超出后自动分段发送，默认 1200 |
| 去重时间窗口（秒） | 窗口内的相同通知只发送一次，默认 20，填 0 关闭 |
| 每秒最大发送条数 | 客户端侧令牌桶限流，默认 3 |
| 失败重试次数 | 网络类失败按指数退避重试，默认 2 |

### 3.1 目标格式

群号与 QQ 号在 OneBot 中都是纯数字，无法自动区分，因此**群目标必须显式加前缀**：

```
123456789          # 私聊，发送给 QQ 123456789
private:123456789  # 同上，显式声明
群:987654321       # 群聊
group:987654321    # 同上
```

多目标可以写在多行，也可以用逗号分隔。

### 3.2 用户定向推送

支持两种写法，可混用：

```
admin=123456789
alice=111111,群:222222
```

```json
{"admin": "123456789", "alice": ["111111", "群:222222"]}
```

未命中的用户（或系统级、无关联用户的通知）一律回落到「默认推送目标」。

---

## 4. 功能一览

### 4.1 全场景通知转发

插件订阅宿主 `EventType.NoticeMessage`，该事件由 `ChainBase.post_message()` 在
**所有**通知场景下发，事件数据包含：

```text
channel, source, mtype(type), ctype, title, text, image, link,
userid, username, targets, buttons, ...
```

因此 MoviePilot 的以下场景都会转发（**默认勾选前六项**，可在配置中按需裁剪）：

| 场景 | 类型名 | 默认 |
| --- | --- | --- |
| ✅ 整理入库 | `Organize` | 开启 |
| 📥 资源下载 | `Download` | 开启 |
| 🔔 订阅 | `Subscribe` | 开启 |
| 🛠️ 手动处理（需人工介入的失败） | `Manual` | 开启 |
| 🌐 站点 | `SiteMessage` | 开启 |
| 🧩 插件 | `Plugin` | 开启 |
| 🎬 媒体服务器（播放/入库） | `MediaServer` | 关闭 |
| 🤖 智能体 | `Agent` | 关闭 |
| 📢 其它 | `Other` | 关闭 |

未勾选任何场景即完全不推送。另外，如果宿主中配置了 QQ 通知渠道且**全部处于禁用
状态**，插件会自动跳过转发，避免与用户在 MoviePilot 中的关闭意图冲突。

### 4.2 入库通知增强

除宿主通知外，插件额外订阅：

- `EventType.TransferComplete`：整理完成
- `EventType.TransferFailed`：整理失败

事件中携带宿主通知未覆盖的字段时，插件补充一条结构化卡片，包含**季集、分类、
媒体身份（`media_source` + `media_id`）、文件数、体积、目标路径、整理方式、
失败阶段与建议动作**。事件缺少可用标题时不会构造通知，避免与宿主原生通知重复刷屏。

### 4.3 消息格式

文本与图片以 OneBot 消息段数组下发，示例：

```json
[
  {"type": "text", "data": {"text": "✅ 整理入库 影片名 (2024)\n季集：S01E01\n分类：电影/华语电影"}},
  {"type": "image", "data": {"file": "https://image.tmdb.org/t/p/original/xxx.jpg"}}
]
```

群消息开启 @ 时在首位插入 `{"type": "at", "data": {"qq": "123456789"}}`。

### 4.4 稳定性设计

- **发送队列**：宿主事件线程只做入队，真正的 HTTP 调用由独立的 `OneBotMsgSender`
  守护线程串行执行，保证顺序且不阻塞事件总线。
- **自动分段**：超过「单条消息最大长度」时按文本长度切分，并在每段前标注 `(1/3)`。
- **限流**：令牌桶控制每秒发送条数，缓解 NapCat 风控。
- **去重**：宿主按 `admin/user/all` 路由会重复派发同一条通知，插件按
  「类型 + 标题 + 正文 + 图片」在时间窗口内去重。
- **失败重试**：连接失败/超时按 `0.6s → 1.2s → 2.4s` 指数退避重试；
  协议层失败（`retcode != 0`）不重试。
- **不重复推送**：通知已指定其它渠道、或宿主 QQ 渠道被禁用时自动跳过。
  （宿主的 QQ 渠道与本插件是两条独立通道，同时启用会收到两条消息，
  建议二选一：要么在宿主中关闭 QQ 渠道，要么把插件目标留空。）

---

## 5. 远程命令

| 命令 | 说明 |
| --- | --- |
| `/onebot_test` | 发送一条测试消息到已配置目标 |
| `/onebot_status` | 回复运行状态（队列长度、发送/失败/过滤/去重统计） |

命令通过宿主 `EventType.PluginAction` 注册；回执优先发回发起命令的会话，
无法识别来源时使用插件配置的目标。

---

## 6. 插件 API

所有接口均需登录态（`auth: bear`），路径前缀为 `/api/v1/plugin/OneBotMsg`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/test` | 发送测试通知（同步返回结果，便于页面立即看到成败） |
| POST | `/push` | 推送自定义通知，供工作流或外部调用 |
| GET | `/status` | 查询运行状态 |

`POST /push` 请求体：

```json
{
  "title": "自定义标题",
  "text": "正文内容",
  "image": "https://example.com/a.jpg",
  "link": "http://moviepilot/#/media",
  "emoji": "🎯",
  "target": "群:987654321"
}
```

`target` 为可选字段，用于临时覆盖推送目标；不传时使用插件配置的目标。

---

## 7. 排查

| 现象 | 排查方向 |
| --- | --- |
| 测试通知报「连接失败」 | 地址/端口是否正确、NapCat 是否启动、容器网络是否互通 |
| 报 `HTTP 401/403` | Access Token 与 NapCat 不一致 |
| 报 `retcode=...` | NapCat 侧拒绝，例如未登录、目标非好友、群号错误 |
| 只有部分目标收到 | 单条消息是否过长被 NapCat 截断；查看日志中的 `成功 x/y` |
| 完全收不到通知 | 插件是否启用；消息类型是否被过滤；宿主场景开关是否被关闭；日志级别调为 `DEBUG` |
| 收到重复消息 | 同时启用了宿主 QQ 渠道与本插件，二选一即可 |

日志关键字：`OneBot v11 通知插件`、`OneBot 通知已发送`、`OneBot 通知部分或全部发送失败`。

---

## 8. 自测

测试位于 `tests/v3/onebotmsg/test_plugin.py`，按仓库约定通过生产命名空间
`app.plugins.onebotmsg` 导入插件与 `onebot` 子模块，只 mock HTTP 调用，
覆盖协议工具、限流去重、目标路由、类型过滤、消息构造、整理事件、远程命令、
生命周期与元数据一致性。

```bash
# 仓库统一门禁（与 CI 一致，需要 MoviePilot 后端与 .venv）
<workspace>/.venv/bin/python tests/run.py

# 只跑本插件
<workspace>/.venv/bin/python -m pytest tests/v3/onebotmsg

# 无宿主环境下的独立运行（自动降级为轻量 stub，仅用于本机快速回归）
python tests/v3/onebotmsg/test_plugin.py
```

> 缺少 MoviePilot 后端时，测试会降级为内置 stub 自检；**正式提交与 CI 必须走
> 真实宿主**。测试不会把插件目录加入 `sys.path`，避免同一源码被双重导入。

在真实 MoviePilot V3 宿主中安装后，还需确认插件能正常加载、启用、禁用与重载，
`DEBUG=true` 时不产生旧导入路径告警，并在页面上点「发送测试通知」验证 QQ 可达。
