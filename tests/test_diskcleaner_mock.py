import enum
import importlib.util
import os
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


PLUGIN_FILE = Path(__file__).resolve().parents[1] / "plugins.v2" / "diskcleaner" / "__init__.py"


def _install_fake_modules() -> None:
    pytz_mod = types.ModuleType("pytz")
    pytz_mod.timezone = lambda _name: None
    sys.modules["pytz"] = pytz_mod

    bg_mod = types.ModuleType("apscheduler.schedulers.background")

    class BackgroundScheduler:
        def __init__(self, *args, **kwargs):
            self._jobs = []

        def add_job(self, *args, **kwargs):
            self._jobs.append((args, kwargs))

        def get_jobs(self):
            return self._jobs

        def print_jobs(self):
            return None

        def start(self):
            return None

        def shutdown(self):
            return None

    bg_mod.BackgroundScheduler = BackgroundScheduler
    sys.modules["apscheduler.schedulers.background"] = bg_mod

    cron_mod = types.ModuleType("apscheduler.triggers.cron")

    class CronTrigger:
        @staticmethod
        def from_crontab(_expr):
            return "cron"

    cron_mod.CronTrigger = CronTrigger
    sys.modules["apscheduler.triggers.cron"] = cron_mod

    app_mod = types.ModuleType("app")
    sys.modules["app"] = app_mod

    chain_mediaserver_mod = types.ModuleType("app.chain.mediaserver")

    class MediaServerChain:
        def librarys(self, *args, **kwargs):
            return []

    chain_mediaserver_mod.MediaServerChain = MediaServerChain
    sys.modules["app.chain.mediaserver"] = chain_mediaserver_mod

    core_config_mod = types.ModuleType("app.core.config")
    core_config_mod.settings = SimpleNamespace(
        TZ="Asia/Shanghai",
        API_TOKEN="unit_test_token",
        RMT_MEDIAEXT={".mkv", ".mp4", ".avi"},
    )
    sys.modules["app.core.config"] = core_config_mod

    db_download_mod = types.ModuleType("app.db.downloadhistory_oper")
    db_download_mod.DownloadHistoryOper = type("DownloadHistoryOper", (), {})
    sys.modules["app.db.downloadhistory_oper"] = db_download_mod

    db_mediaserver_mod = types.ModuleType("app.db.mediaserver_oper")
    db_mediaserver_mod.MediaServerOper = type("MediaServerOper", (), {})
    sys.modules["app.db.mediaserver_oper"] = db_mediaserver_mod

    db_models_transfer_mod = types.ModuleType("app.db.models.transferhistory")
    db_models_transfer_mod.TransferHistory = type("TransferHistory", (), {})
    sys.modules["app.db.models.transferhistory"] = db_models_transfer_mod

    db_transfer_mod = types.ModuleType("app.db.transferhistory_oper")
    db_transfer_mod.TransferHistoryOper = type("TransferHistoryOper", (), {})
    sys.modules["app.db.transferhistory_oper"] = db_transfer_mod

    helper_directory_mod = types.ModuleType("app.helper.directory")

    class DirectoryHelper:
        @staticmethod
        def get_local_dirs():
            return []

        @staticmethod
        def get_local_download_dirs():
            return []

        @staticmethod
        def get_local_library_dirs():
            return []

    helper_directory_mod.DirectoryHelper = DirectoryHelper
    sys.modules["app.helper.directory"] = helper_directory_mod

    helper_downloader_mod = types.ModuleType("app.helper.downloader")

    class DownloaderHelper:
        @staticmethod
        def get_configs():
            return {}

    helper_downloader_mod.DownloaderHelper = DownloaderHelper
    sys.modules["app.helper.downloader"] = helper_downloader_mod

    helper_mediaserver_mod = types.ModuleType("app.helper.mediaserver")

    class MediaServerHelper:
        @staticmethod
        def get_configs():
            return {}

        @staticmethod
        def get_services(name_filters=None):
            return {}

    helper_mediaserver_mod.MediaServerHelper = MediaServerHelper
    sys.modules["app.helper.mediaserver"] = helper_mediaserver_mod

    log_mod = types.ModuleType("app.log")
    log_mod.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    sys.modules["app.log"] = log_mod

    plugins_mod = types.ModuleType("app.plugins")

    class _PluginBase:
        def __init__(self):
            self.chain = None
            self._plugin_data_store = {}

        def get_data(self, key):
            return self._plugin_data_store.get(key)

        def save_data(self, key, value):
            self._plugin_data_store[key] = value

        def update_config(self, _config):
            return None

        def post_message(self, *args, **kwargs):
            return None

    plugins_mod._PluginBase = _PluginBase
    sys.modules["app.plugins"] = plugins_mod

    schemas_mod = types.ModuleType("app.schemas")
    schemas_mod.NotificationType = type("NotificationType", (), {"Plugin": "Plugin"})
    schemas_mod.RefreshMediaItem = type("RefreshMediaItem", (), {})
    sys.modules["app.schemas"] = schemas_mod

    schemas_types_mod = types.ModuleType("app.schemas.types")

    class MediaType(enum.Enum):
        MOVIE = "电影"
        TV = "电视剧"

    schemas_types_mod.MediaType = MediaType
    sys.modules["app.schemas.types"] = schemas_types_mod

    utils_system_mod = types.ModuleType("app.utils.system")

    class SystemUtils:
        @staticmethod
        def free_space(_path):
            return 0, 0, 0

    utils_system_mod.SystemUtils = SystemUtils
    sys.modules["app.utils.system"] = utils_system_mod


def _load_plugin_module():
    _install_fake_modules()
    module_name = "diskcleaner_plugin_under_test"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _iter_components(nodes):
    for node in nodes or []:
        if isinstance(node, dict):
            yield node.get("component")
            yield from _iter_components(node.get("content"))
            yield from _iter_components(node.get("items"))


class DiskCleanerMockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin_mod = _load_plugin_module()
        cls.DiskCleaner = cls.plugin_mod.DiskCleaner

    def _new_cleaner(self):
        cleaner = self.DiskCleaner()
        cleaner._tv_end_state_cache = {}
        cleaner._tv_complete_only = True
        cleaner._media_servers = []
        cleaner._media_libraries = []
        cleaner._mediaserver_chain = None
        return cleaner

    @staticmethod
    def _history(mtype="电视剧", tmdbid=100):
        return SimpleNamespace(type=mtype, tmdbid=tmdbid, seasons="S01", doubanid=None)

    def test_tv_cleanup_ignore_when_switch_off(self):
        cleaner = self._new_cleaner()
        cleaner._tv_complete_only = False
        cleaner.chain = SimpleNamespace(tmdb_info=lambda *a, **k: {"status": "Returning Series"})
        self.assertTrue(cleaner._is_tv_cleanup_allowed(self._history()))

    def test_tv_cleanup_non_tv_always_allowed(self):
        cleaner = self._new_cleaner()
        cleaner.chain = SimpleNamespace(tmdb_info=lambda *a, **k: {"status": "Ended"})
        self.assertTrue(cleaner._is_tv_cleanup_allowed(self._history(mtype="电影")))

    def test_tv_cleanup_allow_ended_status(self):
        cleaner = self._new_cleaner()
        cleaner.chain = SimpleNamespace(tmdb_info=lambda *a, **k: {"status": "Ended"})
        self.assertTrue(cleaner._is_tv_cleanup_allowed(self._history(tmdbid=101)))

    def test_tv_cleanup_block_returning_series(self):
        cleaner = self._new_cleaner()
        cleaner.chain = SimpleNamespace(tmdb_info=lambda *a, **k: {"status": "Returning Series"})
        self.assertFalse(cleaner._is_tv_cleanup_allowed(self._history(tmdbid=102)))

    def test_tv_cleanup_allow_in_production_false_without_status(self):
        cleaner = self._new_cleaner()
        cleaner.chain = SimpleNamespace(tmdb_info=lambda *a, **k: {"in_production": False})
        self.assertTrue(cleaner._is_tv_cleanup_allowed(self._history(tmdbid=103)))

    def test_tv_cleanup_block_when_tmdbid_invalid(self):
        cleaner = self._new_cleaner()
        cleaner.chain = SimpleNamespace(tmdb_info=lambda *a, **k: {"status": "Ended"})
        self.assertFalse(cleaner._is_tv_cleanup_allowed(self._history(tmdbid="")))

    def test_tmdb_status_result_cached(self):
        cleaner = self._new_cleaner()
        calls = {"count": 0}

        def _tmdb_info(*args, **kwargs):
            calls["count"] += 1
            return {"status": "Ended"}

        cleaner.chain = SimpleNamespace(tmdb_info=_tmdb_info)
        history = self._history(tmdbid=104)
        self.assertTrue(cleaner._is_tv_cleanup_allowed(history))
        self.assertTrue(cleaner._is_tv_cleanup_allowed(history))
        self.assertEqual(calls["count"], 1)

    def test_risk_notice_dialog_only_show_once(self):
        cleaner = self._new_cleaner()
        cleaner.save_data("risk_notice_acked", False)
        form, _ = cleaner.get_form()
        self.assertIn("VDialog", list(_iter_components(form)))

        cleaner.save_data("risk_notice_acked", True)
        form_after_ack, _ = cleaner.get_form()
        self.assertNotIn("VDialog", list(_iter_components(form_after_ack)))

    def test_form_contains_usage_doc_link(self):
        cleaner = self._new_cleaner()
        form, _ = cleaner.get_form()
        payload = json.dumps(form, ensure_ascii=False)
        self.assertIn("USAGE.md", payload)
        self.assertIn("打开使用文档", payload)

    def test_library_paths_ignore_media_scope_filter(self):
        cleaner = self._new_cleaner()
        cleaner._media_servers = ["emby"]
        cleaner._media_libraries = ["emby::1"]

        original_helper = self.plugin_mod.DirectoryHelper

        class _FakeDirectoryHelper:
            @staticmethod
            def get_local_library_dirs():
                return [
                    SimpleNamespace(library_path="/media/link/电影/"),
                    SimpleNamespace(library_path="/media/link/电视剧/"),
                ]

        self.plugin_mod.DirectoryHelper = _FakeDirectoryHelper
        try:
            paths = cleaner._library_paths()
        finally:
            self.plugin_mod.DirectoryHelper = original_helper

        self.assertEqual(
            {item.as_posix() for item in paths},
            {"/media/link/电影", "/media/link/电视剧"},
        )

    def test_get_page_uses_compact_history_layout(self):
        cleaner = self._new_cleaner()
        cleaner._collect_monitor_usage = lambda: {
            "download": {"paths": [], "total": 0, "free": 0, "used": 0, "used_percent": 0, "free_percent": 0, "total_text": "0 B", "free_text": "0 B", "used_text": "0 B"},
            "library": {"paths": [], "total": 0, "free": 0, "used": 0, "used_percent": 0, "free_percent": 0, "total_text": "0 B", "free_text": "0 B", "used_text": "0 B"},
        }
        cleaner._collect_run_history = lambda: [
            {
                "time": "2026-02-13 08:00:00",
                "status": "completed",
                "reason": "执行完成",
                "flow_text": "流程X",
                "mode": "apply",
                "action_count": 1,
                "freed_bytes": 1024,
                "triggers": ["资源目录阈值"],
                "items": [{"trigger": "流程X", "target": "/a.mkv", "action": "媒体1", "freed_bytes": 1024, "steps": {}}],
            }
        ]

        page = cleaner.get_page()
        payload = json.dumps(page, ensure_ascii=False)
        self.assertIn("任务执行历史（最近10轮）", payload)

    def test_usage_card_contains_directory_paths(self):
        cleaner = self._new_cleaner()
        cleaner._collect_monitor_usage = lambda: {
            "download": {
                "paths": [
                    "/media/down/a",
                    "/media/down/b",
                    "/media/down/c",
                    "/media/down/d",
                ],
                "total": 1,
                "free": 1,
                "used": 0,
                "used_percent": 0,
                "free_percent": 100,
                "total_text": "1 B",
                "free_text": "1 B",
                "used_text": "0 B",
            },
            "library": {
                "paths": ["/media/link/a"],
                "total": 1,
                "free": 1,
                "used": 0,
                "used_percent": 0,
                "free_percent": 100,
                "total_text": "1 B",
                "free_text": "1 B",
                "used_text": "0 B",
            },
        }
        cleaner._collect_run_history = lambda: []
        payload = json.dumps(cleaner.get_page(), ensure_ascii=False)
        self.assertIn("目录路径", payload)
        self.assertIn("VChip", payload)
        self.assertNotIn("\"text\": \"...\"", payload)
        self.assertIn("/media/down/a", payload)
        self.assertIn("/media/link/a", payload)

    def test_usage_card_show_ellipsis_when_many_paths(self):
        cleaner = self._new_cleaner()
        cleaner._collect_monitor_usage = lambda: {
            "download": {
                "paths": [f"/media/down/{idx}" for idx in range(1, 12)],
                "total": 1,
                "free": 1,
                "used": 0,
                "used_percent": 0,
                "free_percent": 100,
                "total_text": "1 B",
                "free_text": "1 B",
                "used_text": "0 B",
            },
            "library": {
                "paths": ["/media/link/a"],
                "total": 1,
                "free": 1,
                "used": 0,
                "used_percent": 0,
                "free_percent": 100,
                "total_text": "1 B",
                "free_text": "1 B",
                "used_text": "0 B",
            },
        }
        cleaner._collect_run_history = lambda: []
        payload = json.dumps(cleaner.get_page(), ensure_ascii=False)
        self.assertIn("\"text\": \"...\"", payload)

    def test_build_refresh_item_is_json_serializable(self):
        cleaner = self._new_cleaner()
        history = SimpleNamespace(
            type=self.plugin_mod.MediaType.TV,
            title="示例剧",
            year=2026,
            category="国产剧",
            dest="/media/link/tv/demo.mkv",
        )
        item = cleaner._build_refresh_item(history=history, target_path=history.dest)
        self.assertIsInstance(item, dict)
        self.assertEqual(item.get("type"), self.plugin_mod.MediaType.TV.value)
        self.assertEqual(item.get("target_path"), "/media/link/tv/demo.mkv")
        json.dumps(item, ensure_ascii=False)

    def test_sanitize_for_json_handles_enum_and_path(self):
        cleaner = self._new_cleaner()
        raw = {
            "type": self.plugin_mod.MediaType.MOVIE,
            "target_path": Path("/media/link/movie.mkv"),
            "items": [self.plugin_mod.MediaType.TV],
        }
        normalized = cleaner._sanitize_for_json(raw)
        self.assertEqual(normalized.get("type"), self.plugin_mod.MediaType.MOVIE.value)
        self.assertEqual(normalized.get("target_path"), "/media/link/movie.mkv")
        self.assertEqual(normalized.get("items"), [self.plugin_mod.MediaType.TV.value])
        json.dumps(normalized, ensure_ascii=False)

    def test_cleanup_by_torrent_allow_non_mp_with_downloadfile_hardlink_fallback(self):
        cleaner = self._new_cleaner()
        cleaner._dry_run = False
        cleaner._clean_media_data = True
        cleaner._clean_scrape_data = False
        cleaner._clean_downloader_seed = False
        cleaner._clean_transfer_history = False
        cleaner._clean_download_history = False
        cleaner._force_hardlink_cleanup = True
        cleaner._transfer_oper = SimpleNamespace(list_by_hash=lambda _hash: [])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library_root = root / "library"
            download_root = root / "download"
            library_root.mkdir()
            download_root.mkdir()

            library_file = library_root / "movie.mkv"
            download_file = download_root / "movie.mkv"
            library_file.write_bytes(b"movie-bytes")
            os.link(library_file, download_file)

            cleaner._library_paths = lambda: [library_root]
            cleaner._download_paths = lambda: [download_root]
            cleaner._download_oper = SimpleNamespace(
                get_files_by_hash=lambda _hash: [
                    SimpleNamespace(
                        fullpath=download_file.as_posix(),
                        savepath=download_root.as_posix(),
                        filepath="movie.mkv",
                    )
                ]
            )

            result = cleaner._cleanup_by_torrent(
                candidate={"hash": "h1", "downloader": "qb", "name": "movie"},
                trigger="unit-test",
                require_mp_history=False,
            )

            self.assertIsNotNone(result)
            self.assertFalse(library_file.exists())
            self.assertFalse(download_file.exists())
            self.assertEqual(((result.get("steps") or {}).get("media") or {}).get("done"), 2)

    def test_retry_torrent_payload_uses_delete_roots_including_download_paths(self):
        cleaner = self._new_cleaner()
        cleaner._force_hardlink_cleanup = True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library_root = root / "library"
            download_root = root / "download"
            library_root.mkdir()
            download_root.mkdir()

            media_file = library_root / "sample.mkv"
            sidecar_file = library_root / "sample.nfo"
            media_file.write_bytes(b"a")
            sidecar_file.write_text("nfo", encoding="utf-8")

            cleaner._library_paths = lambda: [library_root]
            cleaner._download_paths = lambda: [download_root]
            cleaner._path_size = lambda _path: 1

            delete_roots_args = []

            def _fake_delete(path, roots):
                delete_roots_args.append([Path(item).as_posix() for item in roots])
                return True

            cleaner._delete_local_item = _fake_delete

            result = cleaner._retry_torrent_payload(
                {
                    "failed_steps": ["media", "scrape"],
                    "download_hash": "h2",
                    "downloader": "qb",
                    "media_targets": [media_file.as_posix()],
                    "sidecar_targets": [sidecar_file.as_posix()],
                    "history_dests": [],
                }
            )

            self.assertEqual(len(delete_roots_args), 2)
            for roots in delete_roots_args:
                self.assertIn(download_root.as_posix(), roots)
            self.assertEqual(((result.get("steps") or {}).get("media") or {}).get("done"), 1)
            self.assertEqual(((result.get("steps") or {}).get("scrape") or {}).get("done"), 1)

    def test_clean_by_download_threshold_skip_when_no_trigger(self):
        cleaner = self._new_cleaner()
        cleaner._monitor_download = True
        cleaner._monitor_downloader = False
        cleaner._is_threshold_hit = lambda *args, **kwargs: False
        cleaner._collect_monitor_usage = lambda: {"download": {}, "library": {}}
        cleaner._pick_longest_seeding_torrent = lambda *args, **kwargs: {"hash": "unexpected"}

        actions = cleaner._clean_by_download_threshold(usage={"download": {}, "library": {}})
        self.assertEqual(actions, [])

    def test_clean_by_download_threshold_require_mp_history_toggle(self):
        cleaner = self._new_cleaner()
        cleaner._monitor_download = True
        cleaner._monitor_downloader = False
        cleaner._clean_media_data = True
        cleaner._collect_monitor_usage = lambda: {"download": {}, "library": {}}
        cleaner._is_threshold_hit = lambda *args, **kwargs: True

        calls = {"pick": 0, "require_flags": []}

        def _pick(*args, **kwargs):
            calls["pick"] += 1
            return {"hash": "h1", "downloader": "qb", "name": "n1"} if calls["pick"] == 1 else None

        def _cleanup(candidate, trigger, require_mp_history):
            calls["require_flags"].append(require_mp_history)
            return {"freed_bytes": 0}

        cleaner._pick_longest_seeding_torrent = _pick
        cleaner._cleanup_by_torrent = _cleanup

        cleaner._force_hardlink_cleanup = False
        cleaner._clean_by_download_threshold()
        self.assertEqual(calls["require_flags"][-1], True)

        calls["pick"] = 0
        cleaner._force_hardlink_cleanup = True
        cleaner._clean_by_download_threshold()
        self.assertEqual(calls["require_flags"][-1], False)

    def test_clean_by_download_threshold_relax_min_days_when_download_hit(self):
        cleaner = self._new_cleaner()
        cleaner._monitor_download = True
        cleaner._monitor_downloader = True
        cleaner._seeding_days = 15
        cleaner._collect_monitor_usage = lambda: {"download": {}, "library": {}}
        cleaner._is_threshold_hit = lambda *args, **kwargs: True
        cleaner._cleanup_by_torrent = lambda *args, **kwargs: None

        min_days_calls = []
        call_idx = {"i": 0}

        def _pick(min_days, skipped_hashes):
            min_days_calls.append(min_days)
            call_idx["i"] += 1
            if call_idx["i"] == 1:
                return None
            if call_idx["i"] == 2:
                return {"hash": "h1", "downloader": "qb", "name": "n1"}
            return None

        cleaner._pick_longest_seeding_torrent = _pick
        cleaner._clean_by_download_threshold()

        self.assertGreaterEqual(len(min_days_calls), 2)
        self.assertEqual(min_days_calls[0], 15)
        self.assertEqual(min_days_calls[1], None)

    def test_flow3_trigger_reasons_collect_all(self):
        cleaner = self._new_cleaner()
        cleaner._monitor_library = True
        cleaner._monitor_download = True
        cleaner._monitor_downloader = True
        cleaner._library_threshold_mode = "size"
        cleaner._download_threshold_mode = "size"
        cleaner._library_threshold_value = 100
        cleaner._download_threshold_value = 100
        cleaner._seeding_days = 10

        cleaner._is_threshold_hit = lambda *args, **kwargs: True
        cleaner._pick_longest_seeding_torrent = lambda *args, **kwargs: {"hash": "x"}

        reasons = cleaner._flow3_trigger_reasons({"library": {}, "download": {}})
        self.assertIn("媒体库目录阈值", reasons)
        self.assertIn("资源目录阈值", reasons)
        self.assertIn("下载器做种时长阈值", reasons)

    def test_clean_by_transfer_history_oldest_run_once(self):
        cleaner = self._new_cleaner()
        cleaner._current_run_freed_bytes = 0
        cleaner._flow3_trigger_reasons = lambda usage: ["资源目录阈值"] if cleaner._current_run_freed_bytes == 0 else []
        cleaner._collect_monitor_usage = lambda: {"download": {}, "library": {}}
        cleaner._is_run_limit_reached = lambda actions: False
        cleaner._pick_oldest_transfer_history = lambda skipped_ids: SimpleNamespace(id=1)
        cleaner._cleanup_by_transfer_history = lambda history, trigger: {"freed_bytes": 123}

        actions = cleaner._clean_by_transfer_history_oldest()
        self.assertEqual(len(actions), 1)
        self.assertEqual(cleaner._current_run_freed_bytes, 123)

    def test_cleanup_by_torrent_scope_requires_mp_history_when_no_hardlink_fallback(self):
        cleaner = self._new_cleaner()
        cleaner._media_servers = ["emby"]
        cleaner._media_libraries = []
        cleaner._clean_media_data = True
        cleaner._force_hardlink_cleanup = False
        cleaner._transfer_oper = SimpleNamespace(list_by_hash=lambda _hash: [])
        cleaner._library_paths = lambda: [Path("/tmp/library")]
        cleaner._media_delete_roots = lambda roots: roots

        result = cleaner._cleanup_by_torrent(
            candidate={"hash": "h1", "downloader": "qb", "name": "n1"},
            trigger="unit-test",
            require_mp_history=False,
        )
        self.assertIsNone(result)

    def test_cleanup_by_torrent_scope_allow_fallback_but_skip_out_of_library(self):
        cleaner = self._new_cleaner()
        cleaner._media_servers = ["emby"]
        cleaner._clean_media_data = True
        cleaner._clean_scrape_data = False
        cleaner._clean_transfer_history = False
        cleaner._clean_download_history = False
        cleaner._clean_downloader_seed = False
        cleaner._force_hardlink_cleanup = True
        cleaner._dry_run = True
        cleaner._transfer_oper = SimpleNamespace(list_by_hash=lambda _hash: [])
        cleaner._count_download_records = lambda _hash: 0
        cleaner._library_paths = lambda: [Path("/tmp/library")]
        cleaner._media_delete_roots = lambda roots: roots
        cleaner._download_file_media_paths = lambda _hash: [Path("/tmp/outside/movie.mkv")]
        cleaner._expand_media_targets_with_hardlinks = lambda media_targets, roots, context, enabled: media_targets

        result = cleaner._cleanup_by_torrent(
            candidate={"hash": "h1", "downloader": "qb", "name": "n1"},
            trigger="unit-test",
            require_mp_history=False,
        )
        self.assertIsNone(result)

    def test_cleanup_by_torrent_scope_allow_fallback_when_target_in_library(self):
        cleaner = self._new_cleaner()
        cleaner._media_servers = ["emby"]
        cleaner._clean_media_data = True
        cleaner._clean_scrape_data = False
        cleaner._clean_transfer_history = False
        cleaner._clean_download_history = False
        cleaner._clean_downloader_seed = False
        cleaner._force_hardlink_cleanup = True
        cleaner._dry_run = True
        cleaner._transfer_oper = SimpleNamespace(list_by_hash=lambda _hash: [])
        cleaner._count_download_records = lambda _hash: 0
        cleaner._is_run_bytes_limit_reached = lambda _b: False
        cleaner._is_daily_bytes_limit_reached = lambda _b: False

        with tempfile.TemporaryDirectory() as temp_dir:
            library_root = Path(temp_dir) / "library"
            library_root.mkdir()
            media_file = library_root / "movie.mkv"
            media_file.write_bytes(b"x")

            cleaner._library_paths = lambda: [library_root]
            cleaner._media_delete_roots = lambda roots: roots
            cleaner._download_file_media_paths = lambda _hash: [media_file]
            cleaner._expand_media_targets_with_hardlinks = lambda media_targets, roots, context, enabled: media_targets

            result = cleaner._cleanup_by_torrent(
                candidate={"hash": "h1", "downloader": "qb", "name": "n1"},
                trigger="unit-test",
                require_mp_history=False,
            )

            self.assertIsNotNone(result)
            self.assertEqual(((result.get("steps") or {}).get("media") or {}).get("planned"), 1)

    def test_download_file_media_paths_filter_media_ext(self):
        cleaner = self._new_cleaner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_file = root / "movie.mkv"
            text_file = root / "readme.txt"
            media_file.write_bytes(b"m")
            text_file.write_text("t", encoding="utf-8")

            cleaner._download_oper = SimpleNamespace(
                get_files_by_hash=lambda _hash: [
                    SimpleNamespace(fullpath=media_file.as_posix(), savepath="", filepath=""),
                    SimpleNamespace(fullpath=text_file.as_posix(), savepath="", filepath=""),
                ]
            )

            paths = cleaner._download_file_media_paths("h1")
            self.assertEqual([p.as_posix() for p in paths], [media_file.as_posix()])

    def test_task_operator_init_exception_is_caught(self):
        cleaner = self._new_cleaner()
        cleaner._transfer_oper = None
        cleaner._download_oper = SimpleNamespace()
        cleaner._mediaserver_oper = SimpleNamespace()

        original_transfer_oper = self.plugin_mod.TransferHistoryOper

        class _BrokenTransferOper:
            def __init__(self):
                raise RuntimeError("init failed")

        self.plugin_mod.TransferHistoryOper = _BrokenTransferOper
        try:
            cleaner._task()
        finally:
            self.plugin_mod.TransferHistoryOper = original_transfer_oper

        run_history = cleaner.get_data("run_history") or []
        self.assertTrue(run_history)
        self.assertEqual((run_history[-1] or {}).get("status"), "failed")

    def test_pick_longest_seeding_torrent_handles_get_completed_torrents_exception(self):
        cleaner = self._new_cleaner()
        original_downloader_helper = self.plugin_mod.DownloaderHelper

        class _BrokenInstance:
            @staticmethod
            def get_completed_torrents():
                raise RuntimeError("downloader error")

        class _FakeDownloaderHelper:
            def get_services(self, name_filters=None):
                return {"qb": SimpleNamespace(instance=_BrokenInstance())}

        self.plugin_mod.DownloaderHelper = _FakeDownloaderHelper
        try:
            result = cleaner._pick_longest_seeding_torrent(min_days=None, skipped_hashes=set())
        finally:
            self.plugin_mod.DownloaderHelper = original_downloader_helper

        self.assertIsNone(result)

    def test_pick_longest_seeding_torrent_supports_case_insensitive_filter(self):
        cleaner = self._new_cleaner()
        cleaner._downloaders = ["tr"]
        original_downloader_helper = self.plugin_mod.DownloaderHelper

        class _TRInstance:
            @staticmethod
            def get_completed_torrents():
                return [{"hash": "h1", "name": "demo", "completion_on": 1}]

        tr_service = SimpleNamespace(instance=_TRInstance())

        class _FakeDownloaderHelper:
            def get_services(self, name_filters=None):
                if name_filters is None:
                    return {"TR": tr_service}
                if name_filters == ["tr"]:
                    return {}
                return {"TR": tr_service} if "TR" in name_filters else {}

        self.plugin_mod.DownloaderHelper = _FakeDownloaderHelper
        try:
            result = cleaner._pick_longest_seeding_torrent(min_days=None, skipped_hashes=set())
        finally:
            self.plugin_mod.DownloaderHelper = original_downloader_helper

        self.assertIsNotNone(result)
        self.assertEqual((result or {}).get("downloader"), "TR")
        self.assertEqual((result or {}).get("hash"), "h1")

    def test_pick_longest_seeding_torrent_logs_summary_when_no_candidate(self):
        cleaner = self._new_cleaner()
        cleaner._downloaders = ["tr"]
        original_downloader_helper = self.plugin_mod.DownloaderHelper
        original_logger = self.plugin_mod.logger
        info_logs = []

        class _TRInstance:
            @staticmethod
            def get_completed_torrents():
                return [{"hash": "h1", "name": "demo", "completion_on": 1}]

        class _FakeDownloaderHelper:
            def get_services(self, name_filters=None):
                return {"tr": SimpleNamespace(instance=_TRInstance())}

        self.plugin_mod.DownloaderHelper = _FakeDownloaderHelper
        self.plugin_mod.logger = SimpleNamespace(
            info=lambda msg, *args, **kwargs: info_logs.append(str(msg)),
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
        )
        try:
            result = cleaner._pick_longest_seeding_torrent(min_days=36500, skipped_hashes=set())
        finally:
            self.plugin_mod.DownloaderHelper = original_downloader_helper
            self.plugin_mod.logger = original_logger

        self.assertIsNone(result)
        self.assertTrue(any("下载器候选扫描无结果" in msg for msg in info_logs))
        self.assertTrue(any("tr:完成1 可选0" in msg for msg in info_logs))

    def test_torrent_hash_supports_mapping_like_hash_string(self):
        class _TorrentLike:
            @staticmethod
            def get(key, default=None):
                data = {"hashString": "h-tr-1"}
                return data.get(key, default)

        self.assertEqual(self.DiskCleaner._torrent_hash(_TorrentLike()), "h-tr-1")

    def test_torrent_seed_seconds_supports_mapping_like_done_date(self):
        class _TorrentLike:
            @staticmethod
            def get(key, default=None):
                data = {"doneDate": 1000}
                return data.get(key, default)

        original_time = self.plugin_mod.time.time
        self.plugin_mod.time.time = lambda: 2000
        try:
            self.assertEqual(self.DiskCleaner._torrent_seed_seconds(_TorrentLike()), 1000)
        finally:
            self.plugin_mod.time.time = original_time

    def test_pick_longest_seeding_torrent_qb_includes_completed_status(self):
        cleaner = self._new_cleaner()
        cleaner._downloaders = ["qb"]
        original_downloader_helper = self.plugin_mod.DownloaderHelper

        calls = {"status": None}

        class _QBInstance:
            @staticmethod
            def get_torrents(status=None, ids=None, tags=None):
                calls["status"] = status
                return ([{"hash": "q1", "name": "qb-demo", "completion_on": 1}], False)

            @staticmethod
            def get_completed_torrents():
                return []

        class _FakeDownloaderHelper:
            def get_services(self, name_filters=None):
                return {"qb": SimpleNamespace(instance=_QBInstance(), type="qbittorrent")}

        self.plugin_mod.DownloaderHelper = _FakeDownloaderHelper
        try:
            result = cleaner._pick_longest_seeding_torrent(min_days=None, skipped_hashes=set())
        finally:
            self.plugin_mod.DownloaderHelper = original_downloader_helper

        self.assertEqual(calls["status"], "completed")
        self.assertEqual((result or {}).get("hash"), "q1")
        self.assertEqual((result or {}).get("downloader"), "qb")

    def test_pick_longest_seeding_torrent_tr_includes_stopped_completed(self):
        cleaner = self._new_cleaner()
        cleaner._downloaders = ["TR"]
        original_downloader_helper = self.plugin_mod.DownloaderHelper

        calls = {"status": None}

        class _TRInstance:
            @staticmethod
            def get_torrents(status=None, ids=None, tags=None):
                calls["status"] = status
                return ([{"hashString": "t1", "name": "tr-demo", "doneDate": 1}], False)

            @staticmethod
            def get_completed_torrents():
                return []

        class _FakeDownloaderHelper:
            def get_services(self, name_filters=None):
                return {"TR": SimpleNamespace(instance=_TRInstance(), type="transmission")}

        self.plugin_mod.DownloaderHelper = _FakeDownloaderHelper
        try:
            result = cleaner._pick_longest_seeding_torrent(min_days=None, skipped_hashes=set())
        finally:
            self.plugin_mod.DownloaderHelper = original_downloader_helper

        self.assertEqual(calls["status"], ["seeding", "seed_pending", "stopped"])
        self.assertEqual((result or {}).get("hash"), "t1")
        self.assertEqual((result or {}).get("downloader"), "TR")

    def test_collect_downloader_overview_stats_contains_total_completed_downloading_paused_and_eligible(self):
        cleaner = self._new_cleaner()
        cleaner._downloaders = ["qb"]
        original_downloader_helper = self.plugin_mod.DownloaderHelper

        class _QBInstance:
            @staticmethod
            def get_torrents(status=None, ids=None, tags=None):
                if status is None:
                    return ([{"hash": "h1"}, {"hash": "h2"}, {"hash": "h3"}, {"hash": "h4"}, {"hash": "h5"}], False)
                if status == "completed":
                    return ([{"hash": "h1", "completion_on": 1}, {"hash": "h2", "completion_on": 2}, {"hash": "h3", "completion_on": 3}], False)
                if status == "downloading":
                    return ([{"hash": "h4"}], False)
                if status == "paused":
                    return ([{"hash": "h5"}], False)
                return ([], False)

            @staticmethod
            def get_downloading_torrents():
                return [{"hash": "h4"}]

            @staticmethod
            def get_completed_torrents():
                return [{"hash": "h1", "completion_on": 1}, {"hash": "h2", "completion_on": 2}, {"hash": "h3", "completion_on": 3}]

        class _FakeDownloaderHelper:
            def get_services(self, name_filters=None):
                return {"qb": SimpleNamespace(instance=_QBInstance(), type="qbittorrent")}

        self.plugin_mod.DownloaderHelper = _FakeDownloaderHelper
        try:
            summary, details = cleaner._collect_downloader_overview_stats(min_days=None, skipped_hashes=set())
        finally:
            self.plugin_mod.DownloaderHelper = original_downloader_helper

        self.assertEqual(summary.get("total"), 5)
        self.assertEqual(summary.get("completed"), 3)
        self.assertEqual(summary.get("downloading"), 1)
        self.assertEqual(summary.get("paused"), 1)
        self.assertEqual(summary.get("eligible"), 3)
        self.assertTrue(details)
        self.assertEqual((details[0] or {}).get("name"), "qb")

    def test_log_downloader_overview_stats_prints_compact_summary(self):
        cleaner = self._new_cleaner()
        original_collect = cleaner._collect_downloader_overview_stats
        original_logger = self.plugin_mod.logger
        info_logs = []

        cleaner._collect_downloader_overview_stats = lambda min_days, skipped_hashes: (
            {"total": 5, "completed": 3, "downloading": 1, "paused": 1, "eligible": 2},
            [{"name": "TR", "total": 5, "completed": 3, "downloading": 1, "paused": 1, "eligible": 2}],
        )
        self.plugin_mod.logger = SimpleNamespace(
            info=lambda msg, *args, **kwargs: info_logs.append(str(msg)),
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
        )
        try:
            cleaner._log_downloader_overview_stats(min_days=None, skipped_hashes=set())
        finally:
            cleaner._collect_downloader_overview_stats = original_collect
            self.plugin_mod.logger = original_logger

        self.assertTrue(any("流程2下载器任务统计：总5 完成3 下载中1 暂停1 符合条件2" in msg for msg in info_logs))

    def test_calc_usage_handles_space_usage_exception(self):
        cleaner = self._new_cleaner()
        usage = cleaner._calc_usage([Path("/tmp/noop")])
        self.assertEqual((usage or {}).get("total"), 0)
        self.assertEqual((usage or {}).get("free"), 0)

    def test_run_retry_job_wraps_retry_payload_exception(self):
        cleaner = self._new_cleaner()
        cleaner._retry_max_attempts = 3
        cleaner._retry_torrent_payload = lambda payload: (_ for _ in ()).throw(RuntimeError("retry boom"))

        action, next_job = cleaner._run_retry_job(
            {"attempt": 0, "payload": {"mode": "torrent", "trigger": "t", "target": "x"}}
        )

        self.assertIsNotNone(action)
        self.assertIn("失败", (action or {}).get("action", ""))
        self.assertIsNotNone(next_job)
        self.assertEqual((next_job or {}).get("attempt"), 1)

    def test_count_download_records_handles_oper_exception(self):
        cleaner = self._new_cleaner()
        cleaner._download_oper = SimpleNamespace(
            get_by_hash=lambda _hash: (_ for _ in ()).throw(RuntimeError("db err")),
            get_files_by_hash=lambda _hash: [],
        )
        self.assertEqual(cleaner._count_download_records("h1"), 0)


if __name__ == "__main__":
    unittest.main()
