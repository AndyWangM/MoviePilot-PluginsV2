import copy
import traceback
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, quote_plus

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.context import TorrentInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType
from app.utils.http import RequestUtils


class Prowlarr(_PluginBase):
    # ===== 插件元数据 =====
    plugin_name = "Prowlarr"
    plugin_desc = "通过 Prowlarr 扩展 BT 搜索，每个 Indexer 独立出现在搜索站点列表，可单独启用/禁用"
    plugin_version = "1.3"
    plugin_icon = "https://raw.githubusercontent.com/AndyWangM/MoviePilot-PluginsV2/main/plugins.v2/prowlarr/icon.png"
    plugin_order = 15
    plugin_author = "AndyWangM"
    author_url = "https://github.com/AndyWangM"
    plugin_config_prefix = "prowlarr_"
    auth_level = 1

    # 虚拟 domain 格式：prowlarr-{indexer_id}.local
    # 用于在搜索链路中识别本插件管理的站点，并从中反解 indexer_id
    _DOMAIN_PREFIX = "prowlarr-"
    _DOMAIN_SUFFIX = ".local"

    # ===== 内部状态（类变量提供默认值）=====
    _enabled: bool = False
    _host: str = ""
    _api_key: str = ""
    _proxy: bool = False
    _cron: str = "0 0 * * *"
    _onlyonce: bool = False
    _indexers: List[dict] = []
    _scheduler: Optional[BackgroundScheduler] = None
    _lock: Lock = Lock()

    def init_plugin(self, config: dict = None):
        self._stop_scheduler()
        self._indexers = []

        if not config:
            self._enabled = False
            return

        self._enabled = bool(config.get("enabled", False))
        self._proxy = bool(config.get("proxy", False))
        self._cron = config.get("cron") or "0 0 * * *"
        self._onlyonce = bool(config.get("onlyonce", False))

        host = (config.get("host") or "").strip().rstrip("/")
        if host and not host.startswith("http"):
            host = "http://" + host
        self._host = host

        self._api_key = (config.get("api_key") or "").strip()

        if not self._enabled or not self._host or not self._api_key:
            return

        # 立即刷新一次
        if self._onlyonce:
            self._onlyonce = False
            config["onlyonce"] = False
            self.update_config(config)

        # 拉取 indexer 列表并注册到 SitesHelper
        self._refresh_indexers()

        # 启动定时刷新
        self._start_scheduler()

    def get_state(self) -> bool:
        return self._enabled

    # ===== 搜索模块注入（核心）=====

    def get_module(self) -> Dict[str, Any]:
        if not self._enabled:
            return {}
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
        }

    def _handle_site(self, site: dict, keyword: str, mtype, page: int):
        """
        公共逻辑：判断是否为本插件站点，是则搜索，否则返回 None。
        """
        if not site:
            return None
        domain = site.get("domain", "")
        if not (domain.startswith(self._DOMAIN_PREFIX) and domain.endswith(self._DOMAIN_SUFFIX)):
            return None  # 不是本插件的站点，交给系统模块
        if not self._host or not self._api_key:
            return []
        try:
            middle = domain[len(self._DOMAIN_PREFIX):-len(self._DOMAIN_SUFFIX)]
            indexer_id = int(middle)
        except (ValueError, IndexError):
            logger.warning(f"[Prowlarr] 无法从 domain 解析 indexer_id: {domain}")
            return []
        return self._do_search(
            indexer_id=indexer_id,
            indexer_name=site.get("name", ""),
            keyword=keyword or "",
            mtype=mtype,
            page=page or 0,
        )

    def search_torrents(
        self,
        site: dict,
        keyword: str = None,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> Optional[List[TorrentInfo]]:
        """同步搜索入口（兼容旧链路）。"""
        return self._handle_site(site, keyword, mtype, page or 0)

    async def async_search_torrents(
        self,
        site: dict,
        keyword: str = None,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> Optional[List[TorrentInfo]]:
        """异步搜索入口（主搜索链路调用此方法）。Prowlarr 请求本身是同步的，直接调用。"""
        return self._handle_site(site, keyword, mtype, page or 0)

    # ===== Prowlarr API =====

    def _get_headers(self) -> dict:
        return {
            "X-Api-Key": self._api_key,
            "Accept": "application/json",
            "User-Agent": getattr(settings, "USER_AGENT", "MoviePilot/1.0"),
        }

    def _request_get(self, url: str) -> Optional[Any]:
        """统一 GET 请求，返回解析后的 JSON 或 None。"""
        try:
            proxies = settings.PROXY if self._proxy else None
            resp = RequestUtils(headers=self._get_headers(), proxies=proxies).get_res(url)
            if resp and resp.status_code == 200:
                return resp.json()
            elif resp:
                logger.warning(f"[Prowlarr] HTTP {resp.status_code} — {url}")
            else:
                logger.warning(f"[Prowlarr] 请求无响应（连接失败或超时）— {url}")
        except Exception as e:
            logger.error(f"[Prowlarr] 请求异常 {url}：{e}")
        return None

    def _fetch_indexers_from_prowlarr(self) -> List[dict]:
        """
        调用 GET /api/v1/indexer 获取 Prowlarr 中所有已配置的 indexer。
        注意：/api/v1/indexerstats 只返回有统计记录的 indexer（用过的才有），
              /api/v1/indexer 才是全量列表。
        返回：[{"id": int, "name": str}, ...]
        """
        url = f"{self._host}/api/v1/indexer"
        data = self._request_get(url)
        if not isinstance(data, list):
            logger.warning(f"[Prowlarr] /api/v1/indexer 返回数据异常: {type(data)}")
            return []

        result = []
        for item in data:
            idx_id = item.get("id")
            idx_name = item.get("name") or f"Indexer-{idx_id}"
            if idx_id is None:
                continue
            # 只包含启用的 indexer
            if not item.get("enable", True):
                continue
            result.append({"id": idx_id, "name": idx_name})

        logger.info(f"[Prowlarr] 获取到 {len(result)} 个 indexer")
        return result

    def _do_search(
        self,
        indexer_id: int,
        indexer_name: str,
        keyword: str,
        mtype: Optional[MediaType],
        page: int,
    ) -> List[TorrentInfo]:
        """
        调用 GET /api/v1/search 搜索单个 indexer。

        Prowlarr /api/v1/search 主要返回字段：
          title, guid, infoUrl, downloadUrl, magnetUrl,
          indexerId, indexer, size, seeders, leechers, grabs,
          publishDate, imdbId, sortTitle, categories[],
          downloadVolumeFactor, uploadVolumeFactor,
          minimumRatio, minimumSeedTime, indexerFlags
        """
        categories = self._get_categories(mtype)
        params = [
            ("query", keyword),
            ("indexerIds", str(indexer_id)),
            ("type", "search"),
            ("limit", "100"),
            ("offset", str(page * 100)),
        ] + [("categories", str(c)) for c in categories]

        url = f"{self._host}/api/v1/search?{urlencode(params, quote_via=quote_plus)}"

        try:
            logger.debug(f"[Prowlarr] 搜索 indexer={indexer_name}({indexer_id}) kw={keyword!r} page={page}")
            data = self._request_get(url)

            if not isinstance(data, list):
                if data is None:
                    logger.warning(f"[Prowlarr] {indexer_name} 请求失败（无响应），可能是 Prowlarr 服务或该 Indexer 不可用")
                else:
                    logger.warning(f"[Prowlarr] {indexer_name} 搜索返回格式异常: {type(data)} — {str(data)[:200]}")
                return []

            results: List[TorrentInfo] = []
            seen = set()

            for entry in data:
                title = entry.get("title") or ""
                # 优先用 HTTP 下载链接，其次 magnet
                enclosure = entry.get("downloadUrl") or entry.get("magnetUrl") or ""
                if not title or not enclosure:
                    continue

                # 去重
                dedup_key = f"{title}|{enclosure}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                # 促销因子（Prowlarr 返回 float，0.0 = 免费下载，1.0 = 正常）
                dl_factor = entry.get("downloadVolumeFactor")
                ul_factor = entry.get("uploadVolumeFactor")
                dl_factor = float(dl_factor) if dl_factor is not None else 1.0
                ul_factor = float(ul_factor) if ul_factor is not None else 1.0

                # 发布时间：ISO 8601 → "YYYY-MM-DD HH:MM:SS"
                pubdate_raw = entry.get("publishDate") or ""
                pubdate = ""
                if pubdate_raw:
                    try:
                        dt = datetime.fromisoformat(pubdate_raw.replace("Z", "+00:00"))
                        pubdate = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pubdate = pubdate_raw

                torrent = TorrentInfo(
                    site_name=f"Prowlarr - {indexer_name}",
                    title=title,
                    description=entry.get("sortTitle") or title,
                    enclosure=enclosure,
                    page_url=entry.get("infoUrl") or entry.get("guid") or "",
                    size=entry.get("size") or 0,
                    seeders=entry.get("seeders") or 0,
                    peers=entry.get("leechers") or 0,
                    grabs=entry.get("grabs") or 0,
                    pubdate=pubdate,
                    imdbid=entry.get("imdbId") or "",
                    downloadvolumefactor=dl_factor,
                    uploadvolumefactor=ul_factor,
                )
                results.append(torrent)

            logger.info(f"[Prowlarr] {indexer_name}({indexer_id}) kw={keyword!r} → {len(results)} 条结果")
            return results

        except Exception as e:
            logger.error(f"[Prowlarr] 搜索异常 indexer={indexer_name}: {e}\n{traceback.format_exc()}")
            return []

    @staticmethod
    def _get_categories(mtype: Optional[MediaType]) -> List[int]:
        """MediaType → Prowlarr Newznab categories"""
        if mtype == MediaType.MOVIE:
            return [2000]
        elif mtype == MediaType.TV:
            return [5000]
        else:
            return [2000, 5000]

    # ===== Indexer 注册 =====

    def _refresh_indexers(self):
        """
        拉取最新 indexer 列表，写入 DB 并注册到 SitesHelper（线程安全）。
        写入 DB 的目的：让 Prowlarr 站点出现在"搜索站点"设置页（前端从 GET /site/ 读取），
        用户勾选后 IndexerSites 里存的整数 DB ID 才能匹配 get_indexers() 返回的 id 字段。
        """
        if not self._host or not self._api_key:
            return

        with self._lock:
            try:
                from app.helper.sites import SitesHelper
                from app.db.site_oper import SiteOper

                indexers = self._fetch_indexers_from_prowlarr()
                if not indexers:
                    logger.warning("[Prowlarr] 未获取到任何 indexer，跳过注册")
                    return

                self._indexers = indexers
                sites_helper = SitesHelper()

                for item in indexers:
                    idx_id = item["id"]
                    idx_name = item["name"]
                    # 虚拟 domain：prowlarr-{indexer_id}.local
                    domain = f"{self._DOMAIN_PREFIX}{idx_id}{self._DOMAIN_SUFFIX}"
                    display_name = f"Prowlarr - {idx_name}"

                    # 1. 注册到 SitesHelper 内存，使搜索链路可识别
                    sites_helper.add_indexer({
                        "id": f"prowlarr_{idx_id}",
                        "name": display_name,
                        "domain": domain,
                        "public": False,  # 设为 False，让 get_indexers() 依赖 DB 记录
                        "proxy": self._proxy,
                    })

                    # 2. 写入 DB，使站点出现在设置页"搜索站点"列表
                    try:
                        site_oper = SiteOper()
                        existing = site_oper.get_by_domain(domain)
                        if not existing:
                            ok, msg = site_oper.add(
                                name=display_name,
                                domain=domain,
                                url=f"https://{domain}/",
                                public=0,
                                is_active=True,
                                pri=100,
                            )
                            if ok:
                                logger.info(f"[Prowlarr] 已写入 DB：{display_name} ({domain})")
                            else:
                                logger.debug(f"[Prowlarr] DB 写入跳过（{msg}）: {display_name}")
                        else:
                            # 如果名称变了，更新
                            if existing.name != display_name:
                                site_oper.update(existing.id, {"name": display_name})
                    except Exception as db_err:
                        logger.warning(f"[Prowlarr] 写入 DB 失败（{display_name}）: {db_err}")

                logger.info(f"[Prowlarr] 已注册 {len(indexers)} 个 indexer 到搜索站点列表")
            except Exception as e:
                logger.error(f"[Prowlarr] 注册 indexer 失败：{e}\n{traceback.format_exc()}")

    # ===== 定时任务（内置 APScheduler）=====

    def _start_scheduler(self):
        try:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                self._refresh_indexers,
                CronTrigger.from_crontab(self._cron),
                id="prowlarr_refresh",
                replace_existing=True,
            )
            self._scheduler.start()
            logger.info(f"[Prowlarr] 定时刷新已启动，cron: {self._cron}")
        except Exception as e:
            logger.error(f"[Prowlarr] 启动定时任务失败: {e}")

    def _stop_scheduler(self):
        try:
            if self._scheduler:
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception:
            pass

    def get_service(self) -> List[Dict[str, Any]]:
        return []  # 使用内置 APScheduler

    # ===== REST API =====

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "测试 Prowlarr 连接",
                "description": "连接 Prowlarr 并返回 indexer 列表",
            },
            {
                "path": "/indexers",
                "endpoint": self.api_indexers,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取已注册的 Indexer 列表",
                "description": "返回当前插件注册到 MoviePilot 的所有 Prowlarr Indexer",
            },
        ]

    def api_test(self) -> dict:
        if not self._host or not self._api_key:
            return {"success": False, "message": "未配置 host 或 api_key"}
        indexers = self._fetch_indexers_from_prowlarr()
        if indexers:
            return {
                "success": True,
                "message": f"连接成功，共 {len(indexers)} 个 indexer",
                "indexers": indexers,
            }
        return {"success": False, "message": "连接失败或未获取到 indexer，请检查 host 和 api_key"}

    def api_indexers(self) -> dict:
        return {"indexers": self._indexers}

    # ===== 配置表单 =====

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    # 第一行：启用 / 代理 / 立即刷新
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "proxy", "label": "使用代理"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "onlyonce",
                                        "label": "立即刷新 Indexer 列表",
                                        "hint": "保存后立即拉取一次，无需等待定时周期",
                                    },
                                }],
                            },
                        ],
                    },
                    # 第二行：Host / API Key
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "host",
                                        "label": "Prowlarr 地址",
                                        "placeholder": "http://192.168.1.1:9696",
                                        "hint": "Prowlarr 访问地址，如使用 HTTPS 请加 https:// 前缀",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_key",
                                        "label": "API Key",
                                        "placeholder": "在 Prowlarr → Settings → General → Security → API Key 中获取",
                                        "type": "password",
                                    },
                                }],
                            },
                        ],
                    },
                    # 第三行：刷新周期
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "Indexer 列表刷新周期（cron）",
                                        "placeholder": "0 0 * * *",
                                        "hint": "支持 5 位 cron 表达式，默认每天凌晨刷新一次",
                                    },
                                }],
                            },
                        ],
                    },
                    # 说明信息
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": (
                                            "保存后会自动拉取 Prowlarr 中所有已配置的 Indexer，"
                                            "并以「Prowlarr - {名称}」的形式出现在「设置 → 搜索 → 索引站点」中，"
                                            "可单独勾选启用/禁用。"
                                            "每次修改 Prowlarr 中的 Indexer 后，开启「立即刷新 Indexer 列表」并保存以同步。"
                                        ),
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "host": "",
            "api_key": "",
            "proxy": False,
            "cron": "0 0 * * *",
            "onlyonce": False,
        }

    # ===== 详情页 =====

    def get_page(self) -> Optional[List[dict]]:
        """显示当前已注册的 Prowlarr Indexer 列表。"""
        if not self._indexers:
            return [{
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "尚未加载 Indexer。请启用插件、配置 Prowlarr 地址和 API Key 后保存，或开启「立即刷新」。",
                },
            }]

        rows = []
        for item in self._indexers:
            domain = f"{self._DOMAIN_PREFIX}{item['id']}{self._DOMAIN_SUFFIX}"
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "ps-4"}, "text": str(item.get("id", ""))},
                    {"component": "td", "props": {"class": "ps-4"}, "text": item.get("name", "")},
                    {"component": "td", "props": {"class": "ps-4"}, "text": domain},
                ],
            })

        return [{
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VTable",
                    "props": {"hover": True},
                    "content": [
                        {
                            "component": "thead",
                            "content": [{
                                "component": "tr",
                                "content": [
                                    {"component": "th", "props": {"class": "text-start ps-4"}, "text": "Indexer ID"},
                                    {"component": "th", "props": {"class": "text-start ps-4"}, "text": "名称"},
                                    {"component": "th", "props": {"class": "text-start ps-4"}, "text": "虚拟 Domain"},
                                ],
                            }],
                        },
                        {"component": "tbody", "content": rows},
                    ],
                }],
            }],
        }]

    def stop_service(self):
        self._stop_scheduler()
