"""在真实 V3 类型与隔离数据库上验证磁盘清理的兼容路径。"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests._bootstrap import prepare

prepare()

from app.application import query as query_module  # noqa: E402
from app.application.query import DataQueryService  # noqa: E402
from app.db.adapters.query import SqlAlchemyDataQueryAdapter  # noqa: E402
from app.db.models.transferhistory import TransferHistory  # noqa: E402
from app.db.oper.transferhistory import TransferHistoryOper  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402
from app.plugins.diskcleaner import DiskCleaner  # noqa: E402
from app.plugins import diskcleaner as plugin_module  # noqa: E402
from app.sdk.plugin import base as plugin_base  # noqa: E402


@pytest.fixture
def cleaner(monkeypatch):
    """保留真实插件构造，仅替换本用例不需要的 Chain 外部服务。"""
    chain = SimpleNamespace(tmdb_info=Mock(return_value={"status": "Ended"}))
    monkeypatch.setattr(plugin_base, "PluginChain", lambda: chain)
    plugin = DiskCleaner()
    plugin._tv_complete_only = True
    plugin._tv_end_state_cache = {}
    plugin._playback_ts_cache = {}
    plugin._prefer_playback_history = True
    plugin._mediaserver_chain = None
    plugin._media_cleanup_priority = "tv"
    plugin._path_block_keywords = []
    plugin._protect_recent_days = 0
    yield plugin
    plugin.stop_service()


@pytest.mark.v3
def test_ended_tmdb_series_can_be_cleaned_without_mixing_other_sources(cleaner):
    """V3 身份允许已完结 TMDB 剧集，其他来源继续保护性跳过。"""
    tmdb = SimpleNamespace(type="电视剧", media_source="themoviedb", media_id="101")
    douban = SimpleNamespace(type="电视剧", media_source="douban", media_id="101")

    assert cleaner._is_tv_cleanup_allowed(tmdb)
    assert not cleaner._is_tv_cleanup_allowed(douban)
    cleaner.chain.tmdb_info.assert_called_once()
    assert cleaner.chain.tmdb_info.call_args.kwargs["tmdbid"] == 101


@pytest.mark.v3
def test_transfer_priority_reads_real_v3_query_with_stable_paging(cleaner, monkeypatch):
    """整理记录优先流程使用 V3 查询服务并保留媒体类型优先级。"""
    adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    service = DataQueryService(
        subscriptions=adapter,
        histories=adapter,
        async_executor=SimpleNamespace(),
    )
    monkeypatch.setattr(query_module, "_configured_data_query_service", service)
    with SessionFactory() as session:
        session.query(TransferHistory).delete()
        movies = [TransferHistory(
            status=True, type="电影", title=f"movie-{index}",
            dest=f"/media/movie-{index}.mkv", date="2020-01-01 00:00:00",
            media_source="themoviedb", media_id=str(index + 1000),
        ) for index in range(201)]
        tv = TransferHistory(
            status=True, type="电视剧", title="series", dest="/media/tv.mkv",
            date="2020-01-02 00:00:00", media_source="themoviedb", media_id="101",
        )
        session.add_all([*movies, tv])
        session.commit()
        tv_id = tv.id

    chosen = cleaner._pick_oldest_transfer_history(set())

    assert chosen is not None
    assert chosen.id == tv_id
    assert chosen.media_source.value == "themoviedb"


@pytest.mark.v3
def test_playback_time_uses_v3_identity_and_iso_time(cleaner):
    """媒体库匹配按来源和 ID 查询，并让播放时间参与候选排序。"""
    media_item = SimpleNamespace(server="plex", item_id="item-1")
    cleaner._mediaserver_oper = SimpleNamespace(exists=Mock(return_value=media_item))
    cleaner._mediaserver_chain = SimpleNamespace(
        iteminfo=Mock(return_value=SimpleNamespace(
            user_state=SimpleNamespace(last_played_date="2026-10-20T08:00:00+08:00")
        ))
    )
    history = SimpleNamespace(
        type="电视剧", media_source="themoviedb", media_id="101",
        seasons="S01", title="series", year="2020",
    )

    value = cleaner._get_history_playback_ts(history)

    assert value == int(datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp())
    assert cleaner._mediaserver_oper.exists.call_args.kwargs["media_id"] == "101"
    assert cleaner._mediaserver_oper.exists.call_args.kwargs["media_source"].value == "themoviedb"
    assert cleaner._parse_datetime_to_ts(str(value)) == value
    assert cleaner._parse_datetime_to_ts("2026-10-20 08:00:00") is not None


@pytest.mark.v3
def test_enabled_plugin_registers_automatic_job(cleaner, monkeypatch):
    """保存启用配置后，宿主可见的插件实例实际注册并启动定时任务。"""
    monkeypatch.setattr(cleaner, "update_config", lambda _config: True)
    cleaner.init_plugin({"enabled": True, "cron": "*/5 * * * *", "dry_run": True})

    assert cleaner.get_state()
    assert cleaner._scheduler is not None
    assert cleaner._scheduler.running
    assert any(job.func.__self__ is cleaner for job in cleaner._scheduler.get_jobs())


@pytest.mark.v3
@pytest.mark.parametrize("flow", ["flow_library_mp_downloader", "flow_transfer_oldest"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_media_flow_runs_on_v3_host(cleaner, monkeypatch, tmp_path: Path, flow: str, dry_run: bool):
    """真实 V3 查询下，演练保留文件，执行仅删除隔离目录中的目标。"""
    adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    monkeypatch.setattr(
        query_module,
        "_configured_data_query_service",
        DataQueryService(
            subscriptions=adapter,
            histories=adapter,
            async_executor=SimpleNamespace(),
        ),
    )
    media_file = tmp_path / "series" / "Season 01" / "episode.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"media")
    with SessionFactory() as session:
        session.query(TransferHistory).delete()
        session.add(TransferHistory(
            status=True, type="电视剧", title="series", dest=str(media_file),
            date="2020-01-01 00:00:00", media_source="themoviedb", media_id="101",
            seasons="S01",
        ))
        session.commit()

    monkeypatch.setattr(cleaner, "update_config", lambda _config: True)
    monkeypatch.setattr(
        plugin_module.DirectoryHelper,
        "get_local_library_dirs",
        lambda _self: [SimpleNamespace(library_path=str(tmp_path))],
    )
    monkeypatch.setattr(
        plugin_module.DirectoryHelper,
        "get_local_download_dirs",
        lambda _self: [],
    )
    gib = 1024 ** 3
    monkeypatch.setattr(plugin_module.SystemUtils, "space_usage", lambda _paths: (100 * gib, gib))
    cleaner.init_plugin({
        "enabled": False,
        "dry_run": dry_run,
        "trigger_flow": flow,
        "library_threshold_value": "10G",
        "protect_recent_days": 0,
    })

    cleaner._task(manual_run=True)

    assert media_file.exists() is dry_run
    assert cleaner.get_data("last_run_at")
    run_history = cleaner.get_data("run_history") or []
    assert run_history and run_history[-1]["status"] == "completed"
    assert run_history[-1]["action_count"] >= 1
    assert str(media_file.parent) in str(run_history[-1]["items"])


@pytest.mark.v3
@pytest.mark.parametrize("flow", ["flow_library_mp_downloader", "flow_transfer_oldest"])
def test_recent_episode_blocks_season_deletion(cleaner, monkeypatch, tmp_path: Path, flow: str):
    """整季目录中只要有近期整理的剧集，执行模式也不能删除整季。"""
    season_dir = tmp_path / "series" / "Season 01"
    season_dir.mkdir(parents=True)
    old_file = season_dir / "E01.mkv"
    recent_file = season_dir / "E02.mkv"
    old_file.write_bytes(b"old")
    recent_file.write_bytes(b"recent")
    with SessionFactory() as session:
        session.query(TransferHistory).delete()
        session.add_all([
            TransferHistory(
                status=True, type="电视剧", dest=str(old_file), title="series",
                date="2020-01-01 00:00:00", seasons="S01",
                media_source="themoviedb", media_id="101",
            ),
            TransferHistory(
                status=True, type="电视剧", dest=str(recent_file), title="series",
                date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), seasons="S01",
                media_source="themoviedb", media_id="101",
            ),
        ])
        session.commit()

    adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    monkeypatch.setattr(
        query_module, "_configured_data_query_service",
        DataQueryService(subscriptions=adapter, histories=adapter, async_executor=SimpleNamespace()),
    )
    monkeypatch.setattr(cleaner, "update_config", lambda _config: True)
    monkeypatch.setattr(
        plugin_module.DirectoryHelper, "get_local_library_dirs",
        lambda _self: [SimpleNamespace(library_path=str(tmp_path))],
    )
    monkeypatch.setattr(plugin_module.DirectoryHelper, "get_local_download_dirs", lambda _self: [])
    gib = 1024 ** 3
    monkeypatch.setattr(plugin_module.SystemUtils, "space_usage", lambda _paths: (100 * gib, gib))
    cleaner.init_plugin({
        "enabled": False, "dry_run": False,
        "trigger_flow": flow,
        "library_threshold_value": "10G", "protect_recent_days": 30,
    })

    cleaner._task(manual_run=True)

    assert old_file.exists() and recent_file.exists()
    assert (cleaner.get_data("run_history") or [])[-1]["action_count"] == 0


@pytest.mark.v3
@pytest.mark.parametrize("recent_sibling", [False, True])
def test_downloader_flow_respects_recent_season_history(cleaner, monkeypatch, tmp_path: Path, recent_sibling: bool):
    """下载器优先流程可删除旧季，新增近期剧集时保留整季。"""
    season_dir = tmp_path / "series" / "Season 01"
    season_dir.mkdir(parents=True)
    old_file = season_dir / "E01.mkv"
    old_file.write_bytes(b"old")
    recent_file = season_dir / "E02.mkv"
    with SessionFactory() as session:
        session.query(TransferHistory).delete()
        session.add(TransferHistory(
            status=True, type="电视剧", dest=str(old_file), title="series",
            date="2020-01-01 00:00:00", seasons="S01",
            media_source="themoviedb", media_id="101", download_hash="old-hash",
        ))
        if recent_sibling:
            recent_file.write_bytes(b"recent")
            session.add(TransferHistory(
                status=True, type="电视剧", dest=str(recent_file), title="series",
                date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), seasons="S01",
                media_source="themoviedb", media_id="101", download_hash="new-hash",
            ))
        session.commit()

    adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    monkeypatch.setattr(
        query_module, "_configured_data_query_service",
        DataQueryService(subscriptions=adapter, histories=adapter, async_executor=SimpleNamespace()),
    )
    monkeypatch.setattr(
        plugin_module.DirectoryHelper, "get_local_library_dirs",
        lambda _self: [SimpleNamespace(library_path=str(tmp_path))],
    )
    cleaner._transfer_oper = TransferHistoryOper()
    cleaner._dry_run = False
    cleaner._protect_recent_days = 30
    cleaner._clean_scrape_data = False
    cleaner._clean_downloader_seed = False
    cleaner._clean_transfer_history = False
    cleaner._clean_download_history = False

    result = cleaner._cleanup_by_torrent(
        candidate={"hash": "old-hash", "downloader": "qbittorrent", "name": "series"},
        trigger="测试下载器流程",
    )

    assert old_file.exists() is recent_sibling
    if recent_sibling:
        assert recent_file.exists()
        assert result is None
    else:
        assert result is not None


@pytest.mark.v3
@pytest.mark.parametrize("mode", ["media", "torrent"])
def test_retry_rechecks_episode_added_after_enqueue(cleaner, monkeypatch, tmp_path: Path, mode: str):
    """补偿任务入队后新增剧集时，重试也须保留整季和关联任务。"""
    season_dir = tmp_path / "series" / "Season 01"
    season_dir.mkdir(parents=True)
    old_file = season_dir / "E01.mkv"
    recent_file = season_dir / "E02.mkv"
    old_file.write_bytes(b"old")
    cleaner._protect_recent_days = 30
    cleaner.save_data("retry_queue", [])
    payload = {
        "mode": mode,
        "retry_key": "season-01",
        "target": str(old_file),
        "media_path": str(old_file),
        "media_targets": [str(season_dir)],
        "sidecars": [],
        "sidecar_targets": [],
        "history_dest": str(old_file),
        "history_dests": [str(old_file)],
        "download_hash": "hash-old",
        "downloader": "qbittorrent",
        "failed_steps": ["media", "downloader"],
    }
    cleaner._enqueue_retry(payload)
    recent_file.write_bytes(b"recent")
    with SessionFactory() as session:
        session.query(TransferHistory).delete()
        session.add(TransferHistory(
            status=True, type="电视剧", dest=str(recent_file), title="series",
            date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), seasons="S01",
            media_source="themoviedb", media_id="101",
        ))
        session.commit()

    adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    monkeypatch.setattr(
        query_module, "_configured_data_query_service",
        DataQueryService(subscriptions=adapter, histories=adapter, async_executor=SimpleNamespace()),
    )
    delete_item = Mock()
    delete_torrent = Mock()
    monkeypatch.setattr(cleaner, "_delete_local_item", delete_item)
    monkeypatch.setattr(cleaner, "_delete_torrent", delete_torrent)

    actions = cleaner._process_retry_queue(limit=1)

    assert old_file.exists() and recent_file.exists()
    delete_item.assert_not_called()
    delete_torrent.assert_not_called()
    assert len(actions) == 1
    assert not cleaner.get_data("retry_queue")


@pytest.mark.v3
def test_tmdb_failure_is_retried_on_next_scheduled_run(cleaner, monkeypatch, tmp_path: Path):
    """第一次元数据请求失败后，下一轮仍会重查并清理已完结剧集。"""
    media_file = tmp_path / "series" / "Season 01" / "E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"media")
    with SessionFactory() as session:
        session.query(TransferHistory).delete()
        session.add(TransferHistory(
            status=True, type="电视剧", dest=str(media_file), title="series",
            date="2020-01-01 00:00:00", seasons="S01",
            media_source="themoviedb", media_id="101",
        ))
        session.commit()

    adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    monkeypatch.setattr(
        query_module, "_configured_data_query_service",
        DataQueryService(subscriptions=adapter, histories=adapter, async_executor=SimpleNamespace()),
    )
    monkeypatch.setattr(cleaner, "update_config", lambda _config: True)
    monkeypatch.setattr(
        plugin_module.DirectoryHelper, "get_local_library_dirs",
        lambda _self: [SimpleNamespace(library_path=str(tmp_path))],
    )
    monkeypatch.setattr(plugin_module.DirectoryHelper, "get_local_download_dirs", lambda _self: [])
    gib = 1024 ** 3
    monkeypatch.setattr(plugin_module.SystemUtils, "space_usage", lambda _paths: (100 * gib, gib))
    cleaner.chain.tmdb_info.side_effect = [None, {"status": "Ended"}]
    cleaner.init_plugin({
        "enabled": False, "dry_run": True,
        "trigger_flow": "flow_library_mp_downloader",
        "library_threshold_value": "10G", "protect_recent_days": 0,
    })

    cleaner._task(manual_run=True)
    first = (cleaner.get_data("run_history") or [])[-1]
    cleaner._task(manual_run=True)
    second = (cleaner.get_data("run_history") or [])[-1]

    assert first["action_count"] == 0
    assert second["action_count"] >= 1
    assert cleaner.chain.tmdb_info.call_count == 2
