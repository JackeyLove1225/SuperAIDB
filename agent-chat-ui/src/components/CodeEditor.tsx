"use client";

import React, { useRef } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";

/**
 * 代码编辑器组件——基于 Monaco Editor
 * 支持语法高亮、格式校验，替代原生 textarea
 */
interface CodeEditorProps {
  value: string;
  onChange: (value: string) => void;
  language?: "json" | "yaml" | "markdown" | "plaintext";
  height?: string;
  readOnly?: boolean;
  placeholder?: string;
}

export default function CodeEditor({
  value,
  onChange,
  language = "json",
  height = "200px",
  readOnly = false,
  placeholder,
}: CodeEditorProps) {
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);

  const handleMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    // 配置 YAML 语法（Monaco 原生不包含 YAML 高亮，用 plaintext 降级）
    if (language === "yaml") {
      monaco.languages.register({ id: "yaml" });
    }
  };

  return (
    <div className="relative w-full overflow-hidden rounded-md border border-zinc-300 dark:border-zinc-600">
      <Editor
        height={height}
        language={language}
        value={value}
        onChange={(val) => onChange(val || "")}
        onMount={handleMount}
        theme="vs-dark"
        options={{
          minimap: { enabled: false },
          fontSize: 13,
          lineNumbers: "off",
          scrollBeyondLastLine: false,
          wordWrap: "on",
          tabSize: 2,
          readOnly,
          automaticLayout: true,
          padding: { top: 8, bottom: 8 },
          fontFamily: "'Cascadia Code', 'Fira Code', 'Consolas', monospace",
          fontLigatures: true,
        }}
      />
      {placeholder && !value && (
        <div className="pointer-events-none absolute left-3 top-2 text-xs text-zinc-400">
          {placeholder}
        </div>
      )}
    </div>
  );
}
