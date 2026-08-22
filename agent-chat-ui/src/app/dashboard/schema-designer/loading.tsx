﻿/** schema-designer Suspense boundary (React Flow is large, slow first load) */
export default function SchemaDesignerLoading() {
  return (
    <div className="flex h-[calc(100vh-49px)] items-center justify-center bg-zinc-50 dark:bg-zinc-950">
      <div className="flex flex-col items-center gap-3">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-600 dark:border-zinc-700 dark:border-t-zinc-300" />
        <div className="text-sm text-zinc-500 dark:text-zinc-400">
          Loading schema designer...
        </div>
      </div>
    </div>
  );
}