import { spawn } from "node:child_process"
import { appendFile, readFile } from "node:fs/promises"

const SENTINEL = "flowark-opencode-runtime-plugin-v1"
const RUNTIME_CONTEXT_MARKER = "<flowark-runtime-context"
const KNOWLEDGE_CONTEXT_MARKER = "<flowark-knowledge-injection"
const POST_TOOL_ALLOWED_TOOLS = new Set(["read", "grep", "glob", "bash"])
const MAX_COMMAND_EXCERPT_CHARS = 240

const BLOCKED_COMMAND_RE = /(?:^|[;&|]\s*)(?:rm|mv|cp|mkdir|touch|chmod|chown|ln|truncate|dd|tee|apply_patch)\b/i
const GIT_BLOCKED_RE = /\bgit\s+(?:reset|clean|checkout|apply|restore|switch)\b/i
const SED_INPLACE_RE = /\bsed\b[^\n;&|]*\s-i(?:\s|$)/i
const PERL_INPLACE_RE = /\bperl\b[^\n;&|]*\s-pi(?:\s|$)/i
const WRITE_REDIRECT_RE = /(^|[\s;&|])(?:\d*)>>?\s*(?!&)\S+/
const HEREDOC_RE = /<<-?\s*\w+/
const FIND_DELETE_RE = /\bfind\b[^\n;&|]*\s-delete\b/i
const FIND_EXEC_MUTATING_RE =
  /\bfind\b[^\n;&|]*\s-exec\s+(?:rm|mv|cp|mkdir|touch|chmod|chown|ln|truncate|dd|tee|apply_patch|bash|sh|zsh|python|python3|perl|sed|git\s+(?:reset|clean|checkout|apply|restore|switch))\b/i
const CODE_CONTEXT_RE =
  /(?:^|[;&|]\s*)(?:cat|head|tail|nl|rg|grep|find|ls|tree|sed|awk)\b|\bgit\s+(?:grep|show|diff|blame)\b/i
const TRACE_ONLY_RE =
  /(?:^|[;&|]\s*)(?:pytest|tox|nox|make|cmake|xcodebuild)\b|\b(?:python|python3|uv)\s+(?:run\s+)?(?:-m\s+)?pytest\b|\b(?:npm|pnpm|yarn)\s+(?:test|install|run)\b|\b(?:go|cargo|mvn|gradle)\s+(?:test|build|install|run)\b|\bpip\s+install\b/i
const SECRET_FIELD_RE = /^(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[-_]?key|auth[-_]?token|access[-_]?token|refresh[-_]?token|token|key|secret)$/i
const SECRET_VALUE_RE = /\b(?:sk-[A-Za-z0-9_-]{20,}|m0-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~+/=-]{16,})\b/g

function sanitizeForRecord(value, key = "", depth = 0) {
  if (SECRET_FIELD_RE.test(key)) {
    return value ? "[REDACTED]" : value
  }
  if (typeof value === "string") {
    return value.replace(SECRET_VALUE_RE, "[REDACTED]")
  }
  if (!value || typeof value !== "object") {
    return value
  }
  if (depth > 8) {
    return "[MAX_DEPTH]"
  }
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeForRecord(item, "", depth + 1))
  }
  const sanitized = {}
  for (const [childKey, childValue] of Object.entries(value)) {
    sanitized[childKey] = sanitizeForRecord(childValue, childKey, depth + 1)
  }
  return sanitized
}

function summarizeOutput(output) {
  if (!output || typeof output !== "object") return {}
  return {
    keys: Object.keys(output).sort(),
    title: typeof output.title === "string" ? output.title : undefined,
    output_length: typeof output.output === "string" ? output.output.length : undefined,
    part_count: Array.isArray(output.parts) ? output.parts.length : undefined,
  }
}

async function record(event, input, output, extra = {}) {
  const path = process.env.FLOWARK_OPENCODE_PLUGIN_EVENTS
  if (!path) return
  const payload = {
    sentinel: SENTINEL,
    event,
    ts: Date.now(),
    input: sanitizeForRecord(input),
    output_summary: summarizeOutput(output),
    ...sanitizeForRecord(extra),
  }
  await appendFile(path, JSON.stringify(payload) + "\n", "utf8")
}

function contextFile(options) {
  return String(options?.contextFile || process.env.FLOWARK_OPENCODE_HOOK_CONTEXT_FILE || "").trim()
}

async function knowledgePackagingMode(options) {
  const path = contextFile(options)
  if (!path) return ""
  try {
    const payload = JSON.parse(await readFile(path, "utf8"))
    const hookContext = payload?.hook_runtime_context
    return String(hookContext?.knowledge_packaging_mode || payload?.knowledge_packaging_mode || "").trim()
  } catch {
    return ""
  }
}

async function isInitialAnalysisLogRagMode(options) {
  return (await knowledgePackagingMode(options)) === "analysis_log_rag_initial"
}

function bridgeCommand(options) {
  if (Array.isArray(options?.bridgeCommand) && options.bridgeCommand.length > 0) {
    return options.bridgeCommand.map((item) => String(item))
  }
  const python = String(options?.bridgePython || process.env.FLOWARK_OPENCODE_BRIDGE_PYTHON || "python3")
  const moduleName = String(options?.bridgeModule || process.env.FLOWARK_OPENCODE_BRIDGE_MODULE || "flowark.adapters.opencode.bridge")
  return [python, "-m", moduleName]
}

function runBridge(payload, options) {
  const command = bridgeCommand(options)
  const executable = command[0]
  const args = command.slice(1)
  const input = JSON.stringify(payload)
  return new Promise((resolve) => {
    const child = spawn(executable, args, {
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    })
    let stdout = ""
    let stderr = ""
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString()
    })
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString()
    })
    child.on("error", (error) => {
      resolve({
        action: "error",
        sentinel: SENTINEL,
        output: payload.output || {},
        delivery: {
          status: "failed",
          reason: `${error.name}: ${error.message}`,
          trace: { skip_type: "plugin_bridge_spawn_error" },
        },
        trace: { error_type: error.name, error: error.message },
      })
    })
    child.on("close", (code) => {
      const text = stdout.trim()
      if (!text) {
        resolve({
          action: "error",
          sentinel: SENTINEL,
          output: payload.output || {},
          delivery: {
            status: "failed",
            reason: `bridge exited ${code} without JSON output`,
            trace: { skip_type: "plugin_bridge_empty_output" },
          },
          trace: { exit_code: code, stderr: stderr.slice(0, 1000) },
        })
        return
      }
      try {
        resolve(JSON.parse(text))
      } catch (error) {
        resolve({
          action: "error",
          sentinel: SENTINEL,
          output: payload.output || {},
          delivery: {
            status: "failed",
            reason: `${error.name}: ${error.message}`,
            trace: { skip_type: "plugin_bridge_parse_error" },
          },
          trace: { exit_code: code, stdout: text.slice(0, 1000), stderr: stderr.slice(0, 1000) },
        })
      }
    })
    child.stdin.write(input)
    child.stdin.end()
  })
}

function textParts(parts) {
  if (!Array.isArray(parts)) return []
  return parts.filter((part) => part && part.type === "text")
}

function lastTextPart(parts) {
  const candidates = textParts(parts)
  return candidates.length > 0 ? candidates[candidates.length - 1] : undefined
}

function containsFlowArkContext(value) {
  if (Array.isArray(value)) return value.some((item) => containsFlowArkContext(item))
  if (value && typeof value === "object") {
    if (value.metadata?.flowark_context_message === true) return true
    return Object.values(value).some((item) => containsFlowArkContext(item))
  }
  const text = String(value || "")
  return text.includes(RUNTIME_CONTEXT_MARKER) || text.includes(KNOWLEDGE_CONTEXT_MARKER)
}

function commandExcerpt(command, limit = MAX_COMMAND_EXCERPT_CHARS) {
  const compact = String(command || "").trim().split(/\s+/).filter(Boolean).join(" ")
  if (compact.length <= limit) return compact
  return compact.slice(0, Math.max(0, limit - 3)).trimEnd() + "..."
}

function bashArgs(input, output) {
  if (input?.args && typeof input.args === "object") return input.args
  if (output?.args && typeof output.args === "object") return output.args
  if (output?.state?.input && typeof output.state.input === "object") return output.state.input
  return {}
}

function blockedBashReason(command) {
  if (HEREDOC_RE.test(command)) return "heredoc_not_allowed"
  if (WRITE_REDIRECT_RE.test(command)) return "write_redirection_not_allowed"
  if (FIND_DELETE_RE.test(command)) return "find_delete_not_allowed"
  if (FIND_EXEC_MUTATING_RE.test(command)) return "find_exec_mutating_command"
  if (BLOCKED_COMMAND_RE.test(command)) return "write_or_destructive_command"
  if (GIT_BLOCKED_RE.test(command)) return "git_mutating_command"
  if (SED_INPLACE_RE.test(command) || PERL_INPLACE_RE.test(command)) return "in_place_edit_command"
  return undefined
}

function classifyBashCommand(input, output) {
  const args = bashArgs(input, output)
  const command = String(args.command || "").trim()
  const base = {
    command_excerpt: commandExcerpt(command),
    workdir: args.workdir ? String(args.workdir) : undefined,
    description: args.description ? String(args.description) : undefined,
  }
  if (!command) {
    return { ...base, bash_kind: "trace_only", bash_policy_action: "trace_only", bash_policy_reason: "empty_command" }
  }
  const blockedReason = blockedBashReason(command)
  if (blockedReason) {
    return { ...base, bash_kind: "blocked", bash_policy_action: "blocked", bash_policy_reason: blockedReason }
  }
  if (CODE_CONTEXT_RE.test(command)) {
    return { ...base, bash_kind: "code_context", bash_policy_action: "allow", bash_policy_reason: "code_context_command" }
  }
  if (TRACE_ONLY_RE.test(command)) {
    return { ...base, bash_kind: "trace_only", bash_policy_action: "trace_only", bash_policy_reason: "trace_only_command" }
  }
  return {
    ...base,
    bash_kind: "trace_only",
    bash_policy_action: "trace_only",
    bash_policy_reason: "bash_command_not_code_context",
  }
}

function applyReturnedOutput(target, resultOutput) {
  if (!target || typeof target !== "object" || !resultOutput || typeof resultOutput !== "object") return
  for (const key of ["title", "output", "metadata"]) {
    if (Object.prototype.hasOwnProperty.call(resultOutput, key)) {
      target[key] = resultOutput[key]
    }
  }
}

function rememberRuntime(runtimeBySession, input, options) {
  const sessionID = String(input?.sessionID || "").trim()
  if (!sessionID) return
  const model = input?.model || {
    providerID: String(options?.providerID || "anthropic"),
    modelID: String(options?.modelID || process.env.ANTHROPIC_MODEL || "claude-sonnet-4-5"),
  }
  runtimeBySession.set(sessionID, {
    agent: String(input?.agent || options?.agent || "build"),
    model,
  })
}

function runtimeForSession(runtimeBySession, sessionID, options) {
  return (
    runtimeBySession.get(sessionID) || {
      agent: String(options?.agent || "build"),
      model: {
        providerID: String(options?.providerID || "anthropic"),
        modelID: String(options?.modelID || process.env.ANTHROPIC_MODEL || "claude-sonnet-4-5"),
      },
    }
  )
}

function sessionAffinityMode() {
  return String(process.env.FLOWARK_OPENCODE_SESSION_AFFINITY_MODE || "").trim().toLowerCase()
}

function fixedSessionAffinityValue() {
  return String(process.env.FLOWARK_OPENCODE_SESSION_AFFINITY_VALUE || "flowark-opencode-affinity-fixed").trim()
}

function queueSessionTask(queues, sessionID, task) {
  const previous = queues.get(sessionID) || Promise.resolve()
  const current = previous.then(task, task)
  queues.set(
    sessionID,
    current.catch(() => {
      /* keep the queue alive after failures */
    }),
  )
  return current
}

export const FlowArkRuntimePlugin = async (pluginInput = {}, options = {}) => {
  const client = pluginInput.client
  const deliveredRequestSubmitSessions = new Set()
  const attemptedRequestSubmitSessions = new Set()
  const runtimeBySession = new Map()
  const noReplyQueues = new Map()

  async function bridgePayload(event, input, output) {
    const payload = {
      sentinel: SENTINEL,
      event,
      input,
      output,
      context_file: contextFile(options),
    }
    if (output?.message) payload.message = output.message
    if (Array.isArray(output?.parts)) payload.parts = output.parts
    return payload
  }

  async function handleChatHeaders(input, output) {
    const mode = sessionAffinityMode()
    if (!mode || mode === "default" || mode === "session") {
      await record("chat.headers", input, output, {
        affinity_mode: mode || "default",
        session_id: input?.sessionID,
      })
      return
    }
    if (mode === "fixed") {
      const value = fixedSessionAffinityValue()
      if (value) output.headers["x-session-affinity"] = value
      await record("chat.headers", input, output, {
        affinity_mode: mode,
        session_id: input?.sessionID,
        x_session_affinity: output.headers["x-session-affinity"],
      })
      return
    }
    await record("chat.headers.skip", input, output, {
      reason: "unknown_session_affinity_mode",
      affinity_mode: mode,
      session_id: input?.sessionID,
    })
  }

  async function handleChatMessage(input, output) {
    rememberRuntime(runtimeBySession, input, options)
    await record("chat.message", input, output)
    const sessionID = String(input?.sessionID || "").trim()
    if (!contextFile(options)) {
      await record("chat.message.skip", input, output, { reason: "missing_context_file" })
      return
    }
    if (containsFlowArkContext(output)) {
      await record("chat.message.skip", input, output, { reason: "synthetic_flowark_context" })
      return
    }
    const initialAnalysisLogRag = await isInitialAnalysisLogRagMode(options)
    if (initialAnalysisLogRag && attemptedRequestSubmitSessions.has(sessionID)) {
      await record("chat.message.skip", input, output, { reason: "analysis_log_rag_initial_already_attempted" })
      return
    }
    if (deliveredRequestSubmitSessions.has(sessionID)) {
      await record("chat.message.skip", input, output, { reason: "request_submit_already_delivered" })
      return
    }
    const target = lastTextPart(output?.parts)
    if (!target) {
      await record("chat.message.skip", input, output, { reason: "missing_text_part" })
      return
    }
    const result = await runBridge(await bridgePayload("chat.message", input, output), options)
    await record("chat.message.bridge", input, output, { bridge: summarizeBridgeResult(result) })
    if (initialAnalysisLogRag && sessionID && result?.action !== "error") {
      attemptedRequestSubmitSessions.add(sessionID)
    }
    if (result?.action !== "delivered" || typeof result.text !== "string" || !result.text.trim()) return
    target.text = `${String(target.text || "").trimEnd()}\n\n${result.text.trim()}`
    deliveredRequestSubmitSessions.add(sessionID)
  }

  async function deliverNoReply(input, output, result) {
    if (!client?.session?.prompt || typeof result?.text !== "string" || !result.text.trim()) return
    const sessionID = String(input?.sessionID || "").trim()
    const runtime = runtimeForSession(runtimeBySession, sessionID, options)
    await client.session.prompt({
      path: { id: sessionID },
      body: {
        model: runtime.model,
        agent: runtime.agent,
        noReply: true,
        parts: [
          {
            type: "text",
            text: result.text.trim(),
            synthetic: true,
            metadata: {
              flowark_context_message: true,
              flowark_delivery_surface: "no_reply_context",
              flowark_tool: input?.tool,
              flowark_call_id: input?.callID,
              flowark_sentinel: SENTINEL,
            },
          },
        ],
      },
    })
    await record("tool.execute.after.no_reply", input, output, { bridge: summarizeBridgeResult(result) })
  }

  async function handleAfterTool(input, output) {
    await record("tool.execute.after", input, output)
    const tool = String(input?.tool || "").trim()
    if (!POST_TOOL_ALLOWED_TOOLS.has(tool)) {
      await record("tool.execute.after.skip", input, output, { reason: "unsupported_tool" })
      return
    }
    if (!contextFile(options)) {
      await record("tool.execute.after.skip", input, output, { reason: "missing_context_file" })
      return
    }
    const delivery = String(options?.delivery || "no_reply_context").trim()
    const task = async () => {
      const result = await runBridge(await bridgePayload("tool.execute.after", input, output), options)
      await record("tool.execute.after.bridge", input, output, { bridge: summarizeBridgeResult(result) })
      if (result?.action !== "delivered") return
      if (delivery === "tool_output_append") {
        applyReturnedOutput(output, result.output)
        return
      }
      if (delivery === "no_reply_context") {
        try {
          await deliverNoReply(input, output, result)
        } catch (error) {
          await record("tool.execute.after.no_reply_error", input, output, {
            error: `${error?.name || "Error"}: ${error?.message || String(error)}`,
            bridge: summarizeBridgeResult(result),
          })
        }
      }
    }
    const sessionID = String(input?.sessionID || "").trim()
    if (delivery === "no_reply_context" && sessionID) {
      await queueSessionTask(noReplyQueues, sessionID, task)
      return
    }
    await task()
  }

  async function handleBeforeTool(input, output) {
    await record("tool.execute.before", input, output)
    const tool = String(input?.tool || "").trim()
    if (tool !== "bash") return
    const local = classifyBashCommand(input, output)
    let bridge = undefined
    if (contextFile(options)) {
      bridge = await runBridge(await bridgePayload("tool.execute.before", input, output), options)
      await record("tool.execute.before.bridge", input, output, { bridge: summarizeBridgeResult(bridge) })
    } else {
      await record("tool.execute.before.skip", input, output, { reason: "missing_context_file", bash: local })
    }
    const blockedByBridge = bridge?.action === "blocked"
    const blocked = blockedByBridge || local.bash_policy_action === "blocked"
    if (!blocked) return
    const reason =
      bridge?.trace?.bash_policy_reason ||
      bridge?.delivery?.reason ||
      local.bash_policy_reason ||
      "bash_policy_blocked"
    await record("tool.execute.before.blocked", input, output, {
      reason,
      bash: bridge?.trace || local,
    })
    throw new Error(`FlowArk bash policy blocked command: ${reason}`)
  }

  return {
    "chat.headers": handleChatHeaders,
    "chat.message": handleChatMessage,
    "tool.execute.before": handleBeforeTool,
    "tool.execute.after": handleAfterTool,
    "experimental.chat.messages.transform": async (input, output) => {
      await record("experimental.chat.messages.transform", input, output)
    },
  }
}

function summarizeBridgeResult(result) {
  if (!result || typeof result !== "object") return {}
  return {
    action: result.action,
    event: result.event,
    delivery_surface: result.delivery_surface,
    delivery_status: result.delivery?.status,
    delivery_reason: result.delivery?.reason,
    injected_length: typeof result.text === "string" ? result.text.length : 0,
    bash_kind: result.trace?.bash_kind,
    bash_policy_action: result.trace?.bash_policy_action,
    bash_policy_reason: result.trace?.bash_policy_reason,
    command_excerpt: result.trace?.command_excerpt,
  }
}

export default FlowArkRuntimePlugin
