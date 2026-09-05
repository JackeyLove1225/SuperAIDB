"use client";

import React, { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { showError } from "@/components/ui/error-modal";
import { signMedia, withSig } from "@/lib/media-sign";

/**
 * 文件预览独立页（展示板模式 20260808）
 *
 * 由 URL ?path=<uploads 相对路径> 驱动，直接走 Management API:
 * - 图片/PDF: /api/mgmt/api/files/raw?path=... （浏览器原生渲染，Range 流式）
 * - Office 文档: /api/mgmt/api/preview/pdf?path=... （LibreOffice 转 PDF）
 *
 * 认证接线（媒体签名）：iframe/img/下载锚点带不了 Bearer，先调 /api/media/sign
 * 签出 sig 拼进资源 URL 再渲染（无认证模式 sign 端点同样可用，同一代码路径）。
 *
 * 不依赖任何对话/上传状态——纯展示板能力。
 */

const IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"];
// 与后端 office_converter.SUPPORTED_EXTS 对齐（可转 PDF 预览）
const OFFICE_EXTS = [
  "pdf",
  "docx",
  "doc",
  "rtf",
  "odt",
  "xlsx",
  "xls",
  "ods",
  "pptx",
  "ppt",
  "odp",
];

function fileExt(path: string): string {
  const m = path.split("?").shift() ?? path;
  return m.includes(".") ? (m.split(".").pop() ?? "").toLowerCase() : "";
}

function PreviewBody() {
  const searchParams = useSearchParams();
  const path = searchParams.get("path") ?? "";
  const [error, setError] = useState<string | null>(null);
  // 签名后的资源 URL（sig 签名对象是 path 原值，三个端点共用一次签名）
  const [urls, setUrls] = useState<{
    raw: string;
    preview: string;
    download: string;
  } | null>(null);

  // 防止 path 为空或明显越界（服务端还有二次校验，这里仅兜底 UI）
  useEffect(() => {
    if (!path) {
      setError("缺少 path 参数（格式: /preview?path=uploads/xx.pdf）");
      return;
    }
    setError(null);
    setUrls(null);
    let cancelled = false;
    (async () => {
      try {
        const sig = await signMedia(path, "upload");
        if (cancelled) return;
        const raw = `/api/mgmt/api/files/raw?path=${encodeURIComponent(path)}`;
        const preview = `/api/mgmt/api/preview/pdf?path=${encodeURIComponent(path)}`;
        setUrls({
          raw: withSig(raw, sig),
          preview: withSig(preview, sig),
          download: withSig(`${raw}&download=1`, sig),
        });
      } catch (e) {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : String(e);
        setError(`签名失败: ${msg}`);
        showError(`文件签名失败: ${msg}`);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [path]);

  if (!path) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-zinc-500">
        {error ?? "缺少文件路径"}
      </div>
    );
  }

  const ext = fileExt(path);
  const isImage = IMAGE_EXTS.includes(ext);
  const isOffice = OFFICE_EXTS.includes(ext);

  let content: React.ReactNode;
  if (error) {
    content = (
      <div className="flex h-full items-center justify-center text-sm text-red-500">
        {error}
      </div>
    );
  } else if (!urls) {
    content = (
      <div className="flex h-full items-center justify-center text-sm text-zinc-400">
        签名加载中...
      </div>
    );
  } else if (isImage) {
    content = (
      <div className="flex h-full items-center justify-center overflow-auto p-6">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={urls.raw}
          alt={path}
          className="max-h-full max-w-full object-contain"
        />
      </div>
    );
  } else if (ext === "pdf" || isOffice) {
    content = (
      <iframe
        src={urls.preview}
        title={path}
        className="h-full w-full border-0"
      />
    );
  } else {
    content = (
      <div className="flex h-full items-center justify-center text-sm text-zinc-500">
        该文件类型（.{ext || "未知"}）暂不支持在线预览，可下载查看。
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* 顶栏：文件名 + 下载 + 返回 */}
      <div className="flex shrink-0 items-center justify-between border-b border-[#ececec] px-4 py-2.5">
        <div className="min-w-0">
          <Link
            href="/dashboard/schema-designer"
            className="mr-3 text-sm text-zinc-400 hover:text-zinc-700"
          >
            ← 返回
          </Link>
          <span
            className="truncate font-mono text-sm text-zinc-700"
            title={path}
          >
            {path}
          </span>
        </div>
        {urls ? (
          <a
            href={urls.download}
            className="shrink-0 rounded-lg border border-zinc-200/70 px-3 py-1.5 text-sm text-zinc-600 transition-colors hover:bg-zinc-100"
          >
            下载
          </a>
        ) : (
          <span className="shrink-0 rounded-lg border border-zinc-200/70 px-3 py-1.5 text-sm text-zinc-300">
            下载
          </span>
        )}
      </div>
      {/* 预览内容 */}
      <div className="min-h-0 flex-1 bg-zinc-100">{content}</div>
    </div>
  );
}

export default function PreviewPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center text-sm text-zinc-400">
          加载中...
        </div>
      }
    >
      <PreviewBody />
    </Suspense>
  );
}
