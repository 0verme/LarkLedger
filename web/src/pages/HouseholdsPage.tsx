import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Home, UserPlus, Users } from "lucide-react";
import {
	api,
	type Household,
	type HouseholdInvitation,
	type HouseholdList,
} from "../api";
import { EmptyState, ErrorState, PageSkeleton } from "../components/States";

export function HouseholdsPage() {
	const queryClient = useQueryClient();
	const households = useQuery({
		queryKey: ["households"],
		queryFn: () => api<HouseholdList>("/households"),
	});
	const invitations = useQuery({
		queryKey: ["household-invitations"],
		queryFn: () => api<HouseholdInvitation[]>("/household-invitations"),
	});
	const refresh = async () => {
		await queryClient.invalidateQueries();
	};
	const create = useMutation({
		mutationFn: (name: string) =>
			api<Household>("/households", {
				method: "POST",
				body: JSON.stringify({ name }),
			}),
		onSuccess: refresh,
	});
	const invite = useMutation({
		mutationFn: ({ id, target }: { id: string; target: string }) =>
			api<HouseholdInvitation>(`/households/${id}/invitations`, {
				method: "POST",
				body: JSON.stringify({ target }),
			}),
		onSuccess: refresh,
	});
	const respond = useMutation({
		mutationFn: ({ id, action }: { id: string; action: "accept" | "reject" }) =>
			api<HouseholdInvitation>(`/household-invitations/${id}/${action}`, {
				method: "POST",
			}),
		onSuccess: refresh,
	});
	const leave = useMutation({
		mutationFn: (id: string) =>
			api<void>(`/households/${id}/leave`, { method: "POST" }),
		onSuccess: refresh,
	});
	const remove = useMutation({
		mutationFn: ({
			householdId,
			userId,
		}: {
			householdId: string;
			userId: string;
		}) =>
			api<void>(`/households/${householdId}/members/${userId}`, {
				method: "DELETE",
			}),
		onSuccess: refresh,
	});
	const createHousehold = () => {
		const name = window.prompt("家庭名称");
		if (name?.trim()) create.mutate(name);
	};
	const inviteMember = (id: string) => {
		const target = window.prompt("已有用户的飞书 open_id（ou_xxx）");
		if (target?.trim()) invite.mutate({ id, target });
	};
	const pending = (invitations.data ?? []).filter(
		(item) => item.status === "pending",
	);
	const actionError = [
		create.error,
		invite.error,
		respond.error,
		leave.error,
		remove.error,
	].find((error) => error);
	if (households.isLoading) return <PageSkeleton rows={2} />;
	if (households.isError || !households.data) {
		return (
			<ErrorState
				title="家庭列表加载失败"
				description="家庭空间暂时无法加载，请稍后重试。"
				onRetry={() => households.refetch()}
			/>
		);
	}

	return (
		<div className="dashboard-page">
			<div className="page-heading">
				<div>
					<p className="eyebrow">家庭空间</p>
					<h2>公共账本，共同维护。</h2>
				</div>
				<button className="primary-button" onClick={createHousehold}>
					<Home size={17} /> 创建家庭
				</button>
			</div>
			{invitations.isError && (
				<ErrorState
					compact
					title="邀请列表加载失败"
					description="家庭列表仍可使用，请稍后重试邀请列表。"
					onRetry={() => invitations.refetch()}
				/>
			)}
			{actionError && (
				<ErrorState
					compact
					title="家庭操作失败"
					description={
						actionError instanceof Error
							? actionError.message
							: "请检查输入后重试。"
					}
				/>
			)}
			{pending.length > 0 && (
				<section className="panel">
					<div className="panel-title">
						<h3>待处理邀请</h3>
						<span>{pending.length} 个</span>
					</div>
					<div className="recent-list">
						{pending.map((item) => (
							<div key={item.id}>
								<span>
									<b>{item.household_name}</b>
									<small>邀请编号 {item.invitation_code}</small>
								</span>
								<span>
									<button
										className="quiet-button"
										onClick={() =>
											respond.mutate({ id: item.id, action: "accept" })
										}
									>
										接受
									</button>
									<button
										className="quiet-button"
										onClick={() =>
											respond.mutate({ id: item.id, action: "reject" })
										}
									>
										拒绝
									</button>
								</span>
							</div>
						))}
					</div>
				</section>
			)}
			<div className="dashboard-grid">
				{(households.data?.items ?? []).map((household) => (
					<HouseholdCard
						key={household.id}
						household={household}
						onInvite={inviteMember}
						onLeave={(id) => leave.mutate(id)}
						onRemove={(householdId, userId) =>
							remove.mutate({ householdId, userId })
						}
					/>
				))}
			</div>
			{!households.isLoading && !households.data?.items.length && (
				<section className="panel">
					<EmptyState
						icon={<Users size={28} />}
						title="还没有家庭空间"
						description="创建家庭，或等待已有内部用户发来邀请。"
					/>
				</section>
			)}
		</div>
	);
}

function HouseholdCard({
	household,
	onInvite,
	onLeave,
	onRemove,
}: {
	household: Household;
	onInvite: (id: string) => void;
	onLeave: (id: string) => void;
	onRemove: (householdId: string, userId: string) => void;
}) {
	const detail = useQuery({
		queryKey: ["household", household.id],
		queryFn: () => api<Household>(`/households/${household.id}`),
	});
	const members = detail.data?.members ?? [];
	return (
		<section className="panel">
			<div className="panel-title">
				<h3>{household.name}</h3>
				<span>{household.role === "owner" ? "所有者" : "成员"}</span>
			</div>
			<p>
				公共账本：<strong>{household.ledger.name}</strong>
			</p>
			<div className="category-list">
				{members.map((member) => (
					<div key={member.user_id}>
						<span>{member.display_name || member.user_id}</span>
						<em>{member.role}</em>
						{household.role === "owner" && member.role === "member" && (
							<button
								className="quiet-button"
								onClick={() => onRemove(household.id, member.user_id)}
							>
								移除
							</button>
						)}
					</div>
				))}
			</div>
			<div className="page-heading">
				{household.role === "owner" ? (
					<button
						className="quiet-button"
						onClick={() => onInvite(household.id)}
					>
						<UserPlus size={16} /> 邀请成员
					</button>
				) : (
					<button
						className="quiet-button"
						onClick={() => onLeave(household.id)}
					>
						退出家庭
					</button>
				)}
			</div>
		</section>
	);
}
