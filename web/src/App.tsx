import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowLeftRight,
  BarChart3,
  BookOpen,
  CalendarClock,
  ChevronRight,
  CircleDollarSign,
  Clock3,
  Download,
  FileText,
  HeartPulse,
  Home,
  Landmark,
  LogOut,
  Menu,
  MessageSquareReply,
  PiggyBank,
  RotateCcw,
  Settings,
  ShieldCheck,
  Target,
  Users,
  X,
} from "lucide-react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ApiError, api, type Ledger, type LedgerList, type Me } from "./api";
import { DashboardPage } from "./pages/DashboardPage";
import { DeadPage, EventsPage, HealthPage, OutboxPage } from "./pages/AdminPages";
import { AccountsPage } from "./pages/AccountsPage";
import { TransfersPage } from "./pages/TransfersPage";
import { EntriesPage } from "./pages/EntriesPage";
import { AnalyticsPage, BudgetsPage, ExportsPage, ReportsPage } from "./pages/FinancePages";
import { GoalsPage } from "./pages/GoalsPage";
import { PendingPage } from "./pages/PendingPage";
import { HouseholdsPage } from "./pages/HouseholdsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { RecurringRulesPage } from "./pages/RecurringRulesPage";
import { AboutPage, ConfigPage } from "./pages/SystemPages";

type NavItem = { label: string; path: string; icon: typeof Activity; admin?: boolean };

const groups: Array<{ label?: string; items: NavItem[] }> = [
  {
    items: [
      { label: "总览", path: "/", icon: BarChart3 },
      { label: "家庭总览", path: "/overview", icon: Home },
      { label: "账目", path: "/entries", icon: BookOpen },
      { label: "账户", path: "/accounts", icon: Landmark },
      { label: "转账", path: "/transfers", icon: ArrowLeftRight },
      { label: "待确认", path: "/pending", icon: Clock3 },
      { label: "家庭", path: "/households", icon: Users },
      { label: "预算", path: "/budgets", icon: PiggyBank },
      { label: "目标", path: "/goals", icon: Target },
      { label: "周期账单", path: "/recurring", icon: CalendarClock },
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

function pageElement(item: NavItem) {
  if (item.path === "/") return <DashboardPage />;
  if (item.path === "/overview") return <OverviewPage />;
  if (item.path === "/entries") return <EntriesPage />;
  if (item.path === "/accounts") return <AccountsPage />;
  if (item.path === "/transfers") return <TransfersPage />;
  if (item.path === "/pending") return <PendingPage />;
  if (item.path === "/households") return <HouseholdsPage />;
  if (item.path === "/budgets") return <BudgetsPage />;
  if (item.path === "/goals") return <GoalsPage />;
  if (item.path === "/recurring") return <RecurringRulesPage />;
  if (item.path === "/analytics") return <AnalyticsPage />;
  if (item.path === "/reports") return <ReportsPage />;
  if (item.path === "/exports") return <ExportsPage />;
  if (item.path === "/admin/events") return <EventsPage />;
  if (item.path === "/admin/outbox") return <OutboxPage />;
  if (item.path === "/admin/dead") return <DeadPage />;
  if (item.path === "/admin/health") return <HealthPage />;
  if (item.path === "/admin/config") return <ConfigPage />;
  return <AboutPage />;
}

function Shell({ me }: { me: Me }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const queryClient = useQueryClient();
  const logout = useMutation({
    mutationFn: () => api<void>("/auth/logout", { method: "POST" }),
    onSuccess: () => queryClient.setQueryData(["me"], null),
  });
  const ledgers = useQuery({ queryKey: ["ledgers"], queryFn: () => api<LedgerList>("/ledgers") });
  const selectLedger = useMutation({
    mutationFn: (id: string) => api<Ledger>(`/ledgers/${id}/select`, { method: "POST" }),
    onSuccess: async () => { await queryClient.invalidateQueries(); },
  });
  const createLedger = useMutation({
    mutationFn: (name: string) => api<Ledger>("/ledgers", { method: "POST", body: JSON.stringify({ name }) }),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["ledgers"] });
      selectLedger.mutate(created.id);
    },
  });
  const askCreateLedger = () => {
    const name = window.prompt("新账本名称");
    if (name?.trim()) createLedger.mutate(name);
  };
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
        <div className="ledger-switcher">
          <label htmlFor="current-ledger">当前账本</label>
          <div>
            <select
              id="current-ledger"
              aria-label="当前账本"
              value={(ledgers.data?.items ?? []).find((item) => item.is_current)?.id ?? ""}
              disabled={ledgers.isLoading || selectLedger.isPending}
              onChange={(event) => selectLedger.mutate(event.target.value)}
            >
              {(ledgers.data?.items ?? []).map((ledger) => (
                <option key={ledger.id} value={ledger.id}>{ledger.kind === "household_shared" ? "家庭 · " : "个人 · "}{ledger.name}{ledger.is_default ? "（默认）" : ""}</option>
              ))}
            </select>
            <button type="button" aria-label="创建账本" onClick={askCreateLedger} disabled={createLedger.isPending}>＋</button>
          </div>
          {(ledgers.isError || selectLedger.isError || createLedger.isError) && <small>账本操作失败，请重试</small>}
        </div>
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
  const [authExpired, setAuthExpired] = useState(false);
  useEffect(() => {
    const expire = () => setAuthExpired(true);
    window.addEventListener("larkledger:auth-expired", expire);
    return () => window.removeEventListener("larkledger:auth-expired", expire);
  }, []);
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<Me>("/me"), retry: false });
  if (me.isLoading) return <LoadingScreen />;
  if (authExpired) return <Login />;
  if (me.error instanceof ApiError && me.error.status === 401) return <Login />;
  if (me.isError) {
    return <main className="error-page"><h1>暂时无法加载</h1><p>请检查服务状态后重试。</p><button onClick={() => me.refetch()}>重新加载</button></main>;
  }
  return me.data ? <Shell me={me.data} /> : <Login />;
}
