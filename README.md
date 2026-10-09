# MoviePilot-Plugins（个人插件仓）

这是 [MoviePilot](https://github.com/jxxghp/MoviePilot) 的**个人第三方插件仓**，
只维护一个 V3 插件。通过 `PLUGIN_MARKET` 指向本仓地址即可让 MoviePilot 从本仓安装。

- 仓库地址：`https://github.com/TAN-Z-H/MoviePilot-Plugins`
- 上游仓库：[jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)（fork 来源）

## 插件清单

| 插件 ID | 名称 | 版本 | 说明 |
| --- | --- | --- | --- |
| `OneBotMsg` | OneBot v11 通知 | 1.0.0 | 通过 OneBot v11（NapCat）向 QQ 私聊/群聊推送 MoviePilot 通知：入库整理、下载、订阅、站点、媒体服务器、插件与系统错误等场景 |

详见 [plugins.v3/onebotmsg/README.md](./plugins.v3/onebotmsg/README.md)：NapCat 侧的
HTTP 服务器与 Token 配置、插件配置项、目标格式、远程命令、插件 API 与排查方法。

## 目录结构

```
MoviePilot-Plugins/
├── plugins.v3/onebotmsg/
│   ├── __init__.py        # 插件主类 OneBotMsg
│   ├── onebot.py          # OneBot v11 客户端（纯标准库）
│   ├── pyproject.toml     # 依赖清单（无额外依赖）
│   └── README.md          # 插件使用文档
├── tests/
│   ├── v3/onebotmsg/test_plugin.py   # 插件回归测试
│   ├── _bootstrap.py、conftest.py、run.py   # 测试引导与入口
│   └── README.md          # 测试约定
├── package.v3.json        # V3 插件市场索引
├── package.v2.json        # 历史代索引（本仓为空对象）
├── package.json           # 默认历史索引（本仓为空对象）
├── scripts/               # 上游新增插件 / 依赖 / 导入边界检查工具
├── docs/                  # 上游 V3 插件开发与仓库规范文档
├── .githooks/pre-push     # push 前的版本与 CSS 校验
└── .github/
    ├── scripts/           # 版本与联邦 CSS 校验器
    └── workflows/         # release（发布）、plugin-gate（PR 门禁）、sync-upstream（同步上游）
```

## 发布方式

推送 `package*.json` 变更到 `main` 会触发 `.github/workflows/release.yml`：

1. 校验索引版本与插件类版本一致（`check_plugin_versions.py`）；
2. 对索引中声明 `"release": true` 的插件，按 `插件ID_v版本号` 打 tag；
3. 打包插件目录为 `插件目录小写_v版本号.zip` 并创建 GitHub Release。

因此发布新版本时需要同步三处：`package.v3.json` 的 `version`、插件类的
`plugin_version`、`history` 顶部的当前版本说明。

## 提交前自检

```bash
python .github/scripts/check_plugin_versions.py package.json package.v2.json package.v3.json
python .github/scripts/check_federation_css.py
python -m compileall plugins.v3/onebotmsg

# 需要 MoviePilot 后端（同级目录或 MOVIEPILOT_BACKEND_PATH 指向）
<workspace>/.venv/bin/python tests/run.py
```

`.githooks/pre-push` 已复用上述版本与 CSS 校验；启用方式：

```bash
git config core.hooksPath .githooks
```

## 文档

本仓保留上游 V3 插件开发规范，便于后续维护与新增插件：

- [插件开发指南（V3）](./docs/Plugin_Development.md)
- [仓库与发布指南](./docs/Repository_Guide.md)
- [插件 API 响应适配](./docs/V3_API_Response_Adaptation.md)
- [V2 插件迁移到 V3](./docs/V3_Plugin_Adaptation.md)

上游仓库的其它文档（FAQ 索引、功能专题 `docs/faq/`、V1/V2 历史指南）已随无关内容一并
移除。保留下来的这四份是上游原始副本，其中指向已删除 `docs/faq/`、`docs/FAQ.md` 的
内部链接会失效；需要完整文档时到
[jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins) 查阅。
