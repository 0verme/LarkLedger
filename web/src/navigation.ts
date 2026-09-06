import type { LucideIcon } from "lucide-react";
import {
	Activity,
	ArrowLeftRight,
	BarChart3,
	BookOpen,
	CalendarClock,
	CircleDollarSign,
	Clock3,
	Download,
	FileText,
	HeartPulse,
	Home,
	KeyRound,
	Landmark,
	MessageSquareReply,
	MonitorSmartphone,
	PiggyBank,
	RotateCcw,
	Settings,
	ShieldCheck,
	Target,
	Users,
	AlertTriangle,
} from "lucide-react";

export type NavItem = {
	label: string;
	path: string;
	icon: LucideIcon;
	admin?: boolean;
};

export type NavGroup = {
	label?: string;
	items: NavItem[];
};

/**
 * The single source of truth for application navigation.
 *
 * Shell, desktop sidebar, mobile drawer, route generation, and page titles all
 * derive from this list so responsive navigation cannot drift apart.
 */
export const navigationGroups: NavGroup[] = [
	{
		items: [
			{ label: "首页", path: "/", icon: Home },
			{ label: "流水", path: "/entries", icon: BookOpen },
			{ label: "账户", path: "/accounts", icon: Landmark },
			{ label: "转账", path: "/transfers", icon: ArrowLeftRight },
			{ label: "待确认", path: "/pending", icon: Clock3 },
		],
	},
	{
		label: "家庭与规划",
		items: [
			{ label: "家庭总览", path: "/overview", icon: BarChart3 },
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
			{
				label: "Dead / Replay",
				path: "/admin/dead",
				icon: RotateCcw,
				admin: true,
			},
			{
				label: "Dead Letters",
				path: "/admin/dead-letters",
				icon: AlertTriangle,
				admin: true,
			},
		],
	},
	{
		label: "设置与开发者",
		items: [
			{ label: "登录会话", path: "/sessions", icon: MonitorSmartphone },
			{ label: "API 令牌", path: "/api-tokens", icon: KeyRound },
			{
				label: "健康状态",
				path: "/admin/health",
				icon: HeartPulse,
				admin: true,
			},
			{ label: "配置", path: "/admin/config", icon: Settings, admin: true },
			{ label: "关于", path: "/about", icon: ShieldCheck },
		],
	},
];

export const navigationPageNames = new Map(
	navigationGroups.flatMap((group) =>
		group.items.map((item) => [item.path, item.label] as const),
	),
);

export function getVisibleNavigationGroups(isAdmin: boolean): NavGroup[] {
	return navigationGroups
		.map((group) => ({
			...group,
			items: group.items.filter((item) => !item.admin || isAdmin),
		}))
		.filter((group) => group.items.length > 0);
}
