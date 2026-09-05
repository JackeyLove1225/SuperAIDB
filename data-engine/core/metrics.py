"""轻量级监控指标收集——无第三方依赖

收集请求级别的指标，供 dashboard 展示：
- 请求总数（按端点分组）
- 响应时间（平均/最大/P95）
- 错误率（4xx/5xx 占比）
- 最近 N 分钟的时间序列

所有数据存储在内存中，重启后重置（MVP 足够，生产环境可接 Prometheus）。
"""
import re

import time
import threading
from collections import defaultdict, deque
from dataclasses import dataclass


# 保留最近 60 个时间桶（每桶 60 秒，共 1 小时）
BUCKET_COUNT = 60
BUCKET_SECONDS = 60


@dataclass
class RequestRecord:
    """单次请求记录"""
    method: str
    path: str
    status_code: int
    duration_ms: float
    timestamp: float


class MetricsCollector:
    """指标收集器（线程安全）"""

    def __init__(self):
        self._lock = threading.Lock()
        # 全局统计
        self._total_requests = 0
        self._total_errors = 0  # 4xx + 5xx
        self._total_duration_ms = 0.0
        self._max_duration_ms = 0.0

        # 按端点统计
        self._endpoint_stats: dict[str, dict] = defaultdict(lambda: {
            "count": 0,
            "errors": 0,
            "total_duration_ms": 0.0,
            "max_duration_ms": 0.0,
        })

        # 时间序列（每桶 60 秒）
        self._time_buckets: deque = deque(maxlen=BUCKET_COUNT)
        for _ in range(BUCKET_COUNT):
            self._time_buckets.append({
                "timestamp": 0,
                "count": 0,
                "errors": 0,
                "total_duration_ms": 0.0,
            })

        # 最近的慢请求（响应时间 > 3s）
        self._slow_requests: deque = deque(maxlen=20)

        # 启动时间
        self._start_time = time.time()

    def record(self, method: str, path: str, status_code: int, duration_ms: float):
        """记录一次请求"""
        with self._lock:
            now = time.time()

            # 全局统计
            self._total_requests += 1
            is_error = status_code >= 400
            if is_error:
                self._total_errors += 1
            self._total_duration_ms += duration_ms
            if duration_ms > self._max_duration_ms:
                self._max_duration_ms = duration_ms

            # 端点统计（归一化路径，移除 ID 等动态部分）
            normalized_path = self._normalize_path(path)
            stats = self._endpoint_stats[normalized_path]
            stats["count"] += 1
            if is_error:
                stats["errors"] += 1
            stats["total_duration_ms"] += duration_ms
            if duration_ms > stats["max_duration_ms"]:
                stats["max_duration_ms"] = duration_ms

            # 时间桶
            bucket_idx = int(now // BUCKET_SECONDS) % BUCKET_COUNT
            bucket = self._time_buckets[bucket_idx]
            bucket_ts = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
            # 如果桶过期了，重置
            if bucket["timestamp"] != bucket_ts:
                bucket["timestamp"] = bucket_ts
                bucket["count"] = 0
                bucket["errors"] = 0
                bucket["total_duration_ms"] = 0.0
            bucket["count"] += 1
            if is_error:
                bucket["errors"] += 1
            bucket["total_duration_ms"] += duration_ms

            # 慢请求
            if duration_ms > 3000:
                self._slow_requests.append({
                    "method": method,
                    "path": normalized_path,
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 1),
                    "timestamp": now,
                })

    def _normalize_path(self, path: str) -> str:
        """归一化路径，将动态 ID 替换为 :id"""
        # 替换数字 ID
        normalized = re.sub(r'/\d+', '/:id', path)
        # 替换 UUID
        normalized = re.sub(r'/[a-f0-9-]{36}', '/:uuid', normalized)
        return normalized

    def get_summary(self) -> dict:
        """获取汇总指标"""
        with self._lock:
            uptime = time.time() - self._start_time
            avg_duration = (
                self._total_duration_ms / self._total_requests
                if self._total_requests > 0 else 0
            )
            error_rate = (
                self._total_errors / self._total_requests * 100
                if self._total_requests > 0 else 0
            )

            # 端点统计
            endpoints = []
            for path, stats in sorted(
                self._endpoint_stats.items(),
                key=lambda x: x[1]["count"],
                reverse=True,
            ):
                ep_avg = stats["total_duration_ms"] / stats["count"] if stats["count"] > 0 else 0
                ep_error_rate = stats["errors"] / stats["count"] * 100 if stats["count"] > 0 else 0
                endpoints.append({
                    "path": path,
                    "count": stats["count"],
                    "errors": stats["errors"],
                    "error_rate": round(ep_error_rate, 1),
                    "avg_duration_ms": round(ep_avg, 1),
                    "max_duration_ms": round(stats["max_duration_ms"], 1),
                })

            # 时间序列（最近 1 小时，每分钟一个点）
            now = time.time()
            current_bucket_ts = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
            time_series = []
            for i in range(BUCKET_COUNT - 1, -1, -1):
                bucket_idx = (int(now // BUCKET_SECONDS) - i) % BUCKET_COUNT
                bucket = self._time_buckets[bucket_idx]
                if bucket["timestamp"] > 0:
                    avg = bucket["total_duration_ms"] / bucket["count"] if bucket["count"] > 0 else 0
                    time_series.append({
                        "timestamp": bucket["timestamp"],
                        "count": bucket["count"],
                        "errors": bucket["errors"],
                        "avg_duration_ms": round(avg, 1),
                    })
                else:
                    time_series.append({
                        "timestamp": current_bucket_ts - i * BUCKET_SECONDS,
                        "count": 0,
                        "errors": 0,
                        "avg_duration_ms": 0,
                    })

            return {
                "uptime_seconds": round(uptime),
                "total_requests": self._total_requests,
                "total_errors": self._total_errors,
                "error_rate": round(error_rate, 2),
                "avg_duration_ms": round(avg_duration, 1),
                "max_duration_ms": round(self._max_duration_ms, 1),
                "requests_per_minute": round(self._total_requests / (uptime / 60) if uptime > 0 else 0, 1),
                "endpoints": endpoints[:20],  # Top 20 端点
                "time_series": time_series[-60:],  # 最近 60 分钟
                "slow_requests": list(self._slow_requests),
                "alert_thresholds": {
                    "error_rate": 5.0,      # 错误率 > 5% 告警
                    "response_time_ms": 10000,  # 响应时间 > 10s 告警
                },
                "alerts": self._check_alerts(error_rate, avg_duration),
            }

    def _check_alerts(self, error_rate: float, avg_duration: float) -> list[dict]:
        """检查告警条件"""
        alerts = []
        if error_rate > 5.0:
            alerts.append({
                "level": "warning",
                "message": f"错误率过高: {error_rate:.1f}%（阈值 5%）",
                "metric": "error_rate",
                "value": round(error_rate, 1),
                "threshold": 5.0,
            })
        if avg_duration > 10000:
            alerts.append({
                "level": "warning",
                "message": f"平均响应时间过长: {avg_duration:.0f}ms（阈值 10000ms）",
                "metric": "avg_duration",
                "value": round(avg_duration, 0),
                "threshold": 10000,
            })
        return alerts

    def reset(self):
        """重置所有指标"""
        with self._lock:
            self._total_requests = 0
            self._total_errors = 0
            self._total_duration_ms = 0.0
            self._max_duration_ms = 0.0
            self._endpoint_stats.clear()
            self._slow_requests.clear()
            self._start_time = time.time()


# 全局指标收集器
_collector = MetricsCollector()


def get_metrics_collector() -> MetricsCollector:
    return _collector
