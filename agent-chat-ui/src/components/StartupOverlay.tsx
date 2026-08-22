"use client";

import { MGMT_API } from "@/lib/api-fetch";
import { useState, useEffect, useRef } from "react";

// 通过 Next.js 服务端代理访问 Management API（密钥不出服务器）


type ServiceState = "pending" | "ready" | "error";

interface ServiceInfo {
  name: string;
  desc: string;
  state: ServiceState;
}

/** 带超时的 fetch */
async function fetchWithTimeout(url: string, ms: number): Promise<Response | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    const resp = await fetch(url, { signal: controller.signal });
    return resp;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export function StartupOverlay() {
  const [visible, setVisible] = useState(true);
  const [fadingOut, setFadingOut] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [services, setServices] = useState<ServiceInfo[]>([
    { name: "管理服务", desc: "Management API (:2025)", state: "pending" },
  ]);
  const startTime = useRef(Date.now());
  const hiddenRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    // elapsed 的 ref，供 check 函数读取最新值以计算退避（避免闭包陷阱）
    const elapsedRef = { current: 0 };

    const check = async () => {
      if (cancelled || hiddenRef.current) return;

      // 启动只等 Management API (:2025)——展示板唯一后端依赖
      let mgmtOk = false;

      const healthResp = await fetchWithTimeout(`${MGMT_API}/api/health`, 3000);
      if (healthResp?.ok) {
        mgmtOk = true;
      }

      if (cancelled) return;

      setServices([
        {
          name: "管理服务",
          desc: "Management API (:2025)",
          state: mgmtOk ? "ready" : "pending",
        },
      ]);

      // 管理服务就绪 → 淡出
      if (mgmtOk) {
        hiddenRef.current = true;
        setFadingOut(true);
        setTimeout(() => setVisible(false), 800);
        return; // 不再调度下一次轮询
      }

      // 指数退避：500ms → 1000ms → 2000ms → 4000ms（封顶 4s）
      // 原 500ms 固定轮询太频繁，后端冷启动期间会堆积大量失败请求
      const attempt = Math.min(elapsedRef.current, 3); // 0,1,2,3
      const delay = 500 * Math.pow(2, attempt);
      pollTimer = setTimeout(check, delay);
    };

    // 立即检查一次
    check();

    // 计时器（显示已等待时间）
    const timerInterval = setInterval(() => {
      if (!cancelled && !hiddenRef.current) {
        const sec = Math.floor((Date.now() - startTime.current) / 1000);
        setElapsed(sec);
        elapsedRef.current = sec;
      }
    }, 1000);

    return () => {
      cancelled = true;
      if (pollTimer) clearTimeout(pollTimer);
      clearInterval(timerInterval);
    };
  }, []);

  if (!visible) return null;

  const readyCount = services.filter((s) => s.state === "ready").length;
  const progress = (readyCount / services.length) * 100;
  const isSlow = elapsed > 15;

  return (
    <div
      className={`fixed inset-0 z-[100] flex items-center justify-center bg-background transition-opacity duration-700 ${
        fadingOut ? "opacity-0" : "opacity-100"
      }`}
    >
      <div className="flex w-full max-w-md flex-col items-center gap-6 px-8">
        {/* Logo / 标题 */}
        <div className="flex flex-col items-center gap-2">
          <div className="relative flex h-16 w-16 items-center justify-center">
            <div className="absolute inset-0 animate-ping rounded-full bg-zinc-900/10" />
            <div className="relative flex h-12 w-12 items-center justify-center rounded-xl bg-zinc-900 text-xl font-bold tracking-tight text-white">
              S
            </div>
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-foreground">
            SuperAIDB
          </h1>
          <p className="text-sm text-muted-foreground">
            {readyCount === services.length
              ? "启动完成，正在加载..."
              : "正在启动后端服务..."}
          </p>
        </div>

        {/* 进度条 */}
        <div className="w-full">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-zinc-100">
            <div
              className="h-full rounded-full bg-zinc-900 transition-all duration-500 ease-out"
              style={{ width: `${progress}%` }}
            />
          </div>
          <div className="mt-1.5 flex justify-between text-xs text-muted-foreground">
            <span>{readyCount} / {services.length} 就绪</span>
            <span>{elapsed}s</span>
          </div>
        </div>

        {/* 服务状态列表 */}
        <div className="w-full space-y-2">
          {services.map((svc) => (
            <div
              key={svc.name}
              className="flex items-center gap-3 rounded-xl border border-zinc-200/70 bg-white p-3"
            >
              <div className="flex-shrink-0">
                {svc.state === "ready" ? (
                  <div className="flex h-6 w-6 items-center justify-center rounded-full bg-green-500/15">
                    <svg
                      className="h-4 w-4 text-green-500"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={3}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </div>
                ) : (
                  <div className="flex h-6 w-6 items-center justify-center">
                    <div className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-200 border-t-zinc-900" />
                  </div>
                )}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-foreground">
                  {svc.name}
                </div>
                <div className="text-xs text-muted-foreground truncate">
                  {svc.desc}
                </div>
              </div>
              <div className="flex-shrink-0 text-xs">
                {svc.state === "ready" ? (
                  <span className="text-green-500 font-medium">就绪</span>
                ) : (
                  <span className="text-muted-foreground">启动中...</span>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* 慢启动提示 */}
        {isSlow && readyCount < services.length && (
          <div className="w-full rounded-xl border border-amber-200 bg-amber-50 p-3 text-center">
            <p className="text-xs text-amber-600 dark:text-amber-500">
              首次启动需要等待后端服务就绪，请耐心等待...
            </p>
          </div>
        )}

        {/* 错误提示（60 秒后仍未就绪） */}
        {elapsed > 60 && readyCount < services.length && (
          <div className="w-full rounded-xl border border-red-200 bg-red-50 p-3 text-center">
            <p className="text-xs text-red-600">
              启动时间较长，请检查后端服务是否正常
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              可尝试关闭后重新启动，或运行 stop.bat 后再启动
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
