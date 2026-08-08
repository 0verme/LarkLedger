import { useQuery } from "@tanstack/react-query";
import { BookOpenCheck, Database, LockKeyhole, ShieldCheck } from "lucide-react";
import { api, type SafeSystemConfig } from "../api";

const enabled = (value: boolean) => value ? "已启用" : "未启用";
const configured = (value: boolean) => value ? "已配置" : "未配置";

export function ConfigPage() {
  const query = useQuery({ queryKey: ["admin-config"], queryFn: () => api<SafeSystemConfig>("/admin/config") });
  if (query.isLoading) return <div className="page-skeleton"><div /><div /></div>;
  if (query.isError || !query.data) return <div className="state-panel"><h3>安全配置加载失败</h3><button onClick={() => query.refetch()}>重试</button></div>;
  const config = query.data;
  const rows = [
    ["Event Mode", config.event_mode], ["Timezone", config.timezone], ["Currency", config.currency],
    ["Event Worker", enabled(config.worker_enabled)], ["Reply Worker", enabled(config.reply_worker_enabled)],
    ["Cleanup Worker", enabled(config.cleanup_worker_enabled)], ["Pending Confirmation", enabled(config.pending_enabled)],
    ["AI Provider", config.ai_provider], ["AI Model", config.ai_model],
    ["AI API Key", configured(config.ai_api_key_configured)], ["Lark App Secret", configured(config.lark_app_secret_configured)],
    ["Session TTL", `${Math.round(config.session_ttl_seconds / 60)} 分钟`], ["Secure Cookie", enabled(config.secure_cookie)],
  ];
  return <section><div className="page-heading"><div><p className="eyebrow">SAFE CONFIGURATION</p><h2>系统配置</h2></div><span className="version-chip">v{config.version}</span></div><div className="config-notice"><LockKeyhole size={20} /><p>此页只读。密钥、Token、数据库地址与连接凭据永远不会返回到浏览器。</p></div><section className="config-grid">{rows.map(([label, value]) => <article key={label}><span>{label}</span><strong>{value}</strong></article>)}</section></section>;
}

export function AboutPage() {
  return <section><div className="page-heading"><div><p className="eyebrow">ABOUT LARKLEDGER</p><h2>飞书里的账，网页里看清。</h2></div></div><div className="about-grid"><article><BookOpenCheck /><h3>同一个账本核心</h3><p>Web 与飞书共享 LedgerService、确认流程、revision 与可靠投递规则，不会形成第二套账本。</p></article><article><Database /><h3>PostgreSQL 是唯一状态源</h3><p>会话、账目、确认单与投递状态都保存在 PostgreSQL；不依赖 Redis 或额外任务队列。</p></article><article><ShieldCheck /><h3>默认保护隐私</h3><p>普通用户只能访问自己的财务数据；运维能力仅对配置的管理员开放，敏感载荷和密钥不进入页面。</p></article></div></section>;
}
