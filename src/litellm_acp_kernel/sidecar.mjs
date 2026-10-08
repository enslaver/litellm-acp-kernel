import { createInterface } from "node:readline";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const kernelDir = process.argv[2];
if (!kernelDir) {
  process.stderr.write("usage: sidecar.mjs <acp-kernel dir>\n");
  process.exit(2);
}

const load = (rel) => import(pathToFileURL(join(kernelDir, rel)).href);
const kernel = await load("dist/index.js");
const wire = await load("dist/wire/index.js");

const {
  createCore,
  createInitialState,
  defaultConfig,
  defaultPrompts,
  buildCompressSystemPrompt,
  renderNudgeText,
  parseCompressInput,
  viableRanges,
  COMPRESS_TOOL_NAME,
  COMPRESS_TOOL_OPENAI,
  DECOMPRESS_TOOL_OPENAI,
  SEARCH_CONTEXT_TOOL_OPENAI,
  ACP_STATUS_TOOL_OPENAI,
  RETRIEVE_TOOL_OPENAI,
  createContentStore,
  parseBlockIdArg,
  collectBlockContent,
  countMessageTokens,
} = kernel;
const { openaiToCore, coreToOpenai, injectOpenaiSystem } = wire;

const core = createCore();

const configFor = (contextLimit, overrides) => defaultConfig(contextLimit, overrides ?? {});

const estimateTokens = (msgs) => msgs.reduce((sum, m) => sum + countMessageTokens(m), 0);

const systemToUser = (messages) =>
  messages.map((m) => (m.role === "system" || m.role === "developer" ? { ...m, role: "user" } : m));

const fillNullContent = (messages) =>
  messages.map((m) => (m.role === "assistant" && (m.content === null || m.content === undefined) ? { ...m, content: "" } : m));

const PROXY_TOOLS = [COMPRESS_TOOL_OPENAI, DECOMPRESS_TOOL_OPENAI, SEARCH_CONTEXT_TOOL_OPENAI, ACP_STATUS_TOOL_OPENAI];

const withProxyTools = (tools, ccrOn) => {
  const existing = Array.isArray(tools) ? tools : [];
  const names = new Set(existing.map((t) => t?.function?.name));
  const extra = ccrOn ? [...PROXY_TOOLS, RETRIEVE_TOOL_OPENAI] : PROXY_TOOLS;
  return [...existing, ...extra.filter((t) => !names.has(t.function.name))];
};

function prepare({ body, state, contentStore, contextLimit, tokenCount, config: overrides, injectNudge = true }) {
  const { msgs, systemText } = openaiToCore(body);
  const config = configFor(contextLimit, overrides);
  const turn = core.processTurn({
    messages: msgs,
    state: state ?? createInitialState(),
    config,
    tokenCount: tokenCount ?? estimateTokens(msgs),
    renderTags: "text-only",
    contentStore: contentStore ?? createContentStore(),
  });
  if (turn.nudge) turn.nudge.compressibleRanges = viableRanges(turn.nudge.compressibleRanges);

  let messages = systemToUser(fillNullContent(coreToOpenai(turn.messages)));
  const systemParts = [];
  if (systemText) systemParts.push(systemText);
  systemParts.push(buildCompressSystemPrompt(defaultPrompts));
  messages = injectOpenaiSystem(messages, systemParts);

  let nudged = false;
  if (injectNudge && turn.nudge?.shouldInject) {
    const rendered = renderNudgeText(turn.nudge, defaultPrompts);
    if (rendered.text) {
      messages = [...messages, { role: "user", content: rendered.text }];
      nudged = true;
    }
  }
  return {
    messages,
    tools: withProxyTools(body.tools, config.ccr?.enabled),
    state: turn.state,
    contentStore: turn.contentStore,
    nudged,
  };
}

function apply({ body, state, calls, contextLimit, config: overrides }) {
  const { msgs } = openaiToCore(body);
  const config = configFor(contextLimit, overrides);
  let current = state;
  const results = [];
  for (const call of calls) {
    let args;
    try {
      args = typeof call.arguments === "string" ? JSON.parse(call.arguments) : call.arguments;
    } catch {
      results.push({ id: call.id, ok: false, errors: ["compress arguments are not valid JSON"], warnings: [] });
      continue;
    }
    const warnings = [];
    const ranges = parseCompressInput(args, call.id, (w) => warnings.push(w));
    if (ranges.length === 0) {
      results.push({ id: call.id, ok: false, errors: warnings, warnings: [] });
      continue;
    }
    const out = core.applyCompression({ ranges, messages: msgs, state: current, config });
    current = out.state;
    results.push({
      id: call.id,
      ok: out.result.errors.length === 0,
      blocksCreated: out.result.blocksCreated,
      tokensCompressed: out.result.tokensCompressed,
      errors: out.result.errors,
      warnings: out.result.warnings,
    });
  }
  return { state: current, results };
}

const MAX_RESTORE_CHARS = 32000;

const parseArgs = (call) => {
  try {
    const args = typeof call.arguments === "string" ? (call.arguments ? JSON.parse(call.arguments) : {}) : call.arguments;
    return args && typeof args === "object" ? args : {};
  } catch {
    return null;
  }
};

const clip = (text) =>
  text.length > MAX_RESTORE_CHARS
    ? `${text.slice(0, MAX_RESTORE_CHARS)}\n[truncated: ${text.length - MAX_RESTORE_CHARS} more characters]`
    : text;

// Read-only lookups: they never change session state, so the folded prefix stays cache-stable.
function runTool(name, args, { body, state, contentStore, contextLimit, config: overrides, tokenCount }) {
  const current = state ?? createInitialState();
  if (name === "decompress") {
    const blockId = parseBlockIdArg(String(args.blockId ?? ""));
    const block = blockId ? core.decompress(blockId, current) : undefined;
    if (!block) return { ok: false, content: `decompress failed: unknown block ${JSON.stringify(args.blockId ?? "")}` };
    const { msgs } = openaiToCore(body);
    const { text, count } = collectBlockContent(current, block, msgs, { full: args.full === true });
    if (!text) return { ok: false, content: `decompress failed: no original messages are available for ${block.blockId}` };
    return { ok: true, content: clip(`Block ${block.blockId} (${count} message(s)):\n\n${text}`) };
  }
  if (name === "search_context") {
    const query = String(args.query ?? "");
    const limit = Number.isInteger(args.limit) && args.limit > 0 ? args.limit : 5;
    const hits = core.search(query, current).slice(0, limit);
    if (hits.length === 0) return { ok: true, content: `No compressed blocks match ${JSON.stringify(query)}.` };
    return {
      ok: true,
      content: hits.map((b) => `${b.blockId}${b.topic ? ` (${b.topic})` : ""}: ${b.summary}`).join("\n\n"),
    };
  }
  if (name === "acp_status") {
    const { msgs } = openaiToCore(body);
    const report = core.status(current, tokenCount ?? estimateTokens(msgs), configFor(contextLimit, overrides));
    return { ok: true, content: JSON.stringify(report) };
  }
  if (name === "acp_retrieve") {
    if (!configFor(contextLimit, overrides).ccr?.enabled) return { ok: false, content: "acp_retrieve is not enabled" };
    const out = core.retrieve(contentStore ?? createContentStore(), String(args.ref ?? ""));
    return { ok: out.ok, content: out.toolResultText };
  }
  return { ok: false, content: `unknown tool: ${name}` };
}

function tool({ calls, ...ctx }) {
  return {
    results: calls.map((call) => {
      const args = parseArgs(call);
      if (args === null) return { id: call.id, ok: false, content: `${call.name} arguments are not valid JSON` };
      return { id: call.id, ...runTool(call.name, args, ctx) };
    }),
  };
}

const ops = { prepare, apply, tool };

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of rl) {
  if (!line.trim()) continue;
  let req;
  try {
    req = JSON.parse(line);
    const op = ops[req.op];
    if (!op) throw new Error(`unknown op: ${req.op}`);
    process.stdout.write(JSON.stringify({ id: req.id, ok: true, result: op(req) }) + "\n");
  } catch (err) {
    process.stdout.write(JSON.stringify({ id: req?.id ?? null, ok: false, error: String(err?.stack ?? err) }) + "\n");
  }
}
