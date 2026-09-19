# pedro-dev-toolkit

Personal Claude Code plugin containing four existing workflow skills:

- `context-builder`
- `progress-report`
- `reverse-prompt`
- `verify-plan`

The plugin is also a local marketplace. Add this directory as a marketplace,
then install `pedro-dev-toolkit` from it:

```text
/plugin marketplace add ~/Plugins/pedro-dev-toolkit
/plugin install pedro-dev-toolkit@pedro-dev-toolkit-local
```

The source skill instructions are preserved. Generated audit state, runtime
logs, and bytecode are intentionally omitted from the package. The
`context-builder` rollback snapshot is retained because its bundled validator
requires it.
