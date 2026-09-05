"use client";

import { apiFetch } from "@/lib/api-fetch";
import React, { useState, useEffect, useCallback } from "react";
import * as YAML from "yaml";

import CodeEditor from "./CodeEditor";
import SchemaEditor from "./SchemaEditor";

// 通过 Next.js 服务端代理访问 Management API（密钥不出服务器）

// ==================== 类型定义 ====================

interface Industry {
  name: string;
  display_name: string;
  description: string;
  has_schemas: boolean;
  is_builtin: boolean;
  schema_count: number;
}

interface IndustryConfig {
  name?: string;
  description?: string;
  expert_role?: string;
  hierarchy_desc?: string;
  default_table_name?: string;
  display_name?: string;
  [key: string]: unknown;
}

interface PromptsResponse {
  prompts: {
    classification_hints?: string;
    decompose_examples?: unknown[];
    router_examples?: unknown[];
    tool_examples?: Record<string, unknown>;
    [key: string]: unknown;
  };
}

interface ConfigResponse {
  config: IndustryConfig;
  prompts: Record<string, unknown>;
  schemas: unknown[];
}

type MessageType = "success" | "error" | "warning";

// ==================== 消息样式 ====================

const messageStyles: Record<MessageType, string> = {
  success:
    "border-green-200 bg-green-50 text-green-800 dark:border-green-800 dark:bg-green-950 dark:text-green-400",
  warning:
    "border-yellow-200 bg-yellow-50 text-yellow-800 dark:border-yellow-800 dark:bg-yellow-950 dark:text-yellow-400",
  error:
    "border-red-200 bg-red-50 text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-400",
};

// ==================== 主组件 ====================

export default function IndustryManager() {
  const [industries, setIndustries] = useState<Industry[]>([]);
  const [currentIndustry, setCurrentIndustry] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState<string | null>(null);
  const [message, setMessage] = useState<{
    type: MessageType;
    text: string;
  } | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editingIndustry, setEditingIndustry] = useState<string | null>(null);

  // 创建表单状态
  const [createForm, setCreateForm] = useState({
    name: "",
    description: "",
    template: "engineering",
  });
  const [creating, setCreating] = useState(false);

  // 加载行业列表和当前行业
  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      // apiFetch 统一收口：非 2xx 抛错进 catch（不再静默渲染空列表）；
      // 401 触发已注册的跳登录处理器——本组件嵌在 /settings（AuthGuard 保护页），跳登录是正确行为
      const [industriesResp, settingsResp] = await Promise.all([
        apiFetch<{ industries?: Industry[] }>("/api/industries"),
        apiFetch<{ industry?: string }>("/api/settings"),
      ]);
      setIndustries(industriesResp.industries || []);
      setCurrentIndustry(settingsResp.industry || "");
    } catch {
      setMessage({ type: "error", text: "无法加载行业列表" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // 成功消息 3 秒后自动消失
  useEffect(() => {
    if (message?.type === "success") {
      const timer = setTimeout(() => setMessage(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [message]);

  // 切换行业
  const switchIndustry = async (name: string) => {
    setSwitching(name);
    setMessage(null);
    try {
      await apiFetch("/api/industries/switch", {
        method: "POST",
        body: JSON.stringify({ industry: name }),
      });
      setCurrentIndustry(name);
      setMessage({
        type: "success",
        text: `已切换到行业 ${name}，请重启后端服务以完全生效`,
      });
      await loadData();
    } catch (e) {
      setMessage({
        type: "error",
        text: e instanceof Error ? e.message : "切换失败",
      });
    } finally {
      setSwitching(null);
    }
  };

  // 创建行业
  const createIndustry = async () => {
    const name = createForm.name.trim();
    if (!name) {
      setMessage({ type: "error", text: "请输入行业名" });
      return;
    }
    if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(name)) {
      setMessage({
        type: "error",
        text: "行业名只能包含字母、数字、下划线，且以字母开头",
      });
      return;
    }
    setCreating(true);
    setMessage(null);
    try {
      await apiFetch("/api/industries/create", {
        method: "POST",
        body: JSON.stringify({
          name,
          description: createForm.description,
          template: createForm.template,
        }),
      });
      setMessage({ type: "success", text: `行业 ${name} 创建成功` });
      setShowCreate(false);
      setCreateForm({ name: "", description: "", template: "engineering" });
      await loadData();
    } catch (e) {
      setMessage({
        type: "error",
        text: e instanceof Error ? e.message : "创建失败",
      });
    } finally {
      setCreating(false);
    }
  };

  // 内置行业作为模板选项
  const templateOptions = industries.filter((i) => i.is_builtin);

  if (loading) {
    return (
      <div className="py-8 text-center text-sm text-zinc-500">加载中...</div>
    );
  }

  return (
    <div>
      {/* 消息提示 */}
      {message && (
        <div
          className={`mb-4 rounded-md border p-3 text-sm ${messageStyles[message.type]}`}
        >
          {message.text}
        </div>
      )}

      {/* 顶部操作栏 */}
      <div className="mb-4 flex items-center justify-between">
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          当前行业：
          <span className="font-medium text-zinc-900 dark:text-zinc-100">
            {currentIndustry || "-"}
          </span>
        </p>
        <div className="flex gap-2">
          <button
            onClick={() => setShowCreate(!showCreate)}
            className="rounded-lg border border-zinc-200/70 bg-white px-4 py-2 text-sm font-medium text-zinc-700 transition-colors hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:bg-transparent dark:text-zinc-300 dark:hover:bg-zinc-800"
            disabled={creating}
          >
            {showCreate ? "取消创建" : "创建新行业"}
          </button>
        </div>
      </div>

      {/* 创建新行业表单 */}
      {showCreate && (
        <div className="mb-4 rounded-lg border border-zinc-200 bg-zinc-50 p-4 dark:border-zinc-700 dark:bg-zinc-800/50">
          <h3 className="mb-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            创建新行业
          </h3>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
                行业名（英文，字母开头，可含数字下划线）
              </label>
              <input
                type="text"
                value={createForm.name}
                onChange={(e) =>
                  setCreateForm({ ...createForm, name: e.target.value })
                }
                placeholder="例如：manufacturing"
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
                从模板创建
              </label>
              <select
                value={createForm.template}
                onChange={(e) =>
                  setCreateForm({ ...createForm, template: e.target.value })
                }
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
              >
                {templateOptions.length > 0 ? (
                  templateOptions.map((t) => (
                    <option
                      key={t.name}
                      value={t.name}
                    >
                      {t.name}
                      {t.description ? ` - ${t.description}` : ""}
                    </option>
                  ))
                ) : (
                  <option value="engineering">engineering</option>
                )}
              </select>
            </div>
            <div className="sm:col-span-2">
              <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
                描述
              </label>
              <input
                type="text"
                value={createForm.description}
                onChange={(e) =>
                  setCreateForm({ ...createForm, description: e.target.value })
                }
                placeholder="行业描述"
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
              />
            </div>
          </div>
          <div className="mt-3 flex justify-end gap-2">
            <button
              onClick={() => setShowCreate(false)}
              className="rounded-md bg-zinc-200 px-4 py-2 text-sm font-medium hover:bg-zinc-300 dark:bg-zinc-700 dark:text-zinc-100"
            >
              取消
            </button>
            <button
              onClick={createIndustry}
              disabled={creating}
              className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-700 disabled:opacity-50"
            >
              {creating ? "创建中..." : "创建"}
            </button>
          </div>
        </div>
      )}

      {/* 行业列表 */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {industries.map((industry) => {
          const isCurrent = industry.name === currentIndustry;
          const isEditing = editingIndustry === industry.name;
          return (
            <div key={industry.name}>
              <div
                className={`rounded-xl border p-4 transition-colors ${
                  isCurrent
                    ? "border-zinc-400 bg-zinc-50/60 dark:border-zinc-600 dark:bg-zinc-800/50"
                    : "border-zinc-200/70 bg-white dark:border-zinc-800 dark:bg-zinc-900"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                        {industry.display_name || industry.name}
                      </h3>
                      {isCurrent && (
                        <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
                          当前行业
                        </span>
                      )}
                      {industry.is_builtin && (
                        <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                          内置
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
                      {industry.description || "无描述"}
                    </p>
                    {industry.schema_count > 0 && (
                      <p className="mt-1 text-xs text-zinc-400">
                        {industry.schema_count} 个表结构
                      </p>
                    )}
                  </div>
                </div>
                <div className="mt-3 flex gap-2">
                  {!isCurrent && (
                    <button
                      onClick={() => switchIndustry(industry.name)}
                      disabled={switching !== null}
                      className="rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-700 disabled:opacity-50"
                    >
                      {switching === industry.name ? "切换中..." : "切换"}
                    </button>
                  )}
                  <button
                    onClick={() =>
                      setEditingIndustry(isEditing ? null : industry.name)
                    }
                    className={`rounded-md px-3 py-1.5 text-xs font-medium ${
                      isEditing
                        ? "bg-zinc-200 text-zinc-700 hover:bg-zinc-300 dark:bg-zinc-700 dark:text-zinc-100"
                        : "bg-zinc-100 text-zinc-700 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300"
                    }`}
                  >
                    {isEditing ? "收起配置" : "编辑配置"}
                  </button>
                </div>
              </div>

              {/* 编辑配置面板 */}
              {isEditing && (
                <div className="mt-2">
                  <EditConfigPanel
                    industryName={industry.name}
                    showMessage={setMessage}
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>

      {industries.length === 0 && !loading && (
        <p className="py-8 text-center text-sm text-zinc-500">暂无行业配置</p>
      )}
    </div>
  );
}

// ==================== 编辑配置面板 ====================

type TabKey = "config" | "prompts" | "fields" | "schemas";

function EditConfigPanel({
  industryName,
  showMessage,
}: {
  industryName: string;
  showMessage: (msg: { type: MessageType; text: string }) => void;
}) {
  const [activeTab, setActiveTab] = useState<TabKey>("config");

  // 标签页1：基本信息
  const [configForm, setConfigForm] = useState({
    name: "",
    description: "",
    expert_role: "",
    hierarchy_desc: "",
  });
  const [configLoading, setConfigLoading] = useState(false);
  const [configSaving, setConfigSaving] = useState(false);

  // 标签页2：AI 提示词
  const [decomposeText, setDecomposeText] = useState("");
  const [routerText, setRouterText] = useState("");
  const [toolText, setToolText] = useState("");
  // 文件处理提示词（markdown 编辑）
  const [classificationHints, setClassificationHints] = useState("");
  const [schemaHints, setSchemaHints] = useState("");
  const [extractionPrompt, setExtractionPrompt] = useState("");
  // 术语适配配置（行为别名/对象别名/表别名）
  const [terminologyText, setTerminologyText] = useState("");
  const [promptsLoading, setPromptsLoading] = useState(false);
  const [promptsSaving, setPromptsSaving] = useState(false);
  const [promptsLoaded, setPromptsLoaded] = useState(false);
  const [promptsError, setPromptsError] = useState<string | null>(null);

  // 标签页3：字段别名
  const [fieldsText, setFieldsText] = useState("");
  const [fieldsLoading, setFieldsLoading] = useState(false);
  const [fieldsSaving, setFieldsSaving] = useState(false);
  const [fieldsLoaded, setFieldsLoaded] = useState(false);
  const [fieldsError, setFieldsError] = useState<string | null>(null);

  // 加载基本信息配置
  const loadConfig = useCallback(async () => {
    setConfigLoading(true);
    try {
      const data = await apiFetch<ConfigResponse>(
        `/api/industries/${industryName}/config`,
      );
      const cfg = data.config || {};
      setConfigForm({
        name: cfg.name || industryName,
        description: cfg.description || "",
        expert_role: cfg.expert_role || "",
        hierarchy_desc: cfg.hierarchy_desc || "",
      });
    } catch {
      showMessage({ type: "error", text: "加载配置失败" });
    } finally {
      setConfigLoading(false);
    }
  }, [industryName, showMessage]);

  // 加载提示词配置
  const loadPrompts = useCallback(async () => {
    setPromptsLoading(true);
    try {
      const data = await apiFetch<PromptsResponse>(
        `/api/industries/${industryName}/prompts`,
      );
      const prompts = data.prompts || {};
      setDecomposeText(
        JSON.stringify(prompts.decompose_examples || [], null, 2),
      );
      setRouterText(JSON.stringify(prompts.router_examples || [], null, 2));
      setToolText(JSON.stringify(prompts.tool_examples || {}, null, 2));
      // 加载文件处理提示词
      setClassificationHints(
        typeof prompts.classification_hints === "string"
          ? prompts.classification_hints
          : "",
      );
      setSchemaHints(
        typeof prompts.schema_hints === "string" ? prompts.schema_hints : "",
      );
      const customPrompts = prompts.custom_prompts;
      setExtractionPrompt(
        customPrompts &&
          typeof customPrompts === "object" &&
          "extraction_prompt" in customPrompts &&
          typeof (customPrompts as Record<string, unknown>)
            .extraction_prompt === "string"
          ? ((customPrompts as Record<string, unknown>)
              .extraction_prompt as string)
          : "",
      );
      // 加载术语适配配置
      setTerminologyText(JSON.stringify(prompts.terminology || {}, null, 2));
      setPromptsError(null);
      setPromptsLoaded(true);
    } catch {
      showMessage({ type: "error", text: "加载提示词失败" });
    } finally {
      setPromptsLoading(false);
    }
  }, [industryName, showMessage]);

  // 加载字段别名配置
  const loadFields = useCallback(async () => {
    setFieldsLoading(true);
    try {
      const data = await apiFetch<{ fields?: Record<string, unknown> }>(
        `/api/industries/${industryName}/fields`,
      );
      const fields = data.fields || {};
      setFieldsText(YAML.stringify(fields));
      setFieldsError(null);
      setFieldsLoaded(true);
    } catch {
      showMessage({ type: "error", text: "加载字段别名失败" });
    } finally {
      setFieldsLoading(false);
    }
  }, [industryName, showMessage]);

  // 初始加载第一个标签页
  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  // 切换标签页时按需加载
  const handleTabChange = (tab: TabKey) => {
    setActiveTab(tab);
    if (tab === "prompts" && !promptsLoaded) loadPrompts();
    if (tab === "fields" && !fieldsLoaded) loadFields();
  };

  // 保存基本信息
  const saveConfig = async () => {
    setConfigSaving(true);
    try {
      await apiFetch(`/api/industries/${industryName}/config`, {
        method: "PUT",
        body: JSON.stringify({
          name: configForm.name,
          description: configForm.description,
          expert_role: configForm.expert_role,
          hierarchy_desc: configForm.hierarchy_desc,
        }),
      });
      showMessage({ type: "success", text: "基本信息已保存" });
    } catch (e) {
      showMessage({
        type: "error",
        text: e instanceof Error ? e.message : "保存失败",
      });
    } finally {
      setConfigSaving(false);
    }
  };

  // 保存提示词
  const savePrompts = async () => {
    // 先校验 JSON
    let decomposeVal: unknown;
    let routerVal: unknown;
    let toolVal: unknown;
    try {
      decomposeVal = decomposeText.trim() ? JSON.parse(decomposeText) : [];
    } catch {
      setPromptsError("任务拆解示例 JSON 格式错误");
      return;
    }
    try {
      routerVal = routerText.trim() ? JSON.parse(routerText) : [];
    } catch {
      setPromptsError("语义路由示例 JSON 格式错误");
      return;
    }
    try {
      toolVal = toolText.trim() ? JSON.parse(toolText) : {};
    } catch {
      setPromptsError("工具描述示例 JSON 格式错误");
      return;
    }
    let terminologyVal: unknown;
    try {
      terminologyVal = terminologyText.trim()
        ? JSON.parse(terminologyText)
        : {};
    } catch {
      setPromptsError("术语适配配置 JSON 格式错误");
      return;
    }
    setPromptsError(null);
    setPromptsSaving(true);
    try {
      await apiFetch(`/api/industries/${industryName}/prompts`, {
        method: "PUT",
        body: JSON.stringify({
          classification_hints: classificationHints,
          schema_hints: schemaHints,
          custom_prompts: { extraction_prompt: extractionPrompt },
          decompose_examples: decomposeVal,
          router_examples: routerVal,
          tool_examples: toolVal,
          terminology: terminologyVal,
        }),
      });
      showMessage({ type: "success", text: "提示词已保存" });
    } catch (e) {
      showMessage({
        type: "error",
        text: e instanceof Error ? e.message : "保存失败",
      });
    } finally {
      setPromptsSaving(false);
    }
  };

  // 保存字段别名
  const saveFields = async () => {
    // 先校验 YAML
    let fieldsVal: unknown;
    try {
      fieldsVal = YAML.parse(fieldsText) ?? null;
    } catch {
      setFieldsError("YAML 格式错误，无法解析");
      return;
    }
    setFieldsError(null);
    setFieldsSaving(true);
    try {
      await apiFetch(`/api/industries/${industryName}/fields`, {
        method: "PUT",
        body: JSON.stringify({ fields: fieldsVal }),
      });
      showMessage({ type: "success", text: "字段别名已保存" });
    } catch (e) {
      showMessage({
        type: "error",
        text: e instanceof Error ? e.message : "保存失败",
      });
    } finally {
      setFieldsSaving(false);
    }
  };

  const tabs: { key: TabKey; label: string }[] = [
    { key: "config", label: "基本信息" },
    { key: "prompts", label: "AI 提示词" },
    { key: "fields", label: "字段别名" },
    { key: "schemas", label: "表结构" },
  ];

  return (
    <div className="rounded-lg border border-zinc-200 bg-zinc-50 p-4 dark:border-zinc-700 dark:bg-zinc-800/50">
      {/* 标签页导航 */}
      <div className="mb-4 flex flex-wrap border-b border-zinc-200 dark:border-zinc-700">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => handleTabChange(tab.key)}
            className={`border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
              activeTab === tab.key
                ? "border-zinc-900 text-zinc-900 dark:border-zinc-100 dark:text-zinc-100"
                : "border-transparent text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* 标签页1：基本信息 */}
      {activeTab === "config" && (
        <div>
          {configLoading ? (
            <p className="py-4 text-center text-sm text-zinc-500">加载中...</p>
          ) : (
            <div className="space-y-3">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
                    行业名称
                  </label>
                  <input
                    type="text"
                    value={configForm.name}
                    onChange={(e) =>
                      setConfigForm({ ...configForm, name: e.target.value })
                    }
                    className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
                    描述
                  </label>
                  <input
                    type="text"
                    value={configForm.description}
                    onChange={(e) =>
                      setConfigForm({
                        ...configForm,
                        description: e.target.value,
                      })
                    }
                    className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                  />
                </div>
              </div>
              <div>
                <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
                  AI 专家角色
                </label>
                <textarea
                  value={configForm.expert_role}
                  onChange={(e) =>
                    setConfigForm({
                      ...configForm,
                      expert_role: e.target.value,
                    })
                  }
                  rows={3}
                  className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
                  数据层级描述
                </label>
                <textarea
                  value={configForm.hierarchy_desc}
                  onChange={(e) =>
                    setConfigForm({
                      ...configForm,
                      hierarchy_desc: e.target.value,
                    })
                  }
                  rows={3}
                  className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                />
              </div>
              <div className="flex justify-end">
                <button
                  onClick={saveConfig}
                  disabled={configSaving}
                  className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-700 disabled:opacity-50"
                >
                  {configSaving ? "保存中..." : "保存"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 标签页2：AI 提示词 */}
      {activeTab === "prompts" && (
        <div>
          {promptsLoading ? (
            <p className="py-4 text-center text-sm text-zinc-500">加载中...</p>
          ) : (
            <div className="space-y-4">
              <div className="rounded-lg bg-zinc-50 p-2 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                换行业时只需修改此处，无需改代码
              </div>
              {promptsError && (
                <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-400">
                  {promptsError}
                </div>
              )}
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700 dark:text-zinc-300">
                  任务拆解示例（decompose_examples，JSON 数组）
                </label>
                <CodeEditor
                  value={decomposeText}
                  onChange={setDecomposeText}
                  language="json"
                  height="250px"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700 dark:text-zinc-300">
                  语义路由示例（router_examples，JSON 数组）
                </label>
                <CodeEditor
                  value={routerText}
                  onChange={setRouterText}
                  language="json"
                  height="200px"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700 dark:text-zinc-300">
                  工具描述示例（tool_examples，JSON 对象）
                </label>
                <CodeEditor
                  value={toolText}
                  onChange={setToolText}
                  language="json"
                  height="150px"
                />
              </div>
              {/* 文件处理提示词 */}
              <div className="border-t border-zinc-200 pt-3 dark:border-zinc-700">
                <p className="mb-2 text-xs font-semibold text-zinc-800 dark:text-zinc-200">
                  文件处理提示词
                </p>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700 dark:text-zinc-300">
                  分类提示（classification_hints，Markdown）
                </label>
                <CodeEditor
                  value={classificationHints}
                  onChange={setClassificationHints}
                  language="markdown"
                  height="150px"
                  placeholder="用于指导 AI 对用户问题进行分类的提示信息..."
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700 dark:text-zinc-300">
                  Schema 提示（schema_hints，Markdown）
                </label>
                <CodeEditor
                  value={schemaHints}
                  onChange={setSchemaHints}
                  language="markdown"
                  height="150px"
                  placeholder="用于指导 AI 选择表结构的提示信息..."
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700 dark:text-zinc-300">
                  抽取提示（custom_prompts.extraction_prompt，Markdown）
                </label>
                <CodeEditor
                  value={extractionPrompt}
                  onChange={setExtractionPrompt}
                  language="markdown"
                  height="300px"
                  placeholder="用于指导 AI 从文件中抽取结构化数据的提示词..."
                />
              </div>
              {/* 术语适配配置 */}
              <div className="border-t border-zinc-200 pt-3 dark:border-zinc-700">
                <p className="mb-1 text-xs font-semibold text-zinc-800 dark:text-zinc-200">
                  术语适配配置（terminology）
                </p>
                <p className="mb-2 text-xs text-zinc-500 dark:text-zinc-400">
                  行业/个人表达方式映射到标准行为(7种:
                  查/增/删/改/导入/上传/导出)和标准对象(15种:
                  记录/表/字段/外键/索引/类型/精度/结构/文件/关联/统计/选择集/数据库/模板/会话)。用户说"开单"映射到标准行为"增"，说"订单"映射到标准对象"记录"。
                </p>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700 dark:text-zinc-300">
                  术语映射（JSON 对象，含 table_aliases / behavior_aliases /
                  object_aliases）
                </label>
                <CodeEditor
                  value={terminologyText}
                  onChange={setTerminologyText}
                  language="json"
                  height="300px"
                  placeholder={
                    '{\n  "table_aliases": { "orders": ["订单表", "订单"] },\n  "behavior_aliases": { "增": ["录入", "登记"] },\n  "object_aliases": { "记录": ["订单", "明细"] }\n}'
                  }
                />
              </div>
              <div className="flex justify-end">
                <button
                  onClick={savePrompts}
                  disabled={promptsSaving}
                  className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-700 disabled:opacity-50"
                >
                  {promptsSaving ? "保存中..." : "保存"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 标签页3：字段别名 */}
      {activeTab === "fields" && (
        <div>
          {fieldsLoading ? (
            <p className="py-4 text-center text-sm text-zinc-500">加载中...</p>
          ) : (
            <div className="space-y-3">
              <div className="rounded-lg bg-zinc-50 p-2 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                直接编辑 YAML 格式的字段别名配置，保存时前端会解析 YAML
                为对象后发送给后端
              </div>
              {fieldsError && (
                <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-400">
                  {fieldsError}
                </div>
              )}
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700 dark:text-zinc-300">
                  字段别名配置（YAML 格式）
                </label>
                <CodeEditor
                  value={fieldsText}
                  onChange={setFieldsText}
                  language="yaml"
                  height="400px"
                />
              </div>
              <div className="flex justify-end">
                <button
                  onClick={saveFields}
                  disabled={fieldsSaving}
                  className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-700 disabled:opacity-50"
                >
                  {fieldsSaving ? "保存中..." : "保存"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 标签页4：表结构 */}
      {activeTab === "schemas" && (
        <SchemaEditor
          industryName={industryName}
          showMessage={showMessage}
        />
      )}
    </div>
  );
}
