import { stat } from "node:fs/promises"
import path from "node:path"

const DEFAULT_MAX_DELETE_BYTES = 1024 * 1024
const ISSUE_URL = "https://github.com/anomalyco/opencode/issues/27657"

function deletePaths(patchText) {
  return patchText
    .split(/\r?\n/)
    .filter((line) => line.startsWith("*** Delete File: "))
    .map((line) => line.slice("*** Delete File: ".length).trim())
    .filter(Boolean)
}

function maxDeleteBytes(options) {
  if (!options || typeof options.maxDeleteBytes !== "number") {
    return DEFAULT_MAX_DELETE_BYTES
  }
  if (!Number.isFinite(options.maxDeleteBytes) || options.maxDeleteBytes < 0) {
    return DEFAULT_MAX_DELETE_BYTES
  }
  return options.maxDeleteBytes
}

export const OpenShrimpApplyPatchLargeDeleteGuard = async ({ directory }, options) => {
  const limit = maxDeleteBytes(options)

  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "apply_patch") return

      const patchText = output.args?.patchText
      if (typeof patchText !== "string") return

      const oversized = []
      for (const file of deletePaths(patchText)) {
        const filePath = path.isAbsolute(file) ? file : path.resolve(directory, file)
        try {
          const info = await stat(filePath)
          if (info.isFile() && info.size > limit) {
            oversized.push(`${filePath} (${(info.size / 1024 / 1024).toFixed(1)} MiB)`)
          }
        } catch {
          // Let apply_patch handle missing-file errors normally.
        }
      }

      if (!oversized.length) return

      throw new Error([
        "Blocked apply_patch Delete File for large file(s).",
        "Use Bash rm for generated/binary artifacts instead.",
        `Upstream issue: ${ISSUE_URL}`,
        ...oversized.map((file) => `- ${file}`),
      ].join("\n"))
    },
  }
}

export default OpenShrimpApplyPatchLargeDeleteGuard
