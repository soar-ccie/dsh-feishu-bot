/**
 * feishu-session —— DSH web profile 插件
 *
 * 解决的问题
 * ----------
 * 外部程序（飞书 Bot）**无法**在 Web GUI 的会话列表里创建/整理会话：
 *   - 直接改 storages/workspace.json 无效（dsh-web 启动时读进内存，之后不看文件）
 *   - ACP 建的会话不登记 workspace，GUI 看不到
 *   - dsh-web 没有公开的"建会话 / 归档会话"HTTP 接口
 *
 * 本插件挂进 web profile 后，提供官方机制内的入口：
 *
 *   POST /feishu/new-session
 *     {"title":"...","prompt":"..."}
 *         → 经 webhookRuntime 在 Web Workspace 里创建一个**根会话**
 *         → dsh-web 内存中的 workspace 立刻登记 → GUI 列表立刻可见
 *     {"action":"archive","sessionId":"..."}
 *         → 把该会话从 GUI（分组/单列表/搜索）隐藏；磁盘文件完整保留
 *     {"action":"unarchive","sessionId":"..."}
 *         → 取消隐藏，会话回到原来的位置（workspace 归属不受影响）
 *     {"action":"detach-session","sessionId":"..."}   ← 默认停用
 *         → 把会话从 workspace 成员表里摘掉（DSH 没有"删除会话"功能，只有归档；
 *           要让它从侧栏记录里真正消失只能 detach）。只改登记关系，不动文件。
 *         → **默认不启用**（config.enableDetach 缺省 false，调用返回 403）。
 *           代码保留备用；要用就在 cordis.patch.yml 里加 `enableDetach: true`。
 *     {"action":"archive-strays","sessionIds":[...]}
 *         → 只归档"没有登记进任何 workspace"的那些（即 GUI 的「未分组」成员）
 *         → 返回 {"archived":[...],"skippedRegistered":[...],"skippedArchived":[...],"unknown":[...]}
 *     {"action":"list"}
 *         → 返回 {"archived":[...],"workspaces":[{"title":...,"sessionIds":[...]}]}
 *
 * 为什么「未分组」会整组消失
 * --------------------------
 * 客户端 dsh-client-ui-workspace 的 groupByWorkspace() 只在
 * `stray.length > 0` 时才 push 那个无标题分组。把未登记会话全部归档后，
 * stray 为空 → 「未分组」这一组在 GUI 里彻底不出现（不是变成空组）。
 *
 * 为什么可以零依赖
 * ----------------
 * - `WebhookSourceId` / `WebhookDeliveryId` 在运行时是恒等函数
 *   （只做编译期 brand），`snapshotDelivery` 只校验 `typeof === "string"`，
 *   因此直接传字符串即可。
 * - Cordis 对没有 `Config` 的插件会原样透传 config（`if (!runtime.Config) return config`）。
 *
 * 挂载方式（profile 的 cordis.patch.yml）：
 *   - insert:
 *       - id: feishu-session
 *         name: /绝对路径/feishu-session.mjs
 *         config: { path: /feishu/new-session, workspacePath: /home/xxx }
 */

const name = 'feishu-session';

// 需要的宿主服务：HTTP 服务器 + webhook 运行时 + workspace 注册表
// ⚠️ workspaceRegistry 必须显式 inject，否则 ctx.workspaceRegistry 是 undefined
//    （Cordis 只把 inject 过的服务挂到 ctx 上）—— 少了它 archive 会直接 500。
const inject = ['webServer', 'webhookRuntime', 'workspaceRegistry'];

const RULE_ID = 'feishu-new-session';
const KIND = 'feishu';
const SOURCE = 'feishu-bot';
const MAX_BODY = 256 * 1024;

/** 读取请求体（带上限，避免超大 body 打爆内存） */
function readBody(req, limit) {
	return new Promise((resolve, reject) => {
		let size = 0;
		const chunks = [];
		req.on('data', (chunk) => {
			size += chunk.length;
			if (size > limit) {
				reject(new Error('request body too large'));
				req.destroy();
				return;
			}
			chunks.push(chunk);
		});
		req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
		req.on('error', reject);
	});
}

/** 所有已登记进 workspace 的会话 ID（这些**不是**散客，绝不能动） */
function registeredIds(registry) {
	const out = new Set();
	for (const workspace of registry.list()) {
		for (const id of workspace.sessionIds) out.add(String(id));
	}
	return out;
}

function apply(ctx, config) {
	const cfg = config || {};
	const routePath = cfg.path || '/feishu/new-session';
	const workspacePath = cfg.workspacePath || process.env.HOME || '/';
	const agentPreset = cfg.agentPreset || 'standard';
	const permissionPreset = cfg.permissionPreset || 'workspace-write';
	const secret = cfg.secret || '';

	// ── detach-session 默认【停用】──────────────────────────────
	// DSH 本身没有"删除会话"功能（只有归档），要真正把一条会话从 workspace
	// 成员表里摘掉只能 detach。实测确认后决定【不用它】，但代码保留：
	// 默认不加载（enableDetach 缺省 false），要启用就在 cordis.patch.yml 的
	// config 里加 `enableDetach: true`（config 是热重载的，改完不用重启）。
	const detachEnabled = cfg.enableDetach === true;

	// ctx.get() 兜底：万一某些版本不把 inject 的服务挂成属性
	const registry = ctx.workspaceRegistry ?? ctx.get?.('workspaceRegistry');
	if (!registry || typeof registry.archiveSession !== 'function') {
		ctx.logger?.warn?.('feishu-session: workspaceRegistry 不可用，归档类动作会失败');
	}

	// ── ① 规则：把一次投递变成"创建一个根会话" ──────────────────────
	ctx.effect(() => {
		const dispose = ctx.webhookRuntime.register({
			id: RULE_ID,
			kind: KIND,
			run: (delivery) => {
				const ev = (delivery && delivery.event) || {};
				return {
					workspacePath,
					title: ev.title,
					prompt: ev.prompt,
					agentPreset,
					permissionPreset,
				};
			},
		});
		return () => {
			void dispose();
		};
	}, 'feishu-session: rule');

	// ── ② 归档 / 取消归档（走官方 registry，落盘到 storages/workspace.json）──
	async function archiveMany(ids, force) {
		const registered = registeredIds(registry);
		const report = { archived: [], skippedRegistered: [], skippedArchived: [], unknown: [] };
		for (const raw of ids) {
			const id = String(raw || '').trim();
			if (id === '') continue;
			// 默认【不碰】已登记进 workspace 的会话（防止把项目会话误藏起来、
			// GUI 又没有取消归档的入口）。只有用户显式发起的「归档会话」才带 force。
			if (!force && registered.has(id)) {
				report.skippedRegistered.push(id);
				continue;
			}
			if (registry.archivedSessionIds.includes(id)) {
				report.skippedArchived.push(id);
				continue;
			}
			try {
				await registry.archiveSession(id);
				report.archived.push(id);
			} catch (error) {
				report.unknown.push(id);
				ctx.logger?.debug?.(`feishu-session: 跳过未知会话 ${id}`);
			}
		}
		return report;
	}

	async function unarchive(id) {
		return registry.enqueueOperation(async () => {
			const state = registry.requireState();
			if (!state.archivedSessionIds.includes(id)) return false;
			await registry.setState({
				...state,
				archivedSessionIds: state.archivedSessionIds.filter((x) => x !== id),
			});
			return true;
		});
	}

	// ── ③ HTTP 入口：外部程序（飞书 Bot）调这里 ─────────────────────
	const route = {
		kind: 'exact',
		path: routePath,
		handler: async (req, res) => {
			const respond = (status, text, type) => {
				if (text === undefined) {
					res.writeHead(status);
				} else {
					res.writeHead(status, { 'content-type': type || 'text/plain; charset=utf-8' });
				}
				res.end(text);
			};
			const json = (status, value) =>
				respond(status, JSON.stringify(value), 'application/json; charset=utf-8');

			try {
				if (req.method !== 'POST') return respond(405, 'method not allowed');

				if (secret !== '') {
					if (req.headers['x-feishu-secret'] !== secret) {
						return respond(401, 'invalid secret');
					}
				}

				let payload;
				try {
					payload = JSON.parse(await readBody(req, MAX_BODY));
				} catch (e) {
					return respond(400, `bad body: ${e && e.message ? e.message : e}`);
				}
				if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
					return respond(400, 'body must be a JSON object');
				}

				const action = String(payload.action || '');

				// 查询现状
				if (action === 'list') {
					return json(200, {
						archived: registry.archivedSessionIds,
						workspaces: registry.list().map((w) => ({
							title: w.title,
							sessionIds: w.sessionIds,
						})),
					});
				}

				// 归档（从 GUI 列表隐藏；磁盘文件保留）
				if (action === 'archive') {
					const sid = String(payload.sessionId || '').trim();
					if (sid === '') return respond(400, 'sessionId is required');
					// force=true = 用户在飞书里显式发「归档会话」并选了数字,
					// 明确要连已登记的项目会话一起归档。gui-hide 的自动清理
					// 永远不带 force,保护不变。
					const force = payload.force === true;
					const report = await archiveMany([sid], force);
					if (!force && report.skippedRegistered.length > 0) {
						return json(409, { error: 'session belongs to a workspace', ...report });
					}
					ctx.logger?.info?.(
						`feishu-session: 归档 ${sid} (force=${force}) → ${report.archived.length} 个`,
					);
					return json(202, report);
				}

				// 只归档"未分组"的散客（绝不碰已登记进 workspace 的会话）
				if (action === 'archive-strays') {
					const ids = Array.isArray(payload.sessionIds) ? payload.sessionIds : null;
					if (ids === null) return respond(400, 'sessionIds array is required');
					const report = await archiveMany(ids);
					ctx.logger?.info?.(
						`feishu-session: 归档散客 ${report.archived.length} 个，` +
							`跳过已登记 ${report.skippedRegistered.length} 个`,
					);
					return json(200, report);
				}

				// 取消归档（放回 GUI 列表）
				if (action === 'unarchive') {
					const sid = String(payload.sessionId || '').trim();
					if (sid === '') return respond(400, 'sessionId is required');
					const changed = await unarchive(sid);
					ctx.logger?.info?.(`feishu-session: 已取消归档 ${sid} (${changed})`);
					return json(200, { sessionId: sid, changed });
				}

				// 把会话从 workspace 成员表里摘掉。
				// DSH 本身【没有"删除会话"这个功能】，只有归档；要让一条会话
				// 从侧栏记录里真正消失，只能 detach。本接口只改登记关系，
				// 不碰任何会话文件 —— 文件删除由调用方自己负责。
				//
				// ⚠️ 默认【停用】：用户明确表示不需要删除会话的能力，但同意
				//    把代码留着。要用就在 cordis.patch.yml 里加 enableDetach: true。
				if (action === 'detach-session') {
					if (!detachEnabled) {
						return json(403, {
							error: 'detach-session is disabled',
							hint: '在 cordis.patch.yml 的 config 里加 enableDetach: true 才启用',
						});
					}
					const sid = String(payload.sessionId || '').trim();
					if (sid === '') return respond(400, 'sessionId is required');
					const detached = [];
					for (const workspace of registry.list()) {
						if (!workspace.sessionIds.includes(sid)) continue;
						await workspace.detachSession(sid);
						detached.push(workspace.title || workspace.id);
					}
					ctx.logger?.info?.(
						`feishu-session: 已把 ${sid} 从 workspace 摘除 (${detached.length} 处)`,
					);
					return json(200, { sessionId: sid, detached });
				}

				// 建会话
				const title = String(payload.title || '').trim();
				if (title === '') return respond(400, 'title is required');
				const prompt =
					String(payload.prompt || '').trim() ||
					`（会话「${title}」已创建，等待用户指示。）`;

				const delivery = {
					kind: KIND,
					source: SOURCE,
					deliveryId: `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
					event: { title, prompt },
					receivedAt: Date.now(),
				};

				ctx.webhookRuntime.dispatch(delivery);
				ctx.logger?.info?.(`feishu-session: 已创建会话「${title}」`);
				respond(202);
			} catch (error) {
				ctx.logger?.warn?.(
					`feishu-session: 处理失败: ${error && error.message ? error.message : error}`,
				);
				respond(500, 'feishu-session failed');
			}
		},
	};
	ctx.effect(() => ctx.webServer.register(route), `feishu-session: ${routePath}`);

	ctx.logger?.info?.(
		`feishu-session: 已挂载 ${routePath} (workspace=${workspacePath}) ` +
			`actions=create/list/archive/unarchive/archive-strays ` +
			`detach-session=${detachEnabled ? 'ON' : 'OFF(默认)'}`,
	);
}

export { name, inject, apply };
