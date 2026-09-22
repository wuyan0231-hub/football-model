"""Bounded API-Football client and strict 90-minute result normalization.

Official protocol references (read 2026-09-21):
https://www.api-football.com/documentation-v3
https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide
https://www.api-football.com/news/post/how-ratelimit-works

No request to an authenticated endpoint is made during import or tests. This
client collects scores only; coverage flags do not implement a lineup model.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


BASE_URL = "https://v3.football.api-sports.io"


class APIError(RuntimeError):
    """Actionable, credential-redacted provider or request-budget error."""


class DataError(ValueError):
    """Fixture data is malformed or contradictory; do not silently train."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the authentication header to another host.
        return None


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise DataError("时间必须为带时区的 ISO 8601 字符串") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DataError("时间必须明确时区")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise DataError(f"{label} 必须为不小于 {minimum} 的整数")
    return value


class APIClient:
    """One run, one global request budget, shared by all competitions.

    max_requests includes /status, retries and every network attempt. It is not
    the account's total daily quota: response headers impose that second limit.
    Current-season fetches bypass disk cache by default. Historical cache hits
    keep their original fetched_at and are never relabelled as fresh downloads.
    """

    def __init__(self, data_dir: str | Path, max_requests: int = 80,
                 timeout: float = 25, max_retries: int = 2,
                 min_interval: float = 6.5, *, opener=None, sleep=None,
                 clock=None, monotonic=None):
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("max_requests 必须是正整数")
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            raise ValueError("max_retries 必须在 0 至 2 之间")
        if timeout <= 0 or not 0 <= min_interval <= 60:
            raise ValueError("timeout 必须为正，min_interval 必须在 0 至 60 秒之间")
        self._key = os.environ.get("FOOTBALL_API_KEY", "").strip()
        if not self._key:
            raise APIError("未配置 FOOTBALL_API_KEY；请在 GitHub Actions Secrets 中添加密钥")
        self.data_dir = Path(data_dir)
        self.max_requests = max_requests
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = float(min_interval)
        self.requests_made = 0
        self.daily_remaining: int | None = None
        self.minute_remaining: int | None = None
        self._opener = opener or build_opener(_NoRedirect()).open
        self._sleep = sleep or time.sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._last_request: float | None = None
        self._wait_until: float = 0

    def _redact(self, value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text.replace(self._key, "[REDACTED]")[:1500]

    def _pause(self, seconds: float) -> None:
        # No individual blocking sleep is longer than 60 seconds.
        if seconds > 60:
            raise APIError("接口要求等待超过 60 秒；请稍后重新运行")
        if seconds > 0:
            self._sleep(seconds)

    def _rate_headers(self, headers) -> None:
        if headers is None:
            return
        lowered = {str(k).lower(): v for k, v in headers.items()}
        for field, attr in (("x-ratelimit-requests-remaining", "daily_remaining"),
                            ("x-ratelimit-remaining", "minute_remaining")):
            try:
                val = int(lowered[field])
                if val >= 0:
                    setattr(self, attr, val)
            except (KeyError, TypeError, ValueError):
                pass
        if self.minute_remaining == 0:
            self._wait_until = self._monotonic() + 60

    def _before_request(self) -> None:
        if self.requests_made >= self.max_requests:
            raise APIError(f"达到本次运行请求上限 {self.max_requests}；停止下载，未完成部分不可当作成功")
        if self.daily_remaining == 0:
            raise APIError("接口返回今日剩余额度为 0；停止下载，请等待额度恢复")
        now = self._monotonic()
        wait = max(0.0, self._wait_until - now)
        if self._last_request is not None:
            wait = max(wait, self.min_interval - (now - self._last_request))
        self._pause(wait)
        self._last_request = self._monotonic()
        self.requests_made += 1

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict:
        if endpoint not in {"/status", "/leagues", "/fixtures"}:
            raise APIError("此客户端不允许该 endpoint")
        url = BASE_URL + endpoint
        if params:
            url += "?" + urlencode(sorted(params.items()))
        for attempt in range(self.max_retries + 1):
            self._before_request()
            request = Request(url, headers={"x-apisports-key": self._key,
                                          "Accept": "application/json"}, method="GET")
            try:
                with self._opener(request, timeout=self.timeout) as result:
                    self._rate_headers(getattr(result, "headers", None))
                    body = result.read()
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    raise APIError("接口未返回有效 JSON；未缓存此响应") from None
                if not isinstance(payload, dict) or "response" not in payload:
                    raise APIError("接口响应缺少 response；未缓存此响应")
                if "errors" not in payload or not isinstance(payload["errors"], (list, dict)):
                    raise APIError("接口响应缺少可识别的 errors；未缓存此响应")
                if payload["errors"]:
                    raise APIError("API-Football 返回错误（可能涉及套餐、赛季或额度）："
                                   + self._redact(payload["errors"]))
                paging = payload.get("paging", {})
                if not isinstance(paging, dict):
                    raise APIError("接口分页结构异常")
                if paging.get("total", 1) != 1:
                    raise APIError("接口返回分页数据；此版本停止处理，避免把不完整数据用于训练")
                # Redact even an unexpected provider echo before caching or returning.
                clean = json.loads(self._redact_full(payload))
                return {**clean, "fetched_at": _iso(self._clock()), "from_cache": False}
            except HTTPError as exc:
                self._rate_headers(exc.headers)
                code = exc.code
                if code not in {429, 499, 500, 502, 503, 504} or attempt >= self.max_retries:
                    raise APIError(f"API-Football HTTP {code}；请检查账户权限或稍后重试") from None
                if self.daily_remaining == 0:
                    raise APIError("API-Football 今日额度耗尽；停止重试") from None
                wait = 60.0 if code == 429 else float(2 ** (attempt + 1))
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after is not None:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        # Date-form Retry-After is conservatively not retried.
                        raise APIError("接口要求稍后重试；请稍后重新运行") from None
                self._pause(wait)
            except (URLError, TimeoutError, OSError):
                if attempt >= self.max_retries:
                    # Do not expose raw exception text: it may contain headers.
                    raise APIError("连接 API-Football 失败或超时；已停止有限重试") from None
                self._pause(float(2 ** (attempt + 1)))
        raise APIError("请求未完成")

    def _redact_full(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False).replace(self._key, "[REDACTED]")

    def _cached_request(self, endpoint: str, params: dict, *, force: bool,
                        max_age_seconds: float | None) -> dict:
        if max_age_seconds is not None and max_age_seconds < 0:
            raise ValueError("max_age_seconds 不能为负数")
        identity = json.dumps([endpoint, params], sort_keys=True).encode("utf-8")
        path = self.data_dir / "api_cache" / (hashlib.sha256(identity).hexdigest() + ".json")
        if not force and path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                age = (_utc(self._clock()) - _utc(cached["fetched_at"])).total_seconds()
                valid = (cached.get("endpoint") == endpoint and cached.get("params") == params
                         and isinstance(cached.get("response"), list)
                         and not cached.get("errors") and age >= 0
                         and (max_age_seconds is None or age <= max_age_seconds))
                if valid:
                    cached["from_cache"] = True
                    return cached
            except (ValueError, KeyError, TypeError, OSError):
                # A corrupt cache is not silently treated as evidence.
                pass
        fetched = self._request(endpoint, params)
        if not isinstance(fetched["response"], list):
            raise APIError("该接口 response 应为列表；未缓存此响应")
        record = {**fetched, "endpoint": endpoint, "params": params}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(self._redact_full(record), encoding="utf-8")
        temporary.replace(path)
        return record

    def status(self) -> dict:
        """Fetch account state, do not cache account/profile information."""
        result = self._request("/status", {})
        if not isinstance(result["response"], dict):
            raise APIError("status 接口 response 应为对象")
        return {**result["response"], "fetched_at": result["fetched_at"], "from_cache": False}

    def resolve_league(self, config: dict, force: bool = False) -> dict:
        """Exact name AND country; zero/multiple matches require manual correction."""
        name, country = config.get("name"), config.get("country")
        if not isinstance(name, str) or not name.strip() or not isinstance(country, str) or not country.strip():
            raise APIError("赛事配置必须包含非空 name 和 country")
        result = self._cached_request("/leagues", {"name": name, "country": country},
                                      force=force, max_age_seconds=86400)
        matches = [item for item in result["response"] if isinstance(item, dict)
                   and isinstance(item.get("league"), dict) and isinstance(item.get("country"), dict)
                   and item["league"].get("name") == name and item["country"].get("name") == country]
        if len(matches) != 1:
            raise APIError(f"赛事名称与国家必须精确匹配一项，当前匹配 {len(matches)} 项；请检查配置")
        item = copy.deepcopy(matches[0])
        try:
            _integer(item["league"].get("id"), "league.id", 1)
            seasons = item["seasons"]
            if not isinstance(seasons, list) or not seasons:
                raise ValueError
            years = []
            for season in seasons:
                years.append(_integer(season.get("year"), "season.year", 1900))
                if not isinstance(season.get("coverage"), dict):
                    raise ValueError
            if len(years) != len(set(years)):
                raise ValueError
        except (DataError, ValueError, KeyError, AttributeError):
            raise APIError("赛事缺少有效 ID、唯一赛季或 coverage 信息") from None
        item.update(fetched_at=result["fetched_at"], from_cache=result["from_cache"])
        return item

    def fetch_fixtures(self, league_id: int, season: int, *, current: bool = True,
                       force: bool = False, max_age_seconds: float | None = None) -> dict:
        """All fixtures, including future ones; normalize separately before fitting.

        Default: refresh current season; reuse an old season for at most 7 days.
        An explicit max_age_seconds enables caller-controlled current cache reuse.
        """
        _integer(league_id, "league_id", 1)
        _integer(season, "season", 1900)
        if type(current) is not bool:
            raise ValueError("current 必须为布尔值")
        bypass = force or (current and max_age_seconds is None)
        age = 7 * 86400 if max_age_seconds is None else max_age_seconds
        return self._cached_request("/fixtures", {"league": league_id, "season": season},
                                    force=bypass, max_age_seconds=age)


def _unique_fixtures(fixtures: list) -> list[dict]:
    if not isinstance(fixtures, list):
        raise DataError("fixtures 必须为列表")
    seen: dict[int, tuple[dict, dict]] = {}
    for item in fixtures:
        if not isinstance(item, dict) or not isinstance(item.get("fixture"), dict):
            raise DataError("fixture 结构缺失")
        fixture = item["fixture"]
        mid = _integer(fixture.get("id"), "fixture.id", 1)
        # Ignore unmodelled presentation-only fields, but not result/status identity.
        signature = {"fixture": {key: fixture.get(key) for key in ("date", "timestamp", "status")},
                     "teams": item.get("teams"), "score": item.get("score")}
        if mid in seen and seen[mid][0] != signature:
            raise DataError(f"同一 match_id={mid} 出现互相冲突的比赛记录")
        seen[mid] = (signature, item)
    return [value[1] for value in seen.values()]


def _fixture_identity(item: dict) -> tuple[dict, datetime]:
    fixture = item["fixture"]
    kickoff = _utc(fixture.get("date"))
    if "timestamp" in fixture and fixture["timestamp"] is not None:
        timestamp = _integer(fixture["timestamp"], "fixture.timestamp")
        if abs(kickoff.timestamp() - timestamp) > 1:
            raise DataError("fixture.date 与 timestamp 不一致")
    try:
        home, away = item["teams"]["home"], item["teams"]["away"]
        hid = _integer(home.get("id"), "home.id", 1)
        aid = _integer(away.get("id"), "away.id", 1)
        hn, an = home.get("name"), away.get("name")
        if hid == aid or not isinstance(hn, str) or not hn.strip() or not isinstance(an, str) or not an.strip():
            raise DataError("球队 ID 或名称无效")
    except (KeyError, TypeError, AttributeError):
        raise DataError("球队资料结构缺失") from None
    return {"match_id": fixture["id"], "kickoff": _iso(kickoff),
            "home_id": hid, "away_id": aid, "home_name": hn, "away_name": an}, kickoff


def normalize_fixtures(fixtures: list, asof: datetime | str) -> list[dict]:
    """Finished FT/AET/PEN with explicit regulation scores and a four-hour buffer.

    fulltime includes stoppage time, but never extra time or a penalty shootout.
    Elapsed minutes and the eventual winner do not identify regulation scores.
    Historical asof uses eventual results from a current API download: it is not
    proof that corrected provider records were available at that historical time.
    """
    cutoff = _utc(asof)
    rows = []
    for item in _unique_fixtures(fixtures):
        status = item["fixture"].get("status")
        if not isinstance(status, dict):
            raise DataError("fixture.status 结构缺失")
        short_status = status.get("short")
        if short_status not in ("FT", "AET", "PEN"):
            continue
        row, kickoff = _fixture_identity(item)
        if kickoff + timedelta(hours=4) > cutoff:
            continue
        context = f"match_id={row['match_id']} status={short_status}"
        try:
            score = item["score"]["fulltime"]
            row["home_goals"] = _integer(score.get("home"), "fulltime.home")
            row["away_goals"] = _integer(score.get("away"), "fulltime.away")
        except DataError as exc:
            raise DataError(f"{context} 常规时间比分无效：{exc}；不得用其他比分替代") from None
        except (KeyError, TypeError, AttributeError):
            raise DataError(f"{context} 缺少有效 score.fulltime；不得用其他比分替代") from None
        halftime = item["score"].get("halftime")
        if isinstance(halftime, dict):
            for side in ("home", "away"):
                half_goals = halftime.get(side)
                if type(half_goals) is int and half_goals >= 0 and half_goals > row[f"{side}_goals"]:
                    raise DataError(f"{context} score.halftime.{side} 大于 score.fulltime.{side}；比分矛盾")
        rows.append(row)
    return sorted(rows, key=lambda row: (row["kickoff"], row["match_id"]))


def normalize_upcoming(fixtures: list, asof: datetime | str, days: int = 7) -> list[dict]:
    """Only confirmed-time, NS fixtures strictly after asof and within horizon."""
    _integer(days, "days", 1)
    cutoff = _utc(asof)
    rows = []
    for item in _unique_fixtures(fixtures):
        status = item["fixture"].get("status")
        if not isinstance(status, dict):
            raise DataError("fixture.status 结构缺失")
        if status.get("short") != "NS":
            continue
        row, kickoff = _fixture_identity(item)
        if cutoff < kickoff <= cutoff + timedelta(days=days):
            rows.append(row)
    return sorted(rows, key=lambda row: (row["kickoff"], row["match_id"]))
