(() => {
    "use strict";

    const bridge = window.AstrBotPluginPage;
    const app = document.getElementById("app");
    const toastEl = document.getElementById("toast");
    let view = "overview";
    let roles = { auto: "自动", none: "不推送", teacher: "老师", headteacher: "班主任", class_group: "班级群" };

    const ICON = {
        link: '<svg class="icon" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
        bell: '<svg class="icon" viewBox="0 0 24 24"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/></svg>',
        alert: '<svg class="icon" viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><circle cx="12" cy="12" r="9"/></svg>',
    };

    // ---------- 工具 ----------
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const initial = (name) => esc((String(name || "?").trim()[0] || "?").toUpperCase());
    const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";

    function toast(message, isError = false) {
        toastEl.textContent = message;
        toastEl.className = "toast show" + (isError ? " error" : "");
        clearTimeout(toast.timer);
        toast.timer = setTimeout(() => (toastEl.className = "toast"), isError ? 5000 : 2600);
    }

    // bridge 返回值可能是原始 JSON，也可能再包一层；统一取出 {ok, data}。
    function unwrap(result) {
        let r = result;
        for (let i = 0; i < 3 && r && typeof r === "object"; i++) {
            if (r.ok === true && "data" in r) return r.data;
            if (r.status === "error" || r.ok === false) throw new Error(r.message || "请求失败");
            if ("data" in r) { r = r.data; continue; }
            break;
        }
        return r;
    }

    function errorText(err) {
        return err?.data?.message || err?.response?.data?.message || err?.message || String(err);
    }

    async function get(endpoint) { return unwrap(await bridge.apiGet(endpoint)); }
    async function post(endpoint, body) { return unwrap(await bridge.apiPost(endpoint, body || {})); }

    async function action(fn, okText) {
        try {
            const r = await fn();
            if (okText) toast(okText);
            return r;
        } catch (err) {
            toast(errorText(err), true);
            throw err;
        }
    }

    // ---------- 主题 ----------
    function applyTheme(ctx) {
        if (ctx && typeof ctx.isDark === "boolean") document.documentElement.dataset.theme = ctx.isDark ? "dark" : "light";
    }

    // ---------- 导航 ----------
    document.getElementById("nav").addEventListener("click", (e) => {
        const btn = e.target.closest(".nav-link");
        if (!btn) return;
        view = btn.dataset.view;
        document.querySelectorAll(".nav-link").forEach((b) => b.classList.toggle("active", b === btn));
        render();
    });

    async function render() {
        const renderers = { overview: renderOverview, bindings: renderBindings, sessions: renderSessions, reminders: renderReminders, holidays: renderHolidays, log: renderLog };
        try {
            await renderers[view]();
        } catch (err) {
            app.innerHTML = `<div class="notice error">${ICON.alert}<div><strong>加载失败</strong><span>${esc(errorText(err))}</span></div></div>`;
        }
    }

    function head(eyebrow, title, desc, actions = "") {
        return `<div class="page-head"><div><p class="eyebrow">${eyebrow}</p><h1>${title}</h1><p class="muted">${desc}</p></div><div class="actions">${actions}</div></div>`;
    }

    // ---------- 概览 ----------
    async function renderOverview() {
        const [o, logs] = await Promise.all([get("overview"), get("log")]);
        if (o.roles) roles = o.roles;
        let status;
        if (o.paused) status = `<div class="notice warning">${ICON.alert}<div><strong>主动推送已全局暂停</strong><span>所有老师提醒和班级群课表推送都不会发送。</span></div><button class="btn sm" data-act="resume">恢复推送</button></div>`;
        else if (o.holiday_today) status = `<div class="notice warning">${ICON.alert}<div><strong>今天是${esc(o.holiday_reason || "节假日")}，主动推送自动暂停</strong><span>${o.holiday_tomorrow ? "明天仍在假期中。" : "假期结束前一天晚上会照常推送次日日程。"}</span></div></div>`;
        else status = `<div class="notice ok">${ICON.bell}<div><strong>主动推送运行中</strong><span>${o.holiday_reason ? esc(o.holiday_reason) + "，" : ""}${o.holiday_tomorrow ? "明天放假，不推送次日日程。" : "按设置推送当日/次日日程、课前与换课提醒。"}</span></div><button class="btn sm ghost" data-act="pause">全局暂停</button></div>`;
        const warn = [];
        if (!o.default_server) warn.push("未配置默认服务器地址：用户绑定时需要自己提供服务器地址（插件配置 → default_server_url）。");
        if (!o.official_data) warn.push("尚未获取到今年的法定节假日数据，暂时只按手动假期判断。可在“节假日”页手动刷新。");
        if (o.last_error) warn.push("最近一次调度出错：" + o.last_error);
        app.innerHTML = head("OVERVIEW", "概览", `RemoteCI 课表助手 · ${esc(o.now.replace("T", " "))}`) + status +
            warn.map((w) => `<div class="notice warning">${ICON.alert}<div>${esc(w)}</div></div>`).join("") +
            `<section class="metric-grid">
                <article class="metric-card primary"><span class="metric-label">已绑定账号</span><strong class="metric-value">${o.bindings}</strong>
                    <span class="metric-chip ${o.auth_failed ? "danger" : "positive"}">${o.auth_failed ? o.auth_failed + " 个凭据失效" : "凭据全部有效"}</span></article>
                <article class="metric-card"><span class="metric-label">老师 / 班主任会话</span><strong class="metric-value">${o.teachers}</strong><span class="metric-chip">个人主动提醒</span></article>
                <article class="metric-card"><span class="metric-label">班级群</span><strong class="metric-value">${o.class_groups}</strong><span class="metric-chip">仅定时课表</span></article>
                <article class="metric-card"><span class="metric-label">今日推送</span><strong class="metric-value">${o.sent_today}</strong><span class="metric-chip">${o.holiday_today ? "假期暂停" : "条消息"}</span></article>
            </section>
            <section class="panel"><div class="panel-heading"><div><h2>最近推送</h2><p class="muted small">最新 6 条，完整记录见“推送记录”。</p></div></div>${logList(logs.slice(0, 6))}</section>`;
        app.querySelector("[data-act=pause]")?.addEventListener("click", () => action(() => post("settings/pause", { paused: true }), "已暂停").then(render));
        app.querySelector("[data-act=resume]")?.addEventListener("click", () => action(() => post("settings/pause", { paused: false }), "已恢复").then(render));
    }

    const KIND = { today: "当日日程", tomorrow: "次日日程", before: "课前提醒", change: "换课提醒", auth: "凭据失效", test: "测试推送" };
    function logList(items) {
        if (!items.length) return `<div class="empty-state"><strong>还没有推送</strong><span>老师绑定账号后，到点会自动推送。</span></div>`;
        return `<div class="activity-list">${items.map((x) => `<article><span class="activity-icon ${x.ok ? "" : "bad"}">${x.ok ? ICON.bell : ICON.alert}</span>
            <div><strong>${esc(KIND[x.kind] || x.kind)} → ${esc(x.target)}</strong><pre>${esc(x.text)}</pre></div><time>${fmtTime(x.at)}</time></article>`).join("")}</div>`;
    }

    // ---------- 账号绑定 ----------
    async function renderBindings() {
        const rows = await get("bindings");
        const list = rows.length ? `<div class="link-list">${rows.map((b) => `
            <div class="link-row">
                <div class="who"><span class="avatar">${initial(b.sender_name)}</span><div><strong>${esc(b.sender_name || b.sender_id)}</strong><small>${esc(b.platform)} · ${esc(b.sender_id)}${b.umo ? "" : " · 尚未私聊"}</small></div></div>
                <span class="link-arrow">${ICON.link}</span>
                <div class="who"><span class="avatar rci">${initial(b.display_name || b.username)}</span><div>
                    <strong>${esc(b.display_name || "—")} <small>（${esc(b.username || "")}）</small></strong>
                    <div class="tags"><span class="pill accent">${esc(b.role_kind)}</span><span class="pill">${esc(roles[b.push_role] || b.push_role)}</span>
                    <span class="pill ${b.status === "ok" ? "ok" : "bad"}">${b.status === "ok" ? "正常" : "凭据失效"}</span>
                    <span class="pill">${b.auth_type === "api_key" ? "API Key" : "账号密码"}</span>${b.has_prefs ? '<span class="pill warn">有个人提醒设置</span>' : ""}</div>
                    <small>${esc((b.classes || []).join("、") || "无班级")} · ${esc(b.server_url)}</small></div></div>
                <div class="actions">
                    <button class="btn sm ghost" data-refresh="${esc(b.key)}">刷新</button>
                    ${b.has_prefs ? `<button class="btn sm ghost" data-reset="${esc(b.key)}">重置提醒</button>` : ""}
                    <button class="btn sm danger" data-unbind="${esc(b.key)}">解绑</button>
                </div>
            </div>`).join("")}</div>` : `<div class="empty-state"><strong>还没有人绑定</strong><span>让老师私聊机器人发送 <code>/rci 绑定 &lt;API Key&gt;</code> 或 <code>/rci 登录 &lt;用户名&gt; &lt;密码&gt;</code>，也可以直接用自然语言说明。</span></div>`;
        app.innerHTML = head("BINDINGS", "账号绑定", "聊天中的个人会话与 RemoteCI 姓名的对应关系。凭据由用户在私聊中自行提供，这里不会显示密钥。") +
            `<section class="panel">${list}</section>`;
        app.querySelectorAll("[data-refresh]").forEach((el) => el.addEventListener("click", () => action(() => post("binding/refresh", { key: el.dataset.refresh }), "已刷新账号信息").then(render)));
        app.querySelectorAll("[data-reset]").forEach((el) => el.addEventListener("click", () => action(() => post("binding/reset_prefs", { key: el.dataset.reset }), "已恢复统一设置").then(render)));
        app.querySelectorAll("[data-unbind]").forEach((el) => el.addEventListener("click", () => {
            if (!confirm("确定解除该用户的 RemoteCI 绑定？")) return;
            action(() => post("binding/unbind", { key: el.dataset.unbind }), "已解绑").then(render);
        }));
    }

    // ---------- 会话与推送 ----------
    async function renderSessions() {
        const { sessions, bindings } = await get("sessions");
        const cards = sessions.map((s) => sessionCard(s, bindings)).join("");
        app.innerHTML = head("SESSIONS", "会话与推送", "设置每个会话的角色：老师、班主任（个人会话，接收主动提醒）或班级群（只做定时课表推送，不做个人提醒）。群聊需要先有人 @机器人 使用一次才会出现在这里。") +
            (sessions.length ? `<div class="session-grid">${cards}</div>` : `<section class="panel"><div class="empty-state"><strong>还没有会话</strong><span>用户私聊或在群里 @机器人 使用 /rci 后会出现在这里。</span></div></section>`);
        sessions.forEach((s, i) => wireSession(app.querySelectorAll(".session-card")[i], s));
    }

    function roleOptions(s) {
        const allowed = s.kind === "group" ? ["auto", "none", "class_group"] : ["auto", "none", "teacher", "headteacher"];
        return allowed.map((r) => `<option value="${r}" ${s.role === r ? "selected" : ""}>${esc(roles[r])}${r === "auto" ? `（当前：${esc(roles[s.effective_role])}）` : ""}</option>`).join("");
    }

    function classOptions(s, selected) {
        return `<option value="">— 选择班级 —</option>` + (s.classes || []).map((c) => `<option value="${esc(c.id)}" ${selected === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
    }

    function sessionCard(s, bindings) {
        const p = s.push || {};
        const eff = s.effective_role;
        const isGroup = s.kind === "group";
        let body = `<label class="field">推送角色<select data-f="role">${roleOptions(s)}</select></label>`;
        if (isGroup) {
            body += `<div class="form-grid">
                <label class="field">读取课表的账号<select data-f="binding_key"><option value="">— 选择 —</option>${bindings.map((b) => `<option value="${esc(b.key)}" ${s.binding_key === b.key ? "selected" : ""}>${esc(b.name)}</option>`).join("")}</select></label>
                <label class="field">班级<select data-f="class_id">${classOptions(s, s.class_id)}</select></label></div>
                <div class="switch-row"><div><strong>定时推送班级课表</strong><small>默认关闭；开启后按下方时间推送，节假日自动暂停。</small></div><label class="switch"><input type="checkbox" data-p="enabled" ${p.enabled ? "checked" : ""}><span></span></label></div>
                <div class="form-grid">
                    <label class="field">当日课表时间<input type="time" data-p="today_time" value="${esc(p.today_time)}"></label>
                    <label class="field">次日课表时间<input type="time" data-p="tomorrow_time" value="${esc(p.tomorrow_time)}"></label></div>`;
        } else {
            body += `<p class="muted small">已绑定：<strong>${esc(s.binding_name || "未绑定")}</strong>。提醒时间由老师自己用指令修改，默认值在“提醒设置”页。</p>
                <div class="form-grid">
                    <label class="field">当日/次日推送内容<select data-p="content"><option value="personal" ${p.content !== "class" ? "selected" : ""}>个人日程</option><option value="class" ${p.content === "class" ? "selected" : ""}>班级课表</option></select></label>
                    <label class="field">班级（推送班级课表时）<select data-f="class_id">${classOptions(s, s.class_id)}</select></label></div>`;
            if (eff === "headteacher") {
                const watched = s.watch_class_ids || (s.classes || []).filter((c) => c.roleKind === 4).map((c) => c.id);
                body += `<div class="field"><span class="muted small">班主任关注的班级（班里的课被换时提醒）</span><div class="check-list">${(s.classes || []).map((c) => `<label><input type="checkbox" data-watch="${esc(c.id)}" ${watched.includes(c.id) ? "checked" : ""}>${esc(c.name)}</label>`).join("") || '<span class="muted small">没有可访问的班级</span>'}</div></div>`;
            }
        }
        const pill = eff === "none" ? "" : "accent";
        return `<article class="session-card">
            <header><div><strong>${esc(s.name || s.umo)}</strong><small>${esc(s.umo)}</small></div>
                <div class="tags"><span class="pill">${isGroup ? "群聊" : "私聊"}</span><span class="pill ${pill}">${esc(roles[eff] || eff)}</span></div></header>
            ${body}
            <div class="actions"><button class="btn sm" data-save>保存</button><button class="btn sm ghost" data-test>测试推送</button><button class="btn sm danger" data-del>移除</button>
            <span class="muted small" style="margin-left:auto">最近活跃 ${fmtTime(s.last_seen)}</span></div>
        </article>`;
    }

    function wireSession(card, s) {
        card.querySelector("[data-save]").addEventListener("click", () => {
            const patch = { push: {} };
            card.querySelectorAll("[data-f]").forEach((el) => (patch[el.dataset.f] = el.value || null));
            card.querySelectorAll("[data-p]").forEach((el) => (patch.push[el.dataset.p] = el.type === "checkbox" ? el.checked : el.value));
            const watch = card.querySelectorAll("[data-watch]");
            if (watch.length) patch.watch_class_ids = [...watch].filter((x) => x.checked).map((x) => x.dataset.watch);
            action(() => post("session/update", { umo: s.umo, patch }), "已保存").then(render);
        });
        card.querySelector("[data-test]").addEventListener("click", () => action(() => post("session/test", { umo: s.umo }), "已发送测试推送"));
        card.querySelector("[data-del]").addEventListener("click", () => {
            if (!confirm("移除该会话记录？（不会解绑账号，用户再次使用时会重新出现）")) return;
            action(() => post("session/delete", { umo: s.umo }), "已移除").then(render);
        });
    }

    // ---------- 提醒设置 ----------
    async function renderReminders() {
        const { reminders: r, poll_minutes: poll } = await get("settings");
        const sw = (key, title, desc) => `<div class="switch-row"><div><strong>${title}</strong><small>${desc}</small></div><label class="switch"><input type="checkbox" data-k="${key}" ${r[key] ? "checked" : ""}><span></span></label></div>`;
        app.innerHTML = head("REMINDERS", "提醒设置", "老师和班主任的统一默认设置。老师可以用指令或自然语言修改自己的设置，个人设置优先；群聊不做这些主动提醒。",
            `<button class="btn" id="save">保存统一设置</button>`) +
            `<div class="two-column">
            <section class="panel"><h2>日程推送</h2><p class="muted small" style="margin:4px 0 14px">每天上学时推送当天日程；自己最后一节课下课时推送明天日程。</p>
                ${sw("today_enabled", "当日日程", "上学时提醒一天的个人日程")}
                <div class="form-grid" style="margin-bottom:12px"><label class="field">推送时间<input type="time" data-k="today_time" value="${esc(r.today_time)}"></label></div>
                ${sw("tomorrow_enabled", "次日日程", "提醒明天的个人日程")}
                <div class="form-grid three">
                    <label class="field">推送时机<select data-k="tomorrow_mode"><option value="last_class" ${r.tomorrow_mode === "last_class" ? "selected" : ""}>自己最后一节课下课时</option><option value="fixed" ${r.tomorrow_mode === "fixed" ? "selected" : ""}>固定时间</option></select></label>
                    <label class="field">固定时间<input type="time" data-k="tomorrow_time" value="${esc(r.tomorrow_time)}"></label>
                    <label class="field">当天无课时改在<input type="time" data-k="tomorrow_fallback" value="${esc(r.tomorrow_fallback)}"></label></div>
                <div style="margin-top:14px">${sw("skip_empty_days", "没课的日子不推送", "当天或次日没有课程时跳过日程推送")}</div>
            </section>
            <section class="panel"><h2>课程提醒</h2><p class="muted small" style="margin:4px 0 14px">每 ${esc(poll)} 分钟比对一次课表发现换课（间隔在插件配置中调整）。</p>
                ${sw("before_enabled", "课前提醒", "自己的每节课开始前提醒")}
                <div class="form-grid" style="margin-bottom:12px"><label class="field">提前分钟数<input type="number" min="1" max="120" data-k="before_minutes" value="${esc(r.before_minutes)}"></label></div>
                ${sw("change_enabled", "我的课被换了", "自己的课程被调换、取消、新增或改时间时提醒")}
                ${sw("class_change_enabled", "班里的课被换了（班主任）", "班主任额外接收所管班级的课表变动，含任课教师变更")}
            </section></div>`;
        app.querySelector("#save").addEventListener("click", () => {
            const patch = {};
            app.querySelectorAll("[data-k]").forEach((el) => (patch[el.dataset.k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value));
            action(() => post("settings/reminders", { patch }), "已保存统一设置").then(render);
        });
    }

    // ---------- 节假日 ----------
    async function renderHolidays() {
        const s = await get("settings");
        const h = s.holidays;
        const ranges = (h.ranges || []).map(rangeRow).join("");
        const upcoming = s.upcoming.length ? `<div class="activity-list">${s.upcoming.map((x) => `<article><span class="activity-icon ${x.off ? "" : "bad"}">${x.off ? ICON.bell : ICON.alert}</span>
            <div><strong>${esc(x.name)}${x.off ? "" : "（调休上课）"}</strong><pre>${esc(x.start)}${x.end !== x.start ? " ~ " + esc(x.end) : ""}</pre></div><span class="pill ${x.source === "custom" ? "accent" : ""}">${x.source === "custom" ? "自定义" : "法定"}</span></article>`).join("")}</div>`
            : `<div class="empty-state"><strong>近期没有假期</strong></div>`;
        app.innerHTML = head("HOLIDAYS", "节假日", "节假日期间自动暂停所有主动推送（日程、课前、换课提醒）；假期最后一天仍会推送次日日程。",
            `<button class="btn ghost" id="refresh">刷新法定假期数据</button><button class="btn" id="save">保存</button>`) +
            `<div class="two-column"><section class="panel"><h2>判定规则</h2>
                <div class="switch-row" style="margin-top:14px"><div><strong>使用法定节假日数据</strong><small>含调休补班；数据来自 holiday-cn，${s.official_data ? "今年数据已就绪" : "<span style='color:var(--danger)'>今年数据尚未获取</span>"}</small></div><label class="switch"><input type="checkbox" id="official" ${h.use_official ? "checked" : ""}><span></span></label></div>
                <div class="switch-row"><div><strong>周末视为假期</strong><small>学校周末不上课时可开启；调休补班日仍会推送</small></div><label class="switch"><input type="checkbox" id="weekend" ${h.weekend_as_holiday ? "checked" : ""}><span></span></label></div>
                <h2 style="margin:22px 0 12px">自定义假期（寒暑假、校内放假）</h2>
                <div id="ranges">${ranges}</div>
                <button class="btn sm ghost" id="add">添加假期</button>
            </section>
            <section class="panel"><h2 style="margin-bottom:14px">近期假期</h2>${upcoming}</section></div>`;
        app.querySelector("#add").addEventListener("click", () => app.querySelector("#ranges").insertAdjacentHTML("beforeend", rangeRow({})));
        app.querySelector("#ranges").addEventListener("click", (e) => e.target.closest("[data-remove]")?.closest(".range-row").remove());
        app.querySelector("#refresh").addEventListener("click", () => action(() => post("holidays/refresh"), "").then((errs) => { toast(errs && errs.length ? errs.join("；") : "已刷新", !!(errs && errs.length)); render(); }));
        app.querySelector("#save").addEventListener("click", () => {
            const list = [...app.querySelectorAll(".range-row")].map((row) => ({
                name: row.querySelector("[data-r=name]").value, start: row.querySelector("[data-r=start]").value, end: row.querySelector("[data-r=end]").value,
            })).filter((x) => x.start);
            action(() => post("settings/holidays", { use_official: app.querySelector("#official").checked, weekend_as_holiday: app.querySelector("#weekend").checked, ranges: list }), "已保存").then(render);
        });
    }

    function rangeRow(r) {
        return `<div class="range-row"><label class="field">名称<input data-r="name" value="${esc(r.name || "")}" placeholder="寒假"></label>
            <label class="field">开始<input type="date" data-r="start" value="${esc(r.start || "")}"></label>
            <label class="field">结束<input type="date" data-r="end" value="${esc(r.end || "")}"></label>
            <button class="btn sm danger" data-remove>删除</button></div>`;
    }

    // ---------- 推送记录 ----------
    async function renderLog() {
        const logs = await get("log");
        app.innerHTML = head("LOG", "推送记录", "最近 150 条主动推送，失败的推送通常是机器人已不在该会话或平台离线。", `<button class="btn ghost" id="reload">刷新</button>`) +
            `<section class="panel">${logList(logs)}</section>`;
        app.querySelector("#reload").addEventListener("click", render);
    }

    // ---------- 启动 ----------
    async function boot() {
        if (!bridge) {
            app.innerHTML = `<div class="notice error">${ICON.alert}<div><strong>请在 AstrBot 管理面板中打开此页面</strong><span>插件页面需要 AstrBot 提供的 AstrBotPluginPage 接口（AstrBot 4.x 新版）。</span></div></div>`;
            return;
        }
        try {
            const ctx = await bridge.ready();
            applyTheme(ctx);
            bridge.onContext?.(applyTheme);
        } catch { /* 主题获取失败时保持默认 */ }
        render();
    }
    boot();
})();
