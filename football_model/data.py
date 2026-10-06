"""Bounded API-Football client and strict 90-minute result normalization.

Official protocol references (read 2026-09-21):
https://www.api-football.com/documentation-v3
https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide
https://www.api-football.com/news/post/how-ratelimit-works

No request to an authenticated endpoint is made during import or tests. Optional
fundamentals endpoints collect evidence; coverage flags do not implement a lineup model.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
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


class RequestBudgetError(APIError):
    """An HTTP attempt was refused to preserve a known request budget."""


class RequestDeadlineError(RequestBudgetError):
    """Network work stopped early enough to save the run's reports and state."""


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
    """Provider-aware request budgets shared by all competitions in one run.

    Optional max_requests includes /status, retries and every network attempt.
    Daily allowance comes from status/headers, never a newly reset local quota.
    Current-season fetches bypass disk cache by default. Historical cache hits
    keep their original fetched_at and are never relabelled as fresh downloads.
    Optional lookups can reserve requests for later required work. The known
    daily balance is conservatively reduced for every paid attempt, including
    failures without headers; daily_remaining_reported retains the last provider
    balance (header, or limit minus usage from status). Unknown stays None.
    """

    def __init__(self, data_dir: str | Path, max_requests: int | None = None,
                 timeout: float = 25, max_retries: int = 2,
                 min_interval: float | None = None, *, daily_limit: int | None = None,
                 max_run_seconds: float | None = None, opener=None, sleep=None,
                 clock=None, monotonic=None):
        for label, value in (("max_requests", max_requests), ("daily_limit", daily_limit)):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{label} 必须是正整数或 None")
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            raise ValueError("max_retries 必须在 0 至 2 之间")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必须为有限正数")
        if min_interval is not None and (type(min_interval) not in (int, float)
                or not math.isfinite(min_interval) or not 0 <= min_interval <= 60):
            raise ValueError("min_interval 必须为 None 或 0 至 60 秒之间的有限数值")
        if max_run_seconds is not None and (type(max_run_seconds) not in (int, float)
                or not math.isfinite(max_run_seconds) or max_run_seconds <= 0):
            raise ValueError("max_run_seconds 必须为 None 或有限正数")
        self._key = os.environ.get("FOOTBALL_API_KEY", "").strip()
        if not self._key:
            raise APIError("未配置 FOOTBALL_API_KEY；请在 GitHub Actions Secrets 中添加密钥")
        self.data_dir = Path(data_dir)
        self.max_requests = max_requests
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = None if min_interval is None else float(min_interval)
        self.daily_limit = daily_limit
        self.max_run_seconds = max_run_seconds
        self.requests_made = 0
        self.daily_remaining: int | None = None
        self.daily_remaining_reported: int | None = None
        self.provider_daily_limit: int | None = None
        self.provider_daily_used: int | None = None
        self._estimated_daily_used: int | None = None
        self.quota_observed_at: str | None = None
        self.quota_source = "unknown"
        self.daily_quota_status = "unknown"
        self._last_response_daily_remaining: int | None = None
        self._last_response_daily_limit: int | None = None
        self.minute_limit: int | None = None
        self.minute_remaining: int | None = None
        self._opener = opener or build_opener(_NoRedirect()).open
        self._sleep = sleep or time.sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._started_at = self._monotonic()
        self._last_request: float | None = None
        self._wait_until: float = 0
        self._minute_window_until: float | None = None
        self._reserved_requests = 0

    @contextmanager
    def reserve_requests(self, requests: int):
        """Protect run and known daily quotas during optional network work.

        Entering this scope does not check or consume quota: cached results can
        still be returned when no optional network allowance remains. Nested
        scopes retain the larger reservation, then restore the previous scope.
        Required requests outside the scope can use the protected allowance.
        """
        if type(requests) is not int or requests < 0:
            raise ValueError("reserve_requests 必须是非负整数")
        previous = self._reserved_requests
        self._reserved_requests = max(previous, requests)
        try:
            yield
        finally:
            self._reserved_requests = previous

    def _redact(self, value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text.replace(self._key, "[REDACTED]")[:1500]

    def _pause(self, seconds: float) -> None:
        self._check_runtime(seconds)
        # No individual blocking sleep is longer than 60 seconds.
        if seconds > 60:
            raise APIError("接口要求等待超过 60 秒；请稍后重新运行")
        if seconds > 0:
            self._sleep(seconds)

    def _check_runtime(self, required_seconds: float = 0) -> None:
        if (self.max_run_seconds is not None
                and self._monotonic() - self._started_at + required_seconds >= self.max_run_seconds):
            raise RequestDeadlineError("达到本次运行软时间预算；停止网络请求并保留时间保存报告和状态")

    def _effective_interval(self) -> float:
        provider = min(60.0, 60.0 / self.minute_limit + 0.01) if self.minute_limit else 0.0
        chosen = (6.5 if self.minute_limit is None else 0.0) if self.min_interval is None else self.min_interval
        return max(chosen, provider)

    def _expire_minute_window(self) -> None:
        if self._minute_window_until is not None and self._monotonic() >= self._minute_window_until:
            # The next real header can establish a new balance. Do not invent
            # a refreshed remaining count when the previous window expires.
            self.minute_remaining = None
            self._minute_window_until = None

    def _observe_daily(self, limit, used, remaining, source) -> None:
        if limit is not None:
            self.provider_daily_limit = limit
        if used is not None:
            self.provider_daily_used = used
            self._estimated_daily_used = used
        limits = [value for value in (self.daily_limit, self.provider_daily_limit) if value is not None]
        allowed = (max(0, min(limits) - self._estimated_daily_used)
                   if limits and self._estimated_daily_used is not None else None)
        if remaining is not None:
            self.daily_remaining_reported = remaining
            allowed = remaining if allowed is None else min(allowed, remaining)
        if used is None and remaining is None and self.daily_remaining is not None and allowed is not None:
            # A limit-only header cannot prove that earlier consumption fell.
            allowed = min(allowed, self.daily_remaining)
        if self.daily_limit is not None and self._estimated_daily_used is None:
            # A larger provider plan's remaining count does not disclose how
            # much of our configured daily allowance has already been spent.
            self.daily_remaining = None
            self.daily_quota_status = "incomplete"
            self.quota_source = source
            self.quota_observed_at = _iso(self._clock())
            return
        if allowed is not None:
            self.daily_remaining = allowed
            self.daily_quota_status = "known"
            self.quota_source = source
            self.quota_observed_at = _iso(self._clock())

    def _rate_headers(self, headers) -> None:
        self._last_response_daily_remaining = None
        self._last_response_daily_limit = None
        if headers is None:
            return
        lowered = {str(k).lower(): v for k, v in headers.items()}
        def number(field, minimum=0):
            try:
                val = int(lowered[field])
                return val if val >= minimum else None
            except (KeyError, TypeError, ValueError):
                return None
        daily_limit = number("x-ratelimit-requests-limit", 1)
        self._last_response_daily_limit = daily_limit
        remaining = number("x-ratelimit-requests-remaining")
        self._last_response_daily_remaining = remaining
        limit = daily_limit if daily_limit is not None else self.provider_daily_limit
        used = max(0, limit - remaining) if limit is not None and remaining is not None else None
        if daily_limit is not None or remaining is not None:
            self._observe_daily(daily_limit, used, remaining, "response_headers")
        minute_limit = number("x-ratelimit-limit", 1)
        if minute_limit is not None:
            self.minute_limit = minute_limit
        minute_remaining = number("x-ratelimit-remaining")
        if minute_remaining is not None:
            self.minute_remaining = minute_remaining
            self._minute_window_until = self._monotonic() + 60
            if minute_remaining == 0:
                self._wait_until = max(self._wait_until, self._minute_window_until)

    def budget_snapshot(self) -> dict:
        """Return only numeric budget evidence and timing; never account data."""
        self._expire_minute_window()
        return {"configured_daily_limit": self.daily_limit,
                "provider_daily_limit": self.provider_daily_limit,
                "provider_daily_used": self.provider_daily_used,
                "daily_remaining": self.daily_remaining,
                "daily_remaining_reported": self.daily_remaining_reported,
                "minute_limit": self.minute_limit, "minute_remaining": self.minute_remaining,
                "max_requests_per_run": self.max_requests, "requests_made": self.requests_made,
                "min_interval_seconds": self._effective_interval(),
                "max_run_seconds": self.max_run_seconds,
                "elapsed_seconds": max(0.0, self._monotonic() - self._started_at),
                "quota_observed_at": self.quota_observed_at, "quota_source": self.quota_source,
                "daily_quota_status": self.daily_quota_status}

    def _check_request_budget(self, *, paid=True) -> None:
        self._check_runtime()
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            raise RequestBudgetError(f"达到本次运行请求上限 {self.max_requests}；停止下载，未完成部分不可当作成功")
        if paid and self.daily_limit is not None and self._estimated_daily_used is None:
            raise RequestBudgetError("日额度证据不完整，无法核算配置上限下的实际余额；先通过 status 核实已用量")
        if paid and self.daily_remaining == 0:
            raise RequestBudgetError("今日剩余额度为 0（供应商回报或已知余额按请求保守扣减）；停止下载，请等待额度恢复")
        reserve = self._reserved_requests
        if reserve and self.max_requests is not None and self.requests_made >= self.max_requests - reserve:
            raise RequestBudgetError(f"本次运行请求预算需预留 {reserve} 次供后续下载；可选查询延后")
        if paid and reserve and self.daily_remaining is not None and self.daily_remaining <= reserve:
            raise RequestBudgetError(f"今日已知请求余额需预留 {reserve} 次供后续下载；可选查询延后")

    def _before_request(self, *, paid=True) -> None:
        self._check_request_budget(paid=paid)
        self._expire_minute_window()
        now = self._monotonic()
        wait = max(0.0, self._wait_until - now)
        if self._last_request is not None:
            wait = max(wait, self._effective_interval() - (now - self._last_request))
        self._check_runtime(wait + self.timeout)
        self._pause(wait)
        self._check_runtime(self.timeout)
        self._expire_minute_window()
        self._last_request = self._monotonic()
        self.requests_made += 1
        if paid and self.daily_remaining is not None:
            # An attempt may reach the provider even if the response is lost.
            # Do not keep spending a stale header balance on subsequent retries.
            self.daily_remaining -= 1
        if paid and self._estimated_daily_used is not None:
            self._estimated_daily_used += 1
        if self.minute_remaining is not None:
            self.minute_remaining = max(0, self.minute_remaining - 1)
            if self.minute_remaining == 0 and self._minute_window_until is not None:
                self._wait_until = max(self._wait_until, self._minute_window_until)

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict:
        if endpoint not in {"/status", "/leagues", "/fixtures", "/venues",
                            "/injuries", "/fixtures/lineups", "/fixtures/statistics"}:
            raise APIError("此客户端不允许该 endpoint")
        url = BASE_URL + endpoint
        if params:
            url += "?" + urlencode(sorted(params.items()))
        for attempt in range(self.max_retries + 1):
            paid = endpoint != "/status"
            self._before_request(paid=paid)
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
                if paid and self.daily_remaining == 0:
                    raise APIError("API-Football 今日额度耗尽；停止重试") from None
                self._check_request_budget(paid=paid)
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
                self._check_request_budget(paid=paid)
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
        """Observe the real daily balance without retaining account details.

        API-Football's status endpoint counts as a network attempt but does not
        consume the subscription's daily request allowance.
        """
        result = self._request("/status", {})
        if not isinstance(result["response"], dict):
            raise APIError("status 接口 response 应为对象")
        payload = result["response"]
        requests = payload.get("requests")
        requests = requests if isinstance(requests, dict) else {}
        used, limit = requests.get("current"), requests.get("limit_day")
        valid = type(used) is int and used >= 0 and type(limit) is int and limit > 0
        if valid:
            header_remaining = self._last_response_daily_remaining
            header_limit = self._last_response_daily_limit
            observed_limit = min(limit, header_limit) if header_limit is not None else limit
            observed_used = used
            remaining = max(0, limit - used)
            if header_remaining is not None:
                observed_used = max(used, max(0, (header_limit or limit) - header_remaining))
                remaining = min(remaining, header_remaining)
            source = "status_and_response_headers" if (header_remaining is not None or header_limit is not None) else "status"
            self._observe_daily(observed_limit, observed_used, remaining, source)
        subscription = payload.get("subscription")
        active = subscription.get("active") if isinstance(subscription, dict) else None
        return {"requests": {"current": used, "limit_day": limit} if valid else {},
                "subscription": {"active": active} if type(active) is bool else {},
                "fetched_at": result["fetched_at"], "from_cache": False}

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

    def fetch_fixture(self, fixture_id: int, *, force: bool = True,
                      max_age_seconds: float = 0) -> dict:
        """Read one provider fixture, preserving its real response observation time.

        Consumers must validate the exact returned fixture/league/team identity
        and status. The default refresh is intentional for pre-match collection.
        """
        _integer(fixture_id, "fixture_id", 1)
        if type(force) is not bool:
            raise ValueError("force 必须为布尔值")
        if (type(max_age_seconds) not in (int, float)
                or not math.isfinite(max_age_seconds) or max_age_seconds < 0):
            raise ValueError("max_age_seconds 必须为有限非负数")
        return self._cached_request("/fixtures", {"id": fixture_id}, force=force,
                                    max_age_seconds=max_age_seconds)

    def fetch_fundamentals(self, endpoint: str, fixture_id: int, *, force: bool = False,
                           max_age_seconds: float = 900) -> dict:
        """Collect fixture-bound facts using the existing quota and cache controls.

        A cached response retains its original fetched_at. Empty responses and
        missing xG do not prove absence of injuries or zero expected goals.
        Historical responses fetched today are not historical pre-match evidence.
        Normalization, match/team identity checks and as-of eligibility are the
        responsibility of football_model.fundamentals. Pagination still fails
        closed in _request; partial lists must never appear complete.
        """
        if endpoint not in {"/injuries", "/fixtures/lineups", "/fixtures/statistics"}:
            raise ValueError("基本面接口必须为 injuries、fixtures/lineups 或 fixtures/statistics")
        _integer(fixture_id, "fixture_id", 1)
        if type(force) is not bool:
            raise ValueError("force 必须为布尔值")
        if (type(max_age_seconds) not in (int, float)
                or not math.isfinite(max_age_seconds) or max_age_seconds < 0):
            raise ValueError("max_age_seconds 必须为有限非负数")
        return self._cached_request(endpoint, {"fixture": fixture_id}, force=force,
                                    max_age_seconds=max_age_seconds)

    def fetch_venues(self, *, venue_id: int | None = None, name: str | None = None,
                     force: bool = False) -> dict:
        """Retrieve country evidence; registration checks exact identities separately.

        Venue lookup shares the existing request/retry budget and redacted cache.
        A name query is only a candidate search, never an association inference.
        """
        if (venue_id is None) == (name is None):
            raise ValueError("场地查询须提供且仅提供 venue_id 或 name。")
        if venue_id is not None:
            params = {"id": _integer(venue_id, "venue_id", 1)}
        else:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("场地查询名称不能为空。")
            params = {"name": name}
        return self._cached_request("/venues", params, force=force,
                                    max_age_seconds=7 * 86400)


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
