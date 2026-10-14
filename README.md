# Screen Answer

Press a hotkey and Claude reads the screen under your mouse. The answer shows up in a small
comic-style window in the bottom-right corner.

It works with math (formulas are drawn from LaTeX), code (monospace blocks with a Copy button),
and questions too long for one screen (queue several screenshots and send them together).

## Requirements

- Windows 11 (Windows 10 should work; the colored title bar needs 11)
- Python 3.10 or newer
- The [Claude Code](https://marketplace.visualstudio.com/items?itemName=anthropic.claude-code)
  extension for VS Code, signed in. Screen Answer runs its `claude.exe`, so it uses your Claude
  subscription and needs no API key. A `claude` command on your PATH also works.

```
pip install -r requirements.txt
```

## Use

Double-click `screen_answer.pyw`. It runs in the background with no console window.

| Key | What it does |
| --- | --- |
| `Ctrl+Alt+Q` | Screenshot the monitor under the mouse and ask Claude |
| `Ctrl+Alt+A` | Add the current screen to a queue (for long questions); the next `Ctrl+Alt+Q` sends them all |
| `Esc` | Close the answer window (the program keeps running) |
| `Ctrl+Alt+Shift+Q` | Quit |

In the answer window:

- **Show solution + figure** opens the full working. If a picture helps (a graph, a region, a
  related-rates triangle), Claude describes it and matplotlib draws it here.
- **Rethink ↻** solves the same screenshots again with Opus at higher effort. It's slower
  (roughly 10–20 s) and marks each answer that changed. You can leave a note saying what to check.
- **Copy** copies the code if the answer has any, otherwise the answer text with its LaTeX source.

## Settings

At the top of `screen_answer.pyw`:

- `MODEL` / `EFFORT`: the default, fast path (Sonnet, high effort, no extended thinking)
- `RETHINK_MODEL` / `RETHINK_EFFORT`: the Rethink button (Opus, xhigh)
- `HOTKEYS`: the key bindings

## Privacy

Screenshots and figures stay in memory and are never written to disk, and no Claude session
history is saved. The only files it writes are an error log (`%TEMP%\screen-answer\log.txt`,
cleared on every start, capped at 100 KB) and matplotlib's font cache in
`%TEMP%\screen-answer\mpl`.

## License

MIT, see [LICENSE](LICENSE). The Bangers font in `fonts/` is by The Bangers Project Authors under the SIL Open
Font License, see [fonts/OFL.txt](fonts/OFL.txt).
