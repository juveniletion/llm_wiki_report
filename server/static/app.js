/* 制药成本分析看板 —— React 18（UMD，本地 vendor）+ htm 模板
 *
 * ⚠️ 本文件**面向最终使用者**。
 *    不出现内部实现术语：文件路径、脚本名、数据库、原始资料目录结构……
 *    用户只需要知道「数字可不可信、口径是什么」，不需要知道它怎么算出来的。
 *
 * 前端无构建步骤：htm 提供 JSX 式语法但无需编译。
 */
const { useState, useEffect, useRef } = React;
const html = htm.bind(React.createElement);

/* =============================================================================
 * 登录态
 *
 * 会话由**服务端持有**，浏览器只拿一个 HttpOnly Cookie —— JS 读不到它，
 * 所以也不存在把它存进 localStorage 的问题（上一版正是栽在
 * 「localStorage 里存了脏值 → 请求头出现非 ISO-8859-1 字符」）。
 *
 * CSRF：写操作带 `X-CSRF-Token`，值从**非 HttpOnly** 的 `lw_csrf` cookie 里读。
 * 这是 double-submit 的标准做法。
 * ========================================================================== */

function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}

/* 写操作的请求头：JSON + CSRF。
 * ⚠️ 上传（multipart）**不能**用它——`Content-Type` 要让浏览器自己带 boundary。 */
function writeHeaders(extra) {
  return Object.assign({
    "Content-Type": "application/json; charset=utf-8",
    "X-CSRF-Token": getCookie("lw_csrf"),
  }, extra || {});
}

/* 统一的 API 调用：自动带同源 cookie + CSRF，错误归一成 `{_error}`。
 *
 * ⚠️ `credentials: "same-origin"` 一句都不能少。少了它，**登录态完全不起作用**，
 *    而症状极具迷惑性：接口看着"能通"，服务端却始终认为你没登录。 */
async function api(path, { method = "GET", body } = {}) {
  const opts = { method, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers = writeHeaders();
    opts.body = JSON.stringify(body);
  }
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { return { _error: String(e) }; }
  let d = null;
  try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) return { _error: (d && d.detail) || r.statusText, _status: r.status };
  return d || {};
}

/* ---------------- 数据获取 ---------------- */
function useApi(path) {
  const [s, set] = useState({ loading: true, data: null, error: null });
  useEffect(() => {
    let alive = true;
    set({ loading: true, data: null, error: null });
    fetch(path)
      .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
      .then(d => alive && set({ loading: false, data: d, error: null }))
      .catch(e => alive && set({ loading: false, data: null, error: String(e) }));
    return () => { alive = false; };
  }, [path]);
  return s;
}

/* ---------------- 工具 ---------------- */
const fmt = (v, nd = 2) =>
  (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(nd);
const cls = v => v > 0 ? "up" : (v < 0 ? "down" : "flat");
const ARROW = v => v > 0 ? "▲" : (v < 0 ? "▼" : "—");

/* 默认隐藏 / 加载占位 */
const Loading = () => html`<div class="skel skel-card"></div>`;
const ErrorBox = ({ msg }) => html`<div class="msg err">数据加载失败：${msg}</div>`;

/* ---------------- 滚动渐入 ----------------
 *
 * ⚠️ **可见性绝不能依赖这段 JS。**
 *    这的写法是：元素在 HTML 里**本来就是可见的**（`.reveal{opacity:1}`），
 *    这段代码只是**额外**给它加上 `.js-reveal` 这个类，动画才开始生效。
 *    所以 JS 没跑、浏览器不支持 IO、或脚本报错 —— 内容照样看得见，
 *    只是没有淡入效果。**反过来写（默认隐藏、JS 负责显示）就会开天窗。**
 */
function useReveal() {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    el.classList.add("js-reveal");
    const io = new IntersectionObserver((es) => {
      es.forEach(e => {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.04 });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return ref;
}

/* 包一层：滚动到视口时淡入 */
function Reveal({ children, className = "" }) {
  const ref = useReveal();
  return html`<div ref=${ref} class=${"reveal " + className}>${children}</div>`;
}

/* =============================================================================
 * 注册 / 登录
 *
 * 为什么是**就地一张卡片**而不是整页跳转：
 *   首页的图表、图谱、数据说明都是**公开数据**，一上来就拦到登录页
 *   会把这些全挡掉——看不到东西的人不会想注册。
 *   所以未登录照常展示，只在**需要个人身份的地方**（问答/上传/导出/整改）
 *   就地展开这张卡片。
 * ========================================================================== */
function AuthPanel({ onDone, hint = "" }) {
  const [mode, setMode] = useState("login");     // login | register
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [p2, setP2] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    if (!u.trim() || !p) { setErr("请填写用户名和密码"); return; }
    if (mode === "register") {
      if (p.length < 8) { setErr("密码至少 8 位"); return; }
      if (p !== p2) { setErr("两次输入的密码不一致"); return; }
    }
    setBusy(true);
    const r = await api("/api/auth/" + (mode === "register" ? "register" : "login"),
                        { method: "POST", body: { username: u.trim(), password: p } });
    setBusy(false);
    if (r._error) { setErr(r._error); return; }
    setP(""); setP2("");
    if (onDone) onDone(r);
  };

  return html`
    <div class="authcard">
      <div class="auth-tabs">
        <button class=${"auth-tab" + (mode === "login" ? " on" : "")} type="button"
                onClick=${() => { setMode("login"); setErr(""); }}>登录</button>
        <button class=${"auth-tab" + (mode === "register" ? " on" : "")} type="button"
                onClick=${() => { setMode("register"); setErr(""); }}>注册</button>
      </div>

      ${hint ? html`<p class="auth-hint">${hint}</p>` : null}

      <form class="authform" onSubmit=${submit}>
        <label>
          <span>用户名</span>
          <input value=${u} autoComplete="username" disabled=${busy}
                 placeholder="3–32 个字符，中文 / 字母 / 数字 / 下划线"
                 onInput=${e => setU(e.target.value)} />
        </label>
        <label>
          <span>密码</span>
          <input type="password" value=${p} disabled=${busy}
                 autoComplete=${mode === "register" ? "new-password" : "current-password"}
                 placeholder=${mode === "register" ? "至少 8 位" : "输入密码"}
                 onInput=${e => setP(e.target.value)} />
        </label>
        ${mode === "register" ? html`
          <label>
            <span>确认密码</span>
            <input type="password" value=${p2} disabled=${busy} autoComplete="new-password"
                   placeholder="再输一遍" onInput=${e => setP2(e.target.value)} />
          </label>` : null}

        ${err ? html`<div class="alert">⚠️ ${err}</div>` : null}

        <button class="authsubmit" type="submit" disabled=${busy}>
          ${busy ? "处理中…" : (mode === "register" ? "注册并登录" : "登录")}
        </button>
      </form>

      <p class="auth-foot">
        ${mode === "register"
          ? "注册后你的资料与对话存在独立的数据区，其他成员看不到。"
          : "没有账号？点上方「注册」。"}
      </p>
    </div>`;
}


/* 受限功能的统一提示 —— 未登录时的各处入口都用它。
 *
 * ⚠️ 为什么统一：以前每个组件各自内嵌一张登录卡，四处维护、文案还各不相同。
 *    现在登录只在登录页做，这里只负责**说明原因 + 把人送过去**。 */
function LoginGate({ what, onGoLogin }) {
  return html`
    <div class="logingate">
      <div class="lg-icon">🔒</div>
      <div class="lg-text">${what}需要登录后使用</div>
      <button class="chip" onClick=${onGoLogin}>去登录</button>
    </div>`;
}


/* =============================================================================
 * 登录页（独立整屏）
 *
 * 它是**入口**：未登录时落在这一页。但**不强制**——
 * 「以访客身份浏览」可以进主页面看图表/图谱/数据说明（那些是公司公开数据）。
 * 拦死的话，第一次来的人看不到任何东西，不会想注册。
 * ========================================================================== */
function LoginPage({ onAuthed, onGuest, notice = "" }) {
  return html`
    <div class="loginpage">
      <div class="loginbox">
        <div class="login-brand">
          <div class="mark">成</div>
          <div>
            <h1>制药成本分析</h1>
            <div class="sub">中药一厂 · 成本智能分析系统</div>
          </div>
        </div>

        ${notice ? html`<div class="alert" style=${{ marginBottom: 16 }}>${notice}</div>` : null}

        <p class="login-lead">登录后可使用智能问答、上传资料、导出报告等个人功能。</p>

        <${AuthPanel} onDone=${onAuthed} />

        <button class="linkbtn login-guest" onClick=${onGuest}>
          先随便看看 → 以访客身份浏览
        </button>
      </div>
    </div>`;
}


/* =============================================================================
 * 个人中心
 *
 * 一屏三块：账号信息 / 我的历史对话 / 我的知识库。
 * 数据走 `/api/profile` **一次取回**（避免开页三个请求，任一慢就半残）。
 * ========================================================================== */
function Profile({ me, onAuthed, onLoggedOut }) {
  /* 用 path 上挂一个自增 key 来触发重新拉取——`useApi` 的 useEffect
   * 依赖 `path`，换个字符串就会重跑，不必给它加 refresh API。 */
  const [tick, setTick] = useState(0);
  const { data, loading, error } = useApi(`/api/profile?t=${tick}`);

  /* 打开某个会话：跳回首页并选中它。
   * ⚠️ 不改「首页」组件的内部 state（那是 HeroAsk 的私有状态），
   *    而是把目标会话 id 写进 hash query，由 HeroAsk 读进来。
   *    这样两个组件不必互相持有对方的 setter。 */
  const openConv = (id) => {
    location.hash = `home?conv=${id}`;
  };

  const delConv = async (id) => {
    if (!window.confirm("删除这个对话？该对话的所有消息会一并删除，不可恢复。")) return;
    const r = await api(`/api/conversations/${id}`, { method: "DELETE" });
    if (r._error) return;
    setTick(t => t + 1);     // 重新拉 profile，刷新列表
  };

  if (!me) {
    return html`<div class="card">
      <h2>个人中心</h2>
      <p class="card-sub">请先登录。</p>
      <${AuthPanel} onDone=${onAuthed} />
    </div>`;
  }
  if (loading) return html`<${Loading} />`;
  if (error) return html`<${ErrorBox} msg=${error} />`;

  const ws = data.workspace || { raw: [], wiki: [], counts: {} };
  const convs = data.conversations || [];

  const logout = async () => {
    await api("/api/auth/logout", { method: "POST", body: {} });
    if (onLoggedOut) onLoggedOut();
  };

  return html`
    <div class="card">
      <h2>个人中心</h2>
      <p class="card-sub">
        账号信息、历史对话与个人知识库。资料与对话存在你自己的数据区，其他成员看不到。
      </p>
      <table class="tbl">
        <tbody>
          <tr><td class="strong" style=${{ width: 140 }}>用户名</td><td>${data.external_id}</td></tr>
          <tr><td class="strong">显示名</td><td>${data.name || data.external_id}</td></tr>
          <tr><td class="strong">角色</td><td>${data.role || "user"}</td></tr>
          <tr><td class="strong">个人数据区</td><td>${data.user_db}</td></tr>
        </tbody>
      </table>
      <div class="toolbar" style=${{ marginTop: 20 }}>
        <button class="chip" onClick=${logout}>退出登录</button>
        <button class="chip" onClick=${() => onLoggedOut && onLoggedOut(true)}>切换账号</button>
      </div>
    </div>

    <div class="card">
      <h2>我的历史对话
        <span class="hint">${convs.length} 个会话</span>
      </h2>
      ${data.conversations_error
        ? html`<div class="alert">⚠️ ${data.conversations_error}</div>`
        : convs.length ? html`
          <table class="tbl">
            <thead><tr><th>会话</th><th>建立时间</th></tr></thead>
            <tbody>${convs.map(c => html`<tr key=${c.id}>
              <td>${c.title || "未命名"}</td>
              <td class="nowrap">${String(c.created_at).slice(0, 19)}</td>
              <td class="nowrap">${c.n_msg || 0} 条</td>
              <td class="nowrap">
                <button class="chip" type="button"
                        onClick=${() => openConv(c.id)}
                        title="在首页打开这个对话">打开</button>
                <button class="chip" type="button"
                        onClick=${() => delConv(c.id)}
                        title="删除这个对话">删除</button>
              </td>
            </tr>`)}</tbody>
          </table>`
        : html`<div class="empty">
            <div class="big">还没有对话</div>
            <div>在首页「智能文档」里问一句，对话会出现在这里</div>
          </div>`}
    </div>

    <div class="card">
      <h2>我的知识库
        <span class="hint">资料 ${ws.counts.raw || 0} 份 · 词条 ${ws.counts.wiki || 0} 篇${
          ws.counts.inbox ? html` · 待编译 ${ws.counts.inbox} 份` : null}</span>
      </h2>
      ${data.workspace_error
        ? html`<div class="alert">⚠️ ${data.workspace_error}</div>`
        : (!((ws.inbox || []).length + ws.raw.length + ws.wiki.length)) ? html`
          <div class="empty">
            <div class="big">还没有个人资料</div>
            <div>在首页往下滑，把文件拖进来即可</div>
          </div>`
        : html`
          ${(ws.inbox || []).length ? html`
            <h3 class="ws-h">待编译 <span class="hint">刚上传、还没跑摄入</span></h3>
            <table class="tbl"><tbody>${ws.inbox.map(f => html`<tr key=${f.path}>
              <td>${f.path}</td><td class="num">${(f.size / 1024).toFixed(1)} KB</td>
            </tr>`)}</tbody></table>` : null}
          ${ws.raw.length ? html`
            <h3 class="ws-h">资料</h3>
            <table class="tbl"><tbody>${ws.raw.map(f => html`<tr key=${f.path}>
              <td>${f.path}</td><td class="num">${(f.size / 1024).toFixed(1)} KB</td>
            </tr>`)}</tbody></table>` : null}
          ${ws.wiki.length ? html`
            <h3 class="ws-h">词条</h3>
            <table class="tbl"><tbody>${ws.wiki.map(f => html`<tr key=${f.path}>
              <td>${f.path}</td><td class="num">${(f.size / 1024).toFixed(1)} KB</td>
            </tr>`)}</tbody></table>` : null}`}
    </div>`;
}

/* =============================================================================
 * ECharts 封装
 *
 * 赛题硬性要求使用 ECharts，故全部图表走 ECharts 渲染（本地 vendor，无 CDN）。
 * 主题刻意对齐本项目的暖色克制风格：去掉粗网格线、弱化坐标轴、
 * 提示框做成一枚圆角小卡片——避免 ECharts 默认的"仪表盘"观感。
 * ========================================================================== */
const CHART_FONT = '"Inter","Segoe UI","Microsoft YaHei",system-ui,sans-serif';
const C_INK = "#1C1917", C_MUTED = "#78716C", C_FAINT = "#A8A29E", C_LINE = "#E7E5E4";

/* 统一的提示框样式（白色圆角卡片 + 柔影）*/
const TOOLTIP = {
  trigger: "axis",
  backgroundColor: "#fff",
  borderColor: C_LINE,
  borderWidth: 1,
  padding: [10, 14],
  textStyle: { color: C_INK, fontSize: 13, fontFamily: CHART_FONT },
  extraCssText: "border-radius:12px;box-shadow:0 4px 20px rgba(28,25,23,.10);",
};

function EChart({ option, height = 300, deps = [], title = "" }) {
  const ref = useRef(null);
  const inst = useRef(null);
  const [hover, setHover] = useState(false);

  useEffect(() => {
    if (!ref.current || !window.echarts) return;
    inst.current = window.echarts.init(ref.current, null, { renderer: "canvas" });
    const onResize = () => inst.current && inst.current.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      if (inst.current) { inst.current.dispose(); inst.current = null; }
    };
  }, []);

  // 数据变化时只 setOption，不重建实例（避免闪烁）
  useEffect(() => {
    if (inst.current && option) inst.current.setOption(option, true);
  }, deps);

  /* ---- 导出 PNG ----
   * ECharts 用 canvas 渲染，`getDataURL()` 直接拿到图。
   * ⚠️ 必须显式传 `backgroundColor`——否则导出的 PNG 背景透明，
   *    贴进 Word/PPT 后深色文字压在深色底上会看不清。
   */
  const save = () => {
    if (!inst.current) return;
    const url = inst.current.getDataURL({
      type: "png", pixelRatio: 2, backgroundColor: "#FFFFFF",
    });
    const a = document.createElement("a");
    a.href = url;
    a.download = (title || "图表").replace(/[\\/:*?"<>|]/g, "_") + ".png";
    document.body.appendChild(a); a.click(); a.remove();
  };

  return html`
    <div class="chartwrap" onMouseEnter=${() => setHover(true)}
         onMouseLeave=${() => setHover(false)}>
      <div ref=${ref} style=${{ width: "100%", height: height + "px" }}></div>
      ${title ? html`
        <button class="chart-dl" title="导出为图片" onClick=${save}
                style=${{ opacity: hover ? 1 : 0 }}>⬇ 图片</button>` : null}
    </div>`;
}

/* ---- 折线图：逐月成本走势 ---- */
function LineChart({ series, colors, height = 320, title = "" }) {
  if (!series.length) return html`<div class="empty">该时间段暂无数据</div>`;
  const months = series.map(s => s.月份.slice(5) + "月");

  const option = {
    animationDuration: 420,
    // ⚠️ legend 放 bottom:0 会与 X 轴标签重叠（实测"单位成本"压住了"03月"）。
    //    改为 legend 置顶 + grid 留出上边距，两者各占一行。
    grid: { left: 8, right: 16, top: 42, bottom: 8, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      valueFormatter: v => (v == null ? "—" : Number(v).toFixed(2) + " 元/盒"),
    },
    legend: {
      top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, itemGap: 20,
      textStyle: { color: C_MUTED, fontSize: 13, fontFamily: CHART_FONT },
    },
    xAxis: {
      type: "category", data: months, boundaryGap: false,
      axisLine: { lineStyle: { color: C_LINE } },
      axisTick: { show: false },
      axisLabel: { color: C_FAINT, fontSize: 12, fontFamily: CHART_FONT },
    },
    yAxis: {
      type: "value", scale: true,
      splitLine: { lineStyle: { color: "#F0EFED", type: "dashed" } },
      axisLabel: { color: C_FAINT, fontSize: 12, fontFamily: CHART_FONT },
    },
    series: colors.map(c => ({
      name: c.label,
      type: "line",
      smooth: 0.28,
      symbol: "circle",
      symbolSize: 7,
      lineStyle: { width: 2.5, color: c.color },
      itemStyle: { color: "#fff", borderColor: c.color, borderWidth: 2.5 },
      emphasis: { focus: "series" },
      data: series.map(s => s[c.key]),
    })),
  };

  return html`<${EChart} option=${option} height=${height}
              deps=${[series, colors]} title=${title} />`;
}

/* ---- 柱状图：三产品单位成本对比 ---- */
function BarChart({ items, height = 300, title = "" }) {
  if (!items.length) return html`<div class="empty">该月份暂无数据</div>`;

  const option = {
    animationDuration: 420,
    grid: { left: 8, right: 16, top: 34, bottom: 8, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      trigger: "item",
      valueFormatter: v => (v == null ? "—" : Number(v).toFixed(2) + " 元/盒"),
    },
    xAxis: {
      type: "category",
      data: items.map(i => i.label),
      axisLine: { lineStyle: { color: C_LINE } },
      axisTick: { show: false },
      axisLabel: { color: C_MUTED, fontSize: 13.5, fontFamily: CHART_FONT },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#F0EFED", type: "dashed" } },
      axisLabel: { color: C_FAINT, fontSize: 12, fontFamily: CHART_FONT },
    },
    series: [{
      type: "bar",
      barMaxWidth: 78,
      itemStyle: {
        borderRadius: [10, 10, 0, 0],
        color: p => items[p.dataIndex] && items[p.dataIndex].color,
      },
      label: {
        show: true, position: "top", distance: 10,
        formatter: p => Number(p.value).toFixed(2),
        color: C_INK, fontWeight: 650, fontSize: 14, fontFamily: CHART_FONT,
      },
      data: items.map(i => i.value),
    }],
  };

  return html`<${EChart} option=${option} height=${height} deps=${[items]} title=${title} />`;
}

/* ---- 瀑布图：成本变动分解 ----
 *
 * 赛题 5.2.2 要求"展示各项要素对总变动的贡献"。
 *
 * ⚠️ 关于画法的一个实测取舍：
 *   最初画的是**绝对量级瀑布**（从上月 10.90 落到本月 11.21），
 *   但本数据的变动只有 0.03~0.22，而基数是 11 —— 浮动柱小到几乎看不见，
 *   图等于白画。
 *   改为**变动额瀑布**：从 0 起，各要素的变动额逐项累加，
 *   最后一根是"单位成本合计变动"。柱子高度即各自贡献，一目了然。
 *   代价是看不出 10.90→11.21 的绝对量级——但那个数在页面上方的
 *   指标卡与柱状图里已经有了，此处**回答的是"涨的这点是怎么来的"**。
 */
function WaterfallChart({ rows, height = 330, title = "" }) {
  if (!rows.length) return html`<div class="empty">该月份无对比数据</div>`;

  const cats = rows.map(r => r.name);
  const last = rows.length - 1;

  // 三组柱子：不可见垫高 / 上升 / 下降。
  // 「合计」那根也走 rise/fall，但高度是总变动，单独着色区分。
  const base = [], rise = [], fall = [], isTotal = [];

  rows.forEach((r, i) => {
    isTotal.push(i === last);
    if (i === 0) {                       // 起点：0，不画柱
      base.push(0); rise.push(0); fall.push(0);
      return;
    }
    const stackFrom = rows[i - 1].cum;   // 累计到上一项为止的高度
    if (r.amount >= 0) {
      base.push(stackFrom); rise.push(r.amount); fall.push(0);
    } else {
      base.push(stackFrom + r.amount);
      rise.push(0); fall.push(-r.amount);
    }
  });

  const mk = (color, sign, totalColor) => ({
    type: "bar", stack: "wf", barMaxWidth: 56,
    itemStyle: {
      color: p => (isTotal[p.dataIndex] ? totalColor : color),
      borderRadius: sign < 0 ? [0, 0, 6, 6] : [6, 6, 0, 0],
    },
    label: {
      show: true, position: sign < 0 ? "bottom" : "top", distance: 7,
      fontWeight: 640, fontSize: 12.5, fontFamily: CHART_FONT,
      color: p => (isTotal[p.dataIndex] ? C_INK : color),
      formatter: p => {
        if (!p.value) return "";
        const s = isTotal[p.dataIndex] ? "" : (sign < 0 ? "−" : "+");
        return s + Number(p.value).toFixed(2);
      },
    },
  });

  const option = {
    animationDuration: 420,
    grid: { left: 8, right: 16, top: 32, bottom: 8, containLabel: true },
    tooltip: {
      ...TOOLTIP, trigger: "axis", axisPointer: { type: "shadow" },
      formatter: ps => {
        const i = ps[0].dataIndex, r = rows[i];
        if (i === 0) return `${r.name}`;
        if (i === last)
          return `<b>${r.name}</b><br/>${r.amount >= 0 ? "+" : ""}${r.amount.toFixed(2)} 元/盒`;
        return `${r.name}<br/>${r.amount >= 0 ? "+" : ""}${r.amount.toFixed(2)} 元/盒<br/>`
             + `对总变动的贡献 <b>${r.share == null ? "—" : r.share.toFixed(1) + "%"}</b>`;
      },
    },
    xAxis: {
      type: "category", data: cats,
      axisLine: { lineStyle: { color: C_LINE } },
      axisTick: { show: false },
      axisLabel: {
        color: p => (p === cats[last] ? C_INK : C_MUTED),
        fontWeight: 560, fontSize: 12.5, fontFamily: CHART_FONT,
      },
    },
    yAxis: {
      type: "value", scale: true,
      splitLine: { lineStyle: { color: "#F0EFED", type: "dashed" } },
      axisLabel: { color: C_FAINT, fontSize: 12, fontFamily: CHART_FONT },
    },
    series: [
      { name: "base", type: "bar", stack: "wf", silent: true,
        itemStyle: { color: "transparent" }, emphasis: { disabled: true },
        data: base },
      { name: "上升", ...mk("#C2410C", 1, "#0F766E"), data: rise },
      { name: "下降", ...mk("#15803D", -1, "#0F766E"), data: fall },
    ],
  };
  return html`<${EChart} option=${option} height=${height} deps=${[rows]} title=${title} />`;
}

/* ---- 环形图：本月成本构成 ----
 * 赛题 5.2.2 要求"本月成本构成（饼图/环形图）"。
 * 用环形（不是实心饼）——中心的空白正好放单位成本。
 */
function DonutChart({ items, total, unit, height = 300, title = "" }) {
  const sum = items.reduce((a, b) => a + (b.value || 0), 0);
  if (!sum) return html`<div class="empty">该月份无结构数据</div>`;

  const option = {
    animationDuration: 420,
    tooltip: {
      ...TOOLTIP, trigger: "item",
      formatter: p => `${p.name}<br/><b>${Number(p.value).toFixed(2)}</b> 元/盒`
                     + `<br/>占 ${p.percent}%`,
    },
    legend: {
      bottom: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, itemGap: 18,
      textStyle: { color: C_MUTED, fontSize: 13, fontFamily: CHART_FONT },
    },
    series: [{
      type: "pie",
      // 半径收小一点、中心下移，给外圈标签留出空间——
      // 实测 80% 外径时左侧 "13.65%" 会被容器裁成 "13.6..."
      radius: ["52%", "72%"],
      center: ["50%", "46%"],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: "#fff", borderWidth: 3, borderRadius: 6 },
      label: {
        show: true,
        formatter: "{d}%",
        color: C_INK, fontSize: 12.5, fontWeight: 620, fontFamily: CHART_FONT,
      },
      labelLine: { length: 8, length2: 10, lineStyle: { color: C_LINE } },
      data: items.map(i => ({
        name: i.name, value: i.value,
        itemStyle: { color: i.color },
      })),
    }],
    graphic: total == null ? [] : [{
      type: "text", left: "center", top: "39%",
      style: {
        text: String(total), textAlign: "center",
        fill: C_INK, fontSize: 26, fontWeight: 680, fontFamily: CHART_FONT,
      },
    }, {
      type: "text", left: "center", top: "50%",
      style: {
        text: unit || "", textAlign: "center",
        fill: C_FAINT, fontSize: 12, fontFamily: CHART_FONT,
      },
    }],
  };
  return html`<${EChart} option=${option} height=${height} deps=${[items, total]} title=${title} />`;
}

/* =============================================================================
 * 知识图谱可视化（赛题 6.2「知识图谱增强 RAG」）
 *
 * 图谱回答的是「**经过哪些环节**」，不是「哪段文字提到了它」——
 * 所以这里画的是**连通关系**，不是把文档列表画成一朵花。
 *
 * ⚠️ 画法取舍：ECharts 的 `graph` 系列有两种布局——
 *    · force（力导向）：好看，但**每次布局都不同**，同一张图两次截屏不一样
 *    · circular / none：确定，但不好看
 *    这里用 force，但**固定了 `initLayout` 与 `repulsion`**，
 *    让同一份数据大致落在同一形状上（可复现性对演示很重要）。
 * ========================================================================== */

const GRAPH_COLORS = {
  product:   "#0F766E",   // 主色：产品是入口
  material:  "#C2410C",
  process:   "#6366F1",
  equipment: "#0891B2",
  incident:  "#B45309",
  anomaly:   "#BE123C",
  peer:      "#7C3AED",
};

function GraphChart({ data, height = 460, selected, onSelect, fitKey }) {
  const inst = useRef(null);
  const boxRef = useRef(null);

  useEffect(() => {
    if (!boxRef.current || !window.echarts || !data || !data.nodes) return;
    inst.current = window.echarts.init(boxRef.current, null, { renderer: "canvas" });

    const deg = {};
    data.edges.forEach(e => {
      deg[e.src] = (deg[e.src] || 0) + 1;
      deg[e.dst] = (deg[e.dst] || 0) + 1;
    });

    inst.current.setOption({
      animationDuration: 600,
      tooltip: {
        ...TOOLTIP, trigger: "item", confine: true,
        formatter: p => {
          if (p.dataType === "edge") return p.data.relation;
          const n = p.data.raw || {};
          const lines = [`<b>${n.label}</b>`,
                         `<span style="color:#78716C">${n.typeZh || ""}</span>`];
          if (n.attrs) {
            Object.entries(n.attrs).slice(0, 4).forEach(([k, v]) => {
              if (v) lines.push(`${k}：${v}`);
            });
          }
          return lines.join("<br/>");
        },
      },
      series: [{
        type: "graph",
        layout: "force",
        /* ---- 交互灵敏度：三处调低，都是实测过的误操作来源 ----
         *
         * ① `roam: "move"` 而不是 `true`
         *    用 `true` 时**滚轮会缩放图**。图占了整屏宽，用户往下滚页面
         *    时鼠标常停在图上 → 页面不动、图被缩放，非常恼人。
         *    改为只允许**拖拽平移**，滚轮归还给页面。
         *
         * ② `draggable: false`（原来是 true）
         *    节点可拖时，"想平移但起点落在节点上"会**拖走那个节点**，
         *    图就散了。关掉之后拖哪都是平移，行为唯一。
         *
         * ③ 点击 = **只选中**，不再重新居中（见下面的 click 处理）
         *    原来点一下节点就 onPick → 重新取数 + 重新力导向布局，
         *    落点每次不同 → 一失手就找不回原来的位置。
         *    "选中"和"重新布局"是两件事，必须分开。
         */
        roam: "move",
        draggable: false,
        // ⚠️ 不能用圆形布局：本图是**星形/枢纽形**（银黄口服液为中心，
        //    一圈原料/工序/设备），圆形会把边全挤到中心打结。
        //    力导向才对。调参目标：让外围节点摊开、别挤成一坨。
        //    friction 提高 + gravity 略增 → 收敛更快，减少"点不准"的漂移。
        force: { repulsion: 520, edgeLength: [90, 170], gravity: 0.03,
                 layoutAnimation: true, friction: 0.2 },
        // 标签只给"重要的"（度数高的）显示；全显示会糊成一片。
        // 阈值不能太高——实测 deg>=3 时只剩 3~4 个标签，等于没标。
        label: {
          show: true,
          position: "right",
          fontSize: 11.5,
          fontFamily: CHART_FONT,
          color: C_INK,
          formatter: p => (deg[p.data.id] >= 2 ? p.data.name : ""),
        },
        // 选中的节点加一圈描边（用 ECharts 内建 select，不重设 data，避免重跑布局）
        select: {
          itemStyle: { borderColor: "#0F766E", borderWidth: 3,
                       shadowBlur: 12, shadowColor: "rgba(15,118,110,.35)" },
          label: { fontWeight: 700 },
        },
        selectedMode: "single",
        lineStyle: { color: "#D6D3D1", width: 1.2, curveness: 0.06 },
        emphasis: {
          focus: "adjacency",
          label: { show: true, fontWeight: 700 },
          lineStyle: { width: 2.4, color: "#0F766E" },
        },
        data: data.nodes.map(n => ({
          id: n.id, name: n.label,
          raw: { ...n, typeZh: (data.type_zh || {})[n.type] || n.type },
          symbolSize: Math.min(42, 12 + (deg[n.id] || 0) * 3.2),
          itemStyle: { color: GRAPH_COLORS[n.type] || "#78716C" },
        })),
        edges: data.edges.map(e => ({
          source: e.src, target: e.dst, relation: e.relation,
        })),
      }],
    });

    /* 点击**只选中**，不改焦点、不重新取数。
     * 点空白处则取消选中（符合直觉，也是"反悔"的出口）。 */
    inst.current.on("click", p => {
      const id = p.dataType === "node" ? p.data.id : null;
      if (id) inst.current.dispatchAction({ type: "select", seriesIndex: 0, dataIndex: p.dataIndex });
      else inst.current.dispatchAction({ type: "unselect", seriesIndex: 0 });
      if (onSelect) onSelect(id);
    });

    const onResize = () => inst.current && inst.current.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      if (inst.current) { inst.current.dispose(); inst.current = null; }
    };
  }, [data]);

  /* "重置视图"不走这里——由父组件换 `key` 让本组件**整个重新挂载**。
   * 试过在当前实例上复位 roam 状态：ECharts 的 graph 没有干净的
   * "复位平移/缩放"公开接口，猜内部状态不可靠。重挂载最省事也最确定。 */

  if (!data || !data.nodes || !data.nodes.length)
    return html`<div class="empty">该范围内没有可展示的关系</div>`;
  return html`<div ref=${boxRef} style=${{ width: "100%", height: height + "px" }}></div>`;
}

/* 图例：告诉用户颜色代表什么 */
function GraphLegend({ typeZh }) {
  return html`
    <div class="glegend">
      ${Object.entries(GRAPH_COLORS).map(([k, c]) => html`
        <span key=${k}><i style=${{ background: c }}></i>${(typeZh || {})[k] || k}</span>`)}
    </div>`;
}

/* =============================================================================
 * 热力图：产品 × 月份 × 成本要素（赛题 5.2.2 可选加分项）
 *
 * ⚠️ 画的是**环比变动率**，不是绝对值。
 *    三个产品的单价量级差 2.5 倍（7.47 / 11.21 / 18.09），
 *    跨产品比"颜色深浅"只会得到"六味最深"这种废话。
 *    **变动率才是可比的**——同一色阶下能直接看出"谁在哪个月异动"。
 *
 * 色阶以 **0 为中心对称**：涨红跌绿、0 为中性白。
 * 不对称的话，一个 +5% 和一个 −5% 会呈现完全不同的深浅，
 * 那是**视觉上的谎**。
 * ========================================================================== */
function HeatmapChart({ data, element, height = 300, title = "" }) {
  if (!data || !data.products || !data.products.length)
    return html`<div class="empty">暂无热力图数据</div>`;

  const months = data.products[0].months || [];
  // ECharts heatmap 的 data 是 [xIndex, yIndex, value]
  const cells = [];
  data.products.forEach((p, yi) => {
    p.cells.forEach(([el, m, v]) => {
      if (el !== element) return;
      const xi = months.indexOf(m);
      if (xi >= 0) cells.push([xi, yi, v]);
    });
  });
  if (!cells.length) return html`<div class="empty">该要素暂无可比数据</div>`;

  const bound = data.bound || 1;
  const option = {
    animationDuration: 400,
    grid: { left: 8, right: 16, top: 12, bottom: 46, containLabel: true },
    tooltip: {
      ...TOOLTIP, trigger: "item",
      formatter: p => {
        const v = p.value[2];
        const sign = v > 0 ? "+" : "";
        return `${data.products[p.value[1]].product}<br/>`
             + `${months[p.value[0]]} · ${element}<br/>`
             + `<b>${sign}${v.toFixed(2)}%</b>`;
      },
    },
    // 连续色阶：绿(跌) → 白(平) → 红(涨)，以 0 为中心
    visualMap: {
      min: -bound, max: bound, calculable: true, orient: "horizontal",
      left: "center", bottom: 0,
      itemWidth: 12, itemHeight: 90,
      textStyle: { color: C_MUTED, fontSize: 12, fontFamily: CHART_FONT },
      inRange: { color: ["#15803D", "#F0FDF4", "#FFFBEB", "#FFF7ED", "#C2410C"] },
    },
    xAxis: {
      type: "category", data: months.map(m => m.slice(5) + "月"),
      splitArea: { show: true },
      axisLine: { lineStyle: { color: C_LINE } }, axisTick: { show: false },
      axisLabel: { color: C_MUTED, fontSize: 12.5, fontFamily: CHART_FONT },
    },
    yAxis: {
      type: "category", data: data.products.map(p => p.product),
      splitArea: { show: true },
      axisLine: { lineStyle: { color: C_LINE } }, axisTick: { show: false },
      axisLabel: { color: C_MUTED, fontSize: 12.5, fontFamily: CHART_FONT },
    },
    series: [{
      type: "heatmap",
      data: cells,
      label: {
        show: true, fontSize: 11, fontFamily: CHART_FONT, color: C_INK,
        formatter: p => {
          const v = p.value[2];
          return (v > 0 ? "+" : "") + v.toFixed(1);
        },
      },
      itemStyle: { borderColor: "#fff", borderWidth: 2, borderRadius: 4 },
      emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(28,25,23,.25)" } },
    }],
  };
  return html`<${EChart} option=${option} height=${height}
              deps=${[data, element]} title=${title} />`;
}

/* 决策卡片：自主决策的结果（赛题 6.2） */
function DecideCard() {
  const { data, loading, error } = useApi("/api/decide");

  if (loading) return html`<div class="skel skel-card"></div>`;
  if (error) return html`<${ErrorBox} msg=${error} />`;

  const gen = data.action === "generate_report";
  return html`
    <div class=${"decidecard " + (gen ? "gen" : "dash")}>
      <div class="dc-head">
        <span class="dc-mark">${gen ? "◆" : "◇"}</span>
        <div>
          <div class="dc-title">系统判断：${data.label}</div>
          <div class="dc-sub">
            ${gen
              ? "知识库有实质变化，已有报告可能已过时——需要重新生成。"
              : "知识库自上次分析以来没有实质变化，刷新看板即可，无需重跑分析。"}
          </div>
        </div>
      </div>
      <ul class="dc-signals">
        ${(data.reasons || []).map((s, i) => html`
          <li key=${i} class=${s.fired ? "on" : ""}>
            <span class="dc-dot"></span>
            <b>${s.name}</b>
            <span class="dc-detail">${s.detail}</span>
          </li>`)}
      </ul>
    </div>`;
}

/* =============================================================================
 * 页签 2：成本趋势
 * ========================================================================== */
function Trend() {
  const { data: prod } = useApi("/api/products");
  const [product, setProduct] = useState("银黄口服液");
  const { data, loading, error } =
    useApi(`/api/trend?product=${encodeURIComponent(product)}`);

  useEffect(() => {
    const l = prod && prod.products;
    if (l && l.length && !l.includes(product)) setProduct(l[0]);
  }, [prod]);

  const LINES = [
    { key: "单位成本",     label: "单位成本", color: "#0F766E" },
    { key: "单位材料",     label: "直接材料", color: "#0891B2" },
    { key: "单位人工",     label: "直接人工", color: "#F59E0B" },
    { key: "单位制造费用", label: "制造费用", color: "#6366F1" },
  ];

  const toolbar = html`
    <div class="toolbar">
      <label>产品</label>
      <select value=${product} onChange=${e => setProduct(e.target.value)}>
        ${((prod && prod.products) || []).map(p => html`<option key=${p} value=${p}>${p}</option>`)}
      </select>
      <span class="pill">单位：元 / 盒</span>
    </div>`;

  if (loading) return html`<div>${toolbar}<${Loading} /></div>`;
  if (error) return html`<div>${toolbar}<${ErrorBox} msg=${error} /></div>`;

  const s = data.series || [];
  return html`
    ${toolbar}
    <div class="card">
      <h2>逐月成本走势 <span class="hint">${product}</span></h2>
      <p class="card-sub">三条成本要素线之和等于单位成本线，可用来核对数据一致性。</p>
      <${LineChart} series=${s} colors=${LINES} />
    </div>

    <div class="card">
      <h2>分月明细</h2>
      <table class="tbl">
        <thead><tr>
          <th>月份</th><th style=${{ textAlign: "right" }}>产量（盒）</th>
          <th style=${{ textAlign: "right" }}>直接材料</th>
          <th style=${{ textAlign: "right" }}>直接人工</th>
          <th style=${{ textAlign: "right" }}>制造费用</th>
          <th style=${{ textAlign: "right" }}>单位成本</th>
          <th style=${{ textAlign: "right" }}>环比</th>
        </tr></thead>
        <tbody>
          ${s.map(r => {
            const dd = parseFloat(r.环比);
            return html`<tr key=${r.月份}>
              <td>${r.月份.replace("-", " 年 ")} 月</td>
              <td class="num">${r.产量 == null ? "—" : r.产量.toLocaleString()}</td>
              <td class="num">${fmt(r.单位材料)}</td>
              <td class="num">${fmt(r.单位人工)}</td>
              <td class="num">${fmt(r.单位制造费用)}</td>
              <td class="num strong">${fmt(r.单位成本)}</td>
              <td class=${"num " + cls(dd)}>${r.环比}</td>
            </tr>`;
          })}
        </tbody>
      </table>
    </div>`;
}

/* =============================================================================
 * 页签 3：数据说明（原「冲突登记」）
 *
 * 对用户讲的不是"我们代码里有 26 个 Status 块"，
 * 而是"哪些数字存在口径差异、分析时采信了哪一个、对结论影响多大"。
 * ========================================================================== */
function Notes() {
  const { data, loading, error } = useApi("/api/conflicts");
  const [open, setOpen] = useState({});

  if (loading) return html`<${Loading} />`;
  if (error) return html`<${ErrorBox} msg=${error} />`;

  const LABEL = { Disputed: "口径存疑", Outdated: "已更新", Update: "已修订" };
  const CLS = { Disputed: "disputed", Outdated: "outdated", Update: "updated" };

  /* 分级展示（服务端已去重 + 标注 severity）：
   *   A = 影响**数字可信度** → 默认展开，用户必须看到
   *   B = **资料自身前后不一致** → 默认折叠，供了解
   *
   * ⚠️ 这是这次改版的核心：原来 26 条平铺，把"GMP 摘要章节号对不上"
   *    和"金银花涨幅算不出来"混在一起，看起来像**整个系统都不可靠**。
   *    分开之后用户能看到真实比例——真正影响结论的只有个位数。
   */
  const sevA = (data.items || []).filter(x => x.severity === "A");
  const sevB = (data.items || []).filter(x => x.severity !== "A");

  const Card = ({ it }) => {
    const id = it.article + ":" + it.line;
    const isOpen = !!open[id];
    return html`
      <div class="note-item" key=${id}>
        <div class="note-hd" onClick=${() => setOpen({ ...open, [id]: !isOpen })}>
          <span class=${"badge " + (CLS[it.kind] || "outdated")}>${LABEL[it.kind] || it.kind}</span>
          <div class="grow">
            <div class="hl">${it.headline || "详见下方说明"}
              ${it.merged_count ? html`<span class="merged-tag">合并 ${it.merged_count + 1} 处</span>` : null}
            </div>
            <div class="src">出自：${prettySource(it.article)}${it.section ? " · " + it.section : ""}</div>
          </div>
          <span class=${"caret" + (isOpen ? " open" : "")}>▶</span>
        </div>
        ${isOpen ? html`
          <div class="note-bd">
            <ul>${it.body.map((b, i) => html`<li key=${i}>${inline(b)}</li>`)}</ul>
          </div>` : null}
      </div>`;
  };

  return html`
    <div class="card">
      <h2>数据说明</h2>
      <p class="card-sub">
        分析所依据的原始资料中，本系统逐条记录了 <strong>${data.count} 处</strong>需要注意的地方
        （原始登记 ${data.raw_count} 处，已合并重复）。其中
        <strong>${sevA.length} 处影响数字可信度</strong>——已说明分析时采用了哪种口径；
        其余 <strong>${sevB.length} 处</strong>是资料自身的前后不一致，
        不影响分析结论，供你了解。未列入的事项，均为已核对一致。
      </p>
    </div>

    ${/* ---- A 类：影响结论可信度，默认展开 ---- */ ""}
    ${sevA.length ? html`
      <div class="sev-head">
        <span class="sev-mark sev-a"></span>
        <div>
          <div class="sev-title">影响数字可信度</div>
          <div class="sev-sub">
            这些地方的口径不一致会改变结论，报告里已用<strong>可复现的口径</strong>并注明基准。
          </div>
        </div>
        <span class="sev-n">${sevA.length}</span>
      </div>
      ${sevA.map(it => html`<${Card} it=${it} />`)}` : null}

    ${/* ---- B 类：资料自身不一致，默认折叠 ---- */ ""}
    ${sevB.length ? html`
      <details class="sev-fold">
        <summary>
          <span class="sev-mark sev-b"></span>
          <div>
            <div class="sev-title">资料自身的不一致</div>
            <div class="sev-sub">不影响分析结论（如文档示例与实现不符、章节编号对不上）</div>
          </div>
          <span class="sev-n">${sevB.length}</span>
          <span class="caret">▾</span>
        </summary>
        <div class="sev-body">${sevB.map(it => html`<${Card} it=${it} />`)}</div>
      </details>` : null}`;
}

/* 内部路径 → 用户能懂的中文来源名 */
function prettySource(article) {
  const base = String(article).split("/").pop().replace(/\.md$/, "");
  const MAP = {
    "成本异动登记册": "成本变动分析",
    "三产品成本基线": "产品成本数据",
    "成本数据口径与结构": "成本口径说明",
    "一厂vs二厂对标分析": "两厂对比分析",
    "药材行情与行业基准": "药材行情与行业基准",
    "设备台账与维修历史": "设备与维修记录",
    "GMP质量约束与成本关联": "GMP 质量要求",
    "报告模板契约": "报告格式要求",
    "RPA接口契约": "工单系统接口",
  };
  return MAP[base] || base;
}

/* 行内标记：只渲染 `代码` 与 **粗体**（够用，不引 markdown 库） */
function inline(s) {
  const out = [];
  String(s).split(/(`[^`]+`|\*\*[^*]+\*\*)/g).forEach((p, i) => {
    if (p.startsWith("`") && p.endsWith("`") && p.length > 2) {
      out.push(html`<code key=${i} style=${{
        background: "#fff", border: "1px solid #E7E5E4", borderRadius: 6,
        padding: "1px 6px", fontSize: 13,
      }}>${p.slice(1, -1)}</code>`);
    } else if (p.startsWith("**") && p.endsWith("**") && p.length > 4) {
      out.push(html`<strong key=${i}>${p.slice(2, -2)}</strong>`);
    } else if (p) out.push(p);
  });
  return out;
}

/* =============================================================================
 * 对话里的 markdown 渲染
 *
 * 为什么需要：大模型的回答**本来就是 markdown**（标题、表格、列表、代码、粗体），
 * 但原先消息体是 `${m.content}` 直出 + CSS `white-space: pre-wrap`——
 * 于是用户看到的是**带 `|` 和 `##` 的源码**，而不是排好版的文档。
 * 我们的回答动辄带三张对齐的表格，纯文本根本没法读。
 *
 * 设计取舍
 * --------
 * ① **不引第三方 markdown 库**。本项目前端是 vendor 版、无构建步骤，
 *    引一个 md 库要么加文件、要么走 CDN（而我们刻意不依赖 CDN）。表格 +
 *    标题 + 列表 + 粗体/代码 已覆盖本场景 99%，自己写反而可控。
 * ② **安全**：输出走 `dangerouslySetInnerHTML`，所以**必须**先转义 HTML。
 *    转义放在最前，之后只由本函数插入受控标签——模型输出里的 `<script>`
 *    进不来。链接另外只允许 http/https，挡掉 `javascript:`。
 * ③ **导出**：长回答要能存成本地 .md。所以额外提供
 *    `markdownToPlain()` 与下载按钮（见 Chat 里）——**导出的仍是原始
 *    markdown 源码**，不是渲染后的 HTML（.md 文件就该是源码）。
 * ========================================================================== */

function _escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* 行内：转义后按 `代码` / **粗体** / *斜体* / [文字](链接) 处理 */
function _mdInline(s) {
  return _escHtml(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, text, url) =>
      /^https?:\/\//i.test(url)
        ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`
        : text);
}

/*
 * markdown → 受控 HTML 字符串。
 * 支持：`#`~`####` 标题、`|` 表格、`-`/`*`/数字 列表、``` 代码块、
 *       `>` 引用、`---` 分隔线、段落与单换行。
 */
function markdownToHtml(src) {
  const lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;

  const cellsOf = (line) => line.replace(/^\s*\|/, "").replace(/\|\s*$/, "")
                               .split("|").map(c => c.trim());
  const isSep = (line) => /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.includes("-");

  while (i < lines.length) {
    const line = lines[i];

    // --- 代码块 ---
    if (/^\s*```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++; // 收尾的 ```
      out.push(`<pre><code>${_escHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    // --- 表格：当前行含 |，下一行是分隔行 ---
    if (line.includes("|") && i + 1 < lines.length && isSep(lines[i + 1])) {
      const head = cellsOf(line);
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        body.push(cellsOf(lines[i++]));
      }
      const th = head.map(c => `<th>${_mdInline(c)}</th>`).join("");
      const tr = body.map(r =>
        `<tr>${r.map(c => `<td>${_mdInline(c)}</td>`).join("")}</tr>`).join("");
      out.push(`<div class="md-table-wrap"><table><thead><tr>${th}</tr></thead>` +
               `<tbody>${tr}</tbody></table></div>`);
      continue;
    }

    // --- 标题 ---
    let m = line.match(/^(#{1,4})\s+(.*)$/);
    if (m) {
      const lv = m[1].length + 2;          // # → h3（h1/h2 留给页面本身）
      out.push(`<h${lv}>${_mdInline(m[2])}</h${lv}>`);
      i++;
      continue;
    }

    // --- 分隔线 ---
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr/>"); i++; continue; }

    // --- 引用 ---
    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${buf.map(_mdInline).join("<br/>")}</blockquote>`);
      continue;
    }

    // --- 列表（无序 / 有序）---
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push(_mdInline(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "")));
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map(t => `<li>${t}</li>`).join("")}</${tag}>`);
      continue;
    }

    // --- 空行 ---
    if (!line.trim()) { i++; continue; }

    // --- 段落：连续非空、非结构行合成一段；单换行转 <br/> ---
    const buf = [];
    while (i < lines.length && lines[i].trim()
           && !/^\s*(#{1,4}\s|>|```|([-*+]|\d+\.)\s)/.test(lines[i])
           && !(lines[i].includes("|") && i + 1 < lines.length && isSep(lines[i + 1]))) {
      buf.push(lines[i]);
      i++;
    }
    out.push(`<p>${buf.map(_mdInline).join("<br/>")}</p>`);
  }
  return out.join("");
}

/* 长回答才给导出按钮：这条阈值决定"多长算长"。
   太短的回答导出成文件没意义（一句话一个 .md 很滑稽）。 */
const MD_EXPORT_MIN_CHARS = 400;

/* 触发浏览器下载：把原始 markdown 存成本地 .md */
function downloadMarkdown(content, idx) {
  const ts = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const name = `对话回答_${ts.getFullYear()}${pad(ts.getMonth() + 1)}${pad(ts.getDate())}`
             + `_${pad(ts.getHours())}${pad(ts.getMinutes())}${pad(ts.getSeconds())}`
             + (idx ? `_${idx}` : "") + ".md";
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

/* =============================================================================
 * 页签：对标分析（三步法 · 赛题模块三）
 *
 * 赛题定义：找差异 → 拆结构 → 拆原因。
 * 前两步确定性计算（秒出），第三步调 LLM（用户点按钮才触发）。
 * ========================================================================== */
function Benchmark() {
  const { data: prod } = useApi("/api/products");
  const [product, setProduct] = useState("银黄口服液");
  const [month, setMonth] = useState("2026-05");
  const [llm, setLlm] = useState(false);
  const [busy, setBusy] = useState(false);

  const q = `/api/benchmark?product=${encodeURIComponent(product)}&month=${month}&llm=${llm ? 1 : 0}`;
  const { data, loading, error } = useApi(q);

  useEffect(() => {
    const l = prod && prod.products;
    if (l && l.length && !l.includes(product)) setProduct(l[0]);
  }, [prod]);

  // llm=1 时请求会慢，显示"生成中"
  useEffect(() => { setBusy(loading && llm); }, [loading, llm]);

  const MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"];

  const toolbar = html`
    <div class="toolbar">
      <label>产品</label>
      <select value=${product} onChange=${e => setProduct(e.target.value)}>
        ${((prod && prod.products) || []).map(p => html`<option key=${p} value=${p}>${p}</option>`)}
      </select>
      <label>月份</label>
      <select value=${month} onChange=${e => setMonth(e.target.value)}>
        ${MONTHS.map(m => html`<option key=${m} value=${m}>${m.replace("-", " 年 ")} 月</option>`)}
      </select>
      <span class="pill">与中药二厂同期对比</span>
    </div>`;

  if (loading && !data) {
    return html`<div>${toolbar}<${Loading} />
      ${busy ? html`<p class="card-sub" style=${{ textAlign: "center", marginTop: 12 }}>
        正在生成归因分析…</p>` : null}</div>`;
  }
  if (error) return html`<div>${toolbar}<${ErrorBox} msg=${error} /></div>`;

  const { step1, step2, step3 } = data;

  return html`
    ${toolbar}

    ${/* ---- 三步指示器 ---- */ ""}
    <div class="card">
      <h2>对标分析三步法</h2>
      <p class="card-sub">
        对比本单位与中药二厂同产品同期的成本差异，逐层拆解到成本要素，
        再由分析模型给出归因与改进建议。
      </p>
      <div class="steps">
        ${[["1", "找差异", "对比两厂同期成本"],
           ["2", "拆结构", "按要素逐层拆解"],
           ["3", "拆原因", "归因与改进建议"]].map(([n, t, d], i) => html`
          <div class="step-item" key=${n}>
            <div class="step-num">${n}</div>
            <div class="step-txt">
              <div class="step-title">${t}</div>
              <div class="step-desc">${d}</div>
            </div>
            ${i < 2 ? html`<div class="step-arrow">→</div>` : null}
          </div>`)}
      </div>
    </div>

    ${/* ---- 第一步 ---- */ ""}
    <div class="card">
      <h2>第一步 · 找差异 <span class="hint">差异总览表</span></h2>
      <p class="card-sub">
        差异金额 = 二厂 − 一厂，<strong>正数表示二厂更高</strong>；
        差异率以本单位（一厂）为基准。
      </p>
      <table class="tbl">
        <thead><tr>
          <th>成本要素</th>
          <th style=${{ textAlign: "right" }}>一厂（元/盒）</th>
          <th style=${{ textAlign: "right" }}>二厂（元/盒）</th>
          <th style=${{ textAlign: "right" }}>差异金额</th>
          <th style=${{ textAlign: "right" }}>差异率</th>
          <th>方向</th>
        </tr></thead>
        <tbody>
          ${step1.rows.map(r => html`<tr key=${r.成本要素}>
            <td class=${r.成本要素 === "单位成本" ? "strong" : ""}>${r.成本要素}</td>
            <td class="num">${fmt(r.一厂)}</td>
            <td class="num">${fmt(r.二厂)}</td>
            <td class=${"num " + cls(r.差异金额)}>${r.差异金额 > 0 ? "+" : ""}${fmt(r.差异金额)}</td>
            <td class=${"num " + cls(r.差异率)}>${r.差异率 > 0 ? "+" : ""}${fmt(r.差异率)}%</td>
            <td>${r.方向}</td>
          </tr>`)}
        </tbody>
      </table>
    </div>

    ${/* ---- 第二步 ---- */ ""}
    ${step2.tree ? html`
      <div class="card">
        <h2>第二步 · 拆结构 <span class="hint">差异结构树</span></h2>
        <p class="card-sub">${step2.tree.根}</p>

        <div class="tree">
          ${step2.tree.要素层.map((n, i) => html`
            <div class="tree-node" key=${n.要素}>
              <span class="tree-branch">${i === step2.tree.要素层.length - 1 ? "└─" : "├─"}</span>
              <span class="tree-name">${n.要素}</span>
              <span class=${"tree-amt " + cls(n.差异金额)}>
                ${n.差异金额 > 0 ? "+" : ""}${fmt(n.差异金额)}
              </span>
              <span class="tree-pct">占差异 ${fmt(n.占差异比, 1)}%</span>
              <span class=${"badge " + (n.结论 === "主因" ? "disputed" : "outdated")}>${n.结论}</span>
              <span class="tree-bar"><i style=${{ width: Math.min(100, n.占差异比) + "%" }}></i></span>
            </div>`)}
        </div>

        ${step2.tree.下钻层.明细.length ? html`
          <details style=${{ marginTop: 18 }}>
            <summary style=${{ cursor: "pointer", fontSize: 13.5, color: "var(--muted)" }}>
              下钻：一厂材料构成（${step2.tree.下钻层.明细.length} 项）
            </summary>
            <div class="note" style=${{ marginTop: 12, borderLeftColor: "var(--warn-line)",
                                        background: "var(--warn-soft)", color: "var(--warn)",
                                        fontSize: 13 }}>
              ${step2.tree.下钻层.说明}
            </div>
            <table class="tbl" style=${{ marginTop: 12 }}>
              <thead><tr>
                <th>原材料</th>
                <th style=${{ textAlign: "right" }}>一厂单耗（元/盒）</th>
                <th style=${{ textAlign: "right" }}>占材料成本</th>
                <th style=${{ textAlign: "right" }}>环比</th>
              </tr></thead>
              <tbody>
                ${step2.tree.下钻层.明细.map(m => html`<tr key=${m.原材料名称}>
                  <td>${m.原材料名称}</td>
                  <td class="num">${m.一厂单耗}</td>
                  <td class="num">${m.占比}</td>
                  <td class="num">${m.环比}</td>
                </tr>`)}
              </tbody>
            </table>
          </details>` : null}
      </div>` : null}

    ${/* ---- 第三步 ---- */ ""}
    <div class="card">
      <h2>第三步 · 拆原因 <span class="hint">归因与改进建议</span></h2>
      ${step3.text && step3.text !== "（未调用 LLM）" ? html`
        <div class="prose">${inline(step3.text)}</div>
      ` : html`
        <p class="card-sub">
          归因由分析模型结合成本资料生成本段分析，会明确区分「有数据支撑的结论」
          与「无法判定、需补充数据」的部分。
        </p>
        <button class="ask-btn" disabled=${busy}
                onClick=${() => setLlm(true)}>
          ${busy ? "生成中…" : "生成归因分析"}
        </button>`}

      ${step3.advice && step3.advice.length ? html`
        <h2 style=${{ marginTop: 26 }}>改进建议
          <span class="hint">可转化为整改任务下发</span>
        </h2>
        <table class="tbl">
          <thead><tr>
            <th>序号</th><th>建议事项</th><th>责任部门</th>
            <th>优先级</th><th>预期效果</th><th>建议完成时间</th>
          </tr></thead>
          <tbody>
            ${step3.advice.map((a, i) => html`<tr key=${i}>
              <td>${i + 1}</td>
              <td>${a.建议事项}</td>
              <td>${a.责任部门}</td>
              <td><span class=${"badge " + (a.优先级 === "高" ? "disputed" : "outdated")}>${a.优先级}</span></td>
              <td>${a.预期效果}</td>
              <td>${a.建议完成时间}</td>
            </tr>`)}
          </tbody>
        </table>` : null}
    </div>`;
}

/* =============================================================================
 * 页签 4：数据更新记录（原「摄入时间线」）
 * ========================================================================== */
function History() {
  const { data, loading, error } = useApi("/api/timeline");
  if (loading) return html`<${Loading} />`;
  if (error) return html`<${ErrorBox} msg=${error} />`;

  // 内部操作类型 → 用户语言
  const KIND = {
    ingest: "纳入新资料", skill: "规则调整", lint: "数据校核",
    verify: "效果验证", fix: "问题修复", feat: "功能上线",
    test: "测试", deploy: "系统部署",
  };

  // 只展示与"数据/分析能力"相关的记录，过滤纯开发事件
  const SHOW = new Set(["ingest", "lint", "fix", "skill", "feat", "deploy"]);
  const items = data.items.filter(e => SHOW.has(e.kind)).reverse();

  return html`
    <div class="card">
      <h2>数据更新记录</h2>
      <p class="card-sub">
        本系统持续纳入新的原始资料（成本表、行情、法规、设备清单等），
        每次纳入都会重新核对既有结论。以下是完整记录——
        <strong>包括被后续数据修正过的结论</strong>，不做隐藏。
      </p>
    </div>

    <div class="card">
      <div class="timeline">
        ${items.map((e, i) => html`
          <div class=${"tl-item t-" + e.kind} key=${i}>
            <span class="tl-date">${e.date}</span>
            <span class="tl-kind">${KIND[e.kind] || e.kind}</span>
            <div class="tl-title">${cleanTitle(e.title)}</div>
            ${e.excerpt ? html`<div class="tl-desc">${e.excerpt}</div>` : null}
          </div>`)}
      </div>
    </div>`;
}

/* 标题里去掉内部术语 */
function cleanTitle(t) {
  return String(t)
    .replace(/（骨架 [A-G]'?）/g, "")
    .replace(/^no material:\s*/i, "未纳入（无新信息）：")
    .replace(/raw\/[\w/一-龥.\-（）]+/g, "某原始资料")
    .trim();
}

/* =============================================================================
 * 页签：整改闭环（赛题模块四）
 *
 * 报告第六章产出「整改任务清单」→ 派发到下游工单系统 → 跟踪状态。
 * 这是 human-in-the-loop 的落点：**派发前必须人工确认**。
 * ========================================================================== */
function Rectify({ me, onNeedLogin, onAuthed }) {
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [data, setData] = useState(null);
  const [reports, setReports] = useState([]);
  const [pick, setPick] = useState("");
  const [last, setLast] = useState(null);      // 上次派发结果
  /* 展开的任务详情：{task_id: {loading|task|error}}。
   * 按需拉取——列表给的是"当前状态"，详情才有**状态流转时间线**，
   * 而一次拉全部任务的详情是浪费（列表 N 条 = N 个请求）。 */
  const [detail, setDetail] = useState({});

  const load = () => {
    if (!me) return;
    api("/api/rpa/tasks").then(d => {
      if (d._error) { setErr(d._error); setData(null); return; }
      setData(d); setErr("");
    });
    api("/api/control/exports").then(d => {
      const rs = (d && d.reports) || [];
      setReports(rs);
      if (!pick && rs.length) setPick(rs[0].name);
    });
  };
  useEffect(load, [me]);

  const toggleDetail = (tid) => {
    setDetail(prev => {
      // 已展开 → 收起
      if (prev[tid]) {
        const c = { ...prev }; delete c[tid]; return c;
      }
      // 未展开 → 先占位再拉
      return { ...prev, [tid]: { loading: true } };
    });
    if (detail[tid]) return;                 // 上面那个分支是收起，不用拉
    api("/api/rpa/task/" + encodeURIComponent(tid)).then(d => {
      setDetail(prev => ({
        ...prev,
        [tid]: d._error ? { error: d._error }
                        : (d.ok ? { task: d.task } : { error: d.error || "查询失败" }),
      }));
    });
  };

  const dispatch = async () => {
    if (!pick) return;
    const rep = reports.find(x => x.name === pick);
    setBusy(true); setLast(null); setErr("");
    const m = (rep && rep.stem.match(/\d{4}-\d{2}$/)) || ["2026-05"];
    const r = await api("/api/rpa/dispatch", {
      method: "POST",
      body: { report: pick, month: m[0],
              product: rep ? rep.stem.replace(/_\d{4}-\d{2}$/, "") : "" },
    });
    setBusy(false);
    if (r._error) { setErr("派发失败：" + r._error); return; }
    setLast(r); load();
  };

  if (!me) return html`
    <div class="card">
      <h2>整改闭环</h2>
      <p class="card-sub">把分析结论转为整改任务并下发到责任人，跟踪执行状态。</p>
      <${LoginGate} what="下发与追踪整改任务" onGoLogin=${onNeedLogin} />
    </div>`;

  const c = (data && data.counts) || { generated: 0, delivered: 0, confirmed: 0 };
  const zh = (data && data.status_zh) || {};

  return html`
    <div class="card">
      <h2>整改闭环
        <span class="hint">${data && data.ok ? "下游工单系统已连接" : "下游工单系统未连接"}</span>
      </h2>
      <p class="card-sub">
        报告第六章的「整改任务清单」可直接下发到责任人，并跟踪确认情况。
        下发前需人工确认——这是分析结论进入执行环节的最后一道闸门。
      </p>

      ${err ? html`<div class="alert" style=${{ marginBottom: 14 }}>⚠️ ${err}</div>` : null}

      <div class="toolbar" style=${{ marginBottom: 8 }}>
        <label>选择报告</label>
        <select value=${pick} onChange=${e => setPick(e.target.value)}>
          ${reports.map(r => html`<option key=${r.name} value=${r.name}>${r.stem.replace("_", " · ")}</option>`)}
        </select>
        <button class="ask-btn" disabled=${busy || !pick} onClick=${dispatch}>
          ${busy ? "下发中…" : "下发整改任务"}
        </button>
      </div>
      ${!reports.length ? html`<div class="note">暂无可下发的报告。</div>` : null}

      ${last ? html`
        <div class=${"alert " + (last.failed || last.degraded ? "warn" : "")}
             style=${{ marginTop: 16, background: last.failed || last.degraded ? "var(--warn-soft)" : "var(--down-soft)",
                       color: last.failed || last.degraded ? "var(--warn)" : "var(--down)",
                       borderColor: last.failed || last.degraded ? "var(--warn-line)" : "#BBF7D0" }}>
          ${last.summary}
        </div>
        <table class="tbl" style=${{ marginTop: 12 }}>
          <thead><tr><th>任务编号</th><th>结果</th></tr></thead>
          <tbody>${last.results.map(x => html`<tr key=${x.task_id}>
            <td>${x.task_id}</td>
            <td>${x.ok ? html`<span class="down">✓ ${x.notify || "已下发"}</span>`
                       : html`<span class="up">✗ ${x.error || "失败"}</span>`}</td>
          </tr>`)}</tbody>
        </table>` : null}
    </div>

    <div class="card">
      <h2>任务追踪
        <button class="linkbtn" style=${{ marginLeft: "auto" }} onClick=${load}>刷新</button>
      </h2>
      <div class="grid3" style=${{ marginBottom: 18 }}>
        ${[["已生成", c.generated, "var(--ink)"],
           ["已送达", c.delivered, "var(--info)"],
           ["已确认", c.confirmed, "var(--down)"]].map(([lab, v, col]) => html`
          <div class="kpi" key=${lab} style=${{ padding: "18px 20px" }}>
            <div class="kpi-name">${lab}</div>
            <div class="kpi-value" style=${{ color: col, fontSize: 32 }}>${v}</div>
          </div>`)}
      </div>

      ${data && data.tasks && data.tasks.length ? html`
        <table class="tbl">
          <thead><tr>
            <th>任务编号</th><th>任务标题</th><th>责任人</th>
            <th>优先级</th><th>状态</th><th>截止</th>
          </tr></thead>
          <tbody>${data.tasks.map(t => {
            const open = !!detail[t.task_id];
            const d = detail[t.task_id];
            return html`<${React.Fragment} key=${t.task_id}>
              <tr class="clickable" onClick=${() => toggleDetail(t.task_id)}>
                <td class="nowrap"><span class=${"caret-inline" + (open ? " open" : "")}>▶</span>${t.task_id}</td>
                <td>${String(t.task_title || "").slice(0, 40)}${String(t.task_title || "").length > 40 ? "…" : ""}</td>
                <td>${(t.assignee && t.assignee.name) || "—"}</td>
                <td class="nowrap"><span class=${"badge " + (t.priority === "high" ? "disputed" : "outdated")}>${t.priority}</span></td>
                <td class="nowrap">${zh[t.status] || t.status}</td>
                <td class="nowrap">${t.deadline || "—"}</td>
              </tr>
              ${open ? html`<tr><td colspan="6" class="detailcell">
                ${d && d.loading ? html`<span class="muted">载入中…</span>`
                  : d && d.error ? html`<span class="up">⚠️ ${d.error}</span>`
                  : html`
                    ${d && d.task && d.task.progress ? html`
                      <div class="tl-progress">进展：${d.task.progress}</div>` : null}
                    <div class="tl-title">状态流转</div>
                    <ol class="tl">
                      ${((d && d.task && d.task.status_history) || []).map((h, j) => html`
                        <li key=${j}>
                          <span class="tl-dot"></span>
                          <b>${zh[h.status] || h.status}</b>
                          <span class="tl-time">${String(h.time || "").replace("T", " ").slice(0, 19)}</span>
                        </li>`)}
                      ${!(d && d.task && (d.task.status_history || []).length)
                        ? html`<li class="muted">暂无流转记录</li>` : null}
                    </ol>`}
              </td></tr>` : null}
            </${React.Fragment}>`;
          })}</tbody>
        </table>` : html`
        <div class="empty">
          <div class="big">还没有下发的任务</div>
          <div>选一份报告，点「下发整改任务」</div>
        </div>`}
    </div>`;
}

/* =============================================================================
 * 首页第 ① 段：智能文档（对话）
 *
 * 为什么它不是独立页签：**问答是这个系统的入口动作**——
 * 用户想知道什么，第一反应是问，而不是先选页签再找图表。
 * 独立页签等于把入口藏在导航里。
 *
 * ⚠️ 未登录时**直接给登录卡片**，不显示输入框。
 *    这里和「上传区」的处理不同：上传区是滑到才看见的次要功能，
 *    而问答区在首屏——摆一个点了没反应的输入框比给张登录卡更让人困惑。
 * ========================================================================== */
function HeroAsk({ me, onNeedLogin, onAuthed }) {
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [meta, setMeta] = useState(null);
  const [showTrace, setShowTrace] = useState({});
  const boxRef = useRef(null);
  /* 多会话：当前会话 id 与列表。
   * ⚠️ `convId === null` 表示**尚未落库的新对话**——用户点了"新对话"但还没提问。
   *    这时不建会话（否则点开看看又关掉会攒一堆空的），
   *    首次提问时带 `new: true` 让后端建。 */
  const [convId, setConvId] = useState(null);
  const [convs, setConvs] = useState([]);

  /* 从 URL 里取「要打开哪个会话」——个人中心点「打开」时写进来的。
   * 读后即清，避免刷新页面时又跳一次。 */
  const takePendingConv = () => {
    const m = (location.hash || "").match(/[?&]conv=(\d+)/);
    if (!m) return null;
    history.replaceState(null, "", location.pathname + "#home");
    return Number(m[1]);
  };

  /* 登录态变化时拉自己的历史。
   * ⚠️ `me` 由 Home 统一持有，本组件**不自己去读凭证**——
   *    上一版三个组件各自读 localStorage，结果是"三个真相源"：
   *    在对话区登录了，上传区还不知道。 */
  useEffect(() => {
    if (!me) { setMsgs([]); setMeta(null); setConvs([]); setConvId(null); return; }
    let alive = true;

    const apply = (d) => {
      setMeta({ summary: d.summary, compactions: d.compactions });
      setMsgs((d.messages || [])
        .filter(m => m.role === "user" || m.role === "assistant")
        .map(m => ({ role: m.role === "user" ? "me" : "ai",
                     content: m.content, steps: [] })));
    };

    // 个人中心指定了要打开某个会话 → 打开它；否则续最近一个
    const wanted = takePendingConv();
    const p = wanted != null
      ? api(`/api/conversations/${wanted}`)
      : api("/api/chat/history");

    p.then(d => {
      if (!alive || d._error) {
        // 指定的会话打不开（被删了？）→ 退回"续最近"
        if (wanted != null) {
          api("/api/chat/history").then(h => {
            if (!alive || h._error) return;
            apply(h); setConvId(h.conversation_id || null);
            setConvs(h.conversations || []);
          });
        }
        return;
      }
      apply(d);
      setConvId(wanted != null ? wanted : (d.conversation_id || null));
      // 列表仍从统一的端点取（指定会话时响应里没有列表）
      api("/api/conversations").then(c => {
        if (alive && !c._error) setConvs(c.conversations || []);
      });
    });
    return () => { alive = false; };
  }, [me]);

  useEffect(() => {
    const b = boxRef.current;
    if (b) b.scrollTop = b.scrollHeight;
  }, [msgs]);

  /* 切到另一个会话：整体替换消息与摘要。
   * ⚠️ 必须**整体替换**而不是追加——追加会把上一个会话的内容混进来，
   *    那正是"上下文不隔离"的表现。同时复位 steps/typing 等瞬态。 */
  const switchConv = async (id) => {
    if (busy || id === convId) return;
    const d = await api(`/api/conversations/${id}`);
    if (d._error) return;
    setConvId(id);
    setMeta({ summary: d.summary, compactions: d.compactions });
    setShowTrace({});
    setMsgs((d.messages || [])
      .filter(m => m.role === "user" || m.role === "assistant")
      .map(m => ({ role: m.role === "user" ? "me" : "ai",
                   content: m.content, steps: [] })));
  };

  /* 开一个新对话：只清空界面，**不落后端**（见 convId 的说明）。 */
  const newConv = () => {
    if (busy) return;
    setConvId(null);
    setMsgs([]);
    setMeta(null);
    setShowTrace({});
    setInput("");
  };

  const deleteConv = async (id, e) => {
    e.stopPropagation();
    if (busy) return;
    if (!window.confirm("删除这个对话？该对话的所有消息会一并删除，不可恢复。")) return;
    const r = await api(`/api/conversations/${id}`, { method: "DELETE" });
    if (r._error) return;
    const rest = convs.filter(c => c.id !== id);
    setConvs(rest);
    if (id === convId) newConv();     // 删的是当前会话 → 复位成"新对话"
  };

  const ask = async (q) => {
    q = (q || "").trim();
    if (!q || busy) return;
    if (!me) { onNeedLogin && onNeedLogin(); return; }
    setInput(""); setBusy(true);
    setMsgs(m => [...m, { role: "me", content: q, steps: [] },
                        { role: "ai", content: "", steps: [], err: "", compact: null }]);

    const patch = fn => setMsgs(m => {
      const c = m.slice(), i = c.length - 1;
      c[i] = Object.assign({}, c[i]); fn(c[i]); return c;
    });

    try {
      /* SSE 流式：不能用 api()（那个会把响应读成 JSON）。 */
      const res = await fetch("/api/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: writeHeaders(),
        /* 带上当前会话：
         *   convId 有值 → 在这个会话里继续
         *   convId 为 null（刚点过"新对话"）→ new:true，让后端建一个
         * ⚠️ 仍然**不带 user 字段**：身份只能由服务端从凭证判定。 */
        body: JSON.stringify(convId != null
          ? { question: q, conversation_id: convId }
          : { question: q, new: true }),
      });
      if (res.status === 401) {
        patch(t => { t.err = "登录已过期，请重新登录。"; });
        onNeedLogin && onNeedLogin(); setBusy(false); return;
      }
      const rd = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const chunks = buf.split("\n\n"); buf = chunks.pop();
        for (const line of chunks) {
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6);
          if (raw === "[DONE]") continue;
          let ev; try { ev = JSON.parse(raw); } catch { continue; }
          if (ev.type === "tool_call") patch(t => t.steps.push({ state: "run" }));
          else if (ev.type === "tool_result")
            patch(t => {
              const last = [...t.steps].reverse().find(x => x.state === "run");
              if (last) last.state = ev.ok ? "ok" : "bad";
            });
          else if (ev.type === "token") patch(t => { t.content += ev.text; });
          else if (ev.type === "compact") patch(t => { t.compact = ev; });
          else if (ev.type === "error") patch(t => { t.err = ev.message; });
          else if (ev.type === "done") {
            patch(t => {
              if (ev.answer && !t.content) t.content = ev.answer;
              if (ev.error) t.err = ev.error;
              /* ⚠️ 必须把**残留的 "run" 步骤收尾**。
               *    steps 只在 `tool_result` 到达时才由 "run" → "ok"。
               *    若最后一步工具调用没等到结果（中断 / 报错 / 迭代上限），
               *    那个 "run" 会永远留着 → `running` 恒为真 →
               *    **回答早已结束，却一直按"正在查阅"的纯文本样式显示**：
               *    用户看到的是一堆 `##`、`|`、`**` 原文，而不是排好的 markdown。
               *    收到 done 就说明本轮结束了，无论步骤是否都拿到结果。 */
              t.steps.forEach(s => { if (s.state === "run") s.state = "ok"; });
            });
            if (ev.guard) setMeta(x => ({ ...(x || {}), lastGuard: ev.guard }));
          }
        }
      }
    } catch (e) { patch(t => { t.err = String(e); }); }
    /* 兜底：万一 `done` 事件没送到（连接中途断掉），这里也要把残留的
     * "run" 收尾——否则那条消息会**永远**显示成"正在查阅"，正文永远
     * 停在纯文本样式。宁可显示成已结束，也不要卡在假的"进行中"。 */
    patch(t => t.steps.forEach(s => { if (s.state === "run") s.state = "bad"; }));
    setBusy(false);
    /* 本轮结束后刷新会话列表：
     * "新对话"的首轮提问会让后端**新建**一个会话（并对它起标题），
     * 这里把新 id 与列表同步过来——否则下拉里看不到刚建的会话，
     * 下次提问又会带 new:true 再造一个。 */
    api("/api/conversations").then(d => {
      if (d._error) return;
      setConvs(d.conversations || []);
      if (convId == null && d.conversation_id != null) setConvId(d.conversation_id);
    });
  };

  const SUGGEST = [
    "5 月银黄口服液的成本为什么上涨？",
    "一厂和二厂的成本差在哪里？",
    "金银花涨幅官方说 12%，这个数能复现吗？",
  ];

  if (!me) {
    return html`
      <section class="hero">
        <h1 class="hero-title">制药成本分析</h1>
        <p class="hero-sub">问一句，每个数字都带你回到它的出处。</p>
        <${LoginGate} what="智能问答（查的是你自己的数据区）"
                      onGoLogin=${onNeedLogin} />
      </section>`;
  }

  return html`
    <section class="hero">
      <h1 class="hero-title">制药成本分析</h1>
      <p class="hero-sub">问一句，每个数字都带你回到它的出处。</p>

      <div class="conv-bar">
        <span class="conv-label">会话</span>
        <select class="conv-select" value=${convId == null ? "" : String(convId)}
                disabled=${busy}
                onChange=${e => { const v = e.target.value; if (v) switchConv(Number(v)); }}>
          <option value="">新对话（尚未提问）</option>
          ${convs.map(c => html`<option key=${c.id} value=${String(c.id)}>
            ${(c.title || "未命名")}${c.n_msg ? `（${c.n_msg}）` : ""}
          </option>`)}
        </select>
        <button class="conv-new" type="button" disabled=${busy} onClick=${newConv}
                title="开一个新对话。不同对话的上下文互相隔离。">+ 新对话</button>
        ${convId != null ? html`
          <button class="conv-del" type="button" disabled=${busy}
                  title="删除当前会话"
                  onClick=${e => deleteConv(convId, e)}>删除</button>` : null}
      </div>

      <form class="askbar" onSubmit=${e => { e.preventDefault(); ask(input); }}>
        <span class="askbar-icon">💬</span>
        <input value=${input} disabled=${busy}
               placeholder="问一个成本问题，例如「5 月银黄为什么涨？」"
               onInput=${e => setInput(e.target.value)} />
        <button type="submit" disabled=${busy || !input.trim()}>
          ${busy ? "…" : "提问"}
        </button>
      </form>

      ${!msgs.length ? html`
        <div class="suggest">
          ${SUGGEST.map((s, i) => html`
            <button key=${i} class="chip" onClick=${() => ask(s)}>${s}</button>`)}
        </div>` : null}

      ${msgs.length ? html`
        <div class="chat" ref=${boxRef}>
          ${meta && meta.summary ? html`
            <details class="chat-summary">
              <summary>此前对话的摘要（点击展开）</summary>
              <pre>${meta.summary}</pre>
            </details>` : null}

          ${msgs.map((m, i) => {
            if (m.role === "me") return html`
              <div class="bubble me" key=${i}><div class="txt">${m.content}</div></div>`;
            const running = m.steps.some(s => s.state === "run");
            // 长回答才给导出。阈值见 MD_EXPORT_MIN_CHARS 的说明。
            const canExport = !running && (m.content || "").length >= MD_EXPORT_MIN_CHARS;
            return html`
              <div class="bubble ai" key=${i}>
                ${m.steps.length ? html`
                  <div class="trace">
                    <button class="trace-toggle"
                            onClick=${() => setShowTrace(x => ({ ...x, [i]: !x[i] }))}>
                      <span>${showTrace[i] ? "▾" : "▸"}</span>
                      ${running ? "正在查阅资料…" : `已查阅 ${m.steps.length} 处资料`}
                    </button>
                    ${showTrace[i] ? html`
                      <div class="trace-list">
                        ${m.steps.map((s, j) => html`
                          <div class="trace-row" key=${j}>
                            <span class=${"dot " + s.state}></span>
                            ${s.state === "run" ? "查阅中…" : "已查阅"}
                          </div>`)}
                      </div>` : null}
                  </div>` : null}
                ${m.compact ? html`<div class="compact-note">
                  为保持对话流畅，此前部分内容已归纳为摘要</div>` : null}
                ${running
                  ? html`<div class="txt typing">
                      ${m.content || "正在查阅资料，请稍候…"}</div>`
                  : html`<div class="txt md"
                           dangerouslySetInnerHTML=${{ __html: markdownToHtml(m.content) }} />`}
                ${canExport ? html`
                  <div class="md-actions">
                    <button class="md-export" title="把这条回答按原始 Markdown 存成本地 .md 文件"
                            onClick=${() => downloadMarkdown(m.content, i)}>
                      导出 .md
                    </button>
                  </div>` : null}
                ${m.err ? html`<div class="alert" style=${{ marginTop: 10 }}>${m.err}</div>` : null}
              </div>`;
          })}
        </div>` : null}
    </section>`;
}


/* =============================================================================
 * 首页第 ② 段：把资料加进来（拖拽上传）
 *
 * ⚠️ 上传的资料只进**你自己的个人工作区**，不影响其他人和共享基线。
 *    这一点必须写清楚——否则用户会以为自己在改全公司的库。
 * ========================================================================== */
function UploadZone({ me, onNeedLogin, onChanged }) {
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [logs, setLogs] = useState([]);
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const dirRef = useRef(null);
  const fileRef = useRef(null);
  const logRef = useRef(null);

  useEffect(() => {
    const b = logRef.current;
    if (b) b.scrollTop = b.scrollHeight;
  }, [logs]);

  const upload = async (fileList) => {
    const files = Array.from(fileList || []).filter(f => f.size > 0);
    if (!files.length) return;
    if (!me) { onNeedLogin && onNeedLogin(); return; }
    setBusy(true); setNote(""); setErr("");
    try {
      const fd = new FormData();
      files.forEach(f => fd.append("files", f, f.name));
      fd.append("paths", JSON.stringify(files.map(f => f.webkitRelativePath || "")));
      /* ⚠️ multipart 请求**不能**用 writeHeaders()：
       *    `Content-Type` 必须让浏览器自己带 boundary，写死了服务端解析不了。
       *    只加 CSRF 头。 */
      const r = await fetch("/api/control/upload", {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": getCookie("lw_csrf") }, body: fd,
      });
      if (r.status === 401 || r.status === 403)
        throw new Error("登录已过期，请重新登录");
      const d = await r.json();
      const okN = (d.saved || []).length, badN = (d.rejected || []).length;
      setNote(`已接收 ${okN} 个文件` + (badN ? `，拒绝 ${badN} 个` : "") +
        (badN ? `：${d.rejected.map(x => x.file + "（" + x.reason + "）").join("；")}` : ""));
      onChanged && onChanged();
      if (okN) setTimeout(() => runIngest(), 300);   // 上传完自动开编译
    } catch (e) { setErr(String(e.message || e)); }
    setBusy(false);
  };

  /* 摄入（SSE）：LLM 编译通常 30–120 秒，故流式推进度 */
  const runIngest = async () => {
    setBusy(true); setLogs([{ k: "start", t: "开始处理你上传的资料…" }]);
    try {
      const res = await fetch("/api/control/ingest", {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": getCookie("lw_csrf") },
      });
      if (res.status === 401 || res.status === 403)
        throw new Error("登录已过期，请重新登录");
      const rd = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n"); buf = parts.pop();
        for (const line of parts) {
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6);
          if (raw === "[DONE]") continue;
          let ev; try { ev = JSON.parse(raw); } catch { continue; }
          if (ev.type === "log") setLogs(L => [...L, { k: "log", t: ev.line }]);
          else if (ev.type === "done")
            setLogs(L => [...L, { k: ev.ok ? "ok" : "bad", t: ev.message }]);
          else if (ev.type === "error")
            setLogs(L => [...L, { k: "bad", t: ev.message }]);
        }
      }
      onChanged && onChanged();
    } catch (e) {
      setLogs(L => [...L, { k: "bad", t: String(e.message || e) }]);
    }
    setBusy(false);
  };

  return html`
    <section class="home-sec">
      <h2 class="sec-title">让知识库更懂你的厂
        <span class="sec-hint">仅你可见</span>
      </h2>
      <p class="sec-sub">
        拖入你手头的资料（报表、工艺说明、检查记录…），会被编译进
        <strong>你自己的</strong>知识库，检索与问答都能用到。
        不影响其他成员，也不改动公司共享基线。
      </p>

      <div class=${"drop" + (drag ? " on" : "")}
           onDragOver=${e => { e.preventDefault(); setDrag(true); }}
           onDragLeave=${() => setDrag(false)}
           onDrop=${e => { e.preventDefault(); setDrag(false); upload(e.dataTransfer.files); }}>
        <div class="drop-icon">⬆</div>
        <div class="drop-title">把文件拖到这里</div>
        <div class="drop-sub">
          支持 .md .txt .csv .xlsx .docx .pdf .json ｜ 上传后自动编译
        </div>
        <div class="drop-btns">
          <button type="button" class="chip" disabled=${busy}
                  onClick=${() => fileRef.current && fileRef.current.click()}>选择文件</button>
          <button type="button" class="chip" disabled=${busy}
                  onClick=${() => dirRef.current && dirRef.current.click()}>选择文件夹</button>
        </div>
        <input ref=${fileRef} type="file" multiple hidden
               accept=".md,.txt,.csv,.xlsx,.xls,.docx,.pdf,.json"
               onChange=${e => { upload(e.target.files); e.target.value = ""; }} />
        <input ref=${dirRef} type="file" webkitdirectory multiple hidden
               onChange=${e => { upload(e.target.files); e.target.value = ""; }} />
      </div>

      ${note ? html`<div class="note" style=${{ marginTop: 12 }}>${note}</div>` : null}
      ${err ? html`<div class="alert" style=${{ marginTop: 12 }}>⚠️ ${err}</div>` : null}
      ${busy ? html`<div class="note" style=${{ marginTop: 12 }}>
        处理中…（编译通常 30–120 秒）</div>` : null}

      ${logs.length ? html`
        <div class="ingest-log" ref=${logRef}>
          ${logs.map((l, i) => html`
            <div class=${"ing-line " + l.k} key=${i}>
              <span class="ing-dot"></span>${l.t}
            </div>`)}
        </div>` : null}
    </section>`;
}


/* 我的工作区（清单 + 删除）—— 折叠在次要位置 */
function WorkspaceList({ me, refreshKey }) {
  const [ws, setWs] = useState(null);
  const [err, setErr] = useState("");

  const load = () => {
    if (!me) { setWs(null); return; }
    api("/api/control/workspace").then(d => {
      if (d._error) { setErr(d._error); setWs(null); return; }
      setWs(d); setErr("");
    });
  };
  useEffect(load, [me, refreshKey]);

  const del = async (path) => {
    if (!confirm(`确定删除「${path}」？只影响你的个人工作区。`)) return;
    await fetch("/api/control/workspace?path=" + encodeURIComponent(path),
                { method: "DELETE", credentials: "same-origin",
                  headers: { "X-CSRF-Token": getCookie("lw_csrf") } });
    load();
  };

  // ⚠️ 未登录不能只是 `return null`——那会**什么都不显示**，
  //    用户滑到这里看到空白，不知道是坏了还是该做什么。
  if (!me) return html`<${LoginGate} what="个人工作区"
                               onGoLogin=${() => (location.hash = "login")} />`;
  const nRaw = ws ? ws.counts.raw : 0, nWiki = ws ? ws.counts.wiki : 0;

  return html`
    <details class="wsbox" open=${nRaw + nWiki > 0}>
      <summary>
        我的工作区
        <span class="hint">资料 ${nRaw} 份 · 词条 ${nWiki} 篇</span>
      </summary>
      ${err ? html`<div class="alert" style=${{ marginTop: 10 }}>⚠️ ${err}</div>` : null}
      ${(!ws || (nRaw === 0 && nWiki === 0)) ? html`
        <div class="empty" style=${{ padding: "18px 0" }}>
          <div>还没有个人资料。上传后这里会列出你的资料与编译出的词条。</div>
        </div>` : html`
        ${nRaw ? html`
          <h3 class="ws-h">资料</h3>
          <table class="tbl">
            <thead><tr><th>文件</th><th class="num">大小</th><th></th></tr></thead>
            <tbody>${ws.raw.map(f => html`<tr key=${f.path}>
              <td>${f.path}</td>
              <td class="num">${(f.size / 1024).toFixed(1)} KB</td>
              <td style=${{ textAlign: "right" }}>
                <button class="linkbtn" onClick=${() => del(f.path)}>删除</button></td>
            </tr>`)}</tbody>
          </table>` : null}
        ${nWiki ? html`
          <h3 class="ws-h">词条</h3>
          <table class="tbl">
            <thead><tr><th>词条</th><th class="num">大小</th><th></th></tr></thead>
            <tbody>${ws.wiki.map(f => html`<tr key=${f.path}>
              <td>${f.path}</td>
              <td class="num">${(f.size / 1024).toFixed(1)} KB</td>
              <td style=${{ textAlign: "right" }}>
                <button class="linkbtn" onClick=${() => del(f.path)}>删除</button></td>
            </tr>`)}</tbody>
          </table>` : null}`}
    </details>`;
}


/* 导出文档 —— 视觉上降为次要，放在上传区之后 */
/* =============================================================================
 * 按分析主题生成报告（赛题 5.1.3）
 *
 * 赛题原文：「根据用户选择的分析主题（**月度/季度/专题**）、分析月份、
 * 目标产品，自动生成完整报告」——所以这三个选择项是**硬要求**，
 * 且必须能从前端选，而不是只在命令行支持。
 *
 * ⚠️ 主题与可选期间**由服务端 /api/themes 给**，不在前端硬编：
 *    季度可选哪些、专题能选哪些要素，取决于数据集里有什么；
 *    前端硬编一份清单，加数据时就会悄悄不同步。
 * ========================================================================== */
function ReportGen({ me, onGenerated }) {
  const { data: prod } = useApi("/api/products");
  const { data: th } = useApi("/api/themes");
  const [product, setProduct] = useState("银黄口服液");
  const [month, setMonth] = useState("2026-06");
  const [theme, setTheme] = useState("monthly");
  const [period, setPeriod] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"];
  const t = ((th && th.themes) || []).find(x => x.id === theme) || {};
  const opts = t.period_options || [];

  useEffect(() => {
    const l = prod && prod.products;
    if (l && l.length && !l.includes(product)) setProduct(l[0]);
  }, [prod]);
  // 切主题时重置期间为该项的第一个可选项
  useEffect(() => {
    setPeriod(opts.length ? opts[0] : "");
    // eslint-disable-next-line
  }, [theme, th]);

  const gen = async () => {
    setBusy(true); setMsg(""); setErr("");
    try {
      const r = await api("/api/control/generate", {
        method: "POST",
        body: { product, month, theme, period },
      });
      if (r._error) throw new Error(r._error);
      setMsg(`已生成 ${r.file}（${r.stats.deterministic_fields} 个确定性值）`);
      onGenerated && onGenerated();
    } catch (e) { setErr("生成失败：" + String(e.message || e)); }
    setBusy(false);
  };

  if (!me) return null;      // 生成要花 token，未登录不显示入口

  return html`
    <details class="wsbox" open>
      <summary>按主题生成报告 <span class="hint">月度 / 季度 / 专题</span></summary>
      <p class="sec-sub" style=${{ marginTop: 12 }}>
        选择分析主题、期间与产品，系统自动生成完整报告（数字由脚本算，
        叙述由大模型写）。生成后可到下方「导出文档」转成 Word / PDF。
      </p>
      <div class="toolbar" style=${{ flexWrap: "wrap", gap: 10 }}>
        <label>分析主题</label>
        <select value=${theme} onChange=${e => setTheme(e.target.value)}>
          ${((th && th.themes) || []).map(x => html`
            <option key=${x.id} value=${x.id}>${x.name}</option>`)}
        </select>

        ${theme === "monthly" ? html`
          <label>分析月份</label>
          <select value=${month} onChange=${e => setMonth(e.target.value)}>
            ${MONTHS.map(m => html`<option key=${m} value=${m}>${m}</option>`)}
          </select>` : null}

        ${opts.length ? html`
          <label>${t.period_label || "期间"}</label>
          <select value=${period} onChange=${e => setPeriod(e.target.value)}>
            ${opts.map(o => html`<option key=${o} value=${o}>${o}</option>`)}
          </select>` : null}

        <label>产品</label>
        <select value=${product} onChange=${e => setProduct(e.target.value)}>
          ${((prod && prod.products) || []).map(p => html`
            <option key=${p} value=${p}>${p}</option>`)}
        </select>

        <button class="chip" type="button" disabled=${busy} onClick=${gen}>
          ${busy ? "生成中…（要调大模型，约 10-30 秒）" : "生成报告"}
        </button>
      </div>
      ${msg ? html`<div class="alert ok" style=${{ marginTop: 10 }}>✅ ${msg}</div>` : null}
      ${err ? html`<div class="alert" style=${{ marginTop: 10 }}>${err}</div>` : null}
    </details>`;
}


function ExportBar({ me }) {
  const [docs, setDocs] = useState(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!me) { setDocs(null); return; }
    api("/api/control/exports").then(d => setDocs(d._error ? null : d));
  }, [me]);

  /* POST 拿文件流，用 blob 下载——不能直接 <a href>，导出要带会话 cookie。
   * ⚠️ 下载流不能用 api()（那个按 JSON 解析），得走原生 fetch。 */
  const doExport = async (kind, md, fmt, charts) => {
    const key = kind + fmt + (charts ? "c" : "");
    setBusy(key); setErr("");
    try {
      const r = await fetch("/api/control/export", {
        method: "POST", credentials: "same-origin",
        headers: writeHeaders(),
        // charts=True 时服务端重绘三张图嵌进文档（赛题 5.1.3「图表嵌入」）
        body: JSON.stringify({ kind, md, fmt, charts: !!charts }),
      });
      if (r.status === 401 || r.status === 403)
        throw new Error("登录已过期，请重新登录");
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const blob = await r.blob();
      const cd = r.headers.get("content-disposition") || "";
      const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/.exec(cd);
      const name = m ? decodeURIComponent(m[1]) : `${md || kind}.${fmt}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) { setErr("导出失败：" + String(e.message || e)); }
    setBusy("");
  };

  if (!me) return html`<${LoginGate} what="文档导出"
                               onGoLogin=${() => (location.hash = "login")} />`;

  return html`
    <${ReportGen} me=${me} onGenerated=${() =>
      api("/api/control/exports").then(d => { if (!d._error) setDocs(d); })} />

    <details class="wsbox">
      <summary>导出文档 <span class="hint">Word / PDF</span></summary>
      <p class="sec-sub" style=${{ marginTop: 12 }}>
        导出的文档保留完整排版：页眉页脚、表格样式。
      </p>
      ${err ? html`<div class="alert" style=${{ marginBottom: 12 }}>⚠️ ${err}</div>` : null}
      ${docs && docs.techspec ? html`
        <div class="exp-row">
          <span class="exp-name">技术方案文档</span>
          ${["docx", "pdf"].map(f => html`
            <button key=${f} class="chip" disabled=${busy}
                    onClick=${() => doExport("techspec", "", f)}>
              ${busy === "techspec" + f ? "导出中…" : f.toUpperCase()}
            </button>`)}
        </div>` : null}
      ${docs && docs.reports && docs.reports.length ? html`
        <h3 class="ws-h">月度分析报告</h3>
        ${docs.reports.map(r => html`
          <div class="exp-row" key=${r.name}>
            <span class="exp-name">${r.stem.replace("_", " · ")}</span>
            ${["docx", "pdf"].map(f => html`
              <button key=${f} class="chip" disabled=${busy}
                      onClick=${() => doExport("report", r.name, f, true)}>
                ${busy === "report" + f + "c" ? "导出中…" : f.toUpperCase() + "（含图）"}
              </button>`)}
          </div>`)}` : html`
        <div class="empty" style=${{ padding: "18px 0" }}>暂无可导出的报告</div>`}
    </details>`;
}


/* =============================================================================
 * 首页第 ③ 段：分析（图表 + 知识图谱）
 * ========================================================================== */
function HomeCharts({ month, setMonth }) {
  const { data, loading, error } = useApi(`/api/overview?month=${month}`);
  const MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"];
  const MIX = [
    { key: "材料",     color: "#0F766E" },
    { key: "人工",     color: "#F59E0B" },
    { key: "制造费用", color: "#6366F1" },
  ];

  if (loading) return html`<section class="home-sec"><${Loading} /></section>`;
  if (error) return html`<section class="home-sec"><${ErrorBox} msg=${error} /></section>`;

  const bars = data.items
    .filter(it => it.facts["本月单位成本"] != null)
    .map((it, i) => ({
      label: it.product.replace(/(口服液|颗粒|胶囊)$/, ""),
      value: it.facts["本月单位成本"],
      color: ["#0F766E", "#0891B2", "#6366F1"][i % 3],
    }));

  const first = data.items.find(it => it.facts["本月单位成本"] != null);
  const waterfallRows = (() => {
    if (!first) return [];
    const f = first.facts;
    const prev = f["上月单位成本"], cur = f["本月单位成本"];
    if (prev == null || cur == null) return [];
    const items = [
      { name: "起点", amount: 0, share: null },
      { name: "直接材料", amount: (f["本月材料成本"] ?? 0) - (f["上月材料成本"] ?? 0),
        share: f["材料贡献度"] },
      { name: "直接人工", amount: (f["本月人工成本"] ?? 0) - (f["上月人工成本"] ?? 0),
        share: f["人工贡献度"] },
      { name: "制造费用", amount: (f["本月制造成本"] ?? 0) - (f["上月制造成本"] ?? 0),
        share: f["制造费用贡献度"] },
      { name: "单位成本变动", amount: +(cur - prev).toFixed(2), share: null },
    ];
    let cum = 0;
    items.forEach((it, i) => {
      if (i === 0) { it.cum = 0; return; }
      cum += it.amount; it.cum = cum;
    });
    return items;
  })();

  const donutItems = first ? [
    { name: "直接材料", value: first.facts["本月材料成本"] || 0, color: "#0F766E" },
    { name: "直接人工", value: first.facts["本月人工成本"] || 0, color: "#F59E0B" },
    { name: "制造费用", value: first.facts["本月制造成本"] || 0, color: "#6366F1" },
  ] : [];

  const mLabel = month.replace("-", " 年 ") + " 月";

  return html`
    <section class="home-sec">
      <div class="sec-head">
        <h2 class="sec-title">本月成本分析</h2>
        <div class="toolbar">
          <select value=${month} onChange=${e => setMonth(e.target.value)}>
            ${MONTHS.map(m => html`<option key=${m} value=${m}>${m.replace("-", " 年 ")} 月</option>`)}
          </select>
          <span class="pill">单位：元 / 盒</span>
        </div>
      </div>

      <div class="card">
        <h3>三产品单位成本对比</h3>
        <p class="card-sub">单位成本指每盒成品的综合生产成本，含直接材料、直接人工与制造费用。</p>
        <${BarChart} items=${bars} title=${`三产品成本对比-${month}`} />
      </div>

      <div class="chart2">
        <div class="card">
          <h3>成本变动分解</h3>
          <p class="card-sub">各成本要素对本月单位成本变动的贡献——柱高即该项变动额，最后一根是合计变动。</p>
          <${WaterfallChart} rows=${waterfallRows} title=${`成本变动分解-${month}`} />
        </div>
        <div class="card">
          <h3>本月成本构成</h3>
          <p class="card-sub">直接材料、直接人工、制造费用三项占单位成本的比重。</p>
          <${DonutChart} items=${donutItems} total=${first ? first.facts["本月单位成本"] : null}
                         unit="元/盒" title=${`成本构成-${month}`} />
        </div>
      </div>

      <div class="grid3">
        ${data.items.map(it => {
          const f = it.facts, d = it.display || {};
          const mix = MIX.map(m => ({
            ...m,
            share: f[m.key === "材料" ? "材料贡献度" : m.key === "人工" ? "人工贡献度" : "制造费用贡献度"] || 0,
            amount: f[`本月${m.key === "材料" ? "材料" : m.key === "人工" ? "人工" : "制造"}成本`],
          }));
          const hasMix = mix.some(m => m.share > 0);
          return html`
            <div class="kpi" key=${it.product}>
              <div class="kpi-name">${it.product}</div>
              <div class="kpi-value">
                ${d["本月单位成本"] || "—"}<span class="unit">元/盒</span>
              </div>
              <div class="kpi-deltas">
                <div>
                  <span class="lab">环比</span>
                  <span class=${"val " + cls(f["单位成本环比"])}>
                    <span class="arrow">${ARROW(f["单位成本环比"])}</span>
                    ${(d["单位成本环比"] || "—").replace("+", "")}
                  </span>
                </div>
                <div>
                  <span class="lab">同比</span>
                  <span class=${"val " + cls(f["单位成本同比"])}>
                    <span class="arrow">${ARROW(f["单位成本同比"])}</span>
                    ${(d["单位成本同比"] || "—").replace("+", "")}
                  </span>
                </div>
                <div>
                  <span class="lab">预算偏差</span>
                  <span class=${"val " + cls(f["单位成本预算偏差"])}>
                    ${(d["单位成本预算偏差"] || "—").replace("+", "")}
                  </span>
                </div>
              </div>
              ${hasMix ? html`
                <div class="mix">
                  <div class="mix-title">本月成本变动的来源</div>
                  <div class="mix-bar">
                    ${mix.map(m => html`<i key=${m.key}
                      style=${{ width: m.share + "%", background: m.color }}></i>`)}
                  </div>
                  <div class="mix-legend">
                    ${mix.map(m => html`<span key=${m.key}>
                      <i style=${{ background: m.color }}></i>
                      ${m.key} ${fmt(m.amount)} · ${fmt(m.share, 1)}%
                    </span>`)}
                  </div>
                </div>` : null}
            </div>`;
        })}
      </div>
      <p class="sec-foot">分析月份：${mLabel}</p>
    </section>`;
}

/* 热力图（赛题 5.2.2 可选加分项）—— 产品 × 月份 × 成本要素 */
/* =============================================================================
 * 成本趋势预测（赛题 6.2 加分项）
 *
 * ⚠️ 这个卡片与前后的图表性质**不同**，视觉上必须能区分开：
 *    其它卡片的数字都来自 raw、能复算；这里的数字是**外推**，没有坐标。
 *    一个孤零零的「下月 11.17 元/盒」会被当成承诺读——所以：
 *      · 标题与卡片带「预测 / 外推」标识
 *      · **永远显示区间**，不给单点值
 *      · 服务端给的 warnings 原样显示（样本量不足等）
 * ========================================================================== */
function HomeForecast() {
  const { data: prod } = useApi("/api/products");
  const [product, setProduct] = useState("");
  useEffect(() => {
    const l = prod && prod.products;
    if (l && l.length && !product) setProduct(l[0]);
  }, [prod, product]);

  const q = product
    ? `/api/forecast?product=${encodeURIComponent(product)}&horizon=2` : "";
  const { data, loading, error } = useApi(q || "/api/products");

  if (!product || loading) return html`<section class="home-sec"><${Loading} /></section>`;
  if (error) return html`<section class="home-sec"><${ErrorBox} msg=${error} /></section>`;
  if (!data || !data.predictions || !data.predictions.length) return null;

  const last = data.history[data.history.length - 1];
  const p1 = data.predictions[0];
  const chg = last ? (p1.预测值 - last.单位成本) : 0;

  return html`
    <section class="home-sec">
      <div class="card fc-card">
        <h2>成本趋势预测
          <span class="fc-tag">外推</span>
          <span class="hint">${data.method}　·　样本 ${data.n_samples} 个点</span>
        </h2>
        <p class="card-sub">
          基于历史单位成本外推，<strong>不是已发生的数</strong>——
          预测值没有原始坐标，与看板其余数字的性质不同，请连同区间一起读。
        </p>

        <div class="toolbar" style=${{ marginBottom: 16 }}>
          <label>产品</label>
          <select value=${product} onChange=${e => setProduct(e.target.value)}>
            ${((prod && prod.products) || []).map(p => html`
              <option key=${p} value=${p}>${p}</option>`)}
          </select>
        </div>

        <table class="tbl">
          <thead><tr>
            <th>月份</th><th class="num">预测值（元/盒）</th>
            <th class="num">参考区间</th><th>相对上月实际</th>
          </tr></thead>
          <tbody>
            <tr>
              <td>${last.月份}<span class="hint">（实际）</span></td>
              <td class="num">${fmt(last.单位成本)}</td>
              <td class="num">—</td><td>—</td>
            </tr>
            ${data.predictions.map(p => html`
              <tr key=${p.月份}>
                <td>${p.月份}</td>
                <td class="num strong">${fmt(p.预测值)}</td>
                <td class="num">${fmt(p.下限)} ~ ${fmt(p.上限)}</td>
                <td class=${cls(p.步长 === 1 ? chg : (p.预测值 - last.单位成本))}>
                  ${p.步长 === 1 && last
                    ? `${ARROW(chg)} ${(chg / last.单位成本 * 100).toFixed(2)}%`
                    : "—"}
                </td>
              </tr>`)}
          </tbody>
        </table>

        ${(data.warnings || []).length ? html`
          <div class="fc-warn">
            ${data.warnings.map((w, i) => html`<div key=${i}>⚠️ ${w}</div>`)}
          </div>` : null}
      </div>
    </section>`;
}


function HomeHeatmap() {
  const { data, loading, error } = useApi("/api/heatmap");
  const [el, setEl] = useState("单位材料");
  const els = (data && data.elements) || [];

  /* ⚠️ 这个 hook **必须在任何提前 return 之前**。
   *    放在 return 之后会触发 React #310
   *    （"Rendered more hooks than during the previous render"）——
   *    hooks 数量在两次渲染间必须一致，提前 return 会让后面的 hook 被跳过。
   *    这是本文件踩过的坑，**加载态出现时整页白屏**，日志里只有一串 minified 错误号。 */
  useEffect(() => {
    if (els.length && !els.includes(el)) setEl(els[0]);
  }, [data]);

  if (loading) return html`<section class="home-sec"><${Loading} /></section>`;
  if (error) return html`<section class="home-sec"><${ErrorBox} msg=${error} /></section>`;

  return html`
    <section class="home-sec">
      <div class="sec-head">
        <h2 class="sec-title">异动热力图
          <span class="sec-hint">产品 × 月份 × 成本要素</span>
        </h2>
        <div class="toolbar">
          <select value=${el} onChange=${e => setEl(e.target.value)}>
            ${els.map(k => html`<option key=${k} value=${k}>${k}</option>`)}
          </select>
        </div>
      </div>
      <p class="sec-sub">
        颜色是<strong>逐月环比变动率</strong>，不是绝对金额——
        三个产品单价量级差两倍多，比绝对值只会得出"六味最深"这种废话。
        色阶以 0 为中心对称：<strong>红涨绿跌</strong>，越深变动越大。
      </p>
      <div class="card">
        <${HeatmapChart} data=${data} element=${el}
                         title=${`异动热力图-${el}`} />
      </div>
    </section>`;
}


/* 知识图谱（赛题 6.2 加分项的前端呈现） */
function HomeGraph() {
  const [focus, setFocus] = useState("银黄口服液");
  const [hops, setHops] = useState(2);
  /* key：换中心 / 换跳数 / 点"重置视图"时 +1，让图**整个重新挂载**。
   * 图谱的力导向布局每次都会重算，所以重挂载后落点也变——
   * 这是布局本身的性质，不是 bug；用户要的是"能主动重置"。 */
  const [viewKey, setViewKey] = useState(0);
  const [sel, setSel] = useState(null);        // 当前选中的节点（与"中心"区分）
  const q = `focus=${encodeURIComponent(focus)}&hops=${hops}`;
  const { data, loading, error } = useApi(`/api/graph?${q}`);
  const PRODS = ["银黄口服液", "板蓝根颗粒", "六味地黄胶囊"];

  const reCenter = () => {
    if (!sel || sel === focus) return;
    setFocus(sel); setSel(null); setViewKey(k => k + 1);
  };

  const selNode = sel && data && data.nodes.find(n => n.id === sel);

  return html`
    <section class="home-sec">
      <div class="sec-head">
        <h2 class="sec-title">知识图谱
          <span class="sec-hint">配方 · 工艺 · 设备 · 成本异动</span>
        </h2>
        <div class="toolbar">
          <select value=${focus} onChange=${e => { setFocus(e.target.value); setSel(null); }}>
            ${PRODS.map(p => html`<option key=${p} value=${p}>${p}</option>`)}
          </select>
          <select value=${String(hops)} onChange=${e => { setHops(+e.target.value); setSel(null); }}>
            <option value="1">1 跳</option>
            <option value="2">2 跳</option>
            <option value="3">3 跳</option>
          </select>
          <button class="chip" onClick=${() => setViewKey(k => k + 1)}
                  title="把拖走的图恢复到初始位置">重置视图</button>
        </div>
      </div>
      <p class="sec-sub">
        从产品出发穿透到它的原料、工序与设备。
        <strong>检索给你一段话，图谱给你一条路径</strong>——
        所以它能回答「成本上涨经过了哪些环节」，而不只是「哪段文字提到了它」。
      </p>

      <div class="card">
        ${loading ? html`<${Loading} />`
          : error ? html`<${ErrorBox} msg=${error} />`
          : data && !data.ok ? html`<div class="msg err">${data.error}</div>`
          : html`
            <${GraphChart} key=${viewKey} data=${data}
                           selected=${sel} onSelect=${setSel} />
            <${GraphLegend} typeZh=${(data && data.type_zh) || {}} />

            ${/* 选中后的操作条 —— "查看"与"以它为中心"分成两步，
                 不会因为点错一下就把整个图重新布局。 */ ""}
            <div class="gbar">
              ${selNode ? html`
                <span class="gbar-sel">
                  <i style=${{ background: GRAPH_COLORS[selNode.type] || "#78716C" }}></i>
                  已选：<b>${selNode.label}</b>
                  <span class="gbar-type">${(data.type_zh || {})[selNode.type] || selNode.type}</span>
                </span>
                ${sel !== focus ? html`
                  <button class="chip" onClick=${reCenter}>以它为中心展开</button>` : null}
                <button class="linkbtn" onClick=${() => setSel(null)}>取消选择</button>`
              : html`
                <span class="gbar-hint">
                  点击节点查看，拖动可平移；当前 ${data.stats.nodes} 个节点、
                  ${data.stats.edges} 条关系
                </span>`}
            </div>`}
      </div>
    </section>`;
}


/* =============================================================================
 * 首页：对话 → 上传 → 分析
 *
 * 顺序是刻意的：**先问，再补资料，最后看分析**。
 * 用户的心流是「我想知道 X → 资料不够我补 → 数字都在这里」。
 * ========================================================================== */
function Home({ me, onNeedLogin, onAuthed }) {
  const [month, setMonth] = useState("2026-05");
  const [wsKey, setWsKey] = useState(0);
  const topRef = useRef(null);

  return html`
    <div ref=${topRef}></div>
    <${HeroAsk} me=${me} onNeedLogin=${onNeedLogin} onAuthed=${onAuthed} />
    <${Reveal}><${UploadZone} me=${me} onNeedLogin=${onNeedLogin}
                   onChanged=${() => setWsKey(k => k + 1)} /><//>
    <${Reveal}><${WorkspaceList} me=${me} refreshKey=${wsKey} /><//>
    <${Reveal}><${ExportBar} me=${me} /><//>
    <${Reveal}><${DecideCard} /><//>
    <${Reveal}><${HomeCharts} month=${month} setMonth=${setMonth} /><//>
    <${Reveal}><${HomeHeatmap} /><//>
    <${Reveal}><${HomeForecast} /><//>
    <${Reveal}><${HomeGraph} /><//>`;
}
/* =============================================================================
 * 外壳
 * ========================================================================== */
const TABS = [
  { id: "home",      label: "首页" },
  { id: "trend",     label: "成本趋势" },
  { id: "benchmark", label: "对标分析" },
  { id: "notes",     label: "数据说明" },
  { id: "history",   label: "更新记录" },
  { id: "rectify",   label: "整改闭环" },
  // profile / login **不在导航条显示**（见 NAV_TABS），但从顶栏 / 跳转可达
  { id: "profile",   label: "个人中心" },
  { id: "login",     label: "登录" },
];

// 导航条上真正显示的那些（profile/login 走顶栏与跳转，不占导航）
const NAV_TABS = TABS.filter(t => t.id !== "profile" && t.id !== "login");

/* 旧链接兼容：`#overview` / `#chat` / `#panel` 都指向首页。
 * 用户可能存了旧书签，或分享过带旧锚点的链接——
 * 直接失效会让人以为"网站打不开了"。 */
const TAB_ALIAS = { overview: "home", chat: "home", panel: "home" };

function useHashTab() {
  const read = () => {
    let h = (location.hash || "").replace(/^#/, "");
    // 允许多带一个查询串（如 `home?conv=3`）：个人中心的「打开会话」靠它
    // 把目标会话传给首页。**先剥掉 `?…` 再匹配页签**，否则
    // `home?conv=3` 匹配不上任何 id，虽然会回落到 home，但参数就丢了。
    h = h.split("?")[0];
    h = TAB_ALIAS[h] || h;
    return TABS.some(t => t.id === h) ? h : "home";
  };
  const [tab, setTab] = useState(read);
  useEffect(() => {
    const on = () => setTab(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [tab, id => { location.hash = id; setTab(id); }];
}

function App() {
  const [tab, go] = useHashTab();
  const { data: health } = useApi("/api/health");
  const { data: notes } = useApi("/api/conflicts");

  /* 登录态**只在这里持有一次**，往下传。
   * ⚠️ 曾经每个组件各自读 localStorage —— 那是"三个真相源"：
   *    对话区登录了、上传区还以为没登录。现在只有一个来源。 */
  const [me, setMe] = useState(null);
  const [authReady, setAuthReady] = useState(false);
  const [notice, setNotice] = useState("");

  const refreshMe = () => api("/api/auth/me").then(d => {
    setMe(d && d.logged_in ? d : null);
    setAuthReady(true);
  });
  useEffect(() => { refreshMe(); }, []);

  /* 首屏落在哪：
   *   未登录 → 登录页（入口）          已登录 → 首页
   * ⚠️ 但**不强制**：登录页有「以访客身份浏览」，访客照常看公开数据。
   *    强制跳转会挡住第一次来的人——看不到东西就不会想注册。 */
  const [guest, setGuest] = useState(false);
  useEffect(() => {
    if (!authReady) return;
    if (!me && !guest && tab === "home" && !location.hash) {
      location.hash = "login";
    }
  }, [authReady, me, guest]);

  const onAuthed = () => { setGuest(false); setNotice(""); refreshMe(); go("home"); };
  const onLoggedOut = (switchAccount) => {
    setMe(null);
    setGuest(!switchAccount);          // 退出 → 回登录页；切换账号也是
    setNotice(switchAccount ? "已退出，请用另一个账号登录" : "");
    location.hash = "login";
  };

  // 未登录时停在登录页 → 整屏渲染，不套外壳
  const onLoginRoute = !me && tab === "login";

  const who = me ? html`
    <button class="who" onClick=${() => go("profile")} title="进入个人中心">
      <span class="who-name">${me.name || me.external_id}</span>
      <span class="who-go">个人中心 ›</span>
    </button>` : (authReady ? html`
    <button class="who" onClick=${() => go("login")} title="去登录">
      <span class="who-anon">未登录</span>
      <span class="who-go">登录 ›</span>
    </button>` : null);

  if (onLoginRoute) {
    return html`<${LoginPage} onAuthed=${onAuthed}
                             onGuest=${() => { setGuest(true); go("home"); }}
                             notice=${notice} />`;
  }

  return html`
    <header class="top">
      <div class="wrap">
        <div class="brandrow">
          <div class="brand">
            <div class="mark">成</div>
            <div>
              <h1>制药成本分析</h1>
              <div class="sub">中药一厂 · 成本智能分析系统</div>
            </div>
          </div>
          ${who}
        </div>
        ${health ? html`
          <div class="scope">
            <div>分析区间　<b>2026 年 1 – 6 月</b></div>
            <div>覆盖产品　<b>3 个</b></div>
            <div>依据资料　<b>${health.sources} 份</b></div>
            ${notes ? html`<div>数据说明　<b>${notes.count} 条</b></div>` : null}
          </div>` : html`<div class="scope"></div>`}
      </div>
    </header>

    <div class="wrap">
      <nav class="tabs">
        ${NAV_TABS.map(t => html`
          <button key=${t.id} class=${tab === t.id ? "on" : ""} onClick=${() => go(t.id)}>
            ${t.label}${t.id === "notes" && notes
              ? html`<span class="count">${notes.count}</span>` : null}
          </button>`)}
      </nav>

      ${tab === "home"     ? html`<${Home} me=${me} onNeedLogin=${() => go("login")}
                                        onAuthed=${refreshMe} />` : null}
      ${tab === "trend"    ? html`<${Trend} />` : null}
      ${tab === "benchmark" ? html`<${Benchmark} />` : null}
      ${tab === "notes"    ? html`<${Notes} />` : null}
      ${tab === "history"  ? html`<${History} />` : null}
      ${tab === "profile"  ? html`<${Profile} me=${me} onAuthed=${onAuthed}
                                           onLoggedOut=${onLoggedOut} />` : null}
      ${tab === "rectify"  ? html`<${Rectify} me=${me} onNeedLogin=${() => go("login")}
                                       onAuthed=${refreshMe} />` : null}

      <footer class="foot">
        本系统所有数字均可在原始资料中逐项核对 ｜ 分析文本由 AI 辅助生成，请结合业务判断
      </footer>
    </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
