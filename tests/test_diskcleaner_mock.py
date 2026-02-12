import enum
import importlib.util
import os
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
    core_config_mod.settings = SimpleNamespace(TZ="Asia/Shanghai", API_TOKEN="unit_test_token")
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


if __name__ == "__main__":
    unittest.main()
