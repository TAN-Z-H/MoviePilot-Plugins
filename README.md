# MoviePilot-Plugins

MoviePilot官方插件市场：https://github.com/jxxghp/MoviePilot-Plugins
- [食用说明](MP-readme.md)

### 本仓库插件

#### [onebotmsg](./plugins.v3/onebotmsg/README.md)

把 MoviePilot 通知推到 QQ（私聊 / 群聊）。当前版本 **1.2.0**。

- 本插件为纯AI生成的自用插件，仅用来方便本人使用，未经过长时间测试，请谨慎安装
- 使用前请先看 [插件文档](./plugins.v3/onebotmsg/README.md)，其中包含 NapCat 侧配置要点

功能概览：

| 能力 | 说明 |
| --- | --- |
| 全场景通知 | 入库整理 / 下载 / 订阅 / 站点 / 媒体服务器 / 插件 / 系统错误，按类型裁剪 |
| 图文同条 | 海报与通知文字放进同一条消息，QQ 里显示在同一个气泡；可选图片在上/在下 |
| 排版 | 极简（配海报推荐）/ 卡片（带边框按显示宽度对齐）/ 紧凑 / 原始；配置页可预览 |
| 图片发送 | URL / base64 内嵌 / 本地路径三种方式自动降级，解决图片发不出来 |
| 双传输 | HTTP 正向接口或正向 WebSocket（WS 可收到投递回执） |
| 扩展 API | 合并转发（长通知折叠成卡片）、表情回复、能力探测 |
| 稳定性 | 发送队列、限流、去重、失败重试、超长按行分段 |

推荐配置（海报 + 信息一条气泡）：

```
排版样式     = 极简
图文组合方式 = 图文同条
同条时图片位置 = 图片在上
图片发送方式 = 自动（收不到图就改「仅 base64 内嵌」）
```

> NapCat 侧的 Host 必须填 `0.0.0.0`；填成宿主机局域网 IP 会导致
> `EADDRNOTAVAIL`，插件侧表现为 `Connection reset by peer`。
