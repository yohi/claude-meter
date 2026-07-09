# claude-meter

Local-only analyzer for ClaudeCode usage and estimated AWS Bedrock cost.

## Quick start

```bash
pip install -e .
claude-meter init
claude-meter collect
claude-meter ui
```

All data is stored in `~/.claude-meter/`. The only external network call is to refresh Bedrock pricing; everything else stays on your machine.
