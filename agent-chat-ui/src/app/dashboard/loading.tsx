/**
 * Next.js App Router 自动 Suspense 边界
 *
 * 当 dashboard 及其子路由（schema-designer / datasources / tables）首次加载时，
 * Next.js 会先渲染此 loading.tsx，避免白屏 + 突然闪烁。
 *
 * 之前没有 loading.tsx，路由切换时浏览器要等组件挂载 + 数据请求都完成
 * 才显示内容，给人"卡顿"的感觉。现在用骨架屏先占位。
 */
export default function DashboardLoading() {
  return (
    <div className="flex h-[calc(100vh-49px)] items-center justify-center bg-zinc-50 dark:bg-zinc-950">
      <div className="flex flex-col items-center gap-3">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-600 dark:border-zinc-700 dark:border-t-zinc-300" />
        <div className="text-sm text-zinc-500 dark:text-zinc-400">加载中...</div>
      </div>
    </div>
  );
}
