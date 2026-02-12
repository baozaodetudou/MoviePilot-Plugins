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
from app.db.models.transferhistory import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.downloader import DownloaderHelper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, RefreshMediaItem
from app.schemas.types import MediaType
from app.utils.system import SystemUtils


class DiskCleaner(_PluginBase):
    # 插件信息
    plugin_name = "磁盘清理"
    plugin_desc = "按磁盘阈值与做种时长自动清理媒体、做种与MP整理记录"
    plugin_icon = "https://raw.githubusercontent.com/baozaodetudou/MoviePilot-Plugins/refs/heads/main/icons/diskclean.png"
    plugin_version = "0.15"
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
    _clear_history = False
    _notify = False
    _cron = "0 */8 * * *"
    _dry_run = True
    _cooldown_minutes = 60

    # 监听开关
    _monitor_download = True
    _monitor_library = True
    _monitor_downloader = False
    _trigger_flow = "flow_library_mp_downloader"

    # 阈值配置
    _download_threshold_mode = "size"  # size/percent
    _download_threshold_value = 100.0
    _library_threshold_mode = "size"
    _library_threshold_value = 100.0

    # 下载器策略
    _downloaders: List[str] = []
    _seeding_days = 15
    _media_flow_seed_check = True
    _max_delete_items = 5
    _max_gb_per_run = 50.0
    _max_gb_per_day = 200.0
    _protect_recent_days = 30
    _media_cleanup_priority = "movie"
    _tv_complete_only = True

    # 删除开关
    _clean_media_data = True
    _clean_scrape_data = True
    _clean_downloader_seed = False
    _delete_downloader_files = False
    _force_hardlink_cleanup = False
    _clean_transfer_history = True
    _clean_download_history = True
    _path_allowlist: List[str] = []
    _path_blocklist: List[str] = []
    _media_path_mapping: List[str] = []
    _media_path_rules: List[Tuple[str, str]] = []
    _current_run_freed_bytes = 0

    # 媒体库范围与刷新
    _refresh_mediaserver = False
    _refresh_mode = "item"  # item/root
    _refresh_batch_size = 20
    _media_servers: List[str] = []
    _media_libraries: List[str] = []
    _library_scope_cache: Optional[List[Path]] = None
    _prefer_playback_history = True
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
    _tv_end_state_cache: Dict[str, bool] = {}

    def init_plugin(self, config: dict = None):
        self.stop_service()

        self._transfer_oper = TransferHistoryOper()
        self._download_oper = DownloadHistoryOper()
        self._mediaserver_oper = MediaServerOper()
        self._mediaserver_chain = None
        self._playback_ts_cache = {}
        self._tv_end_state_cache = {}
        self._library_scope_cache = None

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._clear_history = bool(config.get("clear_history", False))
            self._notify = bool(config.get("notify", False))
            self._cron = config.get("cron") or "0 */8 * * *"
            self._dry_run = bool(config.get("dry_run", True))
            self._cooldown_minutes = int(self._safe_float(config.get("cooldown_minutes"), 60))

            self._monitor_download = bool(config.get("monitor_download", True))
            self._monitor_library = bool(config.get("monitor_library", True))
            self._monitor_downloader = bool(config.get("monitor_downloader", False))
            self._trigger_flow = config.get("trigger_flow") or "flow_library_mp_downloader"

            self._download_threshold_mode = config.get("download_threshold_mode") or "size"
            self._download_threshold_value = self._parse_threshold_value(
                config.get("download_threshold_value"),
                self._download_threshold_mode,
            )
            self._library_threshold_mode = config.get("library_threshold_mode") or "size"
            self._library_threshold_value = self._parse_threshold_value(
                config.get("library_threshold_value"),
                self._library_threshold_mode,
            )

            self._downloaders = config.get("downloaders") or []
            self._seeding_days = int(self._safe_float(config.get("seeding_days"), 15))
            self._media_flow_seed_check = bool(config.get("media_flow_seed_check", True))
            self._max_delete_items = max(1, int(self._safe_float(config.get("max_delete_items"), 5)))
            self._max_gb_per_run = self._safe_float(config.get("max_gb_per_run"), 50.0)
            self._max_gb_per_day = self._safe_float(config.get("max_gb_per_day"), 200.0)
            self._protect_recent_days = int(self._safe_float(config.get("protect_recent_days"), 30))
            self._media_cleanup_priority = self._normalize_media_priority(config.get("media_cleanup_priority", "movie"))
            self._tv_complete_only = bool(config.get("tv_complete_only", True))

            self._clean_media_data = bool(config.get("clean_media_data", True))
            self._clean_scrape_data = bool(config.get("clean_scrape_data", True))
            self._clean_downloader_seed = bool(config.get("clean_downloader_seed", False))
            self._delete_downloader_files = bool(config.get("delete_downloader_files", False))
            self._force_hardlink_cleanup = bool(config.get("force_hardlink_cleanup", False))
            self._clean_transfer_history = bool(config.get("clean_transfer_history", True))
            self._clean_download_history = bool(config.get("clean_download_history", True))
            self._path_allowlist = self._parse_path_list(config.get("path_allowlist"))
            self._path_blocklist = self._parse_path_list(config.get("path_blocklist"))
            self._media_path_mapping = self._parse_path_list(config.get("media_path_mapping"))

            self._refresh_mediaserver = bool(config.get("refresh_mediaserver", False))
            self._refresh_mode = config.get("refresh_mode") or "item"
            self._refresh_batch_size = int(self._safe_float(config.get("refresh_batch_size"), 20))
            self._media_servers = config.get("media_servers") or config.get("refresh_servers") or []
            self._media_libraries = config.get("media_libraries") or []
            self._prefer_playback_history = bool(config.get("prefer_playback_history", True))
            self._enable_retry_queue = bool(config.get("enable_retry_queue", True))
            self._retry_max_attempts = int(self._safe_float(config.get("retry_max_attempts"), 3))
            self._retry_interval_minutes = int(self._safe_float(config.get("retry_interval_minutes"), 30))
            self._retry_batch_size = int(self._safe_float(config.get("retry_batch_size"), 5))

        self._normalize_config()
        if config:
            normalized_download_text = self._format_threshold_value(
                self._download_threshold_value,
                self._download_threshold_mode,
            )
            normalized_library_text = self._format_threshold_value(
                self._library_threshold_value,
                self._library_threshold_mode,
            )
            if (
                str(config.get("download_threshold_value", "") or "").strip() != normalized_download_text
                or str(config.get("library_threshold_value", "") or "").strip() != normalized_library_text
                or "media_flow_seed_check" not in config
                or "force_hardlink_cleanup" not in config
            ):
                self.__update_config()

        if self._clear_history:
            for key in ["history", "run_history", "last_run_at", "daily_freed", "latest_usage", "retry_deadletter"]:
                self.del_data(key=key)
            self._clear_history = False
            logger.info(f"{self.plugin_name}已清空历史数据")
            self.__update_config()

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
        return [
            {
                "path": "/ack_risk_notice",
                "endpoint": self.ack_risk_notice,
                "methods": ["GET"],
                "summary": "确认风险提示已阅读",
                "description": "用户首次阅读风险提示后写入确认标记",
            }
        ]

    def ack_risk_notice(self):
        self.save_data("risk_notice_acked", True)
        return {"success": True}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        downloader_items = [
            {"title": conf.name, "value": conf.name}
            for conf in DownloaderHelper().get_configs().values()
        ]
        media_items = [
            {"title": conf.name, "value": conf.name}
            for conf in MediaServerHelper().get_configs().values() if conf.name
        ]
        media_library_items = self._media_library_items(server_filters=self._effective_media_servers())
        risk_notice_acked = bool(self.get_data("risk_notice_acked"))
        risk_notice_shown_once = bool(self.get_data("risk_notice_shown_once"))
        show_risk_notice = not risk_notice_acked and not risk_notice_shown_once
        if show_risk_notice:
            # 兜底：首次进入配置页后不再重复弹窗，避免前端事件异常导致反复提示
            self.save_data("risk_notice_shown_once", True)
        risk_notice_ack_api = f"/api/v1/plugin/{self.__class__.__name__}/ack_risk_notice?apikey={settings.API_TOKEN}"
        delete_module = [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "clean_media_data", "label": "删除媒体数据"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "clean_scrape_data", "label": "删除刮削数据"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "clean_transfer_history", "label": "删除整理记录"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "clean_download_history", "label": "删除下载记录"}}]},
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{"component": "VTextarea", "props": {"density": "compact", "hideDetails": True, "model": "path_allowlist", "label": "删除白名单路径", "rows": 2, "placeholder": "一行一个路径；留空默认使用MP目录"}}],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{"component": "VTextarea", "props": {"density": "compact", "hideDetails": True, "model": "path_blocklist", "label": "删除黑名单路径", "rows": 2, "placeholder": "一行一个路径；命中后永不删除"}}],
                    },
                ],
            },
        ]

        media_module = [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "monitor_library", "label": "启用媒体库空间告警"}}]},
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [{"component": "VSelect", "props": {"density": "compact", "hideDetails": True, "model": "library_threshold_mode", "label": "媒体库告警方式", "items": [{"title": "按剩余容量（如 100G）", "value": "size"}, {"title": "按剩余比例（如 10%）", "value": "percent"}]}}],
                    },
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "library_threshold_value", "label": "媒体库告警阈值", "placeholder": "支持 100G 或 10%"}}]},
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [{"component": "VSelect", "props": {"density": "compact", "hideDetails": True, "chips": True, "multiple": True, "clearable": True, "model": "media_servers", "label": "媒体服务器(多选)", "items": media_items}}],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [{"component": "VSelect", "props": {"density": "compact", "hideDetails": True, "chips": True, "multiple": True, "clearable": True, "model": "media_libraries", "label": "媒体库(多选)", "items": media_library_items}}],
                    },
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "protect_recent_days", "label": "近期入库保护(天)"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "prefer_playback_history", "label": "优先按播放历史判定老化"}}]},
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VSelect",
                                "props": {
                                    "density": "compact",
                                    "hideDetails": True,
                                    "model": "media_cleanup_priority",
                                    "label": "优先删除类型",
                                    "items": [
                                        {"title": "电影（默认）", "value": "movie"},
                                        {"title": "电视剧", "value": "tv"},
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
                                "component": "VSwitch",
                                "props": {
                                    "density": "compact",
                                    "model": "tv_complete_only",
                                    "label": "仅清理已完结电视剧",
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
                                    "density": "compact",
                                    "hideDetails": True,
                                    "model": "media_path_mapping",
                                    "label": "媒体路径映射(媒体服务器路径:MoviePilot路径)",
                                    "rows": 2,
                                    "placeholder": "一行一个映射，如 /data:/mnt/link；路径一致可留空",
                                },
                            }
                        ],
                    },
                ],
            },
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "density": "compact", "class": "mt-2"},
                "content": [
                    {"component": "div", "text": "阈值填写示例：按容量填 100G（默认）；按比例填 10%（建议）"},
                    {"component": "div", "text": "路径映射示例：Emby /data/A.mp4，MP /mnt/link/A.mp4"},
                    {"component": "div", "text": "映射填写：/data:/mnt/link；配置错误会导致无法命中MP整理记录"},
                    {"component": "div", "text": "媒体服务器与媒体库留空代表不过滤；选中后仅在选中范围内监听、删除和刷新"},
                    {"component": "div", "text": "启用“仅清理已完结电视剧”后，仅清理 TMDB 状态为完结/取消的电视剧"},
                ],
            },
        ]

        downloader_module = [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "clean_downloader_seed", "label": "删除做种"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "delete_downloader_files", "label": "同步删除文件"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "media_flow_seed_check", "label": "媒体流程删种校验做种时长"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "monitor_download", "label": "启用下载目录空间告警"}}]},
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [{"component": "VSelect", "props": {"density": "compact", "hideDetails": True, "model": "download_threshold_mode", "label": "下载目录告警方式", "items": [{"title": "按剩余容量（如 100G）", "value": "size"}, {"title": "按剩余比例（如 10%）", "value": "percent"}]}}],
                    },
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "download_threshold_value", "label": "下载目录告警阈值", "placeholder": "支持 100G 或 10%"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "monitor_downloader", "label": "监听下载器做种时长(独立触发)"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "seeding_days", "label": "做种时长阈值(天，按完成时间计算)"}}]},
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [{"component": "VSelect", "props": {"density": "compact", "hideDetails": True, "chips": True, "multiple": True, "clearable": True, "model": "downloaders", "label": "下载器(多选)", "items": downloader_items}}],
                    },
                ],
            },
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "density": "compact", "class": "mt-2"},
                "content": [
                    {"component": "div", "text": "阈值填写示例：按容量填 100G（默认）；按比例填 10%（建议）"},
                    {"component": "div", "text": "做种时长按完成时间计算；仅在“监听下载器做种时长”开启时生效"},
                ],
            },
        ]

        post_module = [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "refresh_mediaserver", "label": "清理后刷新媒体库"}}]},
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [{"component": "VSelect", "props": {"density": "compact", "hideDetails": True, "model": "refresh_mode", "label": "媒体库刷新方式", "items": [{"title": "定向刷新(优先)", "value": "item"}, {"title": "整库刷新", "value": "root"}]}}],
                    },
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "refresh_batch_size", "label": "刷新批次大小"}}]},
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact", "text": "刷新范围复用“媒体库设置”中的媒体服务器与媒体库选择"}}],
                    },
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "model": "enable_retry_queue", "label": "启用失败补偿重试"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "retry_max_attempts", "label": "最大重试次数"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "retry_interval_minutes", "label": "重试间隔(分钟)"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "retry_batch_size", "label": "单轮补偿处理数"}}]},
                ],
            },
        ]

        warning_prefix = []
        if show_risk_notice:
            warning_prefix = [
                {
                    "component": "div",
                    "props": {
                        "onVnodeMounted": (
                            "function(){ "
                            "risk_notice_dialog = true; "
                            "risk_notice_countdown = 5; "
                            "if (window.__diskCleanerRiskTimer) { clearInterval(window.__diskCleanerRiskTimer); } "
                            "window.__diskCleanerRiskTimer = setInterval(function(){ "
                            "if ((risk_notice_countdown || 0) > 0) { risk_notice_countdown = (risk_notice_countdown || 0) - 1; } "
                            "if ((risk_notice_countdown || 0) <= 0) { "
                            "clearInterval(window.__diskCleanerRiskTimer); "
                            "window.__diskCleanerRiskTimer = null; "
                            "} "
                            "}, 1000); "
                            "}"
                        ),
                        "onVnodeBeforeUnmount": (
                            "function(){ "
                            "if (window.__diskCleanerRiskTimer) { "
                            "clearInterval(window.__diskCleanerRiskTimer); "
                            "window.__diskCleanerRiskTimer = null; "
                            "} "
                            "}"
                        ),
                    },
                },
                {
                    "component": "VDialog",
                    "props": {
                        "model": "risk_notice_dialog",
                        "persistent": True,
                        "max-width": "52rem",
                        "scrollable": True,
                    },
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "elevated"},
                            "content": [
                                {"component": "VCardTitle", "text": "使用警告（请先阅读 5 秒）"},
                                {"component": "VDivider"},
                                {
                                    "component": "VCardText",
                                    "content": [
                                        {
                                            "component": "VAlert",
                                            "props": {
                                                "type": "warning",
                                                "variant": "tonal",
                                                "density": "compact",
                                                "text": "该插件为个人自用插件。自用场景：下载后硬链接整理到本地，再同步上传 115；目的是清理本地无效媒体信息。当前版本未做完整测试，建议暂时不要在生产库启用。",
                                            },
                                        },
                                        {"component": "div", "props": {"class": "text-caption mt-2"}, "text": "倒计时结束后才可点击“我已阅读，继续配置”"},
                                        {
                                            "component": "VProgressLinear",
                                            "props": {
                                                "class": "mt-2",
                                                "modelValue": "{{ (5 - (risk_notice_countdown || 0)) * 20 }}",
                                                "height": 8,
                                                "rounded": True,
                                                "striped": True,
                                                "stream": True,
                                                "color": "warning",
                                            },
                                        },
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "class": "mt-2",
                                                "density": "compact",
                                                "hideDetails": True,
                                                "readonly": True,
                                                "model": "risk_notice_countdown",
                                                "label": "剩余阅读时间（秒）",
                                            },
                                        },
                                    ],
                                },
                                {"component": "VDivider"},
                                {
                                    "component": "VCardActions",
                                    "content": [
                                        {"component": "VSpacer"},
                                        {
                                            "component": "VBtn",
                                            "props": {
                                                "color": "warning",
                                                "variant": "flat",
                                                "disabled": "{{ (risk_notice_countdown || 0) > 0 }}",
                                                "onClick": (
                                                    "function(){ "
                                                    "if ((risk_notice_countdown || 0) <= 0) { "
                                                    f"fetch('{risk_notice_ack_api}').catch(function(){{}}); "
                                                    "risk_notice_dialog = false; "
                                                    "} "
                                                    "}"
                                                ),
                                            },
                                            "text": "我已阅读，继续配置",
                                        },
                                    ],
                                },
                            ],
                        }
                    ],
                },
            ]

        return warning_prefix + [
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {"component": "VCardTitle", "text": "基础设置"},
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "onVnodeMounted": (
                                        "function(){ "
                                        "if (window.__diskCleanerThresholdWatcher) { clearInterval(window.__diskCleanerThresholdWatcher); } "
                                        "window.__diskCleanerPrevLibraryMode = (library_threshold_mode || 'size'); "
                                        "window.__diskCleanerPrevDownloadMode = (download_threshold_mode || 'size'); "
                                        "window.__diskCleanerThresholdWatcher = setInterval(function(){ "
                                        "var lm = (library_threshold_mode || 'size'); "
                                        "var dm = (download_threshold_mode || 'size'); "
                                        "if (lm !== window.__diskCleanerPrevLibraryMode) { "
                                        "library_threshold_value = (lm === 'percent') ? '10%' : '100G'; "
                                        "window.__diskCleanerPrevLibraryMode = lm; "
                                        "} "
                                        "if (dm !== window.__diskCleanerPrevDownloadMode) { "
                                        "download_threshold_value = (dm === 'percent') ? '10%' : '100G'; "
                                        "window.__diskCleanerPrevDownloadMode = dm; "
                                        "} "
                                        "}, 300); "
                                        "}"
                                    ),
                                    "onVnodeBeforeUnmount": (
                                        "function(){ "
                                        "if (window.__diskCleanerThresholdWatcher) { "
                                        "clearInterval(window.__diskCleanerThresholdWatcher); "
                                        "window.__diskCleanerThresholdWatcher = null; "
                                        "} "
                                        "}"
                                    ),
                                },
                            },
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "density": "compact",
                                    "text": "先选触发流程（单选），再配置各模块参数。删除按所选链路串行执行。",
                                    "class": "mb-2",
                                },
                            },
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12},
                                        "content": [
                                            {
                                                "component": "VSelect",
                                                "props": {
                                                    "density": "compact",
                                                    "hideDetails": True,
                                                    "model": "trigger_flow",
                                                    "label": "触发流程（单选｜决定删除链路）",
                                                    "items": [
                                                        {
                                                            "title": "1. 媒体优先（推荐）｜媒体目录阈值 -> 优先联动MP/下载器（缺失时仅删本地）",
                                                            "value": "flow_library_mp_downloader",
                                                        },
                                                        {
                                                            "title": "2. 下载器优先｜下载器阈值/做种时长 -> MP整理记录 -> 媒体数据",
                                                            "value": "flow_downloader_mp_library",
                                                        },
                                                        {
                                                            "title": "3. 整理记录优先｜MP整理记录（旧到新） -> 媒体数据 + 下载器做种",
                                                            "value": "flow_transfer_oldest",
                                                        },
                                                    ],
                                                },
                                            }
                                        ],
                                    },
                                ],
                            },
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "density": "compact",
                                    "text": "建议：常规场景选“媒体优先”；做种占用高选“下载器优先”；需要按历史最旧顺序清理选“整理记录优先”。",
                                    "class": "mt-2",
                                },
                            },
                            {
                                "component": "VRow",
                                "content": [
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "class": "text-no-wrap", "model": "enabled", "label": "启用"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "class": "text-no-wrap", "model": "notify", "label": "通知"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "class": "text-no-wrap", "model": "dry_run", "label": "演练"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "class": "text-no-wrap", "model": "onlyonce", "label": "立即运行"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "class": "text-no-wrap", "model": "clear_history", "label": "清空历史"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"density": "compact", "class": "text-no-wrap", "model": "force_hardlink_cleanup", "label": "硬链接强制删除(兜底)"}}]},
                                ],
                            },
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 4},
                                        "content": [{"component": "VCronField", "props": {"density": "compact", "hideDetails": True, "model": "cron", "label": "执行周期", "placeholder": "0 */8 * * *"}}],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 4},
                                        "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "cooldown_minutes", "label": "冷却时间(分钟)"}}],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 4},
                                        "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "max_delete_items", "label": "单轮最多处理条目"}}],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 6},
                                        "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "max_gb_per_run", "label": "单轮最多释放(GB)"}}],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 6},
                                        "content": [{"component": "VTextField", "props": {"density": "compact", "hideDetails": True, "model": "max_gb_per_day", "label": "每日最多释放(GB)"}}],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VTabs",
                        "props": {"model": "tab", "grow": True, "color": "primary"},
                        "content": [
                            {"component": "VTab", "props": {"value": "tab-delete"}, "text": "删除设置"},
                            {"component": "VTab", "props": {"value": "tab-media"}, "text": "媒体库设置"},
                            {"component": "VTab", "props": {"value": "tab-downloader"}, "text": "下载器设置"},
                            {"component": "VTab", "props": {"value": "tab-post"}, "text": "删除后处理"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VWindow",
                        "props": {"model": "tab"},
                        "content": [
                            {"component": "VWindowItem", "props": {"value": "tab-delete"}, "content": [{"component": "VCardText", "content": delete_module}]},
                            {"component": "VWindowItem", "props": {"value": "tab-media"}, "content": [{"component": "VCardText", "content": media_module}]},
                            {"component": "VWindowItem", "props": {"value": "tab-downloader"}, "content": [{"component": "VCardText", "content": downloader_module}]},
                            {"component": "VWindowItem", "props": {"value": "tab-post"}, "content": [{"component": "VCardText", "content": post_module}]},
                        ],
                    },
                ],
            },
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "clear_history": False,
            "dry_run": True,
            "force_hardlink_cleanup": False,
            "tab": "tab-delete",
            "cron": "0 */8 * * *",
            "cooldown_minutes": 60,
            "trigger_flow": "flow_library_mp_downloader",
            "monitor_download": True,
            "monitor_library": True,
            "monitor_downloader": False,
            "download_threshold_mode": "size",
            "download_threshold_value": "100G",
            "library_threshold_mode": "size",
            "library_threshold_value": "100G",
            "media_servers": [],
            "media_libraries": [],
            "downloaders": [],
            "seeding_days": 15,
            "media_flow_seed_check": True,
            "max_delete_items": 5,
            "max_gb_per_run": 50,
            "max_gb_per_day": 200,
            "protect_recent_days": 30,
            "media_cleanup_priority": "movie",
            "tv_complete_only": True,
            "clean_media_data": True,
            "clean_scrape_data": True,
            "clean_downloader_seed": False,
            "delete_downloader_files": False,
            "clean_transfer_history": True,
            "clean_download_history": True,
            "prefer_playback_history": True,
            "media_path_mapping": "",
            "path_allowlist": "",
            "path_blocklist": "",
            "refresh_mediaserver": False,
            "refresh_mode": "item",
            "refresh_batch_size": 20,
            "enable_retry_queue": True,
            "retry_max_attempts": 3,
            "retry_interval_minutes": 30,
            "retry_batch_size": 5,
            "risk_notice_dialog": False,
            "risk_notice_countdown": 0,
        }

    def get_page(self) -> List[dict]:
        usage = self._collect_monitor_usage()
        all_runs = self._collect_run_history()
        recent_runs = all_runs[:10]
        run_stats = self._build_run_stats(all_runs)
        usage_download = usage.get("download", {})
        usage_library = usage.get("library", {})
        daily_freed_bytes = self._get_daily_freed_bytes()
        daily_limit_bytes = int(self._max_gb_per_day * (1024 ** 3)) if self._max_gb_per_day > 0 else 0
        daily_percent = min(100, int(daily_freed_bytes * 100 / daily_limit_bytes)) if daily_limit_bytes else 0
        last_run_text = str(run_stats.get("last_run_at", "-") or "-")
        if len(last_run_text) > 16:
            last_run_text = last_run_text[-16:]

        overview_tiles = [
            self._build_metric_tile(
                title="任务执行次数",
                value=str(run_stats.get("total_runs", 0)),
                subtitle=f"完{run_stats.get('completed_runs', 0)} 跳{run_stats.get('skipped_runs', 0)} 异{run_stats.get('failed_runs', 0)}",
                color="primary",
                icon="mdi-playlist-check",
            ),
            self._build_metric_tile(
                title="累计删除条目",
                value=str(run_stats.get("total_actions", 0)),
                subtitle=f"最近 {last_run_text}",
                color="info",
                icon="mdi-delete-sweep",
            ),
            self._build_metric_tile(
                title="累计释放空间",
                value=self._format_size(int(run_stats.get("total_freed_bytes", 0) or 0)),
                subtitle=f"均值 {self._format_size(int(run_stats.get('avg_freed_bytes', 0) or 0))}",
                color="success",
                icon="mdi-harddisk-plus",
            ),
        ]

        usage_cards = [
            self._build_usage_card(
                title="资源目录",
                usage=usage_download,
                enabled=self._monitor_download,
                mode=self._download_threshold_mode,
                value=self._download_threshold_value,
            ),
            self._build_usage_card(
                title="媒体库目录",
                usage=usage_library,
                enabled=self._monitor_library,
                mode=self._library_threshold_mode,
                value=self._library_threshold_value,
            ),
        ]

        history_cards = []
        if not recent_runs:
            history_cards = [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal", "density": "comfortable", "text": "暂无任务执行记录"},
                }
            ]
        else:
            for index, run in enumerate(recent_runs, 1):
                status_type, status_text = self._run_status_info(run.get("status"))
                detail_items = run.get("items") or []
                detail_content = []
                if detail_items:
                    for seq, item in enumerate(detail_items[:12], 1):
                        detail_content.append({
                            "component": "VCard",
                            "props": {"variant": "tonal", "class": "mb-2"},
                            "content": [
                                {"component": "VCardText", "props": {"class": "py-1 font-weight-medium"}, "text": f"{seq}. {item.get('trigger', '-')}"},
                                {"component": "VCardText", "props": {"class": "py-0"}, "text": f"目标：{item.get('target', '-')}"},
                                {"component": "VCardText", "props": {"class": "py-0"}, "text": f"动作：{item.get('action', '-')}"},
                                {
                                    "component": "VCardText",
                                    "props": {"class": "py-0 text-caption"},
                                    "text": (
                                        f"释放：{self._format_size(int(item.get('freed_bytes', 0) or 0))} | "
                                        f"步骤结果：{self._step_result_text(item.get('steps'))}"
                                    ),
                                },
                            ],
                        })
                    if len(detail_items) > 12:
                        detail_content.append({
                            "component": "VAlert",
                            "props": {
                                "type": "info",
                                "variant": "tonal",
                                "density": "compact",
                                "text": f"本轮明细较多，仅展示前 12 条（共 {len(detail_items)} 条）",
                            },
                        })
                else:
                    detail_content.append({
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "text": "本轮没有实际删除明细（可能是阈值未命中、冷却命中或仅检查补偿队列）。",
                        },
                    })

                history_cards.append({
                    "component": "VCard",
                    "props": {"class": "mb-3", "variant": "outlined"},
                    "content": [
                        {"component": "VCardTitle", "text": f"第 {index} 轮 | {run.get('time', '-')}", "props": {"class": "text-subtitle-1"}},
                        {"component": "VCardSubtitle", "text": run.get("flow_text", self._trigger_flow_label())},
                        {"component": "VDivider"},
                        {
                            "component": "VCardText",
                            "content": [
                                {"component": "VAlert", "props": {"type": status_type, "variant": "tonal", "density": "compact", "text": f"状态：{status_text} | 备注：{run.get('reason', '-') or '-'}"}},
                                {
                                    "component": "VRow",
                                    "content": [
                                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VCardText", "props": {"class": "pa-0"}, "text": f"处理条目：{int(run.get('action_count', 0) or 0)}"}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VCardText", "props": {"class": "pa-0"}, "text": f"释放空间：{self._format_size(int(run.get('freed_bytes', 0) or 0))}"}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VCardText", "props": {"class": "pa-0"}, "text": f"模式：{run.get('mode', '-')}"}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VCardText", "props": {"class": "pa-0"}, "text": f"触发来源：{','.join(run.get('triggers') or ['-'])}"}]},
                                    ],
                                },
                                {"component": "VCardText", "props": {"class": "px-0 pb-1 font-weight-bold"}, "text": "删除详情"},
                                {"component": "div", "content": detail_content},
                            ],
                        },
                    ],
                })

        return [
            {
                "component": "VCard",
                "props": {"class": "mb-1", "variant": "outlined"},
                "content": [
                    {
                        "component": "VCardText",
                        "props": {"class": "py-1 px-2"},
                        "content": [
                            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"}, "text": "运行总览"},
                            {
                                "component": "div",
                                "props": {"class": "text-caption text-medium-emphasis"},
                                "text": f"流程：{self._trigger_flow_label()} | 冷却：{self._cooldown_minutes} 分钟 | 单轮：{self._max_delete_items} 条 / {self._max_gb_per_run} GB",
                            },
                            {
                                "component": "VRow",
                                "props": {"class": "mt-1"},
                                "content": overview_tiles,
                            },
                            {
                                "component": "VRow",
                                "props": {"align": "center", "noGutters": True},
                                "content": [
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "div", "props": {"class": "text-caption"}, "text": f"当日释放 {self._format_size(daily_freed_bytes)} / {self._max_gb_per_day} GB"}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [{"component": "VProgressLinear", "props": {"modelValue": daily_percent, "height": 4, "rounded": True, "striped": True, "stream": True, "color": "warning"}}]},
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"class": "mb-1", "variant": "outlined"},
                "content": [
                    {
                        "component": "VCardText",
                        "props": {"class": "py-1 px-2"},
                        "content": [
                            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"}, "text": "空间监控"},
                            {"component": "VRow", "props": {"class": "mt-1"}, "content": usage_cards},
                        ],
                    }
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "text": "任务执行历史"},
                    {"component": "VCardSubtitle", "text": "可查看每一轮执行删除了哪些对象、释放了多少空间、步骤是否有失败"},
                    {"component": "VDivider"},
                    {"component": "VCardText", "content": history_cards},
                ],
            },
        ]

    def _collect_run_history(self) -> List[dict]:
        run_history = self.get_data("run_history") or []
        if not run_history:
            run_history = self._build_run_history_from_actions(self.get_data("history") or [])
        return sorted(run_history, key=lambda x: x.get("time", ""), reverse=True)

    def _build_run_history_from_actions(self, actions: List[dict]) -> List[dict]:
        if not actions:
            return []
        grouped: Dict[str, dict] = {}
        for action in actions:
            time_text = action.get("time") or "-"
            group = grouped.setdefault(
                time_text,
                {
                    "run_id": f"legacy-{time_text}",
                    "time": time_text,
                    "status": "completed",
                    "reason": "历史记录自动聚合",
                    "flow": self._trigger_flow,
                    "flow_text": self._trigger_flow_label(),
                    "mode": action.get("mode", "apply"),
                    "action_count": 0,
                    "freed_bytes": 0,
                    "triggers": [],
                    "items": [],
                },
            )
            group["action_count"] += 1
            group["freed_bytes"] += int(action.get("freed_bytes", 0) or 0)
            trigger = action.get("trigger")
            if trigger and trigger not in group["triggers"]:
                group["triggers"].append(trigger)
            group["items"].append(
                {
                    "time": action.get("time"),
                    "trigger": trigger,
                    "target": action.get("target"),
                    "action": action.get("action"),
                    "freed_bytes": int(action.get("freed_bytes", 0) or 0),
                    "mode": action.get("mode"),
                    "steps": action.get("steps") or {},
                }
            )
        return list(grouped.values())

    def _build_run_stats(self, run_history: List[dict]) -> dict:
        total_runs = len(run_history)
        total_actions = sum(int(item.get("action_count", 0) or 0) for item in run_history)
        total_freed = sum(int(item.get("freed_bytes", 0) or 0) for item in run_history)
        completed_runs = len([item for item in run_history if item.get("status") == "completed"])
        skipped_runs = len([item for item in run_history if item.get("status") in {"skipped", "idle"}])
        failed_runs = len([item for item in run_history if item.get("status") == "failed"])
        avg_freed = int(total_freed / total_runs) if total_runs else 0
        return {
            "total_runs": total_runs,
            "completed_runs": completed_runs,
            "skipped_runs": skipped_runs,
            "failed_runs": failed_runs,
            "total_actions": total_actions,
            "total_freed_bytes": total_freed,
            "avg_freed_bytes": avg_freed,
            "last_run_at": run_history[0].get("time") if run_history else (self.get_data("last_run_at") or "-"),
        }

    @staticmethod
    def _build_metric_tile(
        title: str,
        value: str,
        subtitle: str,
        color: str = "primary",
        icon: str = "mdi-chart-box-outline",
    ) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 4},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "color": color, "class": "h-100"},
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "py-2 px-2"},
                            "content": [
                                {
                                    "component": "VRow",
                                    "props": {"class": "ma-0", "align": "center", "noGutters": True},
                                    "content": [
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 7},
                                            "content": [
                                                {"component": "div", "props": {"class": "text-caption text-medium-emphasis text-truncate"}, "text": title}
                                            ],
                                        },
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 5, "class": "text-right"},
                                            "content": [
                                                {"component": "VIcon", "props": {"size": "12", "icon": icon}},
                                            ],
                                        },
                                    ],
                                },
                                {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1 text-truncate"}, "text": subtitle},
                                {"component": "div", "props": {"class": "text-body-2 font-weight-bold text-truncate mt-1"}, "text": value},
                            ],
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _run_status_info(status: Optional[str]) -> Tuple[str, str]:
        mapping = {
            "completed": ("success", "执行完成"),
            "skipped": ("warning", "已跳过"),
            "idle": ("info", "无需清理"),
            "failed": ("error", "执行异常"),
        }
        return mapping.get(status or "", ("info", str(status or "未知状态")))

    @staticmethod
    def _step_result_text(steps: Optional[dict]) -> str:
        if not steps:
            return "-"
        step_name_map = {
            "media": "媒体",
            "scrape": "刮削",
            "downloader": "下载器",
            "transfer_history": "整理记录",
            "download_history": "下载记录",
        }
        parts = []
        for key, value in steps.items():
            planned = int((value or {}).get("planned", 0) or 0)
            done = int((value or {}).get("done", 0) or 0)
            failed = int((value or {}).get("failed", 0) or 0)
            label = step_name_map.get(key, key)
            parts.append(f"{label}:{done}/{planned}{'(失败'+str(failed)+')' if failed else ''}")
        return "; ".join(parts) if parts else "-"

    def _append_run_history(
        self,
        run_time: str,
        usage: Optional[dict],
        actions: List[dict],
        freed_bytes: int,
        status: str = "completed",
        reason: str = "",
    ):
        run_history = self.get_data("run_history") or []
        triggers = sorted({item.get("trigger") for item in actions if item.get("trigger")})
        items = []
        for item in actions[:100]:
            items.append(
                {
                    "time": item.get("time"),
                    "trigger": item.get("trigger"),
                    "target": item.get("target"),
                    "action": item.get("action"),
                    "freed_bytes": int(item.get("freed_bytes", 0) or 0),
                    "mode": item.get("mode"),
                    "steps": item.get("steps") or {},
                }
            )
        run_record = {
            "run_id": f"{int(time.time() * 1000)}-{len(run_history) + 1}",
            "time": run_time,
            "status": status,
            "reason": reason,
            "flow": self._trigger_flow,
            "flow_text": self._trigger_flow_label(),
            "mode": "dry-run" if self._dry_run else "apply",
            "action_count": len(actions),
            "freed_bytes": int(freed_bytes or 0),
            "triggers": triggers,
            "usage": usage or {},
            "items": items,
        }
        run_history.append(run_record)
        self.save_data("run_history", run_history[-200:])

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
                run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._current_run_freed_bytes = 0
                self._playback_ts_cache = {}
                retry_actions: List[dict] = []
                skip_normal_cleanup = False
                if self._enable_retry_queue and not self._dry_run:
                    retry_actions = self._process_retry_queue(limit=self._retry_batch_size)

                if self._is_in_cooldown():
                    if not retry_actions:
                        logger.info(f"{self.plugin_name}命中冷却时间，跳过执行")
                        self._append_run_history(
                            run_time=run_time,
                            usage=None,
                            actions=[],
                            freed_bytes=0,
                            status="skipped",
                            reason="命中冷却时间",
                        )
                        return
                    logger.info(f"{self.plugin_name}命中冷却时间，仅执行失败补偿")
                    skip_normal_cleanup = True

                if not self._dry_run and self._is_daily_limit_reached():
                    if not retry_actions:
                        logger.warning(f"{self.plugin_name}已达当日释放上限，跳过执行")
                        self._append_run_history(
                            run_time=run_time,
                            usage=None,
                            actions=[],
                            freed_bytes=0,
                            status="skipped",
                            reason="已达当日释放上限",
                        )
                        return
                    logger.warning(f"{self.plugin_name}已达当日释放上限，仅执行失败补偿")
                    skip_normal_cleanup = True

                usage = self._collect_monitor_usage()
                self.save_data("latest_usage", usage)

                round_actions: List[dict] = []
                if not skip_normal_cleanup:
                    round_actions = self._execute_trigger_flow(usage=usage)

                all_actions = retry_actions + round_actions
                if not all_actions:
                    logger.info(f"{self.plugin_name}本次无需清理")
                    self._append_run_history(
                        run_time=run_time,
                        usage=usage,
                        actions=[],
                        freed_bytes=0,
                        status="idle",
                        reason="本次无需清理",
                    )
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
                    refresh_items = self._filter_refresh_items_by_library_scope(refresh_items)
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

                self._append_run_history(
                    run_time=run_time,
                    usage=usage,
                    actions=all_actions,
                    freed_bytes=freed_bytes,
                    status="completed",
                    reason="执行完成",
                )
                self.save_data("last_run_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                logger.info(
                    f"{self.plugin_name}执行完成，本次处理 {len(all_actions)} 条，"
                    f"{'预计' if self._dry_run else '实际'}释放 {self._format_size(freed_bytes)}"
                )
            except Exception as err:
                try:
                    self._append_run_history(
                        run_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        usage=None,
                        actions=[],
                        freed_bytes=0,
                        status="failed",
                        reason=f"任务异常：{err}",
                    )
                except Exception:
                    pass
                logger.error(f"{self.plugin_name}任务执行异常：{err}", exc_info=True)

    def _execute_trigger_flow(self, usage: dict) -> List[dict]:
        flow = self._trigger_flow or "flow_library_mp_downloader"
        logger.info(f"{self.plugin_name}当前触发流程：{self._trigger_flow_label(flow)}")
        if flow == "flow_downloader_mp_library":
            return self._clean_by_download_threshold(usage=usage)
        if flow == "flow_transfer_oldest":
            return self._clean_by_transfer_history_oldest(usage=usage)
        return self._clean_by_library_threshold(usage=usage)

    def _clean_by_library_threshold(self, usage: Optional[dict] = None) -> List[dict]:
        usage = usage or self._collect_monitor_usage()
        if not self._monitor_library:
            logger.info(f"{self.plugin_name}流程1未启用媒体库监听，跳过")
            return []
        if not self._is_threshold_hit(usage.get("library", {}), self._library_threshold_mode, self._library_threshold_value):
            logger.info(f"{self.plugin_name}流程1未命中媒体库阈值，跳过")
            return []

        actions: List[dict] = []
        skipped_paths = set()
        while not self._is_run_limit_reached(actions):
            current_usage = self._collect_monitor_usage().get("library", {})
            if not self._is_threshold_hit(current_usage, self._library_threshold_mode, self._library_threshold_value):
                break

            candidate = self._pick_oldest_library_media(skipped_paths=skipped_paths)
            if not candidate:
                logger.warning(f"{self.plugin_name}流程1未找到可清理的媒体文件")
                break

            result = self._cleanup_by_media_file(
                candidate=candidate,
                trigger="流程1:媒体目录→优先联动MP整理与下载器",
                require_torrent_link=False,
            )
            skipped_paths.add(candidate.get("path").as_posix())
            if result:
                actions.append(result)
                self._current_run_freed_bytes += int(result.get("freed_bytes", 0) or 0)
        return actions

    def _clean_by_download_threshold(self, usage: Optional[dict] = None) -> List[dict]:
        usage = usage or self._collect_monitor_usage()
        download_hit = self._monitor_download and self._is_threshold_hit(
            usage.get("download", {}),
            self._download_threshold_mode,
            self._download_threshold_value,
        )
        if not download_hit and not self._monitor_downloader:
            logger.info(f"{self.plugin_name}流程2未命中下载器触发条件，跳过")
            return []

        actions: List[dict] = []
        skipped_hashes = set()
        while not self._is_run_limit_reached(actions):
            current_usage = self._collect_monitor_usage()
            download_hit = self._monitor_download and self._is_threshold_hit(
                current_usage.get("download", {}),
                self._download_threshold_mode,
                self._download_threshold_value,
            )
            min_days = self._seeding_days if self._monitor_downloader else None
            candidate = self._pick_longest_seeding_torrent(min_days=min_days, skipped_hashes=skipped_hashes)
            if not candidate and download_hit and min_days is not None:
                # 磁盘阈值已触发时，允许放宽做种时长限制。
                candidate = self._pick_longest_seeding_torrent(min_days=None, skipped_hashes=skipped_hashes)
            if not candidate:
                break

            trigger_text = "流程2:下载器→MP整理→媒体数据(目录阈值)" if download_hit else "流程2:下载器→MP整理→媒体数据(做种阈值)"
            allow_non_mp_hardlink = self._force_hardlink_cleanup and self._clean_media_data
            result = self._cleanup_by_torrent(
                candidate=candidate,
                trigger=trigger_text,
                require_mp_history=not allow_non_mp_hardlink,
            )
            skipped_hashes.add(candidate.get("hash"))
            if result:
                actions.append(result)
                self._current_run_freed_bytes += int(result.get("freed_bytes", 0) or 0)

        return actions

    def _clean_by_transfer_history_oldest(self, usage: Optional[dict] = None) -> List[dict]:
        usage = usage or self._collect_monitor_usage()
        reasons = self._flow3_trigger_reasons(usage)
        if not reasons:
            logger.info(f"{self.plugin_name}流程3未命中触发条件，跳过")
            return []

        actions: List[dict] = []
        skipped_ids = set()
        while not self._is_run_limit_reached(actions):
            current_usage = self._collect_monitor_usage()
            if not self._flow3_trigger_reasons(current_usage):
                break

            history = self._pick_oldest_transfer_history(skipped_ids=skipped_ids)
            if not history:
                logger.warning(f"{self.plugin_name}流程3未找到可清理的整理记录")
                break

            hid = int(getattr(history, "id", 0) or 0)
            if hid > 0:
                skipped_ids.add(hid)

            result = self._cleanup_by_transfer_history(
                history=history,
                trigger="流程3:MP整理记录(旧到新)→媒体与下载器",
            )
            if result:
                actions.append(result)
                self._current_run_freed_bytes += int(result.get("freed_bytes", 0) or 0)

        return actions

    def _flow3_trigger_reasons(self, usage: dict) -> List[str]:
        reasons: List[str] = []
        if self._monitor_library and self._is_threshold_hit(
            usage.get("library", {}),
            self._library_threshold_mode,
            self._library_threshold_value,
        ):
            reasons.append("媒体库目录阈值")
        if self._monitor_download and self._is_threshold_hit(
            usage.get("download", {}),
            self._download_threshold_mode,
            self._download_threshold_value,
        ):
            reasons.append("资源目录阈值")
        if self._monitor_downloader:
            candidate = self._pick_longest_seeding_torrent(min_days=self._seeding_days, skipped_hashes=set())
            if candidate:
                reasons.append("下载器做种时长阈值")
        return reasons

    def _pick_oldest_transfer_history(self, skipped_ids: set) -> Optional[TransferHistory]:
        records = TransferHistory.list_by_page(
            db=self._transfer_oper._db if self._transfer_oper else None,
            page=1,
            count=-1,
            status=True,
        ) or []
        if not records:
            return None

        def _sort_key(item: TransferHistory) -> Tuple[int, int]:
            date_ts = self._parse_datetime_to_ts(getattr(item, "date", None)) or int(time.time())
            item_id = int(getattr(item, "id", 0) or 0)
            return date_ts, item_id

        candidates: List[TransferHistory] = []
        for history in sorted(records, key=_sort_key):
            hid = int(getattr(history, "id", 0) or 0)
            if hid > 0 and hid in skipped_ids:
                continue
            if self._is_history_recent(history):
                continue
            if not self._is_tv_cleanup_allowed(history):
                continue
            if not getattr(history, "dest", None) and not getattr(history, "download_hash", None):
                continue
            candidates.append(history)
        if not candidates:
            return None
        preferred = [item for item in candidates if self._history_media_type_key(item) == self._media_cleanup_priority]
        return preferred[0] if preferred else candidates[0]

    def _trigger_flow_label(self, flow: Optional[str] = None) -> str:
        mapping = {
            "flow_library_mp_downloader": "1. 媒体优先（推荐）-> 优先联动MP整理/下载器（缺失时仅删本地）",
            "flow_downloader_mp_library": "2. 下载器优先 -> MP整理记录 -> 媒体数据",
            "flow_transfer_oldest": "3. 整理记录优先（旧到新）-> 媒体数据 + 下载器做种",
        }
        return mapping.get(flow or self._trigger_flow, mapping["flow_library_mp_downloader"])

    def _cleanup_by_transfer_history(self, history: TransferHistory, trigger: str) -> Optional[dict]:
        if not history:
            return None
        if self._is_history_recent(history):
            logger.info(f"{self.plugin_name}命中近期保护，跳过整理记录：{getattr(history, 'dest', '-')}")
            return None
        if not self._is_tv_cleanup_allowed(history):
            logger.info(f"{self.plugin_name}命中“仅清理已完结电视剧”限制，跳过：{getattr(history, 'dest', '-')}")
            return None

        dest = getattr(history, "dest", None)
        media_path = self._resolve_local_media_path(dest)
        target_text = media_path.as_posix() if media_path else (str(dest) if dest else "-")
        download_hash = getattr(history, "download_hash", None)
        downloader = getattr(history, "downloader", None)
        if self._clean_download_history and not download_hash:
            logger.warning(f"{self.plugin_name}未找到可删除的下载记录，跳过下载记录删除：{dest or '-'}")
        library_paths = self._library_paths()
        delete_roots = self._media_delete_roots(library_paths)
        scope_enabled = bool(self._media_servers or self._media_libraries)
        if scope_enabled:
            if not media_path or not self._is_path_in_roots(media_path, library_paths):
                logger.info(f"{self.plugin_name}整理记录不在所选媒体库范围内，跳过：{dest or '-'}")
                return None

        sidecars = self._collect_scrape_files(media_path) if media_path and self._clean_scrape_data else []
        media_targets: List[Path] = []
        if self._clean_media_data and media_path and media_path.exists():
            media_targets.append(media_path)
        media_targets = self._expand_media_targets_with_hardlinks(
            media_targets=media_targets,
            roots=delete_roots,
            context=target_text,
            enabled=self._force_hardlink_cleanup and self._clean_media_data,
        )

        planned_media = len(media_targets)
        planned_scrape = len([item for item in sidecars if item.exists()]) if self._clean_scrape_data else 0
        can_delete_seed = self._can_delete_torrent_in_media_flow(
            downloader=downloader,
            torrent_hash=download_hash,
            target=target_text,
        )
        planned_downloader = 1 if can_delete_seed else 0
        planned_transfer = (
            1 if (self._clean_transfer_history and scope_enabled) else
            (self._count_transfer_records(download_hash, history) if self._clean_transfer_history else 0)
        )
        planned_download = self._count_download_records(download_hash) if self._clean_download_history and download_hash else 0

        freed_bytes = 0
        for target in media_targets:
            freed_bytes += self._path_size(target)
        for sidecar in sidecars:
            freed_bytes += self._path_size(sidecar)

        if freed_bytes <= 0 and (planned_downloader + planned_transfer + planned_download) <= 0:
            return None

        if not self._dry_run and self._is_run_bytes_limit_reached(freed_bytes):
            logger.info(f"{self.plugin_name}单轮容量上限已达，跳过整理记录：{dest or '-'}")
            return None
        if not self._dry_run and self._is_daily_bytes_limit_reached(freed_bytes):
            logger.info(f"{self.plugin_name}当日容量上限将超额，跳过整理记录：{dest or '-'}")
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
            for target in media_targets:
                if self._delete_local_item(target, delete_roots):
                    removed_media += 1

            if self._clean_scrape_data:
                for sidecar in sidecars:
                    if self._delete_local_item(sidecar, delete_roots):
                        removed_scrape += 1

            if can_delete_seed and download_hash and downloader:
                if self._delete_torrent(downloader=downloader, torrent_hash=download_hash):
                    removed_downloader += 1

            if self._clean_transfer_history:
                removed_transfer = self._delete_transfer_history(
                    download_hash=None if scope_enabled else download_hash,
                    history=history,
                )

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
                "retry_key": f"transfer:{getattr(history, 'id', 0)}",
                "trigger": trigger,
                "target": dest or "-",
                "media_path": dest,
                "media_targets": [item.as_posix() for item in media_targets],
                "sidecars": [item.as_posix() for item in sidecars],
                "download_hash": download_hash,
                "downloader": downloader,
                "history_dest": dest,
                "failed_steps": failed_steps,
            })

        total_actions = removed_media + removed_scrape + removed_downloader + removed_transfer + removed_download
        if total_actions <= 0:
            return None

        action_text = (
            f"媒体{removed_media} 刮削{removed_scrape} 删种{removed_downloader} "
            f"整理记录{removed_transfer} 下载记录{removed_download}"
        )
        logger.info(f"{self.plugin_name}按整理记录清理：{dest or '-'} -> {action_text}")
        refresh_items = []
        refresh_item = self._build_refresh_item(history=history, target_path=dest)
        if refresh_item:
            refresh_items.append(refresh_item)

        return {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger,
            "target": dest or "-",
            "action": action_text,
            "freed_bytes": freed_bytes,
            "mode": "dry-run" if self._dry_run else "apply",
            "refresh_items": refresh_items,
            "steps": step_result,
        }

    def _cleanup_by_media_file(
        self,
        candidate: dict,
        trigger: str = "媒体库阈值",
        require_torrent_link: bool = False,
    ) -> Optional[dict]:
        media_path: Path = candidate.get("path")
        if not media_path:
            return None

        history = self._find_transfer_history_by_media_path(media_path)
        scope_enabled = bool(self._media_servers or self._media_libraries)
        if history and self._is_history_recent(history):
            logger.info(f"{self.plugin_name}命中近期保护，跳过：{media_path.as_posix()}")
            return None
        if history and not self._is_tv_cleanup_allowed(history):
            logger.info(f"{self.plugin_name}命中“仅清理已完结电视剧”限制，跳过：{media_path.as_posix()}")
            return None
        download_hash = getattr(history, "download_hash", None) if history else None
        downloader = getattr(history, "downloader", None) if history else None
        if self._clean_transfer_history and not history and not download_hash:
            logger.warning(f"{self.plugin_name}未找到可删除的整理记录，跳过整理记录删除：{media_path.as_posix()}")
        if self._clean_download_history and not download_hash:
            logger.warning(f"{self.plugin_name}未找到可删除的下载记录，跳过下载记录删除：{media_path.as_posix()}")
        if require_torrent_link and not (history and download_hash and downloader):
            logger.info(f"{self.plugin_name}流程要求MP与下载器均可关联，跳过：{media_path.as_posix()}")
            return None

        library_paths = self._library_paths()
        delete_roots = self._media_delete_roots(library_paths)
        sidecars = self._collect_scrape_files(media_path) if self._clean_scrape_data else []
        media_targets: List[Path] = []
        if self._clean_media_data and media_path.exists():
            media_targets.append(media_path)
        # 兜底策略：当缺失MP关联时，按硬链接关系清理同inode文件（常见于下载目录与媒体库硬链接）。
        if self._force_hardlink_cleanup and self._clean_media_data and not history:
            media_targets = self._expand_media_targets_with_hardlinks(
                media_targets=media_targets,
                roots=delete_roots,
                context=media_path.as_posix(),
                enabled=True,
            )
        else:
            media_targets = self._expand_media_targets_with_hardlinks(
                media_targets=media_targets,
                roots=delete_roots,
                context=media_path.as_posix(),
                enabled=False,
            )

        planned_media = len(media_targets)
        planned_scrape = len([item for item in sidecars if item.exists()]) if self._clean_scrape_data else 0
        planned_downloader = 1 if self._clean_downloader_seed and download_hash and downloader else 0
        planned_transfer = (
            1 if (self._clean_transfer_history and scope_enabled and history) else
            (self._count_transfer_records(download_hash, history) if self._clean_transfer_history else 0)
        )
        planned_download = self._count_download_records(download_hash) if self._clean_download_history and download_hash else 0

        freed_bytes = 0
        for target in media_targets:
            freed_bytes += self._path_size(target)
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
            for target in media_targets:
                if self._delete_local_item(target, delete_roots):
                    removed_media += 1

            if self._clean_scrape_data:
                for sidecar in sidecars:
                    if self._delete_local_item(sidecar, delete_roots):
                        removed_scrape += 1

            if self._clean_downloader_seed and download_hash and downloader:
                if self._delete_torrent(downloader=downloader, torrent_hash=download_hash):
                    removed_downloader += 1

            if self._clean_transfer_history:
                removed_transfer = self._delete_transfer_history(
                    download_hash=None if (scope_enabled and history) else download_hash,
                    history=history,
                )

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
                "trigger": trigger,
                "target": media_path.as_posix(),
                "media_path": media_path.as_posix(),
                "media_targets": [item.as_posix() for item in media_targets],
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
            refresh_item = self._build_refresh_item(history=history, target_path=getattr(history, "dest", media_path))
            if refresh_item:
                refresh_items.append(refresh_item)

        return {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger,
            "target": media_path.as_posix(),
            "action": action_text,
            "freed_bytes": freed_bytes,
            "mode": "dry-run" if self._dry_run else "apply",
            "refresh_items": refresh_items,
            "steps": step_result,
        }

    def _cleanup_by_torrent(
        self,
        candidate: dict,
        trigger: str,
        require_mp_history: bool = False,
    ) -> Optional[dict]:
        download_hash = candidate.get("hash")
        downloader = candidate.get("downloader")
        name = candidate.get("name") or download_hash

        if not download_hash:
            return None

        histories = self._transfer_oper.list_by_hash(download_hash) if self._transfer_oper else []
        scope_enabled = bool(self._media_servers or self._media_libraries)
        library_paths = self._library_paths()
        delete_roots = self._media_delete_roots(library_paths)
        download_file_targets = self._download_file_media_paths(download_hash) if self._clean_media_data else []
        if scope_enabled:
            scoped_histories = []
            for history in histories:
                path = self._resolve_local_media_path(getattr(history, "dest", None))
                if path and self._is_path_in_roots(path, library_paths):
                    scoped_histories.append(history)
            histories = scoped_histories
            allow_non_mp_hardlink = self._force_hardlink_cleanup and self._clean_media_data
            if not histories and not allow_non_mp_hardlink:
                logger.info(f"{self.plugin_name}下载器任务不在所选媒体库范围内，跳过：{downloader}:{name}")
                return None
        if require_mp_history and not histories:
            logger.info(f"{self.plugin_name}跳过非MP关联任务：{downloader}:{name}")
            return None
        if self._clean_transfer_history and not histories:
            logger.warning(f"{self.plugin_name}未找到可删除的整理记录，跳过整理记录删除：{downloader}:{name}")
        if self._clean_download_history and not download_hash:
            logger.warning(f"{self.plugin_name}未找到可删除的下载记录，跳过下载记录删除：{downloader}:{name}")
        if histories and any(self._is_history_recent(history) for history in histories):
            logger.info(f"{self.plugin_name}命中近期保护，跳过：{downloader}:{name}")
            return None
        if histories and self._tv_complete_only and any(not self._is_tv_cleanup_allowed(history) for history in histories):
            logger.info(f"{self.plugin_name}命中“仅清理已完结电视剧”限制，跳过：{downloader}:{name}")
            return None

        media_targets: List[Path] = []
        sidecar_targets: List[Path] = []
        if self._clean_media_data or self._clean_scrape_data:
            handled_paths = set()
            for history in histories:
                dest = getattr(history, "dest", None)
                if not dest:
                    continue
                path = self._resolve_local_media_path(dest)
                if not path:
                    continue
                if path.as_posix() in handled_paths:
                    continue
                handled_paths.add(path.as_posix())
                if self._clean_media_data and path.exists():
                    media_targets.append(path)
                if self._clean_scrape_data:
                    sidecar_targets.extend([item for item in self._collect_scrape_files(path) if item.exists()])
            for path in download_file_targets:
                if path.as_posix() in handled_paths:
                    continue
                handled_paths.add(path.as_posix())
                media_targets.append(path)

        media_targets = self._expand_media_targets_with_hardlinks(
            media_targets=media_targets,
            roots=delete_roots,
            context=f"{downloader}:{name}",
            enabled=self._force_hardlink_cleanup and self._clean_media_data,
        )
        if scope_enabled and not histories and self._force_hardlink_cleanup and self._clean_media_data:
            if not any(self._is_path_in_roots(item, library_paths) for item in media_targets):
                logger.info(f"{self.plugin_name}下载器任务未匹配到媒体库范围，跳过：{downloader}:{name}")
                return None

        if self._clean_scrape_data and media_targets:
            for media_target in media_targets:
                sidecar_targets.extend([item for item in self._collect_scrape_files(media_target) if item.exists()])
        dedup_sidecars = {}
        for sidecar in sidecar_targets:
            dedup_sidecars[sidecar.as_posix()] = sidecar
        sidecar_targets = list(dedup_sidecars.values())

        planned_media = len(media_targets)
        planned_scrape = len(sidecar_targets)
        planned_downloader = 1 if self._clean_downloader_seed and downloader else 0
        planned_transfer = len(histories) if self._clean_transfer_history else 0
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
                if self._delete_local_item(path, delete_roots):
                    removed_media += 1

            for sidecar in sidecar_targets:
                if self._delete_local_item(sidecar, delete_roots):
                    removed_scrape += 1

            if self._clean_downloader_seed and downloader:
                if self._delete_torrent(downloader=downloader, torrent_hash=download_hash):
                    removed_downloader += 1

            if self._clean_transfer_history:
                for history in histories:
                    removed_transfer += self._delete_transfer_history(download_hash=None, history=history)

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
        need_history = bool(self._transfer_oper and self._prefer_playback_history)

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
                    history = self._find_transfer_history_by_media_path(path) if need_history else None
                    if history and self._is_history_recent(history):
                        continue
                    if history and not self._is_tv_cleanup_allowed(history):
                        continue
                    atime = stat.st_atime or stat.st_mtime
                    candidates.append({
                        "path": path,
                        "atime": atime,
                        "history": history,
                        "media_type": self._history_media_type_key(history),
                    })

        if not candidates:
            return None

        preferred = [item for item in candidates if item.get("media_type") == self._media_cleanup_priority]
        candidate_pool = preferred if preferred else candidates
        selected = None
        selected_ts = int(time.time())
        for candidate in candidate_pool:
            current_ts = self._effective_access_ts(
                path=candidate.get("path"),
                history=candidate.get("history"),
                fallback_ts=candidate.get("atime"),
            )
            if selected is None or current_ts < selected_ts:
                selected = candidate
                selected_ts = current_ts
        if not selected:
            return None

        return {
            "path": selected.get("path"),
            "atime": selected_ts,
            "raw_atime": selected.get("atime"),
        }

    @staticmethod
    def _normalize_media_priority(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"tv", "电视剧", "series"}:
            return "tv"
        return "movie"

    @staticmethod
    def _media_type_key(raw_type: Any) -> str:
        text = str(raw_type or "").strip().lower()
        if text in {"电影", "movie", "mov", "film"}:
            return "movie"
        if text in {"电视剧", "tv", "series", "show"}:
            return "tv"
        return "unknown"

    def _history_media_type_key(self, history: Any) -> str:
        if not history:
            return "unknown"
        return self._media_type_key(getattr(history, "type", None))

    def _is_tv_cleanup_allowed(self, history: Any) -> bool:
        if not history or not self._tv_complete_only:
            return True
        if self._history_media_type_key(history) != "tv":
            return True
        try:
            tmdbid = getattr(history, "tmdbid", None)
            return self._is_tv_series_ended_by_tmdb(tmdbid)
        except Exception as err:
            logger.warning(f"{self.plugin_name}检查电视剧完结状态失败：{err}")
            return False

    def _is_tv_series_ended_by_tmdb(self, tmdbid: Any) -> bool:
        if not tmdbid:
            return False
        try:
            tmdb_id = int(tmdbid)
        except Exception:
            return False
        cache_key = str(tmdb_id)
        cached = self._tv_end_state_cache.get(cache_key)
        if cached is not None:
            return bool(cached)
        try:
            tmdb_info = self.chain.tmdb_info(tmdbid=tmdb_id, mtype=MediaType.TV) if self.chain else None
            if not isinstance(tmdb_info, dict) or not tmdb_info:
                self._tv_end_state_cache[cache_key] = False
                return False
            status_text = str(tmdb_info.get("status") or "").strip().lower()
            ended_status = {"ended", "canceled", "cancelled", "已完结", "完结", "已取消", "取消"}
            is_ended = False
            if status_text:
                is_ended = status_text in ended_status
            elif tmdb_info.get("in_production") is False:
                is_ended = True
            self._tv_end_state_cache[cache_key] = bool(is_ended)
            return bool(is_ended)
        except Exception as err:
            logger.warning(f"{self.plugin_name}查询TMDB电视剧状态失败 tmdbid={tmdb_id}: {err}")
            self._tv_end_state_cache[cache_key] = False
            return False

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
        all_library_paths = self._unique_existing_paths(
            [Path(item.library_path) for item in DirectoryHelper().get_local_library_dirs() if item.library_path]
        )
        if not self._media_servers and not self._media_libraries:
            return all_library_paths
        scope_paths = self._library_scope_paths()
        if not scope_paths:
            return []
        return self._filter_paths_by_scope(all_library_paths, scope_paths)

    def _media_library_items(self, server_filters: Optional[List[str]] = None) -> List[dict]:
        services = MediaServerHelper().get_services(
            name_filters=server_filters if server_filters else None
        )
        if not services:
            return []

        if not self._mediaserver_chain:
            self._mediaserver_chain = MediaServerChain()

        items: List[dict] = []
        seen = set()
        for server_name in services.keys():
            try:
                libraries = self._mediaserver_chain.librarys(server=server_name, hidden=True) or []
            except Exception as err:
                logger.warning(f"{self.plugin_name}获取媒体库列表失败 {server_name}：{err}")
                continue
            for library in libraries:
                key = f"{server_name}::{getattr(library, 'id', '')}"
                if key.endswith("::") or key in seen:
                    continue
                seen.add(key)
                name = str(getattr(library, "name", "") or getattr(library, "id", ""))
                path_desc = self._extract_library_path_list(getattr(library, "path", None))
                desc = path_desc[0] if path_desc else ""
                if len(path_desc) > 1:
                    desc = f"{desc} +{len(path_desc) - 1}"
                title = f"{server_name} / {name}"
                if desc:
                    title = f"{title} ({desc})"
                items.append({"title": title, "value": key})
        return sorted(items, key=lambda item: item.get("title", ""))

    def _library_scope_paths(self) -> List[Path]:
        if not self._media_servers and not self._media_libraries:
            return []
        if self._library_scope_cache is not None:
            return self._library_scope_cache

        selected_servers = [item for item in (self._media_servers or []) if str(item).strip()]
        selected_keys = []
        for raw in self._media_libraries or []:
            server_name, library_id = self._parse_library_key(raw)
            if not server_name or not library_id:
                continue
            selected_keys.append(f"{server_name}::{library_id}")
            if server_name not in selected_servers:
                selected_servers.append(server_name)

        if not selected_servers:
            self._library_scope_cache = []
            return self._library_scope_cache

        services = MediaServerHelper().get_services(name_filters=selected_servers)
        if not services:
            self._library_scope_cache = []
            return self._library_scope_cache

        if not self._mediaserver_chain:
            self._mediaserver_chain = MediaServerChain()

        filter_by_key = set(selected_keys)
        resolved_paths: List[Path] = []
        for server_name in services.keys():
            try:
                libraries = self._mediaserver_chain.librarys(server=server_name, hidden=True) or []
            except Exception as err:
                logger.warning(f"{self.plugin_name}获取媒体库范围失败 {server_name}：{err}")
                continue
            for library in libraries:
                library_key = f"{server_name}::{getattr(library, 'id', '')}"
                if filter_by_key and library_key not in filter_by_key:
                    continue
                for raw_path in self._extract_library_path_list(getattr(library, "path", None)):
                    mapped = self._resolve_local_media_path(raw_path)
                    if mapped:
                        resolved_paths.append(mapped)

        self._library_scope_cache = self._unique_existing_paths(resolved_paths)
        return self._library_scope_cache

    def _effective_media_servers(self) -> List[str]:
        servers = [item for item in (self._media_servers or []) if str(item).strip()]
        for item in self._media_libraries or []:
            server_name, _ = self._parse_library_key(item)
            if server_name and server_name not in servers:
                servers.append(server_name)
        return servers

    @staticmethod
    def _filter_paths_by_scope(paths: List[Path], scope_paths: List[Path]) -> List[Path]:
        filtered = []
        for path in paths:
            for scope in scope_paths:
                try:
                    if path == scope or path.is_relative_to(scope) or scope.is_relative_to(path):
                        filtered.append(path)
                        break
                except Exception:
                    continue
        unique: Dict[str, Path] = {}
        for item in filtered:
            unique[item.as_posix()] = item
        return list(unique.values())

    @staticmethod
    def _parse_library_key(raw: Any) -> Tuple[str, str]:
        text = str(raw or "").strip()
        if "::" not in text:
            return "", ""
        server_name, library_id = text.split("::", 1)
        server_name = str(server_name).strip()
        library_id = str(library_id).strip()
        if not server_name or not library_id:
            return "", ""
        return server_name, library_id

    @staticmethod
    def _extract_library_path_list(raw_path: Any) -> List[str]:
        values = raw_path if isinstance(raw_path, list) else [raw_path]
        result = []
        for value in values:
            text = str(value or "").strip()
            if text:
                result.append(text)
        return result

    def _filter_refresh_items_by_library_scope(
        self, refresh_items: Optional[List[RefreshMediaItem]]
    ) -> List[RefreshMediaItem]:
        if not refresh_items:
            return []
        if not self._media_servers and not self._media_libraries:
            return refresh_items
        library_roots = self._library_paths()
        if not library_roots:
            return []
        filtered: List[RefreshMediaItem] = []
        for item in refresh_items:
            target_path = getattr(item, "target_path", None)
            local_path = self._resolve_local_media_path(target_path)
            if local_path and self._is_path_in_roots(local_path, library_roots):
                filtered.append(item)
        return filtered

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
            return f"剩余 <= {self._format_threshold_value(value, 'percent')}"
        return f"剩余 <= {self._format_threshold_value(value, 'size')}"

    @staticmethod
    def _parse_threshold_value(raw_value: Any, mode: str) -> float:
        default_value = 10.0 if mode == "percent" else 100.0
        if raw_value is None or raw_value == "":
            return default_value
        if isinstance(raw_value, (int, float)):
            return float(raw_value)

        text = str(raw_value).strip().lower()
        if not text:
            return default_value

        factor = 1.0
        if text.endswith("%"):
            text = text[:-1].strip()
        elif text.endswith("gb") or text.endswith("g"):
            text = text.rstrip("b").rstrip("g").strip()
        elif text.endswith("tb") or text.endswith("t"):
            text = text.rstrip("b").rstrip("t").strip()
            factor = 1024.0
        elif text.endswith("mb") or text.endswith("m"):
            text = text.rstrip("b").rstrip("m").strip()
            factor = 1.0 / 1024.0

        try:
            value = float(text) * factor
        except Exception:
            return default_value
        return value

    @staticmethod
    def _format_threshold_value(value: Any, mode: str) -> str:
        try:
            val = float(value)
        except Exception:
            val = 10.0 if mode == "percent" else 100.0
        if mode == "percent":
            if abs(val - int(val)) < 1e-9:
                return f"{int(val)}%"
            return f"{val:.1f}%"
        if abs(val - int(val)) < 1e-9:
            return f"{int(val)}G"
        return f"{val:.1f}G"

    def _media_delete_roots(self, library_roots: Optional[List[Path]] = None) -> List[Path]:
        roots = list(library_roots or self._library_paths())
        if self._force_hardlink_cleanup:
            roots.extend(self._download_paths())
        return self._unique_existing_paths(roots)

    def _collect_hardlink_siblings(self, media_path: Path, roots: List[Path]) -> List[Path]:
        try:
            base = Path(media_path)
            if not base.exists() or not base.is_file() or base.is_symlink():
                return []
            base_stat = base.stat()
            if int(getattr(base_stat, "st_nlink", 1) or 1) <= 1:
                return []
        except Exception:
            return []

        siblings: Dict[str, Path] = {}
        for root in roots or []:
            try:
                if not root.exists():
                    continue
            except Exception:
                continue
            for current_root, _, files in os.walk(root.as_posix()):
                for filename in files:
                    path = Path(current_root) / filename
                    if path == base:
                        continue
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        stat = path.stat()
                    except Exception:
                        continue
                    if stat.st_ino == base_stat.st_ino and stat.st_dev == base_stat.st_dev:
                        siblings[path.as_posix()] = path
        return list(siblings.values())

    def _expand_media_targets_with_hardlinks(
        self,
        media_targets: List[Path],
        roots: List[Path],
        context: str,
        enabled: bool,
    ) -> List[Path]:
        dedup: Dict[str, Path] = {}
        for item in media_targets or []:
            if item:
                dedup[item.as_posix()] = item
        if not enabled or not dedup:
            return list(dedup.values())

        origin = list(dedup.values())
        added_count = 0
        for target in origin:
            for sibling in self._collect_hardlink_siblings(target, roots=roots):
                key = sibling.as_posix()
                if key in dedup:
                    continue
                dedup[key] = sibling
                added_count += 1
        if added_count > 0:
            logger.warning(f"{self.plugin_name}触发硬链接兜底删除，新增 {added_count} 个关联文件：{context}")
        return list(dedup.values())

    def _download_file_media_paths(self, download_hash: Optional[str]) -> List[Path]:
        if not self._download_oper or not download_hash:
            return []
        try:
            files = self._download_oper.get_files_by_hash(download_hash) or []
        except Exception as err:
            logger.warning(f"{self.plugin_name}查询下载文件记录失败，跳过提取本地路径 hash={download_hash}: {err}")
            return []

        allowed_exts = {str(ext).strip().lower() for ext in (getattr(settings, "RMT_MEDIAEXT", []) or []) if str(ext).strip()}
        targets: Dict[str, Path] = {}
        for fileinfo in files:
            for raw_path in self._download_file_path_candidates(fileinfo):
                local_path = self._resolve_local_media_path(raw_path)
                if not local_path:
                    continue
                try:
                    if not local_path.exists() and not local_path.is_symlink():
                        continue
                    if not local_path.is_file() and not local_path.is_symlink():
                        continue
                except Exception:
                    continue
                if allowed_exts and local_path.suffix.lower() not in allowed_exts:
                    continue
                targets[local_path.as_posix()] = local_path
        return list(targets.values())

    @staticmethod
    def _download_file_path_candidates(fileinfo: Any) -> List[str]:
        candidates: List[str] = []
        fullpath = str(getattr(fileinfo, "fullpath", "") or "").strip()
        if fullpath:
            candidates.append(fullpath)
        savepath = str(getattr(fileinfo, "savepath", "") or "").strip()
        filepath = str(getattr(fileinfo, "filepath", "") or "").strip()
        if savepath and filepath:
            candidates.append((Path(savepath) / filepath).as_posix())
        elif filepath:
            file_path_obj = Path(filepath)
            if file_path_obj.is_absolute():
                candidates.append(file_path_obj.as_posix())
        return candidates

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

    def _can_delete_torrent_in_media_flow(
        self,
        downloader: Optional[str],
        torrent_hash: Optional[str],
        target: str = "-",
    ) -> bool:
        if not self._clean_downloader_seed or not downloader or not torrent_hash:
            return False
        if not self._media_flow_seed_check:
            return True
        if self._seeding_days <= 0:
            return True

        service = DownloaderHelper().get_service(name=downloader)
        if not service or not service.instance:
            logger.warning(f"{self.plugin_name}媒体流程删种校验失败，下载器不可用，跳过删种：{target}")
            return False

        try:
            torrents = service.instance.get_completed_torrents() or []
        except Exception as err:
            logger.warning(f"{self.plugin_name}媒体流程读取下载器做种信息失败，跳过删种：{target} - {err}")
            return False

        seed_seconds = None
        for torrent in torrents:
            if self._torrent_hash(torrent) != torrent_hash:
                continue
            seed_seconds = self._torrent_seed_seconds(torrent)
            break

        if seed_seconds is None:
            logger.warning(f"{self.plugin_name}媒体流程未找到种子做种信息，跳过删种：{target}")
            return False

        need_seconds = int(self._seeding_days * 86400)
        if seed_seconds < need_seconds:
            logger.info(
                f"{self.plugin_name}媒体流程种子做种时长不足，跳过删种：{target} "
                f"(当前{int(seed_seconds / 86400)}天 < 阈值{self._seeding_days}天)"
            )
            return False
        return True

    def _delete_transfer_history(self, download_hash: Optional[str], history: Any) -> int:
        if not self._transfer_oper:
            return 0

        records = []
        if download_hash:
            records = self._transfer_oper.list_by_hash(download_hash)
        elif history:
            records = [history]

        if not records:
            logger.warning(f"{self.plugin_name}未找到可删除的整理记录，跳过")
            return 0

        removed = 0
        visited = set()
        for record in records:
            rid = getattr(record, "id", None)
            if not rid or rid in visited:
                continue
            visited.add(rid)
            try:
                self._transfer_oper.delete(rid)
                removed += 1
            except Exception as err:
                logger.warning(f"{self.plugin_name}删除整理记录失败，已跳过 id={rid}: {err}")

        return removed

    def _delete_download_history(self, download_hash: str) -> int:
        if not self._download_oper:
            return 0
        if not download_hash:
            logger.warning(f"{self.plugin_name}未找到可删除的下载记录，跳过")
            return 0

        removed = 0
        found_main = False

        while True:
            try:
                history = self._download_oper.get_by_hash(download_hash)
            except Exception as err:
                logger.warning(f"{self.plugin_name}查询下载记录失败，已跳过 hash={download_hash}: {err}")
                break
            if not history:
                break
            found_main = True
            try:
                self._download_oper.delete_history(history.id)
                removed += 1
            except Exception as err:
                logger.warning(f"{self.plugin_name}删除下载记录失败，已跳过 id={history.id}: {err}")

        try:
            files = self._download_oper.get_files_by_hash(download_hash)
        except Exception as err:
            logger.warning(f"{self.plugin_name}查询下载文件记录失败，已跳过 hash={download_hash}: {err}")
            files = []
        found_files = bool(files)
        for fileinfo in files:
            try:
                self._download_oper.delete_downloadfile(fileinfo.id)
                removed += 1
            except Exception as err:
                logger.warning(f"{self.plugin_name}删除下载文件记录失败，已跳过 id={fileinfo.id}: {err}")

        if not found_main and not found_files:
            logger.warning(f"{self.plugin_name}未找到可删除的下载记录，跳过 hash={download_hash}")

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
        media_targets = [Path(item) for item in (payload.get("media_targets") or []) if item]
        if not media_targets and media_path:
            media_targets = [media_path]
        sidecars = [Path(item) for item in (payload.get("sidecars") or []) if item]
        download_hash = payload.get("download_hash")
        downloader = payload.get("downloader")
        history_dest = payload.get("history_dest")
        library_roots = self._library_paths()
        delete_roots = self._media_delete_roots(library_roots)

        freed_bytes = 0
        removed_media = 0
        removed_scrape = 0
        removed_downloader = 0
        removed_transfer = 0
        removed_download = 0

        planned_media = len(media_targets) if "media" in failed else 0
        if planned_media:
            for target in media_targets:
                freed_bytes += self._path_size(target)
                if (not target.exists() and not target.is_symlink()) or self._delete_local_item(target, delete_roots):
                    removed_media += 1

        planned_scrape = len(sidecars) if "scrape" in failed else 0
        if planned_scrape:
            for sidecar in sidecars:
                freed_bytes += self._path_size(sidecar)
                if (not sidecar.exists() and not sidecar.is_symlink()) or self._delete_local_item(sidecar, delete_roots):
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
        delete_roots = self._media_delete_roots(library_roots)

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
                if (not path.exists() and not path.is_symlink()) or self._delete_local_item(path, delete_roots):
                    removed_media += 1

        planned_scrape = len(sidecar_targets) if "scrape" in failed else 0
        if planned_scrape:
            for path in sidecar_targets:
                freed_bytes += self._path_size(path)
                if (not path.exists() and not path.is_symlink()) or self._delete_local_item(path, delete_roots):
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
        media_servers = self._effective_media_servers()
        services = MediaServerHelper().get_services(
            name_filters=media_servers if media_servers else None
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
            completed = torrent.get("completion_on") or torrent.get("completed_on") or 0
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
        status_text = "触发清理" if trigger else "状态正常"
        status_type = "error" if trigger else "success"
        if not enabled:
            status_text = "未启用监听"
            status_type = "info"
        used_percent = float(usage.get("used_percent", 0) or 0)
        used_percent = max(0.0, min(100.0, used_percent))
        free_percent = float(usage.get("free_percent", 0) or 0)
        free_percent = max(0.0, min(100.0, free_percent))
        progress_color = "error" if trigger else ("success" if enabled else "info")

        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 6},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "h-100"},
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "py-1 px-2"},
                            "content": [
                                {
                                    "component": "VRow",
                                    "props": {"align": "center", "noGutters": True},
                                    "content": [
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 8},
                                            "content": [
                                                {
                                                    "component": "VRow",
                                                    "props": {"align": "center", "noGutters": True},
                                                    "content": [
                                                        {
                                                            "component": "VCol",
                                                            "props": {"cols": 7},
                                                            "content": [
                                                                {"component": "div", "props": {"class": "text-caption text-medium-emphasis text-truncate"}, "text": title}
                                                            ],
                                                        },
                                                        {
                                                            "component": "VCol",
                                                            "props": {"cols": 5, "class": "text-right"},
                                                            "content": [
                                                                {"component": "VChip", "props": {"size": "x-small", "color": status_type, "variant": "tonal"}, "text": status_text}
                                                            ],
                                                        },
                                                    ],
                                                }
                                            ],
                                        },
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 4, "class": "text-right"},
                                            "content": [{"component": "div", "props": {"class": "text-body-2 font-weight-bold text-truncate"}, "text": usage.get("free_text", "0 B")}],
                                        },
                                    ],
                                },
                                {"component": "div", "props": {"class": "text-caption mt-1 text-medium-emphasis text-truncate"}, "text": f"已用 {usage.get('used_text', '0 B')} ({used_percent:.1f}%) | 阈值 {self._threshold_text(mode, value)}"},
                                {"component": "VProgressLinear", "props": {"modelValue": used_percent, "height": 4, "rounded": True, "striped": True, "stream": True, "color": progress_color}},
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption mt-1 text-medium-emphasis text-truncate"},
                                    "text": f"总 {usage.get('total_text', '0 B')} | 可用 {free_percent:.1f}% | 路径 {len(paths)}",
                                },
                            ],
                        },
                    ],
                }
            ],
        }

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

    def _parse_media_path_mappings(self, raw_mappings: List[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
        normalized_lines: List[str] = []
        rules: List[Tuple[str, str]] = []
        seen = set()
        for raw in raw_mappings or []:
            line = str(raw).strip()
            if not line:
                continue
            if "=>" in line:
                server_root, local_root = line.split("=>", 1)
            elif ":" in line:
                server_root, local_root = line.rsplit(":", 1)
            else:
                logger.warning(f"{self.plugin_name}路径映射格式无效，已忽略：{line}")
                continue

            server_root = self._normalize_mapping_root(server_root)
            local_root = self._normalize_mapping_root(local_root)
            if not server_root or not local_root:
                continue
            key = (server_root, local_root)
            if key in seen:
                continue
            seen.add(key)
            rules.append(key)
            normalized_lines.append(f"{server_root}:{local_root}")
        return normalized_lines, rules

    @staticmethod
    def _normalize_mapping_root(raw_path: Any) -> str:
        if not raw_path:
            return ""
        text = Path(str(raw_path).strip()).expanduser().as_posix().strip()
        if len(text) > 1 and text.endswith("/"):
            text = text.rstrip("/")
        return text

    @staticmethod
    def _replace_path_root(path_text: str, src_root: str, dst_root: str) -> str:
        if not path_text or not src_root or not dst_root:
            return path_text
        try:
            path = Path(path_text)
            src = Path(src_root)
            dst = Path(dst_root)
            if path == src:
                return dst.as_posix()
            relative = path.relative_to(src)
            return (dst / relative).as_posix()
        except Exception:
            return path_text

    def _history_dest_candidates(self, media_path: Path) -> List[str]:
        base = Path(media_path).expanduser().as_posix()
        candidates = [base]
        for server_root, local_root in self._media_path_rules:
            mapped = self._replace_path_root(base, local_root, server_root)
            if mapped not in candidates:
                candidates.append(mapped)
        return candidates

    def _find_transfer_history_by_media_path(self, media_path: Optional[Path]) -> Optional[TransferHistory]:
        if not media_path or not self._transfer_oper:
            return None
        for candidate_dest in self._history_dest_candidates(media_path):
            history = self._transfer_oper.get_by_dest(candidate_dest)
            if history:
                return history
        return None

    def _resolve_local_media_path(self, dest_path: Any) -> Optional[Path]:
        if not dest_path:
            return None
        path_text = Path(str(dest_path)).expanduser().as_posix()
        for server_root, local_root in self._media_path_rules:
            mapped = self._replace_path_root(path_text, server_root, local_root)
            if mapped != path_text:
                return Path(mapped)
        return Path(path_text)

    def _normalize_config(self):
        self._clear_history = bool(self._clear_history)
        if self._trigger_flow not in (
            "flow_library_mp_downloader",
            "flow_downloader_mp_library",
            "flow_transfer_oldest",
        ):
            self._trigger_flow = "flow_library_mp_downloader"
        if self._download_threshold_mode not in ("size", "percent"):
            self._download_threshold_mode = "size"
        if self._library_threshold_mode not in ("size", "percent"):
            self._library_threshold_mode = "size"

        self._download_threshold_value = max(
            0.0,
            self._parse_threshold_value(self._download_threshold_value, self._download_threshold_mode),
        )
        self._library_threshold_value = max(
            0.0,
            self._parse_threshold_value(self._library_threshold_value, self._library_threshold_mode),
        )
        if self._download_threshold_mode == "percent":
            self._download_threshold_value = min(100.0, self._download_threshold_value)
        if self._library_threshold_mode == "percent":
            self._library_threshold_value = min(100.0, self._library_threshold_value)

        self._cooldown_minutes = max(0, int(self._cooldown_minutes))
        self._seeding_days = max(0, int(self._seeding_days))
        self._max_delete_items = max(1, min(50, int(self._max_delete_items)))
        self._max_gb_per_run = max(0.0, self._safe_float(self._max_gb_per_run, 50.0))
        self._max_gb_per_day = max(0.0, self._safe_float(self._max_gb_per_day, 200.0))
        self._protect_recent_days = max(0, int(self._protect_recent_days))
        self._media_cleanup_priority = self._normalize_media_priority(self._media_cleanup_priority)
        self._tv_complete_only = bool(self._tv_complete_only)
        self._media_flow_seed_check = bool(self._media_flow_seed_check)
        self._force_hardlink_cleanup = bool(self._force_hardlink_cleanup)
        self._refresh_mode = "item" if self._refresh_mode not in ("item", "root") else self._refresh_mode
        self._refresh_batch_size = max(1, min(200, int(self._refresh_batch_size)))
        self._retry_max_attempts = max(1, min(20, int(self._retry_max_attempts)))
        self._retry_interval_minutes = max(1, min(1440, int(self._retry_interval_minutes)))
        self._retry_batch_size = max(1, min(50, int(self._retry_batch_size)))
        self._downloaders = [str(item).strip() for item in (self._downloaders or []) if str(item).strip()]
        self._media_servers = [str(item).strip() for item in (self._media_servers or []) if str(item).strip()]
        normalized_libraries: List[str] = []
        for item in self._media_libraries or []:
            server_name, library_id = self._parse_library_key(item)
            if not server_name or not library_id:
                continue
            normalized_libraries.append(f"{server_name}::{library_id}")
        dedup_libraries: Dict[str, bool] = {}
        for item in normalized_libraries:
            dedup_libraries[item] = True
        self._media_libraries = list(dedup_libraries.keys())
        self._path_allowlist = self._parse_path_list(self._path_allowlist)
        self._path_blocklist = self._parse_path_list(self._path_blocklist)
        self._media_path_mapping = self._parse_path_list(self._media_path_mapping)
        self._media_path_mapping, self._media_path_rules = self._parse_media_path_mappings(self._media_path_mapping)
        self._library_scope_cache = None

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "clear_history": self._clear_history,
                "dry_run": self._dry_run,
                "force_hardlink_cleanup": self._force_hardlink_cleanup,
                "cron": self._cron,
                "cooldown_minutes": self._cooldown_minutes,
                "monitor_download": self._monitor_download,
                "monitor_library": self._monitor_library,
                "monitor_downloader": self._monitor_downloader,
                "trigger_flow": self._trigger_flow,
                "download_threshold_mode": self._download_threshold_mode,
                "download_threshold_value": self._format_threshold_value(
                    self._download_threshold_value,
                    self._download_threshold_mode,
                ),
                "library_threshold_mode": self._library_threshold_mode,
                "library_threshold_value": self._format_threshold_value(
                    self._library_threshold_value,
                    self._library_threshold_mode,
                ),
                "downloaders": self._downloaders,
                "seeding_days": self._seeding_days,
                "media_flow_seed_check": self._media_flow_seed_check,
                "max_delete_items": self._max_delete_items,
                "max_gb_per_run": self._max_gb_per_run,
                "max_gb_per_day": self._max_gb_per_day,
                "protect_recent_days": self._protect_recent_days,
                "media_cleanup_priority": self._media_cleanup_priority,
                "tv_complete_only": self._tv_complete_only,
                "prefer_playback_history": self._prefer_playback_history,
                "clean_media_data": self._clean_media_data,
                "clean_scrape_data": self._clean_scrape_data,
                "clean_downloader_seed": self._clean_downloader_seed,
                "delete_downloader_files": self._delete_downloader_files,
                "clean_transfer_history": self._clean_transfer_history,
                "clean_download_history": self._clean_download_history,
                "path_allowlist": "\n".join(self._path_allowlist),
                "path_blocklist": "\n".join(self._path_blocklist),
                "media_path_mapping": "\n".join(self._media_path_mapping),
                "refresh_mediaserver": self._refresh_mediaserver,
                "refresh_mode": self._refresh_mode,
                "refresh_batch_size": self._refresh_batch_size,
                "media_servers": self._media_servers,
                "media_libraries": self._media_libraries,
                "refresh_servers": self._media_servers,
                "enable_retry_queue": self._enable_retry_queue,
                "retry_max_attempts": self._retry_max_attempts,
                "retry_interval_minutes": self._retry_interval_minutes,
                "retry_batch_size": self._retry_batch_size,
            }
        )
