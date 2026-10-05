(() => {
    "use strict";

    const bridge = window.AstrBotPluginPage;
    const app = document.getElementById("app");
    const fab = document.getElementById("fab");
    const toastEl = document.getElementById("toast");
    let view = "overview";
    let roles = { auto: "自动", none: "不推送", teacher: "老师", headteacher: "班主任", class_group: "班级群" };
    let sessionTab = "private";

    const ICON = {
        info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/></svg>',
        alert: '<svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
        ok: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>',
        arrow: '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
        plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
        refresh: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/></svg>',
    };

    // ---------- 工具 ----------
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const initial = (name) => esc((String(name || "?").trim()[0] || "?").toUpperCase());
    const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";

    function toast(message, isError = false) {
        toastEl.textContent = message;
        toastEl.className = "snackbar show" + (isError ? " error" : "");
        clearTimeout(toast.timer);
        toast.timer = setTimeout(() => (toastEl.className = "snackbar"), isError ? 5000 : 2400);
    }

    // 宿主 bridge 会取出响应体的 data 字段；旧宿主可能原样返回 {ok, data}，这里兼容两种情况。
    function unwrap(r) {
        if (r && typeof r === "object" && !Array.isArray(r)) {
            if (r.status === "error" || r.ok === false) throw new Error(r.message || "请求失败");
            if (r.ok === true && "data" in r) return r.data;
        }
        return r;
    }
    const errorText = (err) => err?.data?.message || err?.response?.data?.message || err?.message || String(err);
    const get = async (endpoint) => unwrap(await bridge.apiGet(endpoint));
    const post = async (endpoint, body) => unwrap(await bridge.apiPost(endpoint, body || {}));

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

    // ---------- 组件（对应 AstrBot 配置页：分节标题、设置卡片、左说明右控件的设置行） ----------
    const head = (title, desc, actions = "", level = 1) =>
        `<div class="section-head"><div><h${level}>${title}</h${level}>${desc ? `<p>${desc}</p>` : ""}</div>${actions ? `<div class="head-actions">${actions}</div>` : ""}</div>`;
    const card = (title, desc, body, foot = "", attrs = "") =>
        `<section class="card" ${attrs}>${title ? `<div class="card-head"><div><h3>${title}</h3>${desc ? `<p>${desc}</p>` : ""}</div></div>` : ""}${body}${foot ? `<div class="card-foot">${foot}</div>` : ""}</section>`;
    const row = (label, desc, control, cls = "") =>
        `<div class="row ${cls}"><div class="row-text"><strong>${label}</strong>${desc ? `<span>${desc}</span>` : ""}</div><div class="row-control">${control}</div></div>`;
    const toggle = (attrs, checked) => `<label class="v-switch"><input type="checkbox" ${attrs} ${checked ? "checked" : ""}><span class="track"></span></label>`;
    const select = (attrs, options, value) =>
        `<span class="v-select"><select ${attrs}>${options.map(([v, t]) => `<option value="${esc(v)}" ${String(value ?? "") === String(v) ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></span>`;
    const field = (attrs, value, type = "text", cls = "") => `<input class="v-field ${cls}" type="${type}" ${attrs} value="${esc(value ?? "")}">`;
    const alertBox = (kind, title, desc) =>
        `<div class="alert ${kind}">${ICON[kind === "ok" ? "ok" : kind === "info" ? "info" : "alert"]}<div><strong>${title}</strong>${desc ? `<span>${desc}</span>` : ""}</div></div>`;
    const empty = (title, desc) => `<div class="empty"><strong>${title}</strong>${desc ? `<span>${desc}</span>` : ""}</div>`;

    function setFab(handler) {
        fab.hidden = !handler;
        fab.onclick = handler || null;
    }

    // ---------- 主题：跟随 AstrBot 深浅色 ----------
    // 优先用 bridge 上下文的 isDark；宿主没给时按系统深浅色。
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    // 宿主在页面加载时就会写好 <html data-theme>，先以它为准，避免闪烁。
    let hostDark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : null;
    function applyTheme() {
        const dark = hostDark ?? (media ? media.matches : false);
        if (document.documentElement.dataset.theme !== (dark ? "dark" : "light")) {
            document.documentElement.dataset.theme = dark ? "dark" : "light";
        }
    }
    function onHostContext(ctx) {
        if (ctx && typeof ctx.isDark === "boolean") hostDark = ctx.isDark;
        applyTheme();
    }
    media?.addEventListener?.("change", applyTheme);

    // ---------- 导航 ----------
    document.getElementById("nav").addEventListener("click", (e) => {
        const btn = e.target.closest(".nav-item");
        if (!btn) return;
        view = btn.dataset.view;
        document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b === btn));
        render();
    });

    async function render() {
        setFab(null);
        const renderers = { overview: renderOverview, bindings: renderBindings, sessions: renderSessions, reminders: renderReminders, holidays: renderHolidays, log: renderLog };
        try {
            await renderers[view]();
        } catch (err) {
            app.innerHTML = alertBox("error", "加载失败", esc(errorText(err)));
        }
    }

    // ---------- 概览 ----------
    async function renderOverview() {
        const [o, logs] = await Promise.all([get("overview"), get("log")]);
        if (o.roles) roles = o.roles;
        document.getElementById("clock").textContent = o.now.replace("T", " ");
        let status;
        if (o.paused) status = alertBox("warn", "主动推送已全局暂停", "所有老师提醒和班级群课表推送都不会发送。");
        else if (o.holiday_today) status = alertBox("warn", `今天是${esc(o.holiday_reason || "节假日")}，主动推送自动暂停`, o.holiday_tomorrow ? "明天仍在假期中。" : "假期最后一天晚上会照常推送次日日程。");
        else status = alertBox("ok", "主动推送运行中", (o.holiday_reason ? esc(o.holiday_reason) + "；" : "") + (o.holiday_tomorrow ? "明天放假，不推送次日日程。" : "按设置推送当日/次日日程、课前与换课提醒。"));
        const warns = [];
        if (!o.default_server) warns.push(alertBox("info", "未配置默认服务器地址", "用户绑定时需要自己提供服务器地址；可在插件配置的 default_server_url 中填写。"));
        if (!o.official_data) warns.push(alertBox("warn", "尚未获取今年的法定节假日数据", "暂时只按自定义假期判断，可在“节假日”页刷新。"));
        if (o.last_error) warns.push(alertBox("error", "最近一次调度出错", esc(o.last_error)));

        app.innerHTML = `<div class="section">${head("概览", "RemoteCI AstrbotPlugin 的运行状态与最近推送。")}${status}${warns.join("")}
            ${card("", "", `<div class="stats">
                <div class="stat"><span>已绑定账号</span><strong>${o.bindings}</strong><small>${o.auth_failed ? `<span class="chip bad">${o.auth_failed} 个凭据失效</span>` : "凭据全部有效"}</small></div>
                <div class="stat"><span>老师 / 班主任</span><strong>${o.teachers}</strong><small>个人主动提醒</small></div>
                <div class="stat"><span>班级群</span><strong>${o.class_groups}</strong><small>仅定时课表</small></div>
                <div class="stat"><span>今日推送</span><strong>${o.sent_today}</strong><small>${o.holiday_today ? "假期暂停" : "条消息"}</small></div></div>`)}
            ${card("推送开关", "全局暂停后，所有主动推送停止；指令与自然语言查询不受影响。",
                row("主动推送", o.paused ? "已暂停" : "运行中", toggle('id="running"', !o.paused)) +
                row("默认服务器", "用户绑定时可省略服务器地址", `<span>${esc(o.default_server || "未配置")}</span>`))}
            </div>
            <div class="section-rule"></div>
            <div class="section">${head("最近推送", "最新 6 条，完整记录见“推送记录”。", "", 2)}${card("", "", logRows(logs.slice(0, 6)))}</div>`;
        app.querySelector("#running").addEventListener("change", (e) =>
            action(() => post("settings/pause", { paused: !e.target.checked }), e.target.checked ? "已恢复推送" : "已暂停推送").then(render, render));
    }

    const KIND = { today: "当日日程", tomorrow: "次日日程", before: "课前提醒", change: "换课提醒", auth: "凭据失效", test: "测试推送" };
    function logRows(items) {
        if (!items.length) return empty("还没有推送", "老师绑定账号后，到点会自动推送。");
        return items.map((x) => `<div class="row"><div class="row-text"><strong>${esc(KIND[x.kind] || x.kind)} → ${esc(x.target)}</strong>
            <pre class="log-text">${esc(x.text)}</pre></div><div class="row-control"><span class="chip ${x.ok ? "ok" : "bad"}">${x.ok ? "已送达" : "失败"}</span><span class="log-time">${fmtTime(x.at)}</span></div></div>`).join("");
    }

    // ---------- 账号绑定 ----------
    async function renderBindings() {
        const rows = await get("bindings");
        const body = rows.length ? rows.map((b) => `
            <div class="row"><div class="row-text">
                <div class="link"><span class="avatar">${initial(b.sender_name)}</span><strong>${esc(b.sender_name || b.sender_id)}</strong>
                    <span class="link-arrow">${ICON.arrow}</span><span class="avatar rci">${initial(b.display_name || b.username)}</span><strong>${esc(b.display_name || "—")}</strong></div>
                <span>${esc(b.platform)} · ${esc(b.sender_id)}${b.umo ? "" : " · 尚未私聊"} → ${esc(b.username || "")} · ${esc((b.classes || []).join("、") || "无班级")}</span>
                <div class="tags"><span class="chip primary">${esc(b.role_kind)}</span><span class="chip">${esc(roles[b.push_role] || b.push_role)}</span>
                    <span class="chip ${b.status === "ok" ? "ok" : "bad"}">${b.status === "ok" ? "凭据有效" : "凭据失效"}</span>
                    <span class="chip">${b.auth_type === "api_key" ? "API Key" : "账号密码"}</span>${b.has_prefs ? '<span class="chip warn">有个人提醒设置</span>' : ""}</div>
            </div><div class="row-control">
                <button class="btn text" data-refresh="${esc(b.key)}">刷新</button>
                ${b.has_prefs ? `<button class="btn text" data-reset="${esc(b.key)}">重置提醒</button>` : ""}
                <button class="btn danger" data-unbind="${esc(b.key)}">解绑</button>
            </div></div>`).join("")
            : empty("还没有人绑定", "让老师私聊机器人发送 <code>/rci 绑定 &lt;API Key&gt;</code> 或 <code>/rci 登录 &lt;用户名&gt; &lt;密码&gt;</code>，也可以直接用自然语言说明。");
        app.innerHTML = `<div class="section">${head("账号绑定", "聊天中的个人会话与 RemoteCI 姓名的对应关系。凭据由用户在私聊中自行提供，这里不显示密钥。")}
            ${card("已绑定账号", `共 ${rows.length} 个`, body)}</div>`;
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
        const inTab = (s, tab) => (tab === "group") === (s.kind === "group");
        const list = sessions.filter((s) => inTab(s, sessionTab));
        const count = (tab) => sessions.filter((s) => inTab(s, tab)).length;
        const desc = sessionTab === "group"
            ? "把群设为班级群后，可以按时间推送班级课表（需显式开启）。群聊不做个人主动提醒；有人 @机器人 使用过一次后群才会出现在这里。"
            : "老师、班主任的私聊会话接收主动提醒；“自动”按 RemoteCI 账号角色判断。";
        app.innerHTML = `<div class="section">${head("会话与推送", desc)}
            <div class="tabs"><button class="tab ${sessionTab === "private" ? "active" : ""}" data-tab="private">个人会话 · ${count("private")}</button>
            <button class="tab ${sessionTab === "group" ? "active" : ""}" data-tab="group">群聊 · ${count("group")}</button></div>
            ${list.length ? list.map((s) => sessionCard(s, bindings)).join("") : card("", "", empty(sessionTab === "group" ? "还没有群聊" : "还没有个人会话", "用户私聊或在群里 @机器人 使用 /rci 后会出现在这里。"))}</div>`;
        app.querySelectorAll("[data-tab]").forEach((el) => el.addEventListener("click", () => { sessionTab = el.dataset.tab; render(); }));
        app.querySelectorAll(".card[data-umo]").forEach((el) => wireSession(el, list.find((s) => s.umo === el.dataset.umo)));
    }

    function sessionCard(s, bindings) {
        const p = s.push || {};
        const eff = s.effective_role;
        const isGroup = s.kind === "group";
        const roleOpts = (isGroup ? ["auto", "none", "class_group"] : ["auto", "none", "teacher", "headteacher"])
            .map((r) => [r, r === "auto" ? `自动（当前：${roles[s.effective_role]}）` : roles[r]]);
        const classOpts = [["", "未选择"], ...(s.classes || []).map((c) => [c.id, c.name])];
        let rows = row("推送角色", isGroup ? "群聊只能设为班级群或不推送" : "老师、班主任接收个人主动提醒", select('data-f="role"', roleOpts, s.role));
        if (isGroup) {
            rows += row("读取课表的账号", "用哪位已绑定用户的权限读取班级课表", select('data-f="binding_key"', [["", "未选择"], ...bindings.map((b) => [b.key, b.name])], s.binding_key))
                + row("班级", "要推送的班级课表", select('data-f="class_id"', classOpts, s.class_id))
                + row("定时推送班级课表", "默认关闭；节假日自动暂停", toggle('data-p="enabled"', p.enabled))
                + row("当日课表时间", "", field('data-p="today_time"', p.today_time, "time", "sm"))
                + row("次日课表时间", "", field('data-p="tomorrow_time"', p.tomorrow_time, "time", "sm"));
        } else {
            rows += row("已绑定账号", "提醒时间由老师自己用指令修改，默认值见“提醒设置”", `<span>${esc(s.binding_name || "未绑定")}</span>`)
                + row("当日/次日推送内容", "推送个人日程，或某个班级的课表", select('data-p="content"', [["personal", "个人日程"], ["class", "班级课表"]], p.content === "class" ? "class" : "personal"))
                + row("班级", "推送班级课表时使用", select('data-f="class_id"', classOpts, s.class_id));
            if (eff === "headteacher") {
                const watched = s.watch_class_ids || (s.classes || []).filter((c) => c.roleKind === 4).map((c) => c.id);
                rows += row("关注的班级", "这些班级的课被换时提醒班主任",
                    `<div class="check-chips">${(s.classes || []).map((c) => `<label><input type="checkbox" data-watch="${esc(c.id)}" ${watched.includes(c.id) ? "checked" : ""}>${esc(c.name)}</label>`).join("") || "<span>没有可访问的班级</span>"}</div>`, "stack");
            }
        }
        const title = `${esc(s.name || s.umo)} <span class="chip ${eff === "none" ? "" : "primary"}">${esc(roles[eff] || eff)}</span>`;
        return card(title, esc(s.umo), rows,
            `<button class="btn" data-save>保存</button><button class="btn tonal" data-test>测试推送</button><span class="spacer"></span>
             <span class="log-time">最近活跃 ${fmtTime(s.last_seen)}</span><button class="btn danger" data-del>移除</button>`,
            `data-umo="${esc(s.umo)}"`);
    }

    function wireSession(el, s) {
        el.querySelector("[data-save]").addEventListener("click", () => {
            const patch = { push: {} };
            el.querySelectorAll("[data-f]").forEach((x) => (patch[x.dataset.f] = x.value || null));
            el.querySelectorAll("[data-p]").forEach((x) => (patch.push[x.dataset.p] = x.type === "checkbox" ? x.checked : x.value));
            const watch = el.querySelectorAll("[data-watch]");
            if (watch.length) patch.watch_class_ids = [...watch].filter((x) => x.checked).map((x) => x.dataset.watch);
            action(() => post("session/update", { umo: s.umo, patch }), "已保存").then(render);
        });
        el.querySelector("[data-test]").addEventListener("click", () => action(() => post("session/test", { umo: s.umo }), "已发送测试推送"));
        el.querySelector("[data-del]").addEventListener("click", () => {
            if (!confirm("移除该会话记录？（不会解绑账号，用户再次使用时会重新出现）")) return;
            action(() => post("session/delete", { umo: s.umo }), "已移除").then(render);
        });
    }

    // ---------- 提醒设置 ----------
    async function renderReminders() {
        const { reminders: r, poll_minutes: poll } = await get("settings");
        const sw = (key, title, desc) => row(title, desc, toggle(`data-k="${key}"`, r[key]));
        app.innerHTML = `<div class="section">${head("提醒设置", "老师和班主任的统一默认设置。老师可以用指令或自然语言修改自己的设置，个人设置优先；群聊不做这些主动提醒。")}
            ${card("日程推送", "每天上学时推送当天日程；自己最后一节课下课时推送明天日程。",
                sw("today_enabled", "当日日程", "上学时提醒一天的个人日程")
                + row("当日推送时间", "", field('data-k="today_time"', r.today_time, "time", "sm"))
                + sw("tomorrow_enabled", "次日日程", "提醒明天的个人日程")
                + row("次日推送时机", "", select('data-k="tomorrow_mode"', [["last_class", "自己最后一节课下课时"], ["fixed", "固定时间"]], r.tomorrow_mode))
                + row("固定时间", "推送时机为“固定时间”时使用", field('data-k="tomorrow_time"', r.tomorrow_time, "time", "sm"))
                + row("当天无课时改在", "推送时机为“最后一节课下课时”且当天没课时使用", field('data-k="tomorrow_fallback"', r.tomorrow_fallback, "time", "sm"))
                + sw("skip_empty_days", "没课的日子不推送", "当天或次日没有课程时跳过日程推送"))}
            ${card("课程提醒", `每 ${esc(poll)} 分钟比对一次课表来发现换课（间隔在插件配置中调整）。`,
                sw("before_enabled", "课前提醒", "自己的每节课开始前提醒")
                + row("提前分钟数", "1 - 120", field('data-k="before_minutes" min="1" max="120"', r.before_minutes, "number", "sm"))
                + sw("change_enabled", "我的课被换了", "自己的课程被调换、取消、新增或改时间时提醒")
                + sw("class_change_enabled", "班里的课被换了", "班主任额外接收所管班级的课表变动，含任课教师变化"))}</div>`;
        setFab(() => {
            const patch = {};
            app.querySelectorAll("[data-k]").forEach((el) => (patch[el.dataset.k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value));
            action(() => post("settings/reminders", { patch }), "已保存统一设置").then(render);
        });
    }

    // ---------- 节假日 ----------
    async function renderHolidays() {
        const s = await get("settings");
        const h = s.holidays;
        const upcoming = s.upcoming.length ? s.upcoming.map((x) => row(`${esc(x.name)}${x.off ? "" : "（调休上课）"}`,
            `${esc(x.start)}${x.end !== x.start ? " ~ " + esc(x.end) : ""}`,
            `<span class="chip ${x.off ? "primary" : "warn"}">${x.off ? "暂停推送" : "照常推送"}</span><span class="chip">${x.source === "custom" ? "自定义" : "法定"}</span>`)).join("")
            : empty("近期没有假期", "");
        app.innerHTML = `<div class="section">${head("节假日", "节假日期间自动暂停所有主动推送（日程、课前、换课提醒）；假期最后一天仍会推送次日日程。",
                `<button class="btn tonal" id="refresh">${ICON.refresh}刷新法定假期</button>`)}
            ${card("判定规则", "",
                row("使用法定节假日数据", `含调休补班，数据来自 holiday-cn；${s.official_data ? "今年数据已就绪" : "今年数据尚未获取"}`, toggle('id="official"', h.use_official))
                + row("周末视为假期", "学校周末不上课时可开启；调休补班日仍会推送", toggle('id="weekend"', h.weekend_as_holiday)))}
            ${card("自定义假期", "寒暑假、校内放假等。", `<div id="ranges">${(h.ranges || []).map(rangeRow).join("")}</div>`,
                `<button class="btn text" id="add">${ICON.plus}添加假期</button>`)}
            ${card("近期假期", "", upcoming)}</div>`;
        app.querySelector("#add").addEventListener("click", () => app.querySelector("#ranges").insertAdjacentHTML("beforeend", rangeRow({})));
        app.querySelector("#ranges").addEventListener("click", (e) => e.target.closest("[data-remove]")?.closest(".row").remove());
        app.querySelector("#refresh").addEventListener("click", () => action(() => post("holidays/refresh")).then((errs) => {
            toast(errs && errs.length ? errs.join("；") : "已刷新", !!(errs && errs.length));
            render();
        }));
        setFab(() => {
            const ranges = [...app.querySelectorAll("#ranges .row")].map((r) => ({
                name: r.querySelector("[data-r=name]").value, start: r.querySelector("[data-r=start]").value, end: r.querySelector("[data-r=end]").value,
            })).filter((x) => x.start);
            action(() => post("settings/holidays", { use_official: app.querySelector("#official").checked, weekend_as_holiday: app.querySelector("#weekend").checked, ranges }), "已保存").then(render);
        });
    }

    function rangeRow(r) {
        return `<div class="row"><div class="row-control range">
            ${field('data-r="name" placeholder="名称，如 寒假"', r.name, "text", "sm")}
            ${field('data-r="start"', r.start, "date", "sm")}<span class="log-time">至</span>${field('data-r="end"', r.end, "date", "sm")}</div>
            <div class="row-control"><button class="btn danger" data-remove>删除</button></div></div>`;
    }

    // ---------- 推送记录 ----------
    async function renderLog() {
        const logs = await get("log");
        app.innerHTML = `<div class="section">${head("推送记录", "最近 150 条主动推送。失败通常是机器人已不在该会话或平台离线。", `<button class="btn tonal" id="reload">${ICON.refresh}刷新</button>`)}
            ${card("", "", logRows(logs))}</div>`;
        app.querySelector("#reload").addEventListener("click", render);
    }

    // ---------- 启动 ----------
    async function boot() {
        applyTheme();
        if (!bridge) {
            app.innerHTML = alertBox("error", "请在 AstrBot 管理面板中打开此页面", "插件页面需要 AstrBot 4.26 或更新版本提供的 AstrBotPluginPage 接口。");
            return;
        }
        try {
            onHostContext(await bridge.ready());
            bridge.onContext?.(onHostContext);
        } catch { /* 拿不到宿主上下文时按系统深浅色 */ }
        render();
    }
    boot();
})();
