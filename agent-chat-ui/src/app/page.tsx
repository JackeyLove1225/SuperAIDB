import { redirect } from "next/navigation";

/**
 * 展示板模式（20260808）：根路径直接进入 Schema 设计器。
 * 对话功能已移除（由 Reasonix 承担），本应用定位为 data-engine 的展示板。
 */
export default function RootPage() {
  redirect("/dashboard/schema-designer");
}
