# 磁盘清理插件 PRD（v1.0）

## 1. 目标
- 在磁盘空间达到阈值时自动回收空间。
- 清理链路可追踪、可限额、可演练。
- 默认优先处理 MoviePilot 可关联的下载/整理记录，降低误删风险。

## 2. 核心能力
- 触发方式：
  - 资源目录阈值触发（固定剩余 GB / 剩余百分比）
  - 媒体库目录阈值触发（固定剩余 GB / 剩余百分比）
  - 下载器做种时长触发（如超过 7 天）
- 清理对象：
  - 媒体文件
  - 同名刮削文件（sidecar）
  - 下载器做种任务（可选是否删除下载文件）
  - MP 整理记录与下载记录
- 清理后动作：
  - 可选通知媒体服务器刷新

## 3. 安全机制（默认开启）
- 演练模式（dry-run）
- 仅处理 MP 可关联记录（mp_only）
- 冷却时间（cooldown）
- 单轮释放上限 / 当日释放上限
- 近期入库保护（protect_recent_days）
- 路径白名单 / 黑名单
- 单轮仅执行一类策略，避免叠加清理

## 4. 执行顺序
1. 采集目录容量与下载器状态
2. 判定是否命中冷却/当日限额
3. 按优先级触发策略（媒体库阈值 > 资源阈值 > 下载器阈值）
4. 生成候选并执行/演练清理
5. 记录审计日志（触发源、动作、释放空间、模式）
6. 可选刷新媒体服务器

## 5. 配置字段说明
| 字段 | 类型 | 说明 |
|---|---|---|
| enabled | bool | 启用插件 |
| dry_run | bool | 演练模式，不执行真实删除 |
| cron | string | 执行周期 |
| cooldown_minutes | int | 冷却时间（分钟） |
| max_delete_items | int | 单轮最多清理条目数 |
| max_gb_per_run | float | 单轮最多释放容量（GB） |
| max_gb_per_day | float | 当日最多释放容量（GB） |
| protect_recent_days | int | 近期入库保护天数 |
| monitor_download | bool | 监听资源目录阈值 |
| monitor_library | bool | 监听媒体库目录阈值 |
| monitor_downloader | bool | 监听下载器做种阈值 |
| download_threshold_mode/value | enum+float | 资源目录阈值 |
| library_threshold_mode/value | enum+float | 媒体库目录阈值 |
| seeding_days | int | 下载器做种时长阈值 |
| mp_only | bool | 仅处理 MP 可关联记录 |
| path_allowlist | string/list | 删除白名单路径（多行） |
| path_blocklist | string/list | 删除黑名单路径（多行） |
| clean_media_data | bool | 删除媒体文件 |
| clean_scrape_data | bool | 删除同名刮削文件 |
| clean_downloader_seed | bool | 删除下载器做种 |
| delete_downloader_files | bool | 删种时删除下载器数据文件 |
| clean_transfer_history | bool | 删除 MP 整理记录 |
| clean_download_history | bool | 删除 MP 下载记录 |
| refresh_mediaserver | bool | 清理后刷新媒体库 |
| refresh_servers | list | 指定刷新媒体服务器 |
| notify | bool | 发送通知 |

## 6. 推荐配置
### 6.1 首次上线（保守）
- dry_run=true
- mp_only=true
- max_delete_items=1
- max_gb_per_run=20
- max_gb_per_day=50
- protect_recent_days=30
- delete_downloader_files=false

### 6.2 稳定后（平衡）
- dry_run=false
- max_delete_items=3
- max_gb_per_run=50
- max_gb_per_day=200
- seeding_days=7

## 7. 配置示例（JSON）
```json
{
  "enabled": true,
  "dry_run": true,
  "cron": "*/30 * * * *",
  "cooldown_minutes": 60,
  "max_delete_items": 3,
  "max_gb_per_run": 50,
  "max_gb_per_day": 200,
  "protect_recent_days": 30,
  "monitor_download": true,
  "monitor_library": true,
  "monitor_downloader": false,
  "download_threshold_mode": "size",
  "download_threshold_value": 100,
  "library_threshold_mode": "size",
  "library_threshold_value": 100,
  "downloaders": [],
  "seeding_days": 7,
  "mp_only": true,
  "path_allowlist": "/media/library\n/downloads",
  "path_blocklist": "/media/library/收藏夹",
  "clean_media_data": true,
  "clean_scrape_data": true,
  "clean_downloader_seed": true,
  "delete_downloader_files": false,
  "clean_transfer_history": true,
  "clean_download_history": true,
  "refresh_mediaserver": false,
  "refresh_servers": [],
  "notify": true
}
```
