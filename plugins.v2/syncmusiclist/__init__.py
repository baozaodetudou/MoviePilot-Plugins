import pytz
from datetime import datetime, timedelta
from threading import Event
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.helper.service import ServiceConfigHelper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.plugins.syncmusiclist.QQmusic import QQMusicApi
from app.plugins.syncmusiclist.cloudmusic import CloudMusic
from app.plugins.syncmusiclist.emby_music import EmbyMusic
from app.plugins.syncmusiclist.plex_music import PlexMusic


class SyncMusicList(_PluginBase):
    # 插件名称
    plugin_name = "歌单同步工具"
    # 插件描述
    plugin_desc = "同步QQ&网易云歌单到plex&emby。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/baozaodetudou/MoviePilot-Plugins/main/icons/music.png"
    # 插件版本
    plugin_version = "7.2.1"
    # 插件作者
    plugin_author = "逗猫"
    # 作者主页
    author_url = "https://github.com/baozaodetudou"
    # 插件配置项ID前缀
    plugin_config_prefix = "music_"
    # 加载顺序
    plugin_order = 17
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler: Optional[BackgroundScheduler] = None
    # 启动
    _enabled = False
    # 运行一次
    _onlyonce = False
    # 定时
    _cron = None
    # 媒体服务器
    _media_server = []
    # 精准匹配开关
    _exact_match = True
    # 网易云登录设置, 同步自己歌单需要登录，不是自己不需要登录
    _wylogin_user = ''
    _wylogin_password = ''
    #
    _wy_daily_list = False
    _wy_daily_song = False

    # 同步列表
    _wymusic_paths = ""
    _qqmusic_paths = ""
    # 退出事件
    _event = Event()

    def init_plugin(self, config: dict = None):
        # docker用默认路径
        _path = self.get_data_path()
        self.cm = CloudMusic(_path)
        self._wylogin_capt = False
        self._wylogin_pass = False
        # 获取用户名信息
        self._username = self.cm.login_status()
        mediaserver_configs = ServiceConfigHelper.get_mediaserver_configs()
        self.media_config = {conf.name: conf for conf in mediaserver_configs if conf.enabled}
        self.media_list = [{'title': conf.name, 'value': conf.name} for conf in mediaserver_configs if conf.enabled]

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._media_server = config.get("media_server") or []
            self._exact_match = config.get("exact_match") or True
            self._wylogin_user = config.get("wylogin_user") or ""
            self._wylogin_password = config.get("wylogin_password") or ""
            self._wymusic_paths = config.get("wymusic_paths") or ""
            self._qqmusic_paths = config.get("qqmusic_paths") or ""
            self._wy_daily_list = config.get("wy_daily_list") or False
            self._wy_daily_song = config.get("wy_daily_song") or False


        if self._username:
            self._wylogin_status = False
            self._wyy_text = f"当前网抑云账号{self._username}登录成功，可以使用用户相关功能"
        else:
            if self._wylogin_user and self._wylogin_password:
                self.cm.login(self._wylogin_user, self._wylogin_password)
                self._username = self.cm.login_status()
                if self._username:
                    self._wylogin_status = False
                    self._wyy_text = f"当前网抑云账号{self._username}登录成功，可以使用用户相关功能"
                else:
                    self._wylogin_status = True
                    self._wyy_text = f"当前未登录网抑云账号,部分功能受限,请点击下方登录"


        # 停止现有任务
        self.stop_service()

        # 启动定时任务 & 立即运行一次
        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                logger.info(f"歌单同步服务启动，周期：{self._cron}")
                try:
                    self._scheduler.add_job(func=self.__run_sync_paylist,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="歌单同步")
                except Exception as e:
                    logger.error(f"歌单同步服务启动失败，原因：{str(e)}")
                    self.systemmessage.put(f"歌单同步服务启动失败，原因：{str(e)}")
            else:
                logger.info(f"歌单同步服务启动，周期：每天七点执行")
                self._scheduler.add_job(func=self.__run_sync_paylist,
                                        trigger=CronTrigger.from_crontab("0 7 * * *"),
                                        name="歌单同步")
            if self._onlyonce:
                logger.info(f"歌单同步服务，立即运行一次")
                self._scheduler.add_job(func=self.__run_sync_paylist, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="歌单同步")
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "enabled": self._enabled,
                    "cron": self._cron,
                    "media_server": self._media_server,
                    "exact_match": self._exact_match,
                    "wylogin_user": self._wylogin_user,
                    "wylogin_password": self._wylogin_password,
                    "wymusic_paths": self._wymusic_paths,
                    "qqmusic_paths": self._qqmusic_paths,
                    "wy_daily_list": self._wy_daily_list,
                    "wy_daily_song": self._wy_daily_song,
                })
            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        self._username = self.cm.login_status()
        if self._username:
            self._wylogin_status = False
            self._wyy_text = f"当前网抑云账号{self._username}登录成功，可以使用用户相关功能"
        else:
            self._wylogin_status = True
            self._wyy_text = f"当前未登录网抑云账号,部分功能受限,请选择对应方式登录"
        self._login_status = not self._wylogin_status
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'media_server',
                                            'label': '媒体服务器',
                                            'items': self.media_list
                                        }
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动, 建议每天一次'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'exact_match',
                                            'label': '精准匹配(同时匹配歌曲名称和歌手)',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'qqmusic_paths',
                                            'label': 'QQ音乐歌单同步设置',
                                            'rows': 4,
                                            'placeholder':
                                                '一行一个歌单配置留空不启用 \n'
                                                '默认格式：QQ音乐歌单id:plex/emby播放列表名称\n'
                                                'eg: 2362260213:经典歌曲\n'
                                                'emby多用户格式：QQ音乐歌单id:plex/emby播放列表名称:emby用户名 \n'
                                                'eg: 2362260213:经典歌曲:doumao\n'
                                                'emby多用户格式：QQ音乐歌单id:plex/emby播放列表名称:emby1,emby2 \n'
                                                'eg: 2362260213:经典歌曲:doumao,tudou\n'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'title': '账号状态',
                                            'text': self._wyy_text
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        "props": {
                            "model": "wylogin_status",
                        },
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCheckboxBtn',
                                        'props': {
                                            'model': 'wylogin_pass',
                                            'label': '使用密码登录(适用邮箱或者手机号)'
                                        }
                                    }
                                ]
                            },
                            # {
                            #     'component': 'VCol',
                            #     'props': {
                            #         'cols': 12,
                            #         'md': 6
                            #     },
                            #     'content': [
                            #         {
                            #             'component': 'VCheckboxBtn',
                            #             'props': {
                            #                 'model': 'wylogin_capt',
                            #                 'label': '使用验证码登录(适用手机号)'
                            #             }
                            #         }
                            #     ]
                            # }
                        ]
                    },
                    {
                        "component": "VDialog",
                        "props": {
                            "model": "wylogin_capt",
                            "overlay-class": "v-dialog--scrollable",
                            "content-class": "v-card v-card--density-default v-card--variant-elevated rounded-t",
                            'cols': 30,
                            'md': 12
                        },
                        "content": [
                            {
                                'component': 'VRow',
                                'content': [
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VTextField',
                                                'props': {
                                                    'model': 'wylogin_user',
                                                    'label': '手机号',
                                                    'placeholder': '',
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VSwitch',
                                                'props': {
                                                    'model': 'capt_swich',
                                                    'label': '获取验证码',
                                                }
                                            }
                                        ]
                                    },
                                ]
                            },
                            {
                                'component': 'VRow',
                                'content': [
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VTextField',
                                                'props': {
                                                    'model': 'wylogin_password',
                                                    'label': '验证码',
                                                    'placeholder': '',
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VSwitch',
                                                'props': {
                                                    'model': 'login_swich',
                                                    'label': '登录',
                                                }
                                            }
                                        ]
                                    },
                                ]
                            },
                        ]
                    },
                    {
                        "component": "VDialog",
                        "props": {
                            "model": "wylogin_pass",
                            "max-width": "65rem",
                            "overlay-class": "v-dialog--scrollable",
                            "content-class": "v-card v-card--density-default v-card--variant-elevated rounded-t",
                        },
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "title": "网抑云配置"
                                },
                                'content': [
                                    {
                                        "component": "VDialogCloseBtn",
                                        "props": {
                                            "model": "wylogin_pass"
                                        }
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {},
                                        'content': [
                                            {
                                                'component': 'VRow',
                                                'content': [
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 12,
                                                            'md': 4
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VTextField',
                                                                'props': {
                                                                    'model': 'wylogin_user',
                                                                    'label': '手机号/邮箱',
                                                                    'placeholder': '',
                                                                }
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 12,
                                                            'md': 4
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VTextField',
                                                                'props': {
                                                                    'model': 'wylogin_password',
                                                                    'label': '密码',
                                                                    'placeholder': '',
                                                                    'type': 'password',
                                                                }
                                                            }
                                                        ]
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VRow',
                                                'content': [
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 12,
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VAlert',
                                                                'props': {
                                                                    'type': 'info',
                                                                    'variant': 'tonal'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'span',
                                                                        'text': '注意：输入信息后关闭窗口点击保存生效'
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    },

                                ]
                            },

                        ]
                    },
                    {
                        'component': 'VRow',
                        "props": {
                            "model": "login_status",
                        },
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'wy_daily_song',
                                            'label': '每日推荐歌曲',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'wy_daily_list',
                                            'label': '每日推荐歌单',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'wymusic_paths',
                                            'label': '网易云歌单同步设置',
                                            'rows': 4,
                                            'placeholder':
                                                '一行一个歌单配置留空不启用 \n'
                                                '默认格式：QQ音乐歌单id:plex/emby播放列表名称\n'
                                                'eg: 2362260213:经典歌曲\n'
                                                'emby多用户格式：QQ音乐歌单id:plex/emby播放列表名称:emby用户名 \n'
                                                'eg: 2362260213:经典歌曲:doumao\n'
                                                'emby多用户格式：QQ音乐歌单id:plex/emby播放列表名称:emby1,emby2 \n'
                                                'eg: 2362260213:经典歌曲:doumao,tudou\n'
                                        }
                                    }
                                ]
                            }
                        ]
                    },

                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'title': '使用说明:',
                                            'text':
                                                '0. 耗时很长，建议每天一次即可，短时间重复运行会卡死; \n'
                                                '1. 网抑云登录后支持每日推荐歌单的递增; \n'
                                                '2. plex/emby服务器中存在音乐类型的库; \n'
                                                '3. plex/emby的播放列表需要提前创建好并且里边至少有一首歌曲; \n'
                                                '4. 如不存在会自动创建歌单, 如库中没符合的歌曲会创建失败; \n'
                                                '5. 歌单同步只会搜索已存在歌曲进行添加,不会自动下载; \n'
                                                '6. 歌曲匹配是模糊匹配只匹配曲名不匹配歌手; \n'
                                                '7. 精准匹配打开后先进行歌曲搜索然后通过歌手来进行过滤; \n'
                                                '8. emby支持多用户设置，plex自带分享无需创建; \n'
                                                '9. v2版本记得要启动媒体服务器才能使用; \n'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "cron": "0 0 */7 * *",
            "mode": "",
            "scraper_paths": "",
            "err_hosts": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def __run_sync_paylist(self):
        """
        开始同步歌单
        """
        global pm, em
        emby_users = []
        if not self._wymusic_paths and not self._qqmusic_paths  and not self._username:
            logger.info("同步配置为空,不进行处理。告退......")
            return
        if not self._media_server:
            logger.info("没有可用的媒体服务器,不进行处理。告退......")
            return
        qq = QQMusicApi()
        # 获取同步列表信息

        qqmusic_paths = self._qqmusic_paths.split("\n") if self._qqmusic_paths else []
        wymusic_paths = self._wymusic_paths.split("\n") if self._wymusic_paths else []

        for name in self._media_server:
            server = self.media_config.get(name)
            if not server:
                continue
            config = server.config
            if server.type == 'plex':
                pm = PlexMusic(host=config['host'], token=config['token'])
                pm.get_music_library()
                pm.get_playlists()
            elif server.type == 'emby':
                em = EmbyMusic(host=config['host'], apikey=config['apikey'])
                emby_users = [em.default_user]
                logger.info('媒体服务器设置包含Emby服务器')
            else:
                continue
            for path in qqmusic_paths:
                data_list = path.split(':')
                if len(data_list) == 2:
                    qq_play_id, media_playlist = data_list[0], data_list[1]
                elif len(data_list) == 3:
                    qq_play_id, media_playlist, emby_users = data_list[0], data_list[1], data_list[2]
                    emby_users = emby_users.split(',')
                else:
                    logger.warn(f"QQ音乐歌单同步设置配置不规范,请认真检查修改")
                    return
                logger.info(f"QQ歌单id: {qq_play_id}, 媒体库播放列表名称: {media_playlist}")
                if qq_play_id and media_playlist:
                    qq_tracks = qq.get_playlist_by_id(qq_play_id)
                    logger.info(f"QQ歌单 {qq_play_id} 获取歌曲[{len(qq_tracks)}]首,列表为: {qq_tracks}")
                    if server.type == 'plex':
                        self.__t_plex(pm, qq_tracks, media_playlist)
                    elif server.type == 'emby':
                        logger.info(
                            f"QQ歌单id: {qq_play_id}, 为Emby用户[{emby_users}]更新媒体库播放列表名称: {media_playlist}")
                        self.__t_emby(em, qq_tracks, media_playlist, emby_users)
                else:
                    logger.warn(f"QQ音乐歌单同步设置配置不规范,请认真检查修改")
            for path in wymusic_paths:
                data_list = path.split(':')
                if len(data_list) == 2:
                    wy_play_id, media_playlist = data_list[0], data_list[1]
                elif len(data_list) == 3:
                    wy_play_id, media_playlist, emby_users = data_list[0], data_list[1], data_list[2]
                    emby_users = emby_users.split(',')
                else:
                    logger.warn(f"网易云歌单同步设置配置不规范,请认真检查修改")
                    return
                logger.info(f"网易云歌单id: {wy_play_id}, 媒体库播放列表名称: {media_playlist}")
                self.cm_emby_plex(wy_play_id, media_playlist, emby_users, server.type)

            if self._username:
                # 每日推荐歌单
                if self._wy_daily_list:
                    try:
                        datas = self.cm.get_list_days()
                        for data in datas:
                            wy_play_id, media_playlist = data[0], data[1]
                            self.cm_emby_plex(wy_play_id, media_playlist, emby_users, server.type)
                    except Exception as e:
                        logger.error(e)
                        logger.error(f"每日推荐歌单获取失败")
                # 每日歌曲推荐
                if self._wy_daily_song:
                    try:
                        wy_tracks =  self.cm.get_song_daily()
                        playlist = "每日歌曲推荐"
                        logger.info(f"网易云歌单 {playlist} 获取歌曲[{len(wy_tracks)}]首,列表为: {wy_tracks}")
                        if server.type == 'plex':
                            self.__t_plex(pm, wy_tracks, playlist)
                        elif server.type == 'emby':
                            logger.info(
                                f"网易云歌单: {wy_tracks}, 为Emby用户{emby_users}更新媒体库播放列表名称: {playlist}")
                            self.__t_emby(em, wy_tracks, playlist, emby_users)
                    except Exception as e:
                        logger.error(e)
                        logger.error(f"每日推荐更新失败")
        return

    def cm_emby_plex(self, wy_play_id, media_playlist, emby_users, type):
        if wy_play_id and media_playlist:
            wy_tracks = self.cm.songofplaylist(wy_play_id)
            if not wy_tracks:
                logger.error("网易云歌单获取失败，请登录账号重试")
            else:
                logger.info(f"网易云歌单 {wy_play_id} 获取歌曲[{len(wy_tracks)}]首,列表为: {wy_tracks}")
                if type== 'plex':
                    self.__t_plex(pm, wy_tracks, media_playlist)
                elif type == 'emby':
                    logger.info(
                        f"网易云歌单: {wy_tracks}, 为Emby用户{emby_users}更新媒体库播放列表名称: {media_playlist}")
                    self.__t_emby(em, wy_tracks, media_playlist, emby_users)
        else:
            logger.warn(f"网易云音乐歌单同步设置配置不规范,请认真检查修改")

    def __t_emby(self, em, t_tracks, media_playlist, emby_users=None):
        if emby_users is None:
            emby_users = [em.default_user]
        one_user = emby_users[0]
        other_users = emby_users[1:]
        em.user = em.get_user(one_user)
        em.get_music_library()
        logger.info("Emby开始同步歌单,涉及搜索时间较长请耐心等待.......")
        playlist_id, music_ids, music_names = em.get_tracks_by_playlist(media_playlist)
        logger.info(f"Emby歌单[{media_playlist}]现有歌曲[{len(music_names)}]首,列表为: {music_names}")
        if music_names:
            new_tracks = [i for i in t_tracks if i[0] not in music_names]
        else:
            new_tracks = t_tracks
        # new_tracks = list(set(i[0] for i in t_tracks) - set(music_names))
        tracks = em.mul_search_music(new_tracks, self._exact_match)
        if playlist_id:
            ids = [i for i in tracks if i not in music_ids]
            em.set_tracks_to_playlist(playlist_id, ','.join(ids))
        else:
            em.create_playlist(media_playlist, ','.join(tracks))
        _, new_music_ids, _ = em.get_tracks_by_playlist(media_playlist)
        for user in other_users:
            em.user = em.get_user(user)
            em.get_music_library()
            user_playlist_id, user_music_ids, user_music_names = em.get_tracks_by_playlist(media_playlist)
            if user_playlist_id:
                new_ids = [i for i in new_music_ids if i not in user_music_ids]
                em.set_tracks_to_playlist(user_playlist_id, ','.join(new_ids), user)
            else:
                em.create_playlist(media_playlist, ','.join(new_music_ids), user)

        logger.info("Emby同步歌单完成,感谢耐心等待.......")
        logger.info("歌单同步完成，END")
        return

    def __t_plex(self, pm, t_tracks, media_playlist):
        logger.info("Plex开始同步歌单,涉及搜索时间较长请耐心等待.......")
        add_tracks = []
        old_tracks = []
        plex_tracks = pm.get_tracks_by_playlist(media_playlist)
        logger.debug(f"plex播放列表 [{media_playlist}] 已存在歌曲[{len(plex_tracks)}]首,列表为: {plex_tracks}")
        # 查找获取tracks
        for t_track in t_tracks:
            if t_track[0] in plex_tracks:
                old_tracks.append(t_track)
                continue
            try:
                tracks = pm.search_music(t_track, self._exact_match)
            except Exception as e:
                logger.error(f"搜索歌曲失败,err:{e}")
                tracks = []
            add_tracks += tracks
        # 去重
        add_tracks = list(set(add_tracks))
        no_list = list(set(i[0] for i in t_tracks) - set([i.title for i in add_tracks]) - set(i[0] for i in old_tracks))
        logger.info(f"Plex库中未搜到歌曲[{len(no_list)}]首,列表为: {no_list}")
        # 有歌曲写入没有就跳过
        if len(add_tracks) > 0:
            if len(plex_tracks) < 1:
                try:
                    # 创建如果存在创建失败就进行添加
                    pm.create_playlist(media_playlist, add_tracks)
                    logger.info(f"Plex创建播放列表[{media_playlist}]成功，并添加歌曲[{len(add_tracks)}]首,列表为: {[i.title for i in add_tracks]}")
                except Exception as err:
                    logger.error(f"{err}")
            else:
                try:
                    pm.set_tracks_to_playlist(media_playlist, add_tracks)
                    logger.info(f"Plex向播放列表[{media_playlist}]添加歌曲[{len(add_tracks)}]首,列表为: {[i.title for i in add_tracks]}成功")
                except Exception as e:
                    logger.error(f"{e}")
        else:
            if len(old_tracks) == len(t_tracks):
                logger.info(f"Plex歌单全部同步，无需再次同步")
            else:
                logger.info(f"Plex歌单同步完成，有部分歌曲没有查询到，请查看日志")
        logger.info("Plex同步歌单完成,感谢耐心等待.......")
        return


    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))





