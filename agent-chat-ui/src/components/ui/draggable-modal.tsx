"use client";

import React, { useState, useEffect, useRef } from "react";

/**
 * 可拖拽 + 可自由调整大小的模态框外壳
 *
 * 特性：
 * - 标题栏可拖拽移动整个窗口（mousedown → mousemove → mouseup）
 * - 8 个自定义 resize 手柄（4 边 + 4 角），支持双向/四向调整大小
 * - 初始居中显示
 * - 拖拽/resize 边界限制（不超出视口、不小于 minSize、不大于 maxSize）
 * - 尺寸通过 state 管理，与 React 协同（避免 ref 直接操作 DOM 被重渲染覆盖）
 */

interface DraggableModalProps {
  title: React.ReactNode;
  children: React.ReactNode;
  onClose: () => void;
  initialWidth?: number;
  initialHeight?: number;
  minWidth?: number;
  minHeight?: number;
  /** 最大宽度（px），默认 95vw */
  maxWidth?: number;
  /** 最大高度（px），默认 92vh */
  maxHeight?: number;
  /** 遮罩层 z-index，默认 50 */
  zIndex?: number;
  /** 标题栏额外类名（用于风险确认等特殊样式） */
  titleClassName?: string;
}

type ResizeDir = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

export default function DraggableModal({
  title,
  children,
  onClose,
  initialWidth = 900,
  initialHeight,
  minWidth = 400,
  minHeight = 300,
  maxWidth,
  maxHeight,
  zIndex = 50,
  titleClassName = "",
}: DraggableModalProps) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const modalRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const dragOffsetRef = useRef({ x: 0, y: 0 });
  const resizingRef = useRef<{
    dir: ResizeDir;
    startX: number;
    startY: number;
    startBox: { x: number; y: number; w: number; h: number };
  } | null>(null);

  // 初始居中 + 设置初始尺寸
  useEffect(() => {
    const maxW = maxWidth || window.innerWidth - 40;
    const maxH = maxHeight || window.innerHeight - 80;
    const w = Math.min(initialWidth, maxW);
    const h = initialHeight || Math.min(520, maxH);
    setPos({
      x: Math.max(20, (window.innerWidth - w) / 2),
      y: Math.max(20, (window.innerHeight - h) / 2 - 20),
    });
    setSize({ w, h });
  }, [initialWidth, initialHeight, maxWidth, maxHeight]);

  // 全局拖拽 + resize 事件（挂载一次，通过 ref 控制开关）
  useEffect(() => {
    const handleMove = (e: MouseEvent) => {
      if (draggingRef.current) {
        // ── 拖拽移动 ──
        const newX = Math.max(
          0,
          Math.min(window.innerWidth - 80, e.clientX - dragOffsetRef.current.x),
        );
        const newY = Math.max(
          0,
          Math.min(
            window.innerHeight - 40,
            e.clientY - dragOffsetRef.current.y,
          ),
        );
        setPos({ x: newX, y: newY });
      } else if (resizingRef.current) {
        // ── resize 调整大小 ──
        const r = resizingRef.current;
        const dx = e.clientX - r.startX;
        const dy = e.clientY - r.startY;
        const maxW = maxWidth || window.innerWidth - 40;
        const maxH = maxHeight || window.innerHeight - 80;

        let { x, y, w, h } = r.startBox;
        const dir = r.dir;

        // 东边：宽度 = startW + dx
        if (dir.includes("e")) {
          w = Math.max(minWidth, Math.min(maxW, r.startBox.w + dx));
        }
        // 南边：高度 = startH + dy
        if (dir.includes("s")) {
          h = Math.max(minHeight, Math.min(maxH, r.startBox.h + dy));
        }
        // 西边：宽度 = startW - dx，左边缘跟随右移
        if (dir.includes("w")) {
          const newW = Math.max(minWidth, Math.min(maxW, r.startBox.w - dx));
          x = r.startBox.x + (r.startBox.w - newW);
          w = newW;
        }
        // 北边：高度 = startH - dy，上边缘跟随下移
        if (dir.includes("n")) {
          const newH = Math.max(minHeight, Math.min(maxH, r.startBox.h - dy));
          y = r.startBox.y + (r.startBox.h - newH);
          h = newH;
        }
        // 边界保护：不允许拖出视口
        if (x < 0) {
          w += x;
          x = 0;
        }
        if (y < 0) {
          h += y;
          y = 0;
        }
        if (x + w > window.innerWidth) w = window.innerWidth - x;
        if (y + h > window.innerHeight) h = window.innerHeight - y;

        setPos({ x, y });
        setSize({ w, h });
      }
    };
    const handleUp = () => {
      draggingRef.current = false;
      resizingRef.current = null;
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
  }, [minWidth, minHeight, maxWidth, maxHeight]);

  // 标题栏 mousedown → 开始拖拽移动
  const handleTitleMouseDown = (e: React.MouseEvent) => {
    // 不拦截关闭按钮等可交互元素
    const target = e.target as HTMLElement;
    if (
      target.closest("button") ||
      target.closest("select") ||
      target.closest("input")
    )
      return;
    draggingRef.current = true;
    // 记录鼠标相对模态框左上角的偏移
    const rect = modalRef.current?.getBoundingClientRect();
    if (rect) {
      dragOffsetRef.current = {
        x: e.clientX - rect.left,
        y: e.clientY - rect.top,
      };
    }
    document.body.style.userSelect = "none"; // 拖拽时禁选文本
  };

  // resize 手柄 mousedown → 开始调整大小
  const handleResizeMouseDown = (dir: ResizeDir) => (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    const rect = modalRef.current?.getBoundingClientRect();
    if (!rect) return;
    resizingRef.current = {
      dir,
      startX: e.clientX,
      startY: e.clientY,
      startBox: { x: rect.left, y: rect.top, w: rect.width, h: rect.height },
    };
    document.body.style.userSelect = "none";
  };

  // 8 个 resize 手柄的样式（贴在边缘内部，hover 高亮）
  const handleStyles: Record<ResizeDir, React.CSSProperties> = {
    n: { top: 0, left: 8, right: 8, height: 6, cursor: "ns-resize" },
    s: { bottom: 0, left: 8, right: 8, height: 6, cursor: "ns-resize" },
    e: { right: 0, top: 8, bottom: 8, width: 6, cursor: "ew-resize" },
    w: { left: 0, top: 8, bottom: 8, width: 6, cursor: "ew-resize" },
    ne: { top: 0, right: 0, width: 12, height: 12, cursor: "nesw-resize" },
    nw: { top: 0, left: 0, width: 12, height: 12, cursor: "nwse-resize" },
    se: { bottom: 0, right: 0, width: 12, height: 12, cursor: "nwse-resize" },
    sw: { bottom: 0, left: 0, width: 12, height: 12, cursor: "nesw-resize" },
  };

  return (
    <div
      className="fixed inset-0 flex items-center justify-center bg-black/40"
      style={{ zIndex }}
      onClick={onClose}
    >
      <div
        ref={modalRef}
        className="absolute flex flex-col overflow-hidden rounded-[18px] bg-white shadow-[0_8px_40px_-8px_rgba(0,0,0,0.16),0_1px_2px_rgba(0,0,0,0.05)] ring-1 ring-[#ececec] dark:bg-zinc-900"
        style={{
          left: pos?.x ?? 0,
          top: pos?.y ?? 0,
          width: size?.w ?? initialWidth,
          height: size?.h ?? initialHeight,
          minWidth,
          minHeight,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 标题栏——可拖拽移动（cursor-move + onMouseDown） */}
        <div
          className={`flex shrink-0 cursor-move items-center justify-between border-b border-[#ececec] px-5 py-3.5 dark:border-zinc-700 ${titleClassName}`}
          onMouseDown={handleTitleMouseDown}
        >
          <h2 className="text-lg font-bold text-zinc-900 select-none dark:text-zinc-100">
            {title}
          </h2>
          <button
            onClick={onClose}
            className="rounded p-1 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700 dark:hover:bg-zinc-800"
          >
            ✕
          </button>
        </div>
        {/* 内容区——独立滚动，min-w-0 修复横向滚动 */}
        <div className="min-w-0 flex-1 overflow-auto p-6">{children}</div>

        {/* 8 个 resize 手柄——绝对定位贴边缘，hover 高亮 */}
        {(Object.keys(handleStyles) as ResizeDir[]).map((dir) => (
          <div
            key={dir}
            onMouseDown={handleResizeMouseDown(dir)}
            className="absolute z-10 transition-colors hover:bg-blue-400/30"
            style={handleStyles[dir]}
          />
        ))}
      </div>
    </div>
  );
}
