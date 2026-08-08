import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  BarChart3,
  BookOpen,
  ChevronRight,
  CircleDollarSign,
  Clock3,
  Download,
  FileText,
  HeartPulse,
  LogOut,
  Menu,
  MessageSquareReply,
  PiggyBank,
  RotateCcw,
  Settings,
  ShieldCheck,
  X,
} from "lucide-react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ApiError, api, type Me } from "./api";
import { DashboardPage } from "./pages/DashboardPage";
import { EntriesPage } from "./pages/EntriesPage";
import { PendingPage } from "./pages/PendingPage";

type NavItem = { label: string; path: string; icon: typeof Activity; admin?: boolean };

const groups: Array<{ label?: string; items: NavItem[] }> = [
  {
    items: [
      { label: "总览", path: "/", icon: BarChart3 },
      { label: "账目", path: "/entries", icon: BookOpen },
      { label: "待确认", path: "/pending", icon: Clock3 },
      { label: "预算", path: "/budgets", icon: PiggyBank },
      { label: "分析", path: "/analytics", icon: CircleDollarSign },
      { label: "报表", path: "/reports", icon: FileText },
      { label: "导出", path: "/exports", icon: Download },
    ],
  },
  {
    label: "可靠投递",
    items: [
      { label: "事件", path: "/admin/events", icon: Activity, admin: true },
      {
        label: "回复队列",
        path: "/admin/outbox",
        icon: MessageSquareReply,
        admin: true,
      },
      { label: "Dead / Replay", path: "/admin/dead", icon: RotateCcw, admin: true },
    ],
  },
  {
    label: "系统",
    items: [
      { label: "健康状态", path: "/admin/health", icon: HeartPulse, admin: true },
      { label: "配置", path: "/admin/config", icon: Settings, admin: true },
      { label: "关于", path: "/about", icon: ShieldCheck },
    ],
  },
];

const pageNames = new Map(groups.flatMap((group) => group.items.map((item) => [item.path, item.label])));

function Login() {
  return (
    <main className="login-page">
      <section className="login-card">
        <div className="brand-mark">飞</div>
        <p className="eyebrow">LARKLEDGER</p>
        <h1>让每一笔，都清清楚楚。</h1>
        <p className="login-copy">使用飞书账号登录，查看只属于你的账目、预算与报表。</p>
        <a className="primary-button" href="/api/web/v1/auth/login">
          使用飞书登录 <ChevronRight size={17} />
        </a>
        <p className="security-note"><ShieldCheck size={15} /> 飞书凭证不会保存在浏览器中</p>
      </section>
    </main>
  );
}

function LoadingScreen() {
  return (
    <main className="loading-page" aria-label="正在加载">
      <div className="brand-mark pulse">飞</div>
      <div className="loading-line" />
    </main>
  );
}

function Placeholder({ title }: { title: string }) {
  return (
    <section className="placeholder-page">
      <p className="eyebrow">WEB DASHBOARD · V0.4.0</p>
      <h2>{title}</h2>
      <p>页面基础已就绪，业务视图将在对应工作包中接入。</p>
    </section>
  );
}

function pageElement(item: NavItem) {
  if (item.path === "/") return <DashboardPage />;
  if (item.path === "/entries") return <EntriesPage />;
  if (item.path === "/pending") return <PendingPage />;
  return <Placeholder title={item.label} />;
}

function Shell({ me }: { me: Me }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const queryClient = useQueryClient();
  const logout = useMutation({
    mutationFn: () => api<void>("/auth/logout", { method: "POST" }),
    onSuccess: () => queryClient.setQueryData(["me"], null),
  });
  const visibleGroups = groups.map((group) => ({
    ...group,
    items: group.items.filter((item) => !item.admin || me.role === "ADMIN"),
  }));

  return (
    <div className="app-shell">
      <button className="mobile-menu" aria-label="打开导航" onClick={() => setMobileOpen(true)}>
        <Menu />
      </button>
      {mobileOpen && <button className="nav-scrim" aria-label="关闭导航" onClick={() => setMobileOpen(false)} />}
      <aside className={`sidebar ${mobileOpen ? "open" : ""}`}>
        <div className="sidebar-brand"><span className="brand-mark small">飞</span><strong>飞账</strong></div>
        <button className="mobile-close" aria-label="关闭导航" onClick={() => setMobileOpen(false)}><X /></button>
        <nav aria-label="主导航">
          {visibleGroups.map((group, groupIndex) => (
            <div className="nav-group" key={group.label ?? groupIndex}>
              {group.label && group.items.length > 0 && <p>{group.label}</p>}
              {group.items.map(({ label, path, icon: Icon }) => (
                <NavLink key={path} to={path} end={path === "/"} onClick={() => setMobileOpen(false)}>
                  <Icon size={18} strokeWidth={1.8} /> {label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="user-card">
          <div className="avatar">{me.name.slice(0, 1)}</div>
          <div><strong>{me.name}</strong><span>{me.role === "ADMIN" ? "管理员" : "用户"}</span></div>
          <button aria-label="退出登录" onClick={() => logout.mutate()}><LogOut size={17} /></button>
        </div>
      </aside>
      <div className="workspace">
        <header><div><span>飞账</span><ChevronRight size={14} /><strong>{pageNames.get(location.pathname) ?? "页面"}</strong></div></header>
        <main className="content">
          <Routes>
            {visibleGroups.flatMap((group) => group.items).map((item) => (
              <Route key={item.path} path={item.path} element={pageElement(item)} />
            ))}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}

export function App() {
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<Me>("/me"), retry: false });
  if (me.isLoading) return <LoadingScreen />;
  if (me.error instanceof ApiError && me.error.status === 401) return <Login />;
  if (me.isError) {
    return <main className="error-page"><h1>暂时无法加载</h1><p>请检查服务状态后重试。</p><button onClick={() => me.refetch()}>重新加载</button></main>;
  }
  return me.data ? <Shell me={me.data} /> : <Login />;
}
