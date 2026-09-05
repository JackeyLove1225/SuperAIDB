"use client";

import type { ReactNode } from "react";
import DraggableModal from "@/components/ui/draggable-modal";

/** 通用模态框遮罩组件 */
export default function ModalOverlay({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  // 问题5：使用 DraggableModal 替代固定居中的遮罩，支持自由拖拽+双向调整大小
  return (
    <DraggableModal
      title={title}
      onClose={onClose}
    >
      {children}
    </DraggableModal>
  );
}
