import datetime
import json
import random
import re
from threading import Event
from typing import Tuple, List, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.helper.browser import BrowserPage, PlaywrightHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType
from app.utils.http import RequestUtils


class MaoyanRank(_PluginBase):
    """
    获取猫眼的排行榜进行订阅,建议每天一次
    电影获取的url: https://piaofang.maoyan.com/dashboard-ajax/movie
    剧集热度接口: https://piaofang.maoyan.com/i/api/encrypt/dashboard/webHeatData
    网络电影接口: https://piaofang.maoyan.com/i/api/encrypt/dashboard/webHeatNetData
        参数 showDate: 时间具体到天
        参数 seriesType: 代表类型 0: 电视剧 1: 网络剧 2: 综艺 不传递-1代表电视剧+网络剧
        参数 platformType: 代表平台 0 全网 3 腾讯视频 2 爱奇艺 1 优酷 7 芒果 5 搜狐 4 乐视 6 PPTV 20 网络电影networkHot=3

    详情链接:
    https://piaofang.maoyan.com/dashboard/movie?movieId=1489349
    https://piaofang.maoyan.com/dashboard/web-heat?movieId=1484643

    """
    # 插件名称
    plugin_name = "猫眼榜单订阅"
    # 插件描述
    plugin_desc = "监控猫眼数据，自动添加订阅。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/baozaodetudou/MoviePilot-Plugins/main/icons/maoyan.jpg"
    # 插件版本
    plugin_version = "2.7"
    # 插件作者
    plugin_author = "逗猫"
    # 作者主页
    author_url = "https://github.com/baozaodetudou"
    # 插件配置项ID前缀
    plugin_config_prefix = "maoyanrank_"
    # 加载顺序
    plugin_order = 6
    # 可使用的用户级别
    auth_level = 1

    # 退出事件
    _event = Event()
    # 私有属性
    downloadchain: DownloadChain = None
    subscribechain: SubscribeChain = None
    _scheduler = None
    _enabled = False
    _onlyonce = False
    _cron = ""
    _clear = False
    # type细分 movie: 电影 web-heat 电视榜单 web-tv 网剧 zongyi 综艺  web-movie 网络电影
    _type = ['movie', 'web-heat', 'web-tv', 'zongyi', 'web-movie']

    _num = 10
    _all_enabled: bool = False
    _all_num = 10
    _tx_enabled: bool = False
    _tx_num = 10
    _iqy_enabled: bool = False
    _iqy_num = 10
    _mg_enabled: bool = False
    _mg_num = 10
    _yk_enabled: bool = False
    _yk_num = 10

    def init_plugin(self, config: dict = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._clear = config.get("clear")
            self._onlyonce = config.get("onlyonce")

            self._type = config.get("type")
            self._num = config.get("num", 10)

            self._all_enabled = config.get("all_enabled", False)
            self._tx_enabled = config.get("tx_enabled", False)
            self._iqy_enabled = config.get("iqy_enabled", False)
            self._mg_enabled = config.get("mg_enabled", False)
            self._yk_enabled = config.get("yk_enabled", False)
            self._all_num = config.get("all_num", 10)
            self._tx_num = config.get("tx_num", 10)
            self._iqy_num = config.get("iqy_num", 10)
            self._mg_num = config.get("mg_num", 10)
            self._yk_num = config.get("yk_num", 10)


        # 停止现有任务
        self.stop_service()

        # 启动服务
        # 清理插件历史
        if self._clear:
            self.del_data(key="history")
            self._clear = False
            self.__update_config()
            logger.info("历史清理完成")

        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            # 周期执行
            if self._cron:
                logger.info(f"猫眼榜单订阅服务启动，周期：{self._cron}")
                try:
                    self._scheduler.add_job(func=self.__refresh_maoyan,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="猫眼榜单订阅")
                except Exception as e:
                    logger.error(f"猫眼榜单订阅服务启动失败，错误信息：{str(e)}")
                    self.systemmessage.put(f"猫眼榜单订阅服务启动失败，错误信息：{str(e)}")
            else:
                self._scheduler.add_job(func=self.__refresh_maoyan, trigger=CronTrigger.from_crontab("0 9 * * *"),
                                        name="猫眼榜单订阅")
                logger.info("猫眼榜单订阅服务启动，周期：每天 09:00")

            # 一次性执行
            if self._onlyonce:
                logger.info("猫眼榜单订阅服务启动，立即运行一次")
                self._scheduler.add_job(func=self.__refresh_maoyan, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

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
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                            'model': 'clear',
                                            'label': '清理历史记录',
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
                                    'cols': 22,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'type',
                                            'label': '订阅类型',
                                            'items': [
                                                {'title': '电影票房榜单', 'value': 'movie'},
                                                {'title': '电视剧热度榜单', 'value': 'web-heat'},
                                                {'title': '网剧热度榜单', 'value': 'web-tv'},
                                                {'title': '综艺热度榜单', 'value': 'zongyi'},
                                                {'title': '网络电影榜单', 'value': 'web-movie'},
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 22,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'num',
                                            'label': '电影榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
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
                                            'text': '下边针对电视剧，网剧，综艺的细分类进行设置不开启则不订阅电视剧；'
                                                    '控制是并行的都打开会都进行订阅当然重复会进行过滤。'
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'all_enabled',
                                            'label': '全网热门订阅',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'all_num',
                                            'label': '榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'tx_enabled',
                                            'label': '腾讯热门订阅',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'tx_num',
                                            'label': '腾讯榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'iqy_enabled',
                                            'label': '爱奇艺热门订阅',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'iqy_num',
                                            'label': '爱奇艺榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'mg_enabled',
                                            'label': '芒果热门订阅',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'mg_num',
                                            'label': '芒果榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'yk_enabled',
                                            'label': '优酷热门订阅',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'yk_num',
                                            'label': '优酷榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
                                        }
                                    }
                                ]
                            }

                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "",
            "clear": False,
            "type": ['movie', 'web-heat', 'web-tv', 'zongyi', 'web-movie'],
            "num": 10,
            "all_enabled": False,
            "tx_enabled": False,
            "iqy_enabled": False,
            "mg_enabled": False,
            "yk_enabled": False,
            "all_num": 10,
            "tx_num": 10,
            "iqy_num": 10,
            "yk_num": 10,
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询历史记录
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            title = history.get("title")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            tmdb_id = history.get("tmdbid")
            release_info = history.get("releaseInfo")
            platform = history.get("platformDesc")
            if mtype == MediaType.TV.value:
                href = f"https://www.themoviedb.org/tv/{tmdb_id}"
            else:
                href = f"https://www.themoviedb.org/movie/{tmdb_id}"
            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': poster,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VCardSubtitle',
                                            'props': {
                                                'class': 'pa-2 font-bold break-words whitespace-break-spaces'
                                            },
                                            'content': [
                                                {
                                                    'component': 'a',
                                                    'props': {
                                                        'href': href,
                                                        'target': '_blank'
                                                    },
                                                    'text': title
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'{release_info}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'平台：{platform}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'类型：{mtype}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'订阅时间：{time_str}'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

    def stop_service(self):
        """
        停止服务
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

    def __update_config(self):
        """
        列新配置
        """

        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "clear": self._clear,
            "onlyonce": self._onlyonce,
            "type": self._type,
            "num": self._num,
            "all_enabled": self._all_enabled,
            "tx_enabled": self._tx_enabled,
            "iqy_enabled": self._iqy_enabled,
            "mg_enabled": self._mg_enabled,
            "yk_enabled": self._yk_enabled,
            "all_num": self._all_num,
            "tx_num": self._tx_num,
            "iqy_num": self._iqy_num,
            "mg_num": self._mg_num,
            "yk_num": self._yk_num,
        })

    def __refresh_maoyan(self):
        """
        刷新猫眼榜单数据
        电影获取的url:
        https://piaofang.maoyan.com/dashboard-ajax/movie
        剧集热度接口:
        {tv_url}?showDate=20240223&seriesType=0&platformType=3
        参数 showDate: 时间具体到天
        参数 seriesType: 代表类型 0: 电视剧 1: 网络剧 2: 综艺 不传递-1代表电视剧+网络剧
        参数 platformType: 代表平台 0 全网 3 腾讯视频 2 爱奇艺 1 优酷 7 芒果 5 搜狐 4 乐视 6 PPTV
        """
        logger.info(f"开始刷新猫眼榜单...")
        # 获取当前日期时间
        current_time = datetime.datetime.now()
        nums = self._num
        #
        history: List[dict] = self.get_data('history') or []
        #
        movie_url = ''
        web_movie_url = ''
        tv_urls = []
        # 获取当前日期时间格式化为字符串
        show_date = current_time.strftime("%Y%m%d")
        maoyan_url = 'https://piaofang.maoyan.com'
        selected_types = self._type or []
        if 'movie' in selected_types:
            movie_url = f'{maoyan_url}/dashboard-ajax/movie'
        if 'web-movie' in selected_types:
            web_movie_url = (f'{maoyan_url}/i/api/encrypt/dashboard/webHeatNetData?'
                             f'showDate={show_date}&platformType=3&dateType=0&rankType=0')
        tv_url = f'{maoyan_url}/i/api/encrypt/dashboard/webHeatData'
        platforms = (
            (None, self._all_enabled, self._all_num),
            (3, self._tx_enabled, self._tx_num),
            (2, self._iqy_enabled, self._iqy_num),
            (7, self._mg_enabled, self._mg_num),
            (1, self._yk_enabled, self._yk_num),
        )
        for category, series_type in (('web-heat', 0), ('web-tv', 1), ('zongyi', 2)):
            if category not in selected_types:
                continue
            for platform_type, enabled, limit in platforms:
                if not enabled:
                    continue
                query = f'showDate={show_date}&seriesType={series_type}'
                if platform_type is not None:
                    query += f'&platformType={platform_type}'
                tv_urls.append([f'{tv_url}?{query}', limit])
        tv_list = []
        movie_list = []
        try:
            movie_list, tv_list = self.__get_url_info(movie_url, tv_urls, web_movie_url, nums)
        except Exception as e:
            logger.warn(e)
        self.set_sub(movie_list, history, MediaType.MOVIE)
        self.set_sub(tv_list, history, MediaType.TV)
        # 保存历史记录
        self.save_data('history', history)
        if movie_list or tv_list:
            logger.info(f"猫眼订阅刷新完成：电影 {len(movie_list)} 条，剧集 {len(tv_list)} 条")
        else:
            logger.warning("猫眼榜单未获取到数据，本轮未新增订阅")

    def set_sub(self, addr_list, history, mtype):
        # 获取当前日期时间
        current_time = datetime.datetime.now()
        for addr in addr_list:
            try:
                title = addr.get('title')
                try:
                    # 计算日期，获取年份信息
                    days_text = ''.join(re.findall(r'\d', str(addr.get('releaseInfo') or '')))
                    year = (current_time - datetime.timedelta(days=int(days_text))).year if days_text else None
                except Exception as e:
                    logger.warn(e)
                    year = None
                # 元数据
                meta = MetaInfo(title)
                meta.year = year
                unique_flag = f"maoyanrank: {mtype}_{title}_{year}"
                # 检查是否已处理过
                if unique_flag in [h.get("unique") for h in history]:
                    continue
                # 匹配媒体信息
                mediainfo: MediaInfo = self.chain.recognize_media(meta=meta, mtype=mtype, cache=False)
                if not mediainfo:
                    logger.warn(f'未识别到媒体信息，标题：{title}，年份：{year}')
                    continue
                # 查询缺失的媒体信息
                exist_flag, _ = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                if exist_flag:
                    logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                    continue
                # 判断用户是否已经添加订阅
                if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
                    logger.info(f'{mediainfo.title_year} 订阅已存在')
                    continue
                # 添加订阅
                season = meta.begin_season if mtype == MediaType.TV else None
                self.subscribechain.add(title=mediainfo.title,
                                        year=mediainfo.year,
                                        mtype=mediainfo.type,
                                        tmdbid=mediainfo.tmdb_id,
                                        season=season,
                                        exist_ok=True,
                                        username="猫眼订阅")
                # 存储历史记录
                history.append({
                    "title": title,
                    "releaseInfo": addr.get('releaseInfo'),
                    "platformDesc": addr.get('platformDesc', '未知'),
                    "type": mediainfo.type.value,
                    "year": mediainfo.year,
                    "poster": mediainfo.get_poster_image(),
                    "overview": mediainfo.overview,
                    "tmdbid": mediainfo.tmdb_id,
                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "unique": unique_flag
                })
            except Exception as e:
                logger.error(str(e))

    def __get_url_info(self, movie_url, tv_urls, web_movie_url, num=10):
        """
        获取电影榜单，并在 CloakBrowser 页面中读取需要浏览器会话的热度榜单。
        """
        movies_list = []
        tv_list = []
        if movie_url:
            try:
                response = RequestUtils().get_res(
                    movie_url, headers={'User-Agent': self.get_random_user_agent()})
                if response is None:
                    raise RuntimeError('无响应')
                if not response.ok:
                    raise RuntimeError(f'HTTP {response.status_code}')
                if 'json' not in response.headers.get('content-type', '').lower():
                    raise RuntimeError(f'响应不是 JSON（HTTP {response.status_code}）')
                res = response.json()
                data = (res.get('movieList') or {}).get('list') or []
                for movie in data[:num]:
                    info = movie.get('movieInfo') or {}
                    if info.get('movieName'):
                        movies_list.append({
                            'title': info['movieName'],
                            'releaseInfo': info.get('releaseInfo'),
                        })
            except Exception as e:
                logger.error(f'获取猫眼电影榜单失败：{e}')

        browser_requests = [{'kind': 'movie', 'url': web_movie_url, 'limit': num}] if web_movie_url else []
        browser_requests.extend({'kind': 'tv', 'url': url, 'limit': limit} for url, limit in tv_urls)
        if browser_requests:
            def page_handler(page: BrowserPage) -> list:
                return page.evaluate("""async (requests) => {
                    const meta = name => document.querySelector(`meta[name="${name}"]`)?.content || '';
                    const uid = meta('csrf');
                    if (!uid) throw new Error('猫眼页面缺少会话标识');
                    const headers = {uid};
                    if (meta('deviceId')) headers.uuid = meta('deviceId');
                    if (meta('channelId')) headers.channelId = meta('channelId');
                    const results = [];
                    for (const request of requests) {
                        try {
                            let response = await fetch(request.url, {headers});
                            if (!response.ok) {
                                results.push({kind: request.kind, error: `HTTP ${response.status}`});
                                continue;
                            }
                            let body = await response.json();
                            if (body.status === false && request.kind === 'movie') {
                                const latest = body.calendarNet?.selectMaxDate?.replaceAll('-', '');
                                const retryUrl = new URL(request.url, location.origin);
                                if (latest && latest !== retryUrl.searchParams.get('showDate')) {
                                    retryUrl.searchParams.set('showDate', latest);
                                    response = await fetch(retryUrl.toString(), {headers});
                                    if (!response.ok) {
                                        results.push({kind: request.kind, error: `HTTP ${response.status}`});
                                        continue;
                                    }
                                    body = await response.json();
                                }
                            }
                            if (body.status === false) {
                                results.push({kind: request.kind, error: '猫眼未提供该榜单数据'});
                                continue;
                            }
                            const rows = body.dataList?.list;
                            if (!Array.isArray(rows)) {
                                results.push({kind: request.kind, error: '榜单数据格式已变化'});
                                continue;
                            }
                            const limit = Math.max(0, Number(request.limit ?? 10));
                            const items = rows.slice(0, limit).map(row => {
                                const info = row.seriesInfo || row.movieInfo || row;
                                return {title: info.name || info.movieName || '',
                                        releaseInfo: info.releaseInfo || '',
                                        platformDesc: info.platformDesc || ''};
                            }).filter(item => item.title);
                            results.push({kind: request.kind, items});
                        } catch (error) {
                            results.push({kind: request.kind, error: String(error)});
                        }
                    }
                    return results;
                }""", browser_requests)

            browser_results = PlaywrightHelper().action(
                url='https://piaofang.maoyan.com',
                callback=page_handler,
                headless=True,
                timeout=30,
            )
            if browser_results is None:
                logger.error('CloakBrowser 获取猫眼榜单失败')
            else:
                for request, result in zip(browser_requests, browser_results):
                    if result.get('error'):
                        logger.warning(f"猫眼榜单 {request['url']}：{result['error']}")
                    elif request['kind'] == 'movie':
                        movies_list.extend(result.get('items') or [])
                    else:
                        tv_list.extend(result.get('items') or [])
                if len(browser_results) != len(browser_requests):
                    logger.error('猫眼榜单响应数量与请求数量不一致')

        tv_list = list({item['title']: item for item in tv_list}.values())

        return movies_list, tv_list

    @staticmethod
    def get_random_user_agent():
        user_agents = [
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        ]
        return random.choice(user_agents)
