# 磁盘清理（DiskCleaner）使用说明

本文档说明插件三种触发模式的执行链路、关键判断关系与常见选择建议。

## 1. 模式总览

- 模式一：`flow_library_mp_downloader`（媒体优先，推荐）
- 模式二：`flow_downloader_mp_library`（下载器优先）
- 模式三：`flow_transfer_oldest`（整理记录优先，旧到新）

## 2. 判断关系（统一）

### 2.1 任务入口

```mermaid
flowchart TD
Start[开始任务 _task] --> Retry[失败补偿重试队列可选]
Retry --> Guard{冷却时间/当日上限检查}
Guard -->|命中且无补偿任务| Skip[本轮跳过]
Guard -->|通过| Flow[进入所选触发流程]
```

### 2.2 统一删除步骤

```mermaid
flowchart TD
A[构建删除目标] --> B[计算 planned 和 freed_bytes]
B --> C{命中单轮/当日容量上限?}
C -->|是| EndSkip[跳过当前候选]
C -->|否| D{dry_run?}
D -->|是| Dry[仅记账，不实际删除]
D -->|否| Real[执行删除：媒体/刮削/删种/整理记录/下载记录]
Real --> Step[生成 steps]
Dry --> Step
Step --> RetryQ{有失败步骤且启用补偿?}
RetryQ -->|是| Enqueue[入 retry_queue]
RetryQ -->|否| Done[返回 action]
Enqueue --> Done
```

## 3. 模式一：媒体优先（推荐）

```mermaid
flowchart TD
A[流程1 媒体库阈值触发] --> B{媒体库阈值命中?}
B -->|否| End[结束]
B -->|是| C[从媒体库挑最老媒体文件]
C --> D{找到候选?}
D -->|否| End
D -->|是| E[_cleanup_by_media_file]
E --> F[优先联动MP记录与下载器]
F --> G[构建 media_targets + sidecars]
G --> H[可选硬链接兜底扩展]
H --> I[执行统一删除步骤]
I --> C
```

关键点：

- 优先从媒体文件反查 MP 整理记录。
- 若开启“硬链接强制删除(兜底)”且缺失 MP 关联，可按同 inode 扩展删除目标。

## 4. 模式二：下载器优先

```mermaid
flowchart TD
A[流程2 下载器优先] --> B{下载目录阈值命中 或 做种时长触发?}
B -->|否| End[结束]
B -->|是| C[按做种时长挑选候选种子]
C --> D{找到候选?}
D -->|否且目录阈值已命中| C2[放宽做种时长再选一次]
C2 --> D2{找到候选?}
D2 -->|否| End
D2 -->|是| E
D -->|是| E[_cleanup_by_torrent]
E --> F[按hash查MP整理记录 histories]
F --> G[提取下载记录文件路径 downloadfiles]
G --> H[合并目标并做硬链接扩展]
H --> I{媒体库范围模式且无MP时，目标是否命中所选媒体库?}
I -->|否| LoopNext[跳过该种子]
I -->|是| J[执行统一删除步骤]
J --> C
LoopNext --> C
```

关键点：

- 可同时受“下载目录阈值”与“做种时长”驱动。
- 开启硬链接兜底时，允许无 MP 记录继续处理，并会从 `downloadfiles` 提取本地路径参与清理。

## 5. 模式三：整理记录优先（旧到新）

```mermaid
flowchart TD
A[流程3 整理记录优先] --> B{任一触发原因命中?}
B -->|否| End[结束]
B -->|是| C[按date/id从旧到新挑整理记录]
C --> D{找到候选?}
D -->|否| End
D -->|是| E[_cleanup_by_transfer_history]
E --> F[校验近期保护/电视剧完结/媒体库范围]
F --> G[构建 media_targets + sidecars]
G --> H[可选硬链接兜底扩展]
H --> I[执行统一删除步骤]
I --> C
```

触发原因：

- 媒体库目录阈值命中；
- 下载目录阈值命中；
- 下载器做种时长触发（开启监控时）。

## 6. 重要开关与影响

- `force_hardlink_cleanup`：开启后会把下载目录加入删除根范围，并对媒体目标做同 inode 扩展（兜底策略）。
- `tv_complete_only`：仅清理已完结电视剧（依据 TMDB 状态）。
- `monitor_download`：启用下载目录空间阈值触发。
- `monitor_downloader` + `seeding_days`：启用下载器做种时长触发。
- `enable_retry_queue`：失败步骤进入补偿队列重试。

## 7. 选择建议

- 常规环境优先选“媒体优先”。
- 下载器占用明显高、希望从做种任务侧回收，选“下载器优先”。
- 需要严格按历史最旧记录清理，选“整理记录优先”。

