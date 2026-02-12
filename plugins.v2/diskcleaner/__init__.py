import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.mediaserver import MediaServerChain
from app.core.config import settings
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.mediaserver_oper import MediaServerOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.downloader import DownloaderHelper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, RefreshMediaItem
from app.utils.system import SystemUtils


class DiskCleaner(_PluginBase):
    # 插件信息
    plugin_name = "磁盘清理"
    plugin_desc = "按磁盘阈值与做种时长自动清理媒体、做种与MP整理记录"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/clean.png"
    plugin_version = "0.5"
    plugin_author = "逗猫"
    author_url = "https://github.com/baozaodetudou"
    plugin_config_prefix = "diskcleaner_"
    plugin_order = 28
    auth_level = 1

    _lock = threading.Lock()

    # 调度
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled = False
    _onlyonce = False
    _notify = False
    _cron = "*/30 * * * *"
    _dry_run = True
    _cooldown_minutes = 60

    # 监听开关
    _monitor_download = True
    _monitor_library = True
    _monitor_downloader = False
    _trigger_logic = "or"  # or/and
    _strategy_quota = 1

    # 阈值配置
    _download_threshold_mode = "size"  # size/percent
    _download_threshold_value = 100.0
    _library_threshold_mode = "size"
    _library_threshold_value = 100.0

    # 下载器策略
    _downloaders: List[str] = []
    _seeding_days = 7
    _max_delete_items = 3
    _max_gb_per_run = 50.0
    _max_gb_per_day = 200.0
    _protect_recent_days = 30

    # 删除开关
    _clean_media_data = True
    _clean_scrape_data = True
    _clean_downloader_seed = True
    _delete_downloader_files = False
    _clean_transfer_history = True
    _clean_download_history = True
    _mp_only = True
    _path_allowlist: List[str] = []
    _path_blocklist: List[str] = []
    _current_run_freed_bytes = 0

    # 媒体库刷新
    _refresh_mediaserver = False
    _refresh_mode = "item"  # item/root
    _refresh_batch_size = 20
    _refresh_servers: List[str] = []
    _prefer_playback_history = True
    _library_probe_limit = 30
    _enable_retry_queue = True
    _retry_max_attempts = 3
    _retry_interval_minutes = 30
    _retry_batch_size = 5

    # 数据操作
    _transfer_oper: Optional[TransferHistoryOper] = None
    _download_oper: Optional[DownloadHistoryOper] = None
    _mediaserver_oper: Optional[MediaServerOper] = None
    _mediaserver_chain: Optional[MediaServerChain] = None
    _playback_ts_cache: Dict[str, int] = {}

    def init_plugin(self, config: dict = None):
        self.stop_service()

        self._transfer_oper = TransferHistoryOper()
        self._download_oper = DownloadHistoryOper()
        self._mediaserver_oper = MediaServerOper()
        self._mediaserver_chain = None
        self._playback_ts_cache = {}

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._notify = bool(config.get("notify", False))
            self._cron = config.get("cron") or "*/30 * * * *"
            self._dry_run = bool(config.get("dry_run", True))
            self._cooldown_minutes = int(self._safe_float(config.get("cooldown_minutes"), 60))

            self._monitor_download = bool(config.get("monitor_download", True))
            self._monitor_library = bool(config.get("monitor_library", True))
            self._monitor_downloader = bool(config.get("monitor_downloader", False))
            self._trigger_logic = config.get("trigger_logic") or "or"
            self._strategy_quota = int(self._safe_float(config.get("strategy_quota"), 1))

            self._download_threshold_mode = config.get("download_threshold_mode") or "size"
            self._download_threshold_value = self._safe_float(config.get("download_threshold_value"), 100.0)
            self._library_threshold_mode = config.get("library_threshold_mode") or "size"
            self._library_threshold_value = self._safe_float(config.get("library_threshold_value"), 100.0)

            self._downloaders = config.get("downloaders") or []
            self._seeding_days = int(self._safe_float(config.get("seeding_days"), 7))
            self._max_delete_items = max(1, int(self._safe_float(config.get("max_delete_items"), 3)))
            self._max_gb_per_run = self._safe_float(config.get("max_gb_per_run"), 50.0)
            self._max_gb_per_day = self._safe_float(config.get("max_gb_per_day"), 200.0)
            self._protect_recent_days = int(self._safe_float(config.get("protect_recent_days"), 30))

            self._clean_media_data = bool(config.get("clean_media_data", True))
            self._clean_scrape_data = bool(config.get("clean_scrape_data", True))
            self._clean_downloader_seed = bool(config.get("clean_downloader_seed", True))
            self._delete_downloader_files = bool(config.get("delete_downloader_files", False))
            self._clean_transfer_history = bool(config.get("clean_transfer_history", True))
            self._clean_download_history = bool(config.get("clean_download_history", True))
            self._mp_only = bool(config.get("mp_only", True))
            self._path_allowlist = self._parse_path_list(config.get("path_allowlist"))
            self._path_blocklist = self._parse_path_list(config.get("path_blocklist"))

            self._refresh_mediaserver = bool(config.get("refresh_mediaserver", False))
            self._refresh_mode = config.get("refresh_mode") or "item"
            self._refresh_batch_size = int(self._safe_float(config.get("refresh_batch_size"), 20))
            self._refresh_servers = config.get("refresh_servers") or []
            self._prefer_playback_history = bool(config.get("prefer_playback_history", True))
            self._library_probe_limit = int(self._safe_float(config.get("library_probe_limit"), 30))
            self._enable_retry_queue = bool(config.get("enable_retry_queue", True))
            self._retry_max_attempts = int(self._safe_float(config.get("retry_max_attempts"), 3))
            self._retry_interval_minutes = int(self._safe_float(config.get("retry_interval_minutes"), 30))
            self._retry_batch_size = int(self._safe_float(config.get("retry_batch_size"), 5))

        self._normalize_config()

        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._enabled and self._cron:
                try:
                    self._scheduler.add_job(
                        func=self._task,
                        trigger=CronTrigger.from_crontab(self._cron),
                        name=self.plugin_name,
                    )
                    logger.info(f"{self.plugin_name}服务启动，周期：{self._cron}")
                except Exception as err:
                    logger.error(f"{self.plugin_name}定时任务配置错误：{err}")

            if self._onlyonce:
                logger.info(f"{self.plugin_name}服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self._task,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name=f"{self.plugin_name}-once",
                )
                self._onlyonce = False
                self.__update_config()

            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        downloader_items = [
            {"title": conf.name, "value": conf.name}
            for conf in DownloaderHelper().get_configs().values()
        ]
        media_items = [
            {"title": conf.name, "value": conf.name}
            for conf in MediaServerHelper().get_configs().values()
        ]

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "dry_run", "label": "演练模式(不执行删除)"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "trigger_logic",
                                            "label": "阈值触发逻辑",
                                            "items": [
                                                {"title": "OR（任一阈值命中）", "value": "or"},
                                                {"title": "AND（全部阈值命中）", "value": "and"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "strategy_quota",
                                            "label": "同轮单策略配额",
                                            "placeholder": "每个策略单轮最多处理条目数",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "*/30 * * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_delete_items",
                                            "label": "单次最多清理条目",
                                            "placeholder": "建议 1-5",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cooldown_minutes",
                                            "label": "触发冷却时间(分钟)",
                                            "placeholder": "建议 30-180",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_gb_per_run",
                                            "label": "单轮最多释放(GB)",
                                            "placeholder": "如 50",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_gb_per_day",
                                            "label": "每日最多释放(GB)",
                                            "placeholder": "如 200",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "protect_recent_days",
                                            "label": "近期入库保护(天)",
                                            "placeholder": "如 30",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "monitor_download", "label": "监听资源目录"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "download_threshold_mode",
                                            "label": "资源阈值类型",
                                            "items": [
                                                {"title": "剩余固定值(GB)", "value": "size"},
                                                {"title": "剩余百分比(%)", "value": "percent"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "download_threshold_value",
                                            "label": "资源触发阈值",
                                            "placeholder": "如 100 或 1",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "monitor_library", "label": "监听媒体库目录"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "library_threshold_mode",
                                            "label": "媒体库阈值类型",
                                            "items": [
                                                {"title": "剩余固定值(GB)", "value": "size"},
                                                {"title": "剩余百分比(%)", "value": "percent"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "library_threshold_value",
                                            "label": "媒体库触发阈值",
                                            "placeholder": "如 100 或 1",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "monitor_downloader", "label": "独立下载器监听"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "seeding_days",
                                            "label": "做种时长阈值(天)",
                                            "placeholder": "如 7",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "chips": True,
                                            "multiple": True,
                                            "clearable": True,
                                            "model": "downloaders",
                                            "label": "下载器",
                                            "items": downloader_items,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "clean_media_data", "label": "删除媒体数据"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "clean_scrape_data", "label": "删除刮削数据"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "clean_downloader_seed", "label": "删除下载器做种"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "delete_downloader_files", "label": "删种时删除下载文件"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "clean_transfer_history", "label": "删除MP整理记录"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "clean_download_history", "label": "删除MP下载记录"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "refresh_mediaserver", "label": "清理后刷新媒体库"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "mp_only", "label": "仅处理MP可关联记录"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "prefer_playback_history", "label": "优先按播放历史判定老化"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "library_probe_limit",
                                            "label": "媒体候选扫描数量",
                                            "placeholder": "建议 10-100",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "refresh_mode",
                                            "label": "媒体库刷新方式",
                                            "items": [
                                                {"title": "定向刷新(优先)", "value": "item"},
                                                {"title": "整库刷新", "value": "root"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "refresh_batch_size",
                                            "label": "刷新批次大小",
                                            "placeholder": "单次定向刷新条目数",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "enable_retry_queue", "label": "启用失败补偿重试"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retry_max_attempts",
                                            "label": "最大重试次数",
                                            "placeholder": "建议 2-5",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retry_interval_minutes",
                                            "label": "重试间隔(分钟)",
                                            "placeholder": "建议 15-120",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retry_batch_size",
                                            "label": "单轮补偿处理数",
                                            "placeholder": "建议 1-10",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_allowlist",
                                            "label": "删除白名单路径",
                                            "rows": 3,
                                            "placeholder": "一行一个路径；留空时默认仅允许MP资源目录/媒体库目录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_blocklist",
                                            "label": "删除黑名单路径",
                                            "rows": 3,
                                            "placeholder": "一行一个路径；命中后永不删除",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "chips": True,
                                            "multiple": True,
                                            "clearable": True,
                                            "model": "refresh_servers",
                                            "label": "刷新媒体服务器(留空代表全部)",
                                            "items": media_items,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": "插件会自动读取 MoviePilot 配置中的本地资源目录与媒体库目录。建议先开启“删除下载器做种”，关闭“删种时删除下载文件”，验证逻辑后再逐步放开。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "dry_run": True,
            "cron": "*/30 * * * *",
            "cooldown_minutes": 60,
            "monitor_download": True,
            "monitor_library": True,
            "monitor_downloader": False,
            "trigger_logic": "or",
            "strategy_quota": 1,
            "download_threshold_mode": "size",
            "download_threshold_value": 100,
            "library_threshold_mode": "size",
            "library_threshold_value": 100,
            "downloaders": [],
            "seeding_days": 7,
            "max_delete_items": 3,
            "max_gb_per_run": 50,
            "max_gb_per_day": 200,
            "protect_recent_days": 30,
            "clean_media_data": True,
            "clean_scrape_data": True,
            "clean_downloader_seed": True,
            "delete_downloader_files": False,
            "clean_transfer_history": True,
            "clean_download_history": True,
            "mp_only": True,
            "prefer_playback_history": True,
            "library_probe_limit": 30,
            "path_allowlist": "",
            "path_blocklist": "",
            "refresh_mediaserver": False,
            "refresh_mode": "item",
            "refresh_batch_size": 20,
            "refresh_servers": [],
            "enable_retry_queue": True,
            "retry_max_attempts": 3,
            "retry_interval_minutes": 30,
            "retry_batch_size": 5,
        }

    def get_page(self) -> List[dict]:
        usage = self._collect_monitor_usage()
        historys: List[dict] = self.get_data("history") or []
        historys = sorted(historys, key=lambda x: x.get("time", ""), reverse=True)[:20]

        cards = [
            self._build_usage_card(
                title="资源目录",
                usage=usage.get("download", {}),
                enabled=self._monitor_download,
                mode=self._download_threshold_mode,
                value=self._download_threshold_value,
            ),
            self._build_usage_card(
                title="媒体库目录",
                usage=usage.get("library", {}),
                enabled=self._monitor_library,
                mode=self._library_threshold_mode,
                value=self._library_threshold_value,
            ),
        ]

        if not historys:
            history_content = [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无清理记录",
                    },
                }
            ]
        else:
            history_content = []
            for item in historys:
                history_content.append(
                    {
                        "component": "VCard",
                        "props": {"class": "mb-2"},
                        "content": [
                            {"component": "VCardText", "text": f"时间：{item.get('time', '-')}"},
                            {"component": "VCardText", "text": f"触发器：{item.get('trigger', '-')}"},
                            {"component": "VCardText", "text": f"对象：{item.get('target', '-')}"},
                            {"component": "VCardText", "text": f"动作：{item.get('action', '-')}"},
                            {"component": "VCardText", "text": f"释放：{self._format_size(int(item.get('freed_bytes', 0) or 0))}"},
                            {"component": "VCardText", "text": f"模式：{item.get('mode', 'apply')}"},
                        ],
                    }
                )

        return [
            {
                "component": "VCard",
                "content": [
                    {"component": "VCardText", "text": f"运行模式：{'演练模式' if self._dry_run else '实际删除'}"},
                    {"component": "VCardText", "text": f"阈值逻辑：{self._trigger_logic.upper()} / 单策略配额：{self._strategy_quota}"},
                    {"component": "VCardText", "text": f"冷却时间：{self._cooldown_minutes} 分钟"},
                    {"component": "VCardText", "text": f"单轮上限：{self._max_delete_items} 条 / {self._max_gb_per_run} GB"},
                    {"component": "VCardText", "text": f"当日已释放：{self._format_size(self._get_daily_freed_bytes())} / {self._max_gb_per_day} GB"},
                    {"component": "VCardText", "text": f"失败补偿队列：{self._retry_queue_size()} 条"},
                ],
            },
            {
                "component": "div",
                "props": {"class": "grid gap-3"},
                "content": cards,
            },
            {
                "component": "VCard",
                "content": [
                    {"component": "VCardText", "text": "最近20条清理记录"},
                    {"component": "div", "props": {"class": "px-3 pb-3"}, "content": history_content},
                ],
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error(f"{self.plugin_name}停止服务失败：{err}")

    def _task(self):
        if not self._transfer_oper:
            self._transfer_oper = TransferHistoryOper()
        if not self._download_oper:
            self._download_oper = DownloadHistoryOper()
        if not self._mediaserver_oper:
            self._mediaserver_oper = MediaServerOper()

        with self._lock:
            try:
                logger.info(f"{self.plugin_name}开始执行")
                self._current_run_freed_bytes = 0
                self._playback_ts_cache = {}
                retry_actions: List[dict] = []
                skip_normal_cleanup = False
                if self._enable_retry_queue and not self._dry_run:
                    retry_actions = self._process_retry_queue(limit=self._retry_batch_size)

                if self._is_in_cooldown():
                    if not retry_actions:
                        logger.info(f"{self.plugin_name}命中冷却时间，跳过执行")
                        return
                    logger.info(f"{self.plugin_name}命中冷却时间，仅执行失败补偿")
                    skip_normal_cleanup = True

                if not self._dry_run and self._is_daily_limit_reached():
                    if not retry_actions:
                        logger.warning(f"{self.plugin_name}已达当日释放上限，跳过执行")
                        return
                    logger.warning(f"{self.plugin_name}已达当日释放上限，仅执行失败补偿")
                    skip_normal_cleanup = True

                usage = self._collect_monitor_usage()
                self.save_data("latest_usage", usage)

                round_actions: List[dict] = []
                if not skip_normal_cleanup:
                    strategy_plan = self._resolve_strategy_plan(usage)
                    if strategy_plan:
                        logger.info(f"{self.plugin_name}本轮触发策略：{','.join(strategy_plan)}")
                    for strategy in strategy_plan:
                        remaining = self._remaining_round_quota(round_actions)
                        if remaining <= 0:
                            break
                        quota = min(self._strategy_quota, remaining)
                        if quota <= 0:
                            continue

                        if strategy == "library":
                            actions = self._clean_by_library_threshold(quota=quota)
                        elif strategy == "download":
                            actions = self._clean_by_download_threshold(quota=quota)
                        elif strategy == "downloader":
                            actions = self._clean_by_downloader_seeding(quota=quota)
                        else:
                            actions = []
                        round_actions.extend(actions)

                all_actions = retry_actions + round_actions
                if not all_actions:
                    logger.info(f"{self.plugin_name}本次无需清理")
                    self.save_data("last_run_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    return

                history = self.get_data("history") or []
                history.extend(all_actions)
                self.save_data("history", history[-200:])

                freed_bytes = sum(int(item.get("freed_bytes", 0) or 0) for item in all_actions)
                if not self._dry_run and freed_bytes > 0:
                    self._update_daily_freed_bytes(freed_bytes)

                if self._refresh_mediaserver:
                    refresh_items = self._collect_refresh_items(all_actions)
                    refreshed = self._refresh_media_servers(refresh_items=refresh_items)
                    if refreshed > 0:
                        logger.info(f"{self.plugin_name}已触发 {refreshed} 个媒体服务器刷新")

                if self._notify:
                    text = "\n".join(
                        [f"{item.get('time')} | {item.get('trigger')} | {item.get('action')}" for item in all_actions[:20]]
                    )
                    mode_text = "演练模式" if self._dry_run else "实际删除"
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title=f"【磁盘清理】任务完成（{mode_text}）",
                        text=f"{text}\n预计/实际释放：{self._format_size(freed_bytes)}",
                    )

                self.save_data("last_run_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                logger.info(
                    f"{self.plugin_name}执行完成，本次处理 {len(all_actions)} 条，"
                    f"{'预计' if self._dry_run else '实际'}释放 {self._format_size(freed_bytes)}"
                )
            except Exception as err:
                logger.error(f"{self.plugin_name}任务执行异常：{err}", exc_info=True)

    def _resolve_strategy_plan(self, usage: dict) -> List[str]:
        library_hit = self._monitor_library and self._is_threshold_hit(
            usage.get("library", {}), self._library_threshold_mode, self._library_threshold_value
        )
        download_hit = self._monitor_download and self._is_threshold_hit(
            usage.get("download", {}), self._download_threshold_mode, self._download_threshold_value
        )

        plan: List[str] = []
        if self._trigger_logic == "and":
            threshold_enabled = self._monitor_library or self._monitor_download
            threshold_ok = threshold_enabled and \
                (not self._monitor_library or library_hit) and \
                (not self._monitor_download or download_hit)
            if threshold_ok:
                if self._monitor_library:
                    plan.append("library")
                if self._monitor_download:
                    plan.append("download")
        else:
            if library_hit:
                plan.append("library")
            if download_hit:
                plan.append("download")

        if self._monitor_downloader:
            plan.append("downloader")

        return plan

    def _remaining_round_quota(self, actions: List[dict]) -> int:
        if self._max_delete_items <= 0:
            return max(1, self._strategy_quota)
        return max(0, self._max_delete_items - len(actions))

    def _clean_by_library_threshold(self, quota: Optional[int] = None) -> List[dict]:
        actions: List[dict] = []
        skipped_paths = set()
        strategy_quota = max(1, int(quota if quota is not None else self._max_delete_items))
        for _ in range(strategy_quota):
            if self._is_run_limit_reached(actions):
                break
            usage = self._collect_monitor_usage().get("library", {})
            if not self._is_threshold_hit(usage, self._library_threshold_mode, self._library_threshold_value):
                break

            candidate = self._pick_oldest_library_media(skipped_paths=skipped_paths)
            if not candidate:
                logger.warning(f"{self.plugin_name}未找到可清理的媒体文件")
                break

            result = self._cleanup_by_media_file(candidate)
            skipped_paths.add(candidate.get("path").as_posix())
            if result:
                actions.append(result)
                self._current_run_freed_bytes += int(result.get("freed_bytes", 0) or 0)
        return actions

    def _clean_by_download_threshold(self, quota: Optional[int] = None) -> List[dict]:
        actions: List[dict] = []
        skipped_hashes = set()
        strategy_quota = max(1, int(quota if quota is not None else self._max_delete_items))
        for _ in range(strategy_quota):
            if self._is_run_limit_reached(actions):
                break
            usage = self._collect_monitor_usage().get("download", {})
            if not self._is_threshold_hit(usage, self._download_threshold_mode, self._download_threshold_value):
                break

            candidate = self._pick_longest_seeding_torrent(min_days=None, skipped_hashes=skipped_hashes)
            if not candidate:
                logger.warning(f"{self.plugin_name}未找到可清理的做种任务")
                break

            result = self._cleanup_by_torrent(candidate, trigger="资源目录阈值")
            skipped_hashes.add(candidate.get("hash"))
            if result:
                actions.append(result)
                self._current_run_freed_bytes += int(result.get("freed_bytes", 0) or 0)

        return actions

    def _clean_by_downloader_seeding(self, quota: Optional[int] = None) -> List[dict]:
        actions: List[dict] = []
        skipped_hashes = set()
        strategy_quota = max(1, int(quota if quota is not None else self._max_delete_items))
        for _ in range(strategy_quota):
            if self._is_run_limit_reached(actions):
                break
            candidate = self._pick_longest_seeding_torrent(min_days=self._seeding_days, skipped_hashes=skipped_hashes)
            if not candidate:
                break

            result = self._cleanup_by_torrent(candidate, trigger="下载器做种阈值")
            skipped_hashes.add(candidate.get("hash"))
            if result:
                actions.append(result)
                self._current_run_freed_bytes += int(result.get("freed_bytes", 0) or 0)

        return actions

    def _cleanup_by_media_file(self, candidate: dict) -> Optional[dict]:
        media_path: Path = candidate.get("path")
        if not media_path:
            return None

        history = self._transfer_oper.get_by_dest(media_path.as_posix()) if self._transfer_oper else None
        if self._mp_only and not history:
            logger.info(f"{self.plugin_name}跳过非MP关联媒体：{media_path.as_posix()}")
            return None
        if history and self._is_history_recent(history):
            logger.info(f"{self.plugin_name}命中近期保护，跳过：{media_path.as_posix()}")
            return None
        download_hash = getattr(history, "download_hash", None) if history else None
        downloader = getattr(history, "downloader", None) if history else None

        library_paths = self._library_paths()
        sidecars = self._collect_scrape_files(media_path) if self._clean_scrape_data else []

        planned_media = 1 if self._clean_media_data and media_path.exists() else 0
        planned_scrape = len([item for item in sidecars if item.exists()]) if self._clean_scrape_data else 0
        planned_downloader = 1 if self._clean_downloader_seed and download_hash and downloader else 0
        planned_transfer = self._count_transfer_records(download_hash, history) if self._clean_transfer_history else 0
        planned_download = self._count_download_records(download_hash) if self._clean_download_history and download_hash else 0

        freed_bytes = 0
        if planned_media:
            freed_bytes += self._path_size(media_path)
        for sidecar in sidecars:
            freed_bytes += self._path_size(sidecar)

        if freed_bytes <= 0 and (planned_downloader + planned_transfer + planned_download) <= 0:
            return None

        if not self._dry_run and self._is_run_bytes_limit_reached(freed_bytes):
            logger.info(f"{self.plugin_name}单轮容量上限已达，跳过：{media_path.as_posix()}")
            return None
        if not self._dry_run and self._is_daily_bytes_limit_reached(freed_bytes):
            logger.info(f"{self.plugin_name}当日容量上限将超额，跳过：{media_path.as_posix()}")
            return None

        removed_media = 0
        removed_scrape = 0
        removed_downloader = 0
        removed_transfer = 0
        removed_download = 0

        if self._dry_run:
            removed_media = planned_media
            removed_scrape = planned_scrape
            removed_downloader = planned_downloader
            removed_transfer = planned_transfer
            removed_download = planned_download
        else:
            if self._clean_media_data and self._delete_local_item(media_path, library_paths):
                removed_media += 1

            if self._clean_scrape_data:
                for sidecar in sidecars:
                    if self._delete_local_item(sidecar, library_paths):
                        removed_scrape += 1

            if self._clean_downloader_seed and download_hash and downloader:
                if self._delete_torrent(downloader=downloader, torrent_hash=download_hash):
                    removed_downloader += 1

            if self._clean_transfer_history:
                removed_transfer = self._delete_transfer_history(download_hash=download_hash, history=history)

            if self._clean_download_history and download_hash:
                removed_download = self._delete_download_history(download_hash)

        step_result = {
            "media": self._build_step_result(planned_media, removed_media),
            "scrape": self._build_step_result(planned_scrape, removed_scrape),
            "downloader": self._build_step_result(planned_downloader, removed_downloader),
            "transfer_history": self._build_step_result(planned_transfer, removed_transfer),
            "download_history": self._build_step_result(planned_download, removed_download),
        }
        failed_steps = self._collect_failed_steps(step_result)
        if failed_steps and not self._dry_run and self._enable_retry_queue:
            self._enqueue_retry({
                "mode": "media",
                "retry_key": f"media:{media_path.as_posix()}:{download_hash or ''}",
                "trigger": "媒体库阈值",
                "target": media_path.as_posix(),
                "media_path": media_path.as_posix(),
                "sidecars": [item.as_posix() for item in sidecars],
                "download_hash": download_hash,
                "downloader": downloader,
                "history_dest": getattr(history, "dest", None) if history else None,
                "failed_steps": failed_steps,
            })

        total_actions = removed_media + removed_scrape + removed_downloader + removed_transfer + removed_download
        if total_actions <= 0:
            return None

        action_text = (
            f"媒体{removed_media} 刮削{removed_scrape} 删种{removed_downloader} "
            f"整理记录{removed_transfer} 下载记录{removed_download}"
        )
        logger.info(f"{self.plugin_name}按媒体文件清理：{media_path.as_posix()} -> {action_text}")
        refresh_items = []
        if history:
            refresh_item = self._build_refresh_item(history=history, target_path=media_path)
            if refresh_item:
                refresh_items.append(refresh_item)

        return {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": "媒体库阈值",
            "target": media_path.as_posix(),
            "action": action_text,
            "freed_bytes": freed_bytes,
            "mode": "dry-run" if self._dry_run else "apply",
            "refresh_items": refresh_items,
            "steps": step_result,
        }

    def _cleanup_by_torrent(self, candidate: dict, trigger: str) -> Optional[dict]:
        download_hash = candidate.get("hash")
        downloader = candidate.get("downloader")
        name = candidate.get("name") or download_hash

        if not download_hash:
            return None

        histories = self._transfer_oper.list_by_hash(download_hash) if self._transfer_oper else []
        if self._mp_only and not histories:
            logger.info(f"{self.plugin_name}跳过非MP关联任务：{downloader}:{name}")
            return None
        if histories and any(self._is_history_recent(history) for history in histories):
            logger.info(f"{self.plugin_name}命中近期保护，跳过：{downloader}:{name}")
            return None
        library_paths = self._library_paths()

        media_targets: List[Path] = []
        sidecar_targets: List[Path] = []
        if self._clean_media_data or self._clean_scrape_data:
            handled_paths = set()
            for history in histories:
                dest = getattr(history, "dest", None)
                if not dest:
                    continue
                path = Path(dest)
                if path.as_posix() in handled_paths:
                    continue
                handled_paths.add(path.as_posix())
                if self._clean_media_data and path.exists():
                    media_targets.append(path)
                if self._clean_scrape_data:
                    sidecar_targets.extend([item for item in self._collect_scrape_files(path) if item.exists()])

        dedup_sidecars = {}
        for sidecar in sidecar_targets:
            dedup_sidecars[sidecar.as_posix()] = sidecar
        sidecar_targets = list(dedup_sidecars.values())

        planned_media = len(media_targets)
        planned_scrape = len(sidecar_targets)
        planned_downloader = 1 if self._clean_downloader_seed and downloader else 0
        planned_transfer = self._count_transfer_records(download_hash, None) if self._clean_transfer_history else 0
        planned_download = self._count_download_records(download_hash) if self._clean_download_history else 0

        freed_bytes = sum(self._path_size(path) for path in media_targets) + \
            sum(self._path_size(path) for path in sidecar_targets)

        if freed_bytes <= 0 and (planned_downloader + planned_transfer + planned_download) <= 0:
            return None

        if not self._dry_run and self._is_run_bytes_limit_reached(freed_bytes):
            logger.info(f"{self.plugin_name}单轮容量上限已达，跳过：{downloader}:{name}")
            return None
        if not self._dry_run and self._is_daily_bytes_limit_reached(freed_bytes):
            logger.info(f"{self.plugin_name}当日容量上限将超额，跳过：{downloader}:{name}")
            return None

        removed_media = 0
        removed_scrape = 0
        removed_downloader = 0
        removed_transfer = 0
        removed_download = 0

        if self._dry_run:
            removed_media = planned_media
            removed_scrape = planned_scrape
            removed_downloader = planned_downloader
            removed_transfer = planned_transfer
            removed_download = planned_download
        else:
            for path in media_targets:
                if self._delete_local_item(path, library_paths):
                    removed_media += 1

            for sidecar in sidecar_targets:
                if self._delete_local_item(sidecar, library_paths):
                    removed_scrape += 1

            if self._clean_downloader_seed and downloader:
                if self._delete_torrent(downloader=downloader, torrent_hash=download_hash):
                    removed_downloader += 1

            if self._clean_transfer_history:
                removed_transfer = self._delete_transfer_history(download_hash=download_hash, history=None)

            if self._clean_download_history:
                removed_download = self._delete_download_history(download_hash)

        step_result = {
            "media": self._build_step_result(planned_media, removed_media),
            "scrape": self._build_step_result(planned_scrape, removed_scrape),
            "downloader": self._build_step_result(planned_downloader, removed_downloader),
            "transfer_history": self._build_step_result(planned_transfer, removed_transfer),
            "download_history": self._build_step_result(planned_download, removed_download),
        }
        failed_steps = self._collect_failed_steps(step_result)
        if failed_steps and not self._dry_run and self._enable_retry_queue:
            self._enqueue_retry({
                "mode": "torrent",
                "retry_key": f"torrent:{download_hash}",
                "trigger": trigger,
                "target": f"{downloader}:{name}",
                "download_hash": download_hash,
                "downloader": downloader,
                "media_targets": [item.as_posix() for item in media_targets],
                "sidecar_targets": [item.as_posix() for item in sidecar_targets],
                "history_dests": [getattr(item, "dest", None) for item in histories if getattr(item, "dest", None)],
                "failed_steps": failed_steps,
            })

        total_actions = removed_media + removed_scrape + removed_downloader + removed_transfer + removed_download
        if total_actions <= 0:
            return None

        action_text = (
            f"媒体{removed_media} 刮削{removed_scrape} 删种{removed_downloader} "
            f"整理记录{removed_transfer} 下载记录{removed_download}"
        )
        logger.info(f"{self.plugin_name}按下载器任务清理：{name}({download_hash}) -> {action_text}")
        refresh_items = []
        for history in histories:
            refresh_item = self._build_refresh_item(history=history, target_path=getattr(history, "dest", None))
            if refresh_item:
                refresh_items.append(refresh_item)

        return {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger,
            "target": f"{downloader}:{name}",
            "action": action_text,
            "freed_bytes": freed_bytes,
            "mode": "dry-run" if self._dry_run else "apply",
            "refresh_items": refresh_items,
            "steps": step_result,
        }

    def _pick_oldest_library_media(self, skipped_paths: Optional[set] = None) -> Optional[dict]:
        library_paths = self._library_paths()
        if not library_paths:
            return None

        skipped_paths = skipped_paths or set()
        media_exts = {ext.lower() for ext in settings.RMT_MEDIAEXT}
        candidates: List[dict] = []
        need_history = bool(self._transfer_oper and (self._mp_only or self._prefer_playback_history))

        for root in library_paths:
            if not root.exists():
                continue

            for current_root, _, files in os.walk(root.as_posix()):
                for filename in files:
                    path = Path(current_root) / filename
                    if path.as_posix() in skipped_paths:
                        continue
                    if path.suffix.lower() not in media_exts:
                        continue
                    try:
                        stat = path.stat()
                    except Exception:
                        continue
                    history = self._transfer_oper.get_by_dest(path.as_posix()) if need_history and self._transfer_oper else None
                    if self._mp_only and not history:
                        continue
                    if history and self._is_history_recent(history):
                        continue
                    atime = stat.st_atime or stat.st_mtime
                    candidates.append({
                        "path": path,
                        "atime": atime,
                        "history": history,
                    })

        if not candidates:
            return None

        candidates.sort(key=lambda item: item.get("atime") or float("inf"))
        shortlist = candidates[:max(1, self._library_probe_limit)]
        selected = shortlist[0]
        selected_ts = self._effective_access_ts(
            path=selected.get("path"),
            history=selected.get("history"),
            fallback_ts=selected.get("atime"),
        )
        for candidate in shortlist[1:]:
            current_ts = self._effective_access_ts(
                path=candidate.get("path"),
                history=candidate.get("history"),
                fallback_ts=candidate.get("atime"),
            )
            if current_ts < selected_ts:
                selected = candidate
                selected_ts = current_ts

        return {
            "path": selected.get("path"),
            "atime": selected_ts,
            "raw_atime": selected.get("atime"),
        }

    def _effective_access_ts(self, path: Optional[Path], history: Any, fallback_ts: Optional[float]) -> int:
        playback_ts = self._get_history_playback_ts(history)
        if playback_ts:
            return int(playback_ts)

        history_ts = self._parse_datetime_to_ts(getattr(history, "date", None)) if history else None
        if history_ts:
            return int(history_ts)

        if fallback_ts:
            return int(fallback_ts)

        try:
            if path and path.exists():
                return int(path.stat().st_mtime)
        except Exception:
            pass
        return int(time.time())

    def _get_history_playback_ts(self, history: Any) -> Optional[int]:
        if not self._prefer_playback_history or not history:
            return None

        mtype = getattr(history, "type", None)
        tmdbid = getattr(history, "tmdbid", None)
        if not mtype or not tmdbid:
            return None

        season = self._extract_season_number(getattr(history, "seasons", None))
        cache_key = f"{mtype}:{tmdbid}:{season if season is not None else ''}"
        cached_ts = self._playback_ts_cache.get(cache_key)
        if cached_ts is not None:
            return cached_ts or None

        try:
            if not self._mediaserver_oper:
                self._mediaserver_oper = MediaServerOper()
            media_item = self._mediaserver_oper.exists(
                tmdbid=tmdbid,
                mtype=mtype,
                season=season,
            )
            if not media_item:
                self._playback_ts_cache[cache_key] = 0
                return None

            server = getattr(media_item, "server", None)
            item_id = getattr(media_item, "item_id", None)
            if not server or not item_id:
                self._playback_ts_cache[cache_key] = 0
                return None

            if not self._mediaserver_chain:
                self._mediaserver_chain = MediaServerChain()
            item_info = self._mediaserver_chain.iteminfo(server=server, item_id=item_id)
            user_state = getattr(item_info, "user_state", None)
            last_played = getattr(user_state, "last_played_date", None) if user_state else None
            ts = self._parse_datetime_to_ts(last_played) or 0
            self._playback_ts_cache[cache_key] = ts
            return ts or None
        except Exception as err:
            logger.debug(f"{self.plugin_name}读取播放历史失败：{err}")
            self._playback_ts_cache[cache_key] = 0
            return None

    @staticmethod
    def _extract_season_number(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        match = re.search(r"(\d+)", str(value))
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return None

    @staticmethod
    def _parse_datetime_to_ts(date_text: Optional[str]) -> Optional[int]:
        if not date_text:
            return None
        try:
            return int(datetime.strptime(str(date_text), "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            return None

    def _pick_longest_seeding_torrent(self, min_days: Optional[int], skipped_hashes: set) -> Optional[dict]:
        name_filters = self._downloaders if self._downloaders else None
        services = DownloaderHelper().get_services(name_filters=name_filters)
        if not services:
            return None

        best = None
        linked_cache: Dict[str, bool] = {}
        min_seconds = int(min_days * 86400) if min_days else 0

        for downloader_name, service in services.items():
            instance = service.instance
            if not instance:
                continue
            if hasattr(instance, "is_inactive") and instance.is_inactive():
                continue

            torrents = instance.get_completed_torrents() or []
            for torrent in torrents:
                torrent_hash = self._torrent_hash(torrent)
                if not torrent_hash or torrent_hash in skipped_hashes:
                    continue

                if self._mp_only:
                    linked = linked_cache.get(torrent_hash)
                    if linked is None:
                        linked = self._has_mp_history(torrent_hash)
                        linked_cache[torrent_hash] = linked
                    if not linked:
                        continue

                seed_seconds = self._torrent_seed_seconds(torrent)
                if seed_seconds < min_seconds:
                    continue

                if not best or seed_seconds > best["seed_seconds"]:
                    best = {
                        "downloader": downloader_name,
                        "hash": torrent_hash,
                        "name": self._torrent_name(torrent),
                        "seed_seconds": seed_seconds,
                    }

        return best

    def _collect_monitor_usage(self) -> dict:
        return {
            "download": self._calc_usage(self._download_paths()),
            "library": self._calc_usage(self._library_paths()),
        }

    def _download_paths(self) -> List[Path]:
        return self._unique_existing_paths(
            [Path(item.download_path) for item in DirectoryHelper().get_local_download_dirs() if item.download_path]
        )

    def _library_paths(self) -> List[Path]:
        return self._unique_existing_paths(
            [Path(item.library_path) for item in DirectoryHelper().get_local_library_dirs() if item.library_path]
        )

    @staticmethod
    def _unique_existing_paths(paths: List[Path]) -> List[Path]:
        unique = {}
        for path in paths:
            if not path:
                continue
            norm = Path(path).expanduser()
            unique[norm.as_posix()] = norm
        return list(unique.values())

    def _calc_usage(self, paths: List[Path]) -> dict:
        total, free = SystemUtils.space_usage(paths)
        used = max(0, total - free)
        free_percent = (free * 100 / total) if total else 0
        used_percent = (used * 100 / total) if total else 0

        return {
            "paths": [path.as_posix() for path in paths],
            "total": total,
            "free": free,
            "used": used,
            "free_percent": round(free_percent, 2),
            "used_percent": round(used_percent, 2),
            "total_text": self._format_size(total),
            "free_text": self._format_size(free),
            "used_text": self._format_size(used),
        }

    @staticmethod
    def _is_threshold_hit(usage: dict, mode: str, value: float) -> bool:
        if not usage or not usage.get("total"):
            return False
        if mode == "percent":
            return usage.get("free_percent", 0) <= value
        free_gb = usage.get("free", 0) / (1024 ** 3)
        return free_gb <= value

    def _threshold_text(self, mode: str, value: float) -> str:
        if mode == "percent":
            return f"剩余 <= {value}%"
        return f"剩余 <= {value}GB"

    def _delete_local_item(self, path: Path, roots: List[Path]) -> bool:
        path = Path(path)
        if not path.exists() and not path.is_symlink():
            return False

        allow_roots = self._effective_allow_roots(roots)
        if not self._is_path_in_roots(path, allow_roots):
            logger.warning(f"{self.plugin_name}跳过越界删除：{path.as_posix()}")
            return False

        if self._is_path_in_roots(path, self._block_paths()):
            logger.warning(f"{self.plugin_name}命中黑名单，跳过删除：{path.as_posix()}")
            return False

        for root in allow_roots:
            if path == root:
                logger.warning(f"{self.plugin_name}跳过根目录删除：{path.as_posix()}")
                return False

        if self._dry_run:
            return True

        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                parent = path.parent
            else:
                path.unlink(missing_ok=True)
                parent = path.parent
        except Exception as err:
            logger.error(f"{self.plugin_name}删除失败 {path.as_posix()}：{err}")
            return False

        self._delete_empty_parent_dirs(parent, allow_roots)
        return True

    def _delete_empty_parent_dirs(self, parent: Path, roots: List[Path]):
        current = parent
        root_set = {root.as_posix() for root in roots}
        while current and current.exists():
            if current.as_posix() in root_set:
                break
            try:
                if any(current.iterdir()):
                    break
                current.rmdir()
                current = current.parent
            except Exception:
                break

    @staticmethod
    def _is_path_in_roots(path: Path, roots: List[Path]) -> bool:
        for root in roots:
            try:
                if path == root or path.is_relative_to(root):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _collect_scrape_files(media_path: Path) -> List[Path]:
        if not media_path:
            return []
        media_path = Path(media_path)
        parent = media_path.parent
        if not parent.exists() or not parent.is_dir():
            return []

        allowed_exts = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tbn", ".txt"}
        result = []
        for child in parent.iterdir():
            if not child.is_file():
                continue
            if child.stem == media_path.stem and child.suffix.lower() in allowed_exts:
                result.append(child)

        return result

    def _delete_torrent(self, downloader: str, torrent_hash: str) -> bool:
        if not downloader or not torrent_hash:
            return False
        service = DownloaderHelper().get_service(name=downloader)
        if not service or not service.instance:
            return False
        try:
            return bool(
                service.instance.delete_torrents(
                    delete_file=self._delete_downloader_files,
                    ids=torrent_hash,
                )
            )
        except Exception as err:
            logger.error(f"{self.plugin_name}删除下载器任务失败 {downloader}:{torrent_hash} - {err}")
            return False

    def _delete_transfer_history(self, download_hash: Optional[str], history: Any) -> int:
        if not self._transfer_oper:
            return 0

        records = []
        if download_hash:
            records = self._transfer_oper.list_by_hash(download_hash)
        elif history:
            records = [history]

        removed = 0
        visited = set()
        for record in records:
            rid = getattr(record, "id", None)
            if not rid or rid in visited:
                continue
            visited.add(rid)
            self._transfer_oper.delete(rid)
            removed += 1

        return removed

    def _delete_download_history(self, download_hash: str) -> int:
        if not self._download_oper or not download_hash:
            return 0

        removed = 0

        while True:
            history = self._download_oper.get_by_hash(download_hash)
            if not history:
                break
            self._download_oper.delete_history(history.id)
            removed += 1

        files = self._download_oper.get_files_by_hash(download_hash)
        for fileinfo in files:
            self._download_oper.delete_downloadfile(fileinfo.id)
            removed += 1

        return removed

    @staticmethod
    def _build_step_result(planned: int, done: int) -> dict:
        planned_num = max(0, int(planned or 0))
        done_num = max(0, int(done or 0))
        return {
            "planned": planned_num,
            "done": done_num,
            "failed": max(0, planned_num - done_num),
        }

    @staticmethod
    def _collect_failed_steps(step_result: dict) -> List[str]:
        return [
            step for step, result in (step_result or {}).items()
            if int((result or {}).get("failed", 0) or 0) > 0
        ]

    def _retry_queue_size(self) -> int:
        return len(self.get_data("retry_queue") or [])

    def _enqueue_retry(self, payload: dict):
        if not payload or not payload.get("failed_steps"):
            return
        queue = self.get_data("retry_queue") or []
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        retry_key = payload.get("retry_key") or f"retry:{int(time.time() * 1000)}"
        merged = False
        for item in queue:
            if item.get("retry_key") != retry_key:
                continue
            item_payload = item.get("payload") or {}
            old_steps = set(item_payload.get("failed_steps") or [])
            new_steps = set(payload.get("failed_steps") or [])
            item_payload.update(payload)
            item_payload["failed_steps"] = sorted(old_steps | new_steps)
            item["payload"] = item_payload
            item["next_run_at"] = now_text
            merged = True
            break
        if not merged:
            queue.append({
                "id": f"{int(time.time() * 1000)}-{len(queue) + 1}",
                "retry_key": retry_key,
                "attempt": 0,
                "created_at": now_text,
                "next_run_at": now_text,
                "payload": payload,
            })
        self.save_data("retry_queue", queue[-500:])

    def _process_retry_queue(self, limit: int) -> List[dict]:
        queue = self.get_data("retry_queue") or []
        if not queue:
            return []
        now = datetime.now()
        limit = max(1, int(limit or 1))
        processed = 0
        remain_queue = []
        actions: List[dict] = []
        for job in queue:
            next_run_at = self._parse_datetime_text(job.get("next_run_at"))
            if next_run_at and next_run_at > now:
                remain_queue.append(job)
                continue
            if processed >= limit:
                remain_queue.append(job)
                continue
            processed += 1
            action, next_job = self._run_retry_job(job)
            if action:
                actions.append(action)
            if next_job:
                remain_queue.append(next_job)
        self.save_data("retry_queue", remain_queue[-500:])
        return actions

    def _run_retry_job(self, job: dict) -> Tuple[Optional[dict], Optional[dict]]:
        payload = job.get("payload") or {}
        mode = payload.get("mode")
        attempt = int(job.get("attempt", 0) or 0) + 1
        if mode == "media":
            result = self._retry_media_payload(payload)
        elif mode == "torrent":
            result = self._retry_torrent_payload(payload)
        else:
            return None, None

        failed_steps = result.get("failed_steps") or []
        ok = len(failed_steps) == 0
        action = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": f"失败补偿:{payload.get('trigger') or '-'}",
            "target": payload.get("target") or "-",
            "action": f"重试{'成功' if ok else '失败'}(第{attempt}次)；失败步骤：{','.join(failed_steps) if failed_steps else '无'}",
            "freed_bytes": int(result.get("freed_bytes", 0) or 0),
            "mode": "retry",
            "steps": result.get("steps") or {},
            "refresh_items": result.get("refresh_items") or [],
        }
        if ok:
            return action, None

        if attempt >= self._retry_max_attempts:
            dead = self.get_data("retry_deadletter") or []
            dead.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "job": job,
                "last_failed_steps": failed_steps,
            })
            self.save_data("retry_deadletter", dead[-200:])
            return action, None

        retry_job = dict(job)
        retry_payload = dict(payload)
        retry_payload["failed_steps"] = failed_steps
        retry_job["payload"] = retry_payload
        retry_job["attempt"] = attempt
        next_run = datetime.now() + timedelta(minutes=max(1, self._retry_interval_minutes) * attempt)
        retry_job["next_run_at"] = next_run.strftime("%Y-%m-%d %H:%M:%S")
        return action, retry_job

    def _retry_media_payload(self, payload: dict) -> dict:
        failed = set(payload.get("failed_steps") or [])
        media_path = Path(payload.get("media_path")) if payload.get("media_path") else None
        sidecars = [Path(item) for item in (payload.get("sidecars") or []) if item]
        download_hash = payload.get("download_hash")
        downloader = payload.get("downloader")
        history_dest = payload.get("history_dest")
        library_roots = self._library_paths()

        freed_bytes = 0
        removed_media = 0
        removed_scrape = 0
        removed_downloader = 0
        removed_transfer = 0
        removed_download = 0

        planned_media = 1 if "media" in failed and media_path else 0
        if planned_media and media_path:
            freed_bytes += self._path_size(media_path)
            if (not media_path.exists() and not media_path.is_symlink()) or self._delete_local_item(media_path, library_roots):
                removed_media = 1

        planned_scrape = len(sidecars) if "scrape" in failed else 0
        if planned_scrape:
            for sidecar in sidecars:
                freed_bytes += self._path_size(sidecar)
                if (not sidecar.exists() and not sidecar.is_symlink()) or self._delete_local_item(sidecar, library_roots):
                    removed_scrape += 1

        planned_downloader = 1 if "downloader" in failed and download_hash and downloader else 0
        if planned_downloader and self._delete_torrent(downloader=downloader, torrent_hash=download_hash):
            removed_downloader = 1

        planned_transfer = 0
        if "transfer_history" in failed:
            history = self._transfer_oper.get_by_dest(history_dest) if history_dest and self._transfer_oper else None
            planned_transfer = self._count_transfer_records(download_hash, history)
            if planned_transfer > 0:
                removed_transfer = self._delete_transfer_history(download_hash=download_hash, history=history)

        planned_download = 0
        if "download_history" in failed and download_hash:
            planned_download = self._count_download_records(download_hash)
            if planned_download > 0:
                removed_download = self._delete_download_history(download_hash)

        steps = {
            "media": self._build_step_result(planned_media, removed_media),
            "scrape": self._build_step_result(planned_scrape, removed_scrape),
            "downloader": self._build_step_result(planned_downloader, removed_downloader),
            "transfer_history": self._build_step_result(planned_transfer, removed_transfer),
            "download_history": self._build_step_result(planned_download, removed_download),
        }
        refresh_items = []
        history = self._transfer_oper.get_by_dest(history_dest) if history_dest and self._transfer_oper else None
        if history:
            refresh_item = self._build_refresh_item(history=history, target_path=history_dest)
            if refresh_item:
                refresh_items.append(refresh_item)
        return {
            "steps": steps,
            "failed_steps": self._collect_failed_steps(steps),
            "freed_bytes": freed_bytes,
            "refresh_items": refresh_items,
        }

    def _retry_torrent_payload(self, payload: dict) -> dict:
        failed = set(payload.get("failed_steps") or [])
        download_hash = payload.get("download_hash")
        downloader = payload.get("downloader")
        media_targets = [Path(item) for item in (payload.get("media_targets") or []) if item]
        sidecar_targets = [Path(item) for item in (payload.get("sidecar_targets") or []) if item]
        history_dests = [item for item in (payload.get("history_dests") or []) if item]
        library_roots = self._library_paths()

        freed_bytes = 0
        removed_media = 0
        removed_scrape = 0
        removed_downloader = 0
        removed_transfer = 0
        removed_download = 0

        planned_media = len(media_targets) if "media" in failed else 0
        if planned_media:
            for path in media_targets:
                freed_bytes += self._path_size(path)
                if (not path.exists() and not path.is_symlink()) or self._delete_local_item(path, library_roots):
                    removed_media += 1

        planned_scrape = len(sidecar_targets) if "scrape" in failed else 0
        if planned_scrape:
            for path in sidecar_targets:
                freed_bytes += self._path_size(path)
                if (not path.exists() and not path.is_symlink()) or self._delete_local_item(path, library_roots):
                    removed_scrape += 1

        planned_downloader = 1 if "downloader" in failed and downloader and download_hash else 0
        if planned_downloader and self._delete_torrent(downloader=downloader, torrent_hash=download_hash):
            removed_downloader = 1

        planned_transfer = 0
        if "transfer_history" in failed:
            planned_transfer = self._count_transfer_records(download_hash, None)
            if planned_transfer > 0:
                removed_transfer = self._delete_transfer_history(download_hash=download_hash, history=None)

        planned_download = 0
        if "download_history" in failed and download_hash:
            planned_download = self._count_download_records(download_hash)
            if planned_download > 0:
                removed_download = self._delete_download_history(download_hash)

        steps = {
            "media": self._build_step_result(planned_media, removed_media),
            "scrape": self._build_step_result(planned_scrape, removed_scrape),
            "downloader": self._build_step_result(planned_downloader, removed_downloader),
            "transfer_history": self._build_step_result(planned_transfer, removed_transfer),
            "download_history": self._build_step_result(planned_download, removed_download),
        }
        refresh_items = []
        for dest in history_dests:
            history = self._transfer_oper.get_by_dest(dest) if self._transfer_oper else None
            if not history:
                continue
            refresh_item = self._build_refresh_item(history=history, target_path=dest)
            if refresh_item:
                refresh_items.append(refresh_item)
        return {
            "steps": steps,
            "failed_steps": self._collect_failed_steps(steps),
            "freed_bytes": freed_bytes,
            "refresh_items": refresh_items,
        }

    @staticmethod
    def _parse_datetime_text(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    def _collect_refresh_items(self, actions: List[dict]) -> List[RefreshMediaItem]:
        if not actions:
            return []

        dedup: Dict[str, RefreshMediaItem] = {}
        for action in actions:
            for item in action.get("refresh_items") or []:
                if isinstance(item, RefreshMediaItem):
                    refresh_item = item
                elif isinstance(item, dict):
                    try:
                        refresh_item = RefreshMediaItem(**item)
                    except Exception:
                        continue
                else:
                    continue
                key_path = Path(refresh_item.target_path) if refresh_item.target_path else None
                group_key = key_path.parent.as_posix() if key_path and key_path.suffix else (key_path.as_posix() if key_path else "")
                key = "|".join([
                    str(refresh_item.title or ""),
                    str(refresh_item.year or ""),
                    str(refresh_item.type or ""),
                    group_key,
                ])
                dedup[key] = refresh_item
        return list(dedup.values())

    def _build_refresh_item(self, history: Any, target_path: Any) -> Optional[dict]:
        if not history:
            return None

        mtype = getattr(history, "type", None)
        title = getattr(history, "title", None)
        year = getattr(history, "year", None)
        category = getattr(history, "category", None)
        dest = target_path or getattr(history, "dest", None)
        if not mtype or not title or not dest:
            return None
        try:
            item = RefreshMediaItem(
                title=title,
                year=year,
                type=mtype,
                category=category,
                target_path=Path(dest),
            )
            return item.model_dump()
        except Exception:
            return None

    def _refresh_media_servers(self, refresh_items: Optional[List[RefreshMediaItem]] = None) -> int:
        services = MediaServerHelper().get_services(
            name_filters=self._refresh_servers if self._refresh_servers else None
        )
        refreshed = 0
        refresh_items = refresh_items or []
        for server_name, service in services.items():
            instance = service.instance
            if not instance or not hasattr(instance, "refresh_root_library"):
                continue
            try:
                state = None
                if self._refresh_mode == "item" and refresh_items and hasattr(instance, "refresh_library_by_items"):
                    batch_failed = False
                    for i in range(0, len(refresh_items), max(1, self._refresh_batch_size)):
                        batch = refresh_items[i:i + max(1, self._refresh_batch_size)]
                        batch_state = instance.refresh_library_by_items(batch)
                        if batch_state is False:
                            batch_failed = True
                            logger.warning(f"{self.plugin_name}定向刷新失败，准备回退整库刷新：{server_name}")
                            break
                    if batch_failed and hasattr(instance, "refresh_root_library"):
                        state = instance.refresh_root_library()
                    else:
                        state = True
                else:
                    state = instance.refresh_root_library()
                if state is not False:
                    refreshed += 1
                    logger.info(f"{self.plugin_name}已刷新媒体服务器：{server_name}")
            except Exception as err:
                logger.error(f"{self.plugin_name}刷新媒体服务器失败 {server_name}：{err}")
        return refreshed

    @staticmethod
    def _torrent_hash(torrent: Any) -> Optional[str]:
        if hasattr(torrent, "get"):
            return torrent.get("hash")
        return getattr(torrent, "hashString", None)

    @staticmethod
    def _torrent_name(torrent: Any) -> str:
        if hasattr(torrent, "get"):
            return str(torrent.get("name") or "")
        return str(getattr(torrent, "name", ""))

    @staticmethod
    def _torrent_seed_seconds(torrent: Any) -> int:
        now = int(time.time())

        if hasattr(torrent, "get"):
            completed = torrent.get("completion_on") or torrent.get("added_on") or 0
            try:
                completed = int(completed)
            except Exception:
                completed = 0
            if completed <= 0:
                return 0
            return max(0, now - completed)

        date_done = getattr(torrent, "date_done", None)
        if date_done and hasattr(date_done, "timestamp"):
            return max(0, now - int(date_done.timestamp()))

        done_date = getattr(torrent, "doneDate", None)
        if isinstance(done_date, (int, float)) and done_date > 0:
            return max(0, now - int(done_date))

        added_date = getattr(torrent, "addedDate", None)
        if isinstance(added_date, (int, float)) and added_date > 0:
            return max(0, now - int(added_date))

        return 0

    @staticmethod
    def _format_size(size: float) -> str:
        if not size:
            return "0 B"

        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.2f} {unit}"
            value /= 1024

        return f"{value:.2f} PB"

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _build_usage_card(self, title: str, usage: dict, enabled: bool, mode: str, value: float) -> dict:
        paths = usage.get("paths") or []
        trigger = self._is_threshold_hit(usage, mode, value) if enabled else False
        status_text = "已触发清理" if trigger else "正常"
        if not enabled:
            status_text = "未启用监听"

        return {
            "component": "VCard",
            "content": [
                {"component": "VCardText", "text": f"{title}（{status_text}）"},
                {"component": "VCardText", "text": f"监控目录数量：{len(paths)}"},
                {"component": "VCardText", "text": f"总空间：{usage.get('total_text', '0 B')}"},
                {"component": "VCardText", "text": f"已使用：{usage.get('used_text', '0 B')} ({usage.get('used_percent', 0)}%)"},
                {"component": "VCardText", "text": f"剩余：{usage.get('free_text', '0 B')} ({usage.get('free_percent', 0)}%)"},
                {"component": "VCardText", "text": f"阈值：{self._threshold_text(mode, value)}"},
            ],
        }

    def _has_mp_history(self, download_hash: str) -> bool:
        if not download_hash:
            return False
        if self._transfer_oper and self._transfer_oper.list_by_hash(download_hash):
            return True
        if self._download_oper and self._download_oper.get_by_hash(download_hash):
            return True
        return False

    def _count_transfer_records(self, download_hash: Optional[str], history: Any) -> int:
        if not self._transfer_oper:
            return 0
        if download_hash:
            return len(self._transfer_oper.list_by_hash(download_hash) or [])
        if history:
            return 1
        return 0

    def _count_download_records(self, download_hash: Optional[str]) -> int:
        if not self._download_oper or not download_hash:
            return 0
        count = 0
        if self._download_oper.get_by_hash(download_hash):
            count += 1
        count += len(self._download_oper.get_files_by_hash(download_hash) or [])
        return count

    @staticmethod
    def _path_size(path: Path) -> int:
        try:
            if path and path.exists() and path.is_file():
                return int(path.stat().st_size)
            if path and path.exists() and path.is_dir():
                return int(SystemUtils.get_directory_size(path))
        except Exception:
            return 0
        return 0

    def _is_history_recent(self, history: Any) -> bool:
        if not history or self._protect_recent_days <= 0:
            return False
        date_text = getattr(history, "date", None)
        if not date_text:
            return False
        try:
            history_dt = datetime.strptime(date_text, "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - history_dt).days < self._protect_recent_days
        except Exception:
            return False

    def _is_in_cooldown(self) -> bool:
        if self._cooldown_minutes <= 0:
            return False
        last_run = self.get_data("last_run_at")
        if not last_run:
            return False
        try:
            last_dt = datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return False
        return (datetime.now() - last_dt).total_seconds() < self._cooldown_minutes * 60

    def _get_daily_freed_bytes(self) -> int:
        data = self.get_data("daily_freed") or {}
        today = datetime.now().strftime("%Y-%m-%d")
        if data.get("date") != today:
            return 0
        return int(data.get("bytes", 0) or 0)

    def _update_daily_freed_bytes(self, add_bytes: int):
        if add_bytes <= 0:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        current = self._get_daily_freed_bytes()
        self.save_data("daily_freed", {"date": today, "bytes": current + int(add_bytes)})

    def _is_daily_limit_reached(self) -> bool:
        if self._max_gb_per_day <= 0:
            return False
        limit_bytes = int(self._max_gb_per_day * (1024 ** 3))
        return self._get_daily_freed_bytes() >= limit_bytes

    def _is_run_bytes_limit_reached(self, next_bytes: int) -> bool:
        if self._max_gb_per_run <= 0:
            return False
        limit_bytes = int(self._max_gb_per_run * (1024 ** 3))
        return int(next_bytes) > limit_bytes

    def _is_daily_bytes_limit_reached(self, next_bytes: int) -> bool:
        if self._max_gb_per_day <= 0:
            return False
        limit_bytes = int(self._max_gb_per_day * (1024 ** 3))
        return self._get_daily_freed_bytes() + int(self._current_run_freed_bytes) + int(next_bytes) > limit_bytes

    def _is_run_limit_reached(self, actions: List[dict]) -> bool:
        if self._max_delete_items > 0 and len(actions) >= self._max_delete_items:
            return True
        if self._max_gb_per_run > 0:
            return int(self._current_run_freed_bytes) >= int(self._max_gb_per_run * (1024 ** 3))
        return False

    def _effective_allow_roots(self, fallback_roots: List[Path]) -> List[Path]:
        allow = self._allow_paths()
        return allow if allow else fallback_roots

    def _allow_paths(self) -> List[Path]:
        return self._normalize_paths(self._path_allowlist)

    def _block_paths(self) -> List[Path]:
        return self._normalize_paths(self._path_blocklist)

    @staticmethod
    def _normalize_paths(raw_paths: List[str]) -> List[Path]:
        unique: Dict[str, Path] = {}
        for raw in raw_paths or []:
            try:
                path = Path(raw).expanduser()
                unique[path.as_posix()] = path
            except Exception:
                continue
        return list(unique.values())

    def _parse_path_list(self, raw: Any) -> List[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw]
        else:
            text = str(raw).replace(",", "\n")
            values = [line.strip() for line in text.splitlines()]
        return [item for item in values if item]

    def _normalize_config(self):
        if self._download_threshold_mode not in ("size", "percent"):
            self._download_threshold_mode = "size"
        if self._library_threshold_mode not in ("size", "percent"):
            self._library_threshold_mode = "size"
        if self._trigger_logic not in ("or", "and"):
            self._trigger_logic = "or"

        self._download_threshold_value = max(0.0, self._safe_float(self._download_threshold_value, 100.0))
        self._library_threshold_value = max(0.0, self._safe_float(self._library_threshold_value, 100.0))
        if self._download_threshold_mode == "percent":
            self._download_threshold_value = min(100.0, self._download_threshold_value)
        if self._library_threshold_mode == "percent":
            self._library_threshold_value = min(100.0, self._library_threshold_value)

        self._cooldown_minutes = max(0, int(self._cooldown_minutes))
        self._seeding_days = max(0, int(self._seeding_days))
        self._max_delete_items = max(1, min(50, int(self._max_delete_items)))
        self._strategy_quota = max(1, min(50, int(self._strategy_quota)))
        self._max_gb_per_run = max(0.0, self._safe_float(self._max_gb_per_run, 50.0))
        self._max_gb_per_day = max(0.0, self._safe_float(self._max_gb_per_day, 200.0))
        self._protect_recent_days = max(0, int(self._protect_recent_days))
        self._library_probe_limit = max(1, min(200, int(self._library_probe_limit)))
        self._refresh_mode = "item" if self._refresh_mode not in ("item", "root") else self._refresh_mode
        self._refresh_batch_size = max(1, min(200, int(self._refresh_batch_size)))
        self._retry_max_attempts = max(1, min(20, int(self._retry_max_attempts)))
        self._retry_interval_minutes = max(1, min(1440, int(self._retry_interval_minutes)))
        self._retry_batch_size = max(1, min(50, int(self._retry_batch_size)))
        self._downloaders = [str(item).strip() for item in (self._downloaders or []) if str(item).strip()]
        self._refresh_servers = [str(item).strip() for item in (self._refresh_servers or []) if str(item).strip()]
        self._path_allowlist = self._parse_path_list(self._path_allowlist)
        self._path_blocklist = self._parse_path_list(self._path_blocklist)

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "dry_run": self._dry_run,
                "cron": self._cron,
                "cooldown_minutes": self._cooldown_minutes,
                "monitor_download": self._monitor_download,
                "monitor_library": self._monitor_library,
                "monitor_downloader": self._monitor_downloader,
                "trigger_logic": self._trigger_logic,
                "strategy_quota": self._strategy_quota,
                "download_threshold_mode": self._download_threshold_mode,
                "download_threshold_value": self._download_threshold_value,
                "library_threshold_mode": self._library_threshold_mode,
                "library_threshold_value": self._library_threshold_value,
                "downloaders": self._downloaders,
                "seeding_days": self._seeding_days,
                "max_delete_items": self._max_delete_items,
                "max_gb_per_run": self._max_gb_per_run,
                "max_gb_per_day": self._max_gb_per_day,
                "protect_recent_days": self._protect_recent_days,
                "prefer_playback_history": self._prefer_playback_history,
                "library_probe_limit": self._library_probe_limit,
                "clean_media_data": self._clean_media_data,
                "clean_scrape_data": self._clean_scrape_data,
                "clean_downloader_seed": self._clean_downloader_seed,
                "delete_downloader_files": self._delete_downloader_files,
                "clean_transfer_history": self._clean_transfer_history,
                "clean_download_history": self._clean_download_history,
                "mp_only": self._mp_only,
                "path_allowlist": "\n".join(self._path_allowlist),
                "path_blocklist": "\n".join(self._path_blocklist),
                "refresh_mediaserver": self._refresh_mediaserver,
                "refresh_mode": self._refresh_mode,
                "refresh_batch_size": self._refresh_batch_size,
                "refresh_servers": self._refresh_servers,
                "enable_retry_queue": self._enable_retry_queue,
                "retry_max_attempts": self._retry_max_attempts,
                "retry_interval_minutes": self._retry_interval_minutes,
                "retry_batch_size": self._retry_batch_size,
            }
        )
