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

const withCompressTool = (tools) => {
  const existing = Array.isArray(tools) ? tools : [];
  const present = existing.some((t) => t?.function?.name === COMPRESS_TOOL_NAME);
  return present ? existing : [...existing, COMPRESS_TOOL_OPENAI];
};

function prepare({ body, state, contextLimit, tokenCount, config: overrides, injectNudge = true }) {
  const { msgs, systemText } = openaiToCore(body);
  const config = configFor(contextLimit, overrides);
  const turn = core.processTurn({
    messages: msgs,
    state: state ?? createInitialState(),
    config,
    tokenCount: tokenCount ?? estimateTokens(msgs),
    renderTags: "text-only",
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
  return { messages, tools: withCompressTool(body.tools), state: turn.state, nudged };
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

const ops = { prepare, apply };

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
