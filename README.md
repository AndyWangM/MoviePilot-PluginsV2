# MoviePilot-PluginsV2

MoviePilot V2 自定义插件仓库。

## 插件列表

| 插件 | 版本 | 功能 |
|---|---|---|
| [Prowlarr](plugins.v2/prowlarr/) | 1.0 | 通过 Prowlarr 扩展 BT 搜索，每个 Indexer 独立出现在搜索站点列表 |

## 安装方法

在 MoviePilot → 设置 → 插件市场 → 添加插件源：

```
https://github.com/AndyWangM/MoviePilot-PluginsV2
```

## Prowlarr 插件

### 功能

- 自动拉取 Prowlarr 中所有已配置的 Indexer
- 每个 Indexer 以「Prowlarr - {名称}」形式独立出现在 MoviePilot 搜索站点列表
- 可在「设置 → 搜索 → 索引站点」中单独勾选启用/禁用
- 完整映射种子信息：做种数、下载数、完成数、IMDB ID、促销因子
- 定时自动刷新 Indexer 列表

### 配置

| 字段 | 说明 |
|---|---|
| Prowlarr 地址 | 如 `http://192.168.1.1:9696` |
| API Key | 在 Prowlarr → Settings → General → Security 中获取 |
| 使用代理 | 是否通过 MoviePilot 配置的代理访问 Prowlarr |
| 刷新周期 | cron 表达式，默认每天凌晨 `0 0 * * *` |
| 立即刷新 | 开启后保存时立即拉取一次 Indexer 列表 |
