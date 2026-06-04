# @openshrimp/opencode-apply-patch-large-delete-guard

OpenCode plugin that blocks `apply_patch` `*** Delete File` operations on large files before OpenCode computes a textual diff.

This avoids hangs when generated binaries or other large artifacts are deleted through `apply_patch`. Agents should use shell deletion commands such as `rm` for those files instead.

Upstream issue: https://github.com/anomalyco/opencode/issues/27657

## Configuration

```json
{
  "plugin": ["@openshrimp/opencode-apply-patch-large-delete-guard"]
}
```

Optional threshold override:

```json
{
  "plugin": [
    [
      "@openshrimp/opencode-apply-patch-large-delete-guard",
      { "maxDeleteBytes": 1048576 }
    ]
  ]
}
```
