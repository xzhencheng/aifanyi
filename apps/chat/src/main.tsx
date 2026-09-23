import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
type Block = {
  type: string;
  text?: string;
  result?: { text?: string; items?: { text: string }[] };
  quality_status?: string;
  revision?: number;
  error?: { message?: string };
  code?: string;
  status?: string;
  evidence_refs?: string[];
};
type Message = { id: string; role: string; content: string; blocks?: Block[] };
type Run = {
  run_id: string;
  version: number;
  conversation_id: string;
  conversation_version: number;
  status: string;
  job_id?: string;
  question?: { question_id: string; prompt: string };
  blocks: Block[];
};
type Job = {
  job_id: string;
  translation_id: string;
  revision: number;
  state_version: number;
  status: string;
  stage: string;
  source_id?: string;
};
type Segment = {
  id: string;
  index: number;
  source_text: string;
  text?: string;
  revision: number;
  status: string;
  reviewed: boolean;
  issues: {
    id: string;
    explanation: string;
    severity: string;
    status: string;
    hard?: boolean;
  }[];
};
const labels: Record<string, string> = {
  queued: "排队中",
  running: "处理中",
  awaiting_input: "需要补充信息",
  awaiting_review: "等待审阅",
  completed: "已完成",
  succeeded: "已完成",
  failed: "执行失败",
  canceled: "已取消",
};
const qualities: Record<string, string> = {
  auto_passed: "自动检查通过",
  human_approved: "人工审核通过",
  needs_review: "待审草稿",
  pending: "检查中",
};
const stages: Record<string, string> = {
  validating: "检查输入",
  contextualizing: "构建上下文",
  translating: "初译",
  reviewing: "对照原文审校",
  checking: "程序检查",
  repairing: "修复译文",
  assembling: "整理结果",
};
function App() {
  const [token, setToken] = useState(""),
    [connected, setConnected] = useState(false),
    [projects, setProjects] = useState<Record<string, string[]>>({}),
    [project, setProject] = useState("project_default");
  const [target, setTarget] = useState("en"),
    [source, setSource] = useState("auto"),
    [mode, setMode] = useState("translate"),
    [input, setInput] = useState("");
  const [convs, setConvs] = useState<{ id: string; created_at: string }[]>([]),
    [conv, setConv] = useState(""),
    [version, setVersion] = useState(1),
    [messages, setMessages] = useState<Message[]>([]);
  const [run, setRun] = useState<Run | null>(null),
    [job, setJob] = useState<Job | null>(null),
    [segments, setSegments] = useState<Segment[]>([]),
    [selected, setSelected] = useState<string[]>([]);
  const [scope, setScope] = useState("once"),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [reconnect, setReconnect] = useState(false);
  const activeConv = useRef(""),
    stream = useRef<AbortController | null>(null),
    bottom = useRef<HTMLDivElement>(null),
    eventCursor = useRef<Record<string, string>>({});
  const pendingSubmission = useRef<{
    fingerprint: string;
    path: string;
    body: any;
    key: string;
  } | null>(null);
  async function api(path: string, body?: unknown, key?: string) {
    const r = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body === undefined
          ? {}
          : {
              "Content-Type": "application/json",
              "Idempotency-Key": key || crypto.randomUUID(),
            }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok)
      throw new Error(
        `${data.error?.message || "请求失败"}（${data.error?.code || r.status}）`,
      );
    return data;
  }
  async function listConversations(p = project) {
    const r = await api(
      `/v1/agent/conversations?project_id=${encodeURIComponent(p)}`,
    );
    setConvs(r.items);
    return r.items;
  }
  async function history(id: string) {
    let all: Message[] = [],
      cursor = "",
      r: any;
    do {
      r = await api(
        `/v1/agent/conversations/${id}/messages?limit=100${cursor ? "&cursor=" + encodeURIComponent(cursor) : ""}`,
      );
      all.push(...r.items);
      cursor = r.next_cursor || "";
    } while (cursor);
    if (activeConv.current === id) {
      setMessages(all);
      setVersion(r.version);
    }
    return r;
  }
  async function refresh(id: string) {
    const r: Run = await api(`/v1/agent/runs/${id}`);
    if (activeConv.current !== r.conversation_id) return r;
    setRun(r);
    setVersion(r.conversation_version);
    if (r.job_id) {
      const j = await api(`/v1/translation-jobs/${r.job_id}`);
      setJob(j);
      if (["succeeded", "awaiting_review"].includes(j.status)) {
        let all: Segment[] = [],
          cursor = "";
        do {
          const p = await api(
            `/v1/translation-jobs/${j.job_id}/segments?limit=100${cursor ? "&cursor=" + encodeURIComponent(cursor) : ""}`,
          );
          all.push(...p.items);
          cursor = p.next_cursor || "";
        } while (cursor);
        setSegments(all.sort((a, b) => a.index - b.index));
      }
    }
    if (["completed", "failed", "canceled"].includes(r.status))
      await history(r.conversation_id);
    return r;
  }
  async function watch(id: string) {
    stream.current?.abort();
    const controller = new AbortController();
    stream.current = controller;
    setReconnect(false);
    try {
      const headers: Record<string, string> = {
        Authorization: `Bearer ${token}`,
      };
      if (eventCursor.current[id])
        headers["Last-Event-ID"] = eventCursor.current[id];
      let r = await fetch(`/v1/agent/runs/${id}/events`, {
        headers,
        signal: controller.signal,
      });
      if (r.status === 410) {
        delete eventCursor.current[id];
        await refresh(id);
        delete headers["Last-Event-ID"];
        r = await fetch(`/v1/agent/runs/${id}/events`, {
          headers,
          signal: controller.signal,
        });
      }
      if (!r.ok || !r.body) throw new Error("事件连接失败");
      const reader = r.body.getReader(),
        decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        buffer += decoder.decode(part.value, { stream: true });
        let end: number;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
          const item = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const line = item.split("\n").find((x) => x.startsWith("id: "));
          if (line) {
            eventCursor.current[id] = line.slice(4);
            await refresh(id);
          }
        }
      }
    } catch {
      if (!controller.signal.aborted) {
        await refresh(id).catch(() => null);
        setReconnect(true);
      }
    }
  }
  async function open(id: string) {
    stream.current?.abort();
    activeConv.current = id;
    setConv(id);
    localStorage.setItem("aifanyi.conversation", id);
    setRun(null);
    setJob(null);
    setSegments([]);
    setSelected([]);
    const h = await history(id);
    if (h.active_run_id) {
      await refresh(h.active_run_id);
      watch(h.active_run_id);
    } else {
      const last = [...h.items].reverse().find((m: any) => m.run_id);
      if (last) await refresh(last.run_id);
    }
  }
  async function login() {
    try {
      setError("");
      const me = await api("/v1/me");
      setProjects(me.projects);
      const p = Object.keys(me.projects)[0] || project;
      setProject(p);
      setConnected(true);
      const list = await listConversations(p);
      const id = localStorage.getItem("aifanyi.conversation");
      if (id && list.some((item: { id: string }) => item.id === id))
        await open(id);
    } catch (e) {
      setError(String(e));
    }
  }
  async function create() {
    const c = await api("/v1/agent/conversations", {
      project_id: project,
      defaults: { target_language: target, source_language: source },
    });
    await open(c.conversation_id);
    await listConversations();
    return c;
  }
  async function send() {
    if (!input.trim()) return;
    setBusy(true);
    setError("");
    try {
      let id = conv,
        v = version;
      if (!id) {
        const c = await create();
        id = c.conversation_id;
        v = c.version;
      }
      const fingerprint = JSON.stringify([
        id,
        input,
        mode,
        target,
        source,
        selected,
        run?.question?.question_id,
      ]);
      if (pendingSubmission.current?.fingerprint !== fingerprint) {
        let path = "/v1/agent/messages";
        let body: any;
        if (
          run?.question &&
          ["awaiting_input", "awaiting_review"].includes(run.status)
        ) {
          path = `/v1/agent/runs/${run.run_id}/responses`;
          body = {
            question_id: run.question.question_id,
            message: input,
            expected_run_version: run.version,
            expected_conversation_version: v,
          };
        } else {
          body = {
            conversation_id: id,
            expected_conversation_version: v,
            client_message_id: crypto.randomUUID(),
            message: input,
            target_language: target,
            source_language: source,
          };
          if (mode === "translate")
            body.source_spans = [{ start: 0, end: [...input].length }];
          if (selected.length && job)
            body.target_ref = {
              translation_id: job.translation_id,
              base_revision: job.revision,
              segment_ids: selected,
            };
        }
        pendingSubmission.current = {
          fingerprint,
          path,
          body,
          key: crypto.randomUUID(),
        };
      }
      const pending = pendingSubmission.current!;
      const r = await api(pending.path, pending.body, pending.key);
      pendingSubmission.current = null;
      setSelected([]);
      setInput("");
      await history(id);
      await refresh(r.run_id);
      watch(r.run_id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    if (!run) return;
    try {
      await api(`/v1/agent/runs/${run.run_id}/cancel`, {
        reason: "用户取消",
        expected_run_version: run.version,
      });
      await refresh(run.run_id);
    } catch (e) {
      setError(String(e));
    }
  }
  async function review(seg: Segment, action: string) {
    if (!job) return;
    const replacement =
      action === "replace_translation"
        ? prompt("修改译文，保存后将重新审校：", seg.text)
        : undefined;
    if (action === "replace_translation" && !replacement) return;
    try {
      const j = await api(`/v1/translation-jobs/${job.job_id}`);
      await api(`/v1/translation-jobs/${job.job_id}/review-decisions`, {
        revision: j.revision,
        expected_version: j.state_version,
        decisions: [
          {
            segment_id: seg.id,
            segment_revision: seg.revision,
            action,
            reason: replacement ? "人工修订" : "已逐项核对原文与当前译文",
            ...(replacement ? { replacement_text: replacement } : {}),
          },
        ],
        finalize: true,
      });
      if (run) {
        await refresh(run.run_id);
        watch(run.run_id);
      }
    } catch (e) {
      setError(String(e));
    }
  }
  async function feedback() {
    if (!job || !selected.length || !input.trim()) {
      setError("请勾选片段，并在输入框填写修正后的译文。");
      return;
    }
    try {
      await api(`/v1/translations/${job.translation_id}/feedback`, {
        revision: job.revision,
        segment_ids: selected,
        category: "sentence",
        scope,
        before: segments
          .filter((s) => selected.includes(s.id))
          .map((s) => s.text)
          .join("\n"),
        after: input,
        reason: "用户提交修正",
      });
      setError("反馈草稿已保存，尚未发布或修改当前译文。");
      setInput("");
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, run?.status]);
  useEffect(() => () => stream.current?.abort(), []);
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <b>译</b>
          <div>
            译知<small>TRANSLATION AGENT</small>
          </div>
        </div>
        <p className="intro">让每次翻译，都有原文可依。</p>
        <label>
          访问凭据
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="输入访问令牌"
            autoComplete="off"
          />
        </label>
        <button className="secondary" onClick={login}>
          {connected ? "刷新连接" : "连接服务"}
        </button>
        <label>
          当前项目
          <select
            value={project}
            onChange={(e) => {
              stream.current?.abort();
              setJob(null);
              setSelected([]);
              setProject(e.target.value);
              setConv("");
              activeConv.current = "";
              setMessages([]);
              setRun(null);
              setSegments([]);
              listConversations(e.target.value).catch((e) =>
                setError(String(e)),
              );
            }}
          >
            {Object.keys(projects).length ? (
              Object.keys(projects).map((p) => <option key={p}>{p}</option>)
            ) : (
              <option>project_default</option>
            )}
          </select>
        </label>
        <button
          disabled={!connected}
          onClick={() => create().catch((e) => setError(String(e)))}
        >
          ＋ 新建对话
        </button>
        <small className="historyTitle">会话记录</small>
        <nav>
          {convs.map((c) => (
            <button
              className={"history " + (conv === c.id ? "active" : "")}
              key={c.id}
              onClick={() => open(c.id).catch((e) => setError(String(e)))}
            >
              文本翻译<small>{new Date(c.created_at).toLocaleString()}</small>
            </button>
          ))}
        </nav>
        <div className="asideFoot">
          一期 · 文本与对话
          <br />
          中文 / English / 日本語
        </div>
      </aside>
      <main>
        <header>
          <div>
            <small>准确性优先</small>
            <h1>翻译工作间</h1>
          </div>
          <span className="modelTag">● Hy-MT2-7B ＋ Qwen 3.5</span>
        </header>
        <section className="toolbar">
          <label>
            原文
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="auto">自动识别</option>
              <option value="zh-CN">中文</option>
              <option value="en">英文</option>
              <option value="ja">日文</option>
            </select>
          </label>
          <span>→</span>
          <label>
            译文
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="en">英文</option>
              <option value="zh-CN">中文</option>
              <option value="ja">日文</option>
            </select>
          </label>
          <small className="toolbarNote">
            术语与上下文 · 原文审校 · 有限修复
          </small>
        </section>
        <section className="thread">
          {!messages.length && (
            <div className="empty">
              <span>文 / A</span>
              <h2>从一段原文开始</h2>
              <p>
                粘贴需要翻译的内容。完成后可以补充背景、
                <br />
                询问译法，或选择片段重新翻译。
              </p>
              <button
                className="secondary"
                onClick={() => {
                  setMode("translate");
                  setInput("设备处于待机状态。");
                }}
              >
                试试文本翻译 ↗
              </button>
            </div>
          )}
          {messages.map((m) => (
            <article className={"message " + m.role} key={m.id}>
              <small>{m.role === "user" ? "你" : "翻译 Agent"}</small>
              {m.content && <p>{m.content}</p>}
              {m.blocks?.map((b, i) => (
                <div key={i}>
                  {b.type === "translation" ? (
                    <>
                      <span className="quality">
                        {qualities[b.quality_status || "pending"]}
                      </span>
                      <p>
                        {b.result?.text ||
                          b.result?.items?.map((x) => x.text).join("\n\n")}
                      </p>
                      <small>版本 {b.revision}</small>
                    </>
                  ) : (
                    <>
                      <p>
                        {b.text ||
                          b.error?.message ||
                          labels[b.status || ""] ||
                          b.code}
                      </p>
                      {!!b.evidence_refs?.length && (
                        <small>依据：{b.evidence_refs.join("、")}</small>
                      )}
                    </>
                  )}
                </div>
              ))}
            </article>
          ))}
          {run && !["completed", "canceled"].includes(run.status) && (
            <div className="runState">
              <b>{labels[run.status]}</b>
              <span>
                {run.question?.prompt || stages[job?.stage || ""] || ""}
              </span>
              {run.status !== "failed" && (
                <button onClick={cancel}>取消</button>
              )}
              <button onClick={() => watch(run.run_id)}>
                {reconnect ? "重新连接" : "恢复进度"}
              </button>
            </div>
          )}
          {segments.length > 0 && (
            <details
              className="review"
              open={job?.status === "awaiting_review"}
            >
              <summary>
                原译对照与问题 <small>{segments.length} 个片段</small>
              </summary>
              {segments.map((seg) => (
                <div className="segment" key={seg.id}>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={selected.includes(seg.id)}
                      onChange={(e) =>
                        setSelected(
                          e.target.checked
                            ? [...selected, seg.id]
                            : selected.filter((x) => x !== seg.id),
                        )
                      }
                    />
                    片段 {seg.index + 1} ·{" "}
                    {seg.status === "needs_review"
                      ? "待审草稿"
                      : seg.status === "accepted"
                        ? "检查通过"
                        : seg.status}
                  </label>
                  <div className="comparison">
                    <p>{seg.source_text}</p>
                    <p>{seg.text || "尚未产生译文"}</p>
                  </div>
                  {seg.issues.map((i) => (
                    <p className="issue" key={i.id}>
                      {i.severity} · {i.explanation}（{i.status}）
                    </p>
                  ))}
                  {job?.status === "awaiting_review" &&
                    projects[project]?.includes("reviewer") && (
                      <div className="actions">
                        <button
                          className="secondary"
                          onClick={() => review(seg, "replace_translation")}
                        >
                          修改后重新检查
                        </button>
                        <button
                          disabled={
                            !seg.reviewed ||
                            seg.issues.some((i) => i.status === "open")
                          }
                          onClick={() => review(seg, "approve_segment")}
                        >
                          确认当前片段
                        </button>
                      </div>
                    )}
                </div>
              ))}
              <div className="actions feedback">
                <select
                  value={scope}
                  onChange={(e) => setScope(e.target.value)}
                >
                  <option value="once">仅本次</option>
                  <option value="personal">个人</option>
                  <option value="project">项目</option>
                  <option value="enterprise">企业</option>
                </select>
                <button className="secondary" onClick={feedback}>
                  保存所选片段的反馈
                </button>
              </div>
            </details>
          )}
          <div ref={bottom} />
        </section>
        <footer>
          {error && (
            <div className="notice" role="alert">
              {error}
              <button onClick={() => setError("")}>×</button>
            </div>
          )}
          <div className="composer">
            <div className="tabs">
              <button
                className={mode === "translate" ? "selected" : ""}
                onClick={() => setMode("translate")}
              >
                原文翻译
              </button>
              <button
                className={mode === "chat" ? "selected" : ""}
                onClick={() => setMode("chat")}
              >
                对话追问
              </button>
              {selected.length > 0 && (
                <small>已选 {selected.length} 个片段</small>
              )}
            </div>
            <textarea
              value={input}
              maxLength={20000}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                run?.question
                  ? "补充需要的信息…"
                  : mode === "translate"
                    ? "在这里粘贴待翻译的原文…"
                    : "补充背景、询问译法，或说明需要重译的片段…"
              }
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <div className="composerBottom">
              <small>仅支持文本 · Ctrl / ⌘ + Enter 发送</small>
              <button
                disabled={
                  !connected ||
                  busy ||
                  !input.trim() ||
                  !!(run && ["queued", "running"].includes(run.status))
                }
                onClick={send}
              >
                {run?.question ? "提交补充" : "发送"} ↑
              </button>
            </div>
          </div>
          <p className="footnote">
            译文经过自动检查；有歧义或高风险内容会保留为待审草稿。
          </p>
        </footer>
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
