# Fictional alert example

`fictional-event.json` is a deterministic documentation fixture. All names, identifiers, dates and records are fictional. The `demo.example.test` asset is an example only; no request is made to it, and the fixture grants no testing authorization.

`fictional-alert.txt` is the complete single-message output of the project's `tower.render.render_event` function for that fixture. It is not a Telegram screenshot or evidence of a real program change. The README shows the same labelled fictional notification. The input fixture retains the complete policy and source record; the notification selects only a few readable fields.

To regenerate it from the repository root, use the project virtual environment's Python:

```python
import json
from pathlib import Path

from tower.render import render_event

assets = Path("docs/assets")
event = json.loads((assets / "fictional-event.json").read_text(encoding="utf-8"))
messages = render_event(event)
assert len(messages) == 1, "The example must fit one compact notification."
rendered = messages[0] + "\n"
(assets / "fictional-alert.txt").write_text(rendered, encoding="utf-8", newline="\n")
```

This only reads the local fixture and writes the local text artifact. The separate `python -m tower demo` command also checks the observation-to-journal-to-mocked-delivery workflow; its detection times vary between runs.
