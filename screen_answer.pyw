"""Screen Answer: press Ctrl+Alt+Q to screenshot the monitor under the mouse, send it to Claude,
and show the answer in a small window at the bottom-right.

Start:  double-click this file (runs in the background, no console window).
Long question: Ctrl+Alt+A queues the current screen; scroll, repeat, then Ctrl+Alt+Q sends
               the queued screenshots plus the current screen together.
Quit:   Ctrl+Alt+Shift+Q.
Close the answer window: Esc or the X (the program keeps running).
Math:   Claude writes formulas in LaTeX ($...$); they are drawn inline with matplotlib's mathtext
        (a LaTeX subset, Computer Modern font). One it can't draw is shown as its source.
Code:   ``` blocks show in a monospace font, unwrapped (a scrollbar appears for long lines).
        Copy puts the answer's code on the clipboard (or the answer text, with LaTeX source).
The window never grows taller than the screen; long answers get a scrollbar instead.
Look:   comic book (VALORANT's RadiantCrisis 001 plus Spider-Punk collage). The Bangers font in
        fonts/ (SIL Open Font License, see fonts/OFL.txt) is loaded for this program only, not
        installed.
Figures: when a picture helps (sketch a graph, a region, a related-rates triangle), Claude adds a
         small JSON description and the program draws it with matplotlib. It shows up under
         "Show solution + figure".
Rethink: if an answer looks wrong, the "Rethink ↻" button solves the same screenshots again with
         Opus at xhigh effort (much slower) and marks each answer that changed with "(changed)".
         It first opens a box for an optional note telling Opus what to check (Enter to start).

Calls claude.exe from the VS Code Claude Code extension, so it uses your Claude subscription
(no API key). User settings (hooks, default model) are not loaded and no tools are enabled.

Leaves nothing behind: screenshots and figures stay in memory and are never written to disk, no
Claude session history is saved, and the error log (%TEMP%\\screen-answer\\log.txt) is cleared on
every start and capped at 100 KB. matplotlib keeps one font cache file in
%TEMP%\\screen-answer\\mpl; it is reused, not added to. (claude.exe also rotates backups of
~/.claude.json in ~/.claude/backups, but it keeps only the latest 5 itself, so they don't pile up.)
"""
import ast
import base64
import ctypes
import ctypes.wintypes as wt
import glob
import io
import itertools
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from tkinter import messagebox

from PIL import ImageGrab

MODEL = "sonnet"  # "opus" is more accurate but slower
EFFORT = "high"  # no hidden thinking: the written work comes before the answers and does that job
TIMEOUT_SECS = 240
# The Rethink button: much slower, so only when the user asks for it
RETHINK_MODEL = "opus"
RETHINK_EFFORT = "xhigh"  # "max" thought for over 300 s on a real calculus problem
RETHINK_TIMEOUT_SECS = 300
MAX_IMAGE_BYTES = 2_000_000
MAX_SHOTS = 6
QUEUE_TTL_SECS = 300

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
VK_A, VK_Q = 0x41, 0x51
HOTKEYS = {  # id: (event, modifiers, key, label)
    1: ("answer", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_Q, "Ctrl+Alt+Q"),
    2: ("quit", MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_Q, "Ctrl+Alt+Shift+Q"),
    3: ("add", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_A, "Ctrl+Alt+A"),
}
WM_HOTKEY = 0x0312

WORK_DIR = os.path.join(tempfile.gettempdir(), "screen-answer")
LOG_PATH = os.path.join(WORK_DIR, "log.txt")
MAX_LOG_BYTES = 100_000

ANSWER_MARKER = "===ANSWERS==="
FIGURE_MARKER = "===FIGURE==="
# Raw string: the LaTeX below has backslashes (\frac would otherwise turn into a form feed)
SYSTEM_PROMPT = rf"""You solve problems shown in a screenshot, usually from an online calculus course, sometimes programming questions.

Reply in English with this exact layout:

<work>
{ANSWER_MARKER}
<final answers>
{FIGURE_MARKER}
<figure JSON>

Work the problem first, then write the final answers. The final answers must be exactly what your work concluded. If you find a mistake while working, fix it in the work before you write {ANSWER_MARKER}; never correct or revise anything after {ANSWER_MARKER}. Write the {ANSWER_MARKER} line exactly once. The {FIGURE_MARKER} section is optional and always last.

Work (before {ANSWER_MARKER}):
- The key steps in a few lines per sub-question, then one line that checks the answers (derivative formula, plugging back in, etc.).
- Multiple choice: the choices and their letters can differ between sub-questions, so read the choices for each sub-question separately and name the letter that matches your conclusion.
- For estimates read off a graph, state the values you read and an acceptable range.

Final answers (after {ANSWER_MARKER}), no explanation:
- The first line after {ANSWER_MARKER} is an answer: no heading, no label like "Answer:" or "Part 1", no blank line.
- One line per blank or sub-question, labeled so it is clear which blank it fills (e.g. "a) $t = 4$", "$t = 4.9$ to $5$: $v_{{avg}} = 39.89$").
- Use exactly the format the question asks for: reduced fractions, number of decimal places, units, sign.
- Multiple choice: give the letter and the choice text.
- Programming question: the complete code to submit, in one ``` block, or the exact output if the question asks what the code prints.
- If the screenshot already shows answers filled in or selected, mark each one: "correct", or "wrong -> <right answer>".
- If no problem is visible, or a key part is cut off or unreadable, say so and do not guess.

Figure (after {FIGURE_MARKER}), only when a picture is needed:
- Include one when the question asks you to sketch, draw, graph or plot something, to pick or place something on a graph, or when a picture is much clearer than words (the region for an area or volume, a related-rates triangle). Otherwise leave out the {FIGURE_MARKER} line and the JSON entirely.
- When the answer is a graph, the final answers still list what to enter or look for (intercepts, asymptotes, extrema, points to click).
- Exactly one JSON object, no code fence, no comments:
  {{"x": [-5, 5], "y": [-4, 6], "title": "optional", "items": [...]}}
  "x" and "y" are the visible ranges; give both. Add "axes": false for a diagram without coordinate axes (geometry, related rates); it is drawn to scale.
- Item types:
  {{"type": "function", "expr": "x^2 - 4", "from": -3, "to": 3, "label": "f(x)"}}  from/to are optional; use several with from/to for a piecewise function.
  {{"type": "point", "at": [2, 0], "open": false, "label": "(2, 0)"}}  open: true for a hole.
  {{"type": "vline", "x": 1, "label": "x = 1"}} and {{"type": "hline", "y": 0}}  dashed, for asymptotes.
  {{"type": "line", "points": [[0, 0], [3, 0], [3, 4]], "closed": true, "fill": false, "label": "..."}}  a segment, tangent line, polyline or polygon.
  {{"type": "shade", "expr": "x + 2", "expr2": "x^2", "from": -1, "to": 2, "label": "R"}}  the region between expr and expr2 (the x-axis if expr2 is left out).
  {{"type": "text", "at": [1.5, 2], "text": "5 ft"}}
  Any item may add "color" (e.g. "red") or "style": "dashed".
- expr uses only x, numbers, + - * / ^ and parentheses, with explicit * (2*x, not 2x), and sqrt cbrt abs exp ln log10 sin cos tan sec csc cot asin acos atan sinh cosh tanh pi e. ln is the natural log.
- Keep labels short. Math in a label goes inside $...$ as elsewhere, but inside the JSON every backslash must be doubled, e.g. "$y = e^{{-x}}$", "$y = \\frac{{1}}{{x}}$".

Example reply:
a) $-3t^{{2}} + 48 = 0$, so $t^{{2}} = 16$ and $t = 4$ ($t = -4$ is rejected).
b) $v(t) = s'(t) = -6t$, so $v(4) = -24$.
Check: $v$ is negative because the ball falls, as expected.
{ANSWER_MARKER}
a) $t = 4$ s
b) $v_{{1}} = -24$ ft/s

Example reply with a figure:
$f(x) = \frac{{x^{{2}} - 4}}{{x - 1}}$. Zeros where $x^{{2}} = 4$; the denominator is $0$ at $x = 1$.
Long division: $x^{{2}} - 4 = (x - 1)(x + 1) - 3$, so $f(x) = x + 1 - \frac{{3}}{{x - 1}}$.
Check: $f(0) = \frac{{-4}}{{-1}} = 4$, the y-intercept.
{ANSWER_MARKER}
x-intercepts: $(-2, 0)$, $(2, 0)$
vertical asymptote: $x = 1$
slant asymptote: $y = x + 1$
{FIGURE_MARKER}
{{"x": [-6, 6], "y": [-10, 10], "items": [{{"type": "function", "expr": "(x^2 - 4)/(x - 1)", "label": "f(x)"}}, {{"type": "vline", "x": 1, "label": "x = 1"}}, {{"type": "function", "expr": "x + 1", "style": "dashed", "color": "gray", "label": "y = x + 1"}}, {{"type": "point", "at": [-2, 0], "label": "(-2, 0)"}}, {{"type": "point", "at": [2, 0], "label": "(2, 0)"}}]}}

Rules:
- Write every formula, expression, equation and variable in LaTeX between single dollar signs, inline, e.g. $f'(x) = 3x^{{2}} \sin x$ or $\frac{{dh}}{{dt}} = -\frac{{1}}{{4\pi}} \approx -0.080$. Words stay outside. Each formula must fit on one line: no $$, \[ \], \begin environments (cases, align) or \displaystyle.
- Available in formulas: \frac, \sqrt{{}}, ^{{}}, _{{}}, \lim_{{x \to a}}, \int_{{a}}^{{b}}, \sum, \infty, \leq, \geq, \neq, \approx, \pm, \cdot, \left( \right), Greek letters, \sin \cos \tan \ln \log, and \text{{}} for a word inside a formula. Write a dollar amount as \$5.
- Code goes in ``` blocks with the language (```python), written exactly as it should be typed; a name from the code inside a sentence goes in `backticks`. No LaTeX or $ math inside code or backticks, and use backticks, not $...$, for code names.
- No Markdown apart from code (no #, **, tables).
- A score shown on the page (e.g. "0 of 2 points") may just mean it is not answered yet. Do not use it to judge answers."""
RETHINK_PROMPT = """A faster model already answered, and the user suspects a mistake. Its final answers were:

{previous}

Solve the problem again from scratch, without assuming those answers are right or wrong. Reread the screenshot closely: every number and sign, which choices belong to which sub-question, and the exact answer format asked for. Check each result a second, independent way. In the work, say which earlier answers were wrong and why, if any. In the final answers, add " (changed)" to the end of each line whose answer differs from the earlier one; for code, put "(changed)" on its own line after the ``` block, never inside it."""
RETHINK_NOTE_PROMPT = """The user added this note about what to check or focus on:

{note}

Follow what it says about the task itself: what the question means, the answer format wanted, which part to look at hardest. If it says an answer is right or wrong, or states a fact, check that yourself instead of taking it on faith, and say in the work whether you agree."""
# Also matches a layout placeholder echoed back as a tag, like "<work>" or "</work>"
HEADING_RE = re.compile(r"^\s*(part\s*\d+|final answers?|answers?|solution|work|figure|</?[a-z ]+>)"
                        r"\s*[:.]?\s*$", re.IGNORECASE)

FONT_UI = ("Segoe UI", 10)
FONT_ANSWER = ("Segoe UI Semibold", 12)
FONT_SOLUTION = ("Segoe UI", 11)
FONT_CODE = ("Consolas", 10)

# The look: RadiantCrisis 001 (VALORANT's comic-book skins: thick ink, halftone dots, sound-effect
# bursts) with Spider-Punk accents (ransom-note letters, highlighter pink, a title that twitches at
# a low frame rate while busy). The text boxes stay plain so answers read cleanly.
PAPER, INK, WHITE = "#FFF6DC", "#111111", "#FFFFFF"
YELLOW, RED, CYAN, PINK = "#FFD23F", "#E63946", "#35A7FF", "#FF3EA5"
BURST_TEXT = {RED: YELLOW, YELLOW: RED, CYAN: WHITE}  # sound-effect lettering on each burst color
SOLUTION_BG = "#FFFDF4"
CODE_BG = "#F1ECDD"
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
COMIC_FAMILY = "Impact"  # Bangers once load_comic_font() has run
RANSOM_FONTS = ["Bangers", "Impact", "Arial Black", "Georgia", "Courier New", "Times New Roman",
                "Comic Sans MS", "Segoe UI Black"]
RANSOM_PAPERS = [(WHITE, INK), (YELLOW, INK), (PINK, WHITE), (CYAN, INK), (INK, WHITE), (RED, WHITE)]
TEXT_WIDTH_CHARS = 44
MAX_ANSWER_LINES = 14
SOLUTION_LINES = 16
SOLUTION_LINES_WITH_FIGURE = 8  # the figure takes the rest of the height
# How far each box may shrink so the window fits on the screen (both boxes scroll)
MIN_SOLUTION_LINES = 4
MIN_ANSWER_LINES = 3
CODE_RE = re.compile(r"```[^\n]*\n(.*?)(?:\n?```|\Z)", re.S)  # a ``` block; an unclosed one runs to the end
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# $...$ on one line, by pandoc's rule so prices like "$5 and $6" stay text: no space just inside
# either $, and no digit right after the closing one. \$ is a literal dollar.
MATH_RE = re.compile(r"(?<!\\)\$(?=\S)([^$\n]+?)(?<=[^\s\\])\$(?!\d)")
MATH_SCALE = 1.1  # Computer Modern looks smaller than Segoe UI at the same point size

FIGURE_ASPECT = 0.72  # height / width
FIGURE_SAMPLES = 1500
MAX_FIGURE_ITEMS = 40
FIGURE_COLORS = ["#1F6FEB", RED, "#2A9D8F", "#F4A261", "#7B2CBF", PINK]
EXPR_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Constant, ast.Load,
              ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd)
# matplotlib keeps its font cache here instead of ~/.matplotlib
os.environ["MPLCONFIGDIR"] = os.path.join(WORK_DIR, "mpl")

user32 = ctypes.windll.user32


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]


def enable_dpi_awareness():
    # Use physical pixels so the screenshot rectangle matches the monitor
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


def log(text):
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
        os.remove(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")


def monitor_under_cursor():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    user32.GetMonitorInfoW(user32.MonitorFromPoint(pt, 2), ctypes.byref(info))
    return info.rcMonitor, info.rcWork


def grab_png(rect):
    img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
    while True:
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            break
        img = img.resize((img.width * 3 // 4, img.height * 3 // 4))
    return buf.getvalue()


def find_claude():
    hits = glob.glob(os.path.expanduser(
        r"~\.vscode\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe"))
    if hits:
        return max(hits, key=os.path.getmtime)
    return shutil.which("claude")


def ask_claude(pngs, previous=None, note=""):
    # previous: the answers to double-check, for a rethink with the slower model.
    # note: what the user typed for that rethink, if anything.
    exe = find_claude()
    if not exe:
        raise RuntimeError("Can't find claude.exe (the VS Code Claude Code extension).")
    rethink = previous is not None
    if len(pngs) == 1:
        prompt = "Solve the problem(s) in this screenshot."
    else:
        prompt = (f"These {len(pngs)} screenshots are consecutive parts of the same page, in order "
                  "(scrolled down). Content may overlap between them. Solve the problem(s).")
    if rethink:
        prompt += "\n\n" + RETHINK_PROMPT.format(previous=previous)
        if note:
            prompt += "\n\n" + RETHINK_NOTE_PROMPT.format(note=note)
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(png).decode()}}
               for png in pngs]
    message = {"type": "user", "message": {"role": "user",
                                           "content": content + [{"type": "text", "text": prompt}]}}
    cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--model", RETHINK_MODEL if rethink else MODEL,
           "--effort", RETHINK_EFFORT if rethink else EFFORT, "--system-prompt", SYSTEM_PROMPT,
           "--setting-sources", "project", "--tools", "", "--strict-mcp-config",
           "--disable-slash-commands", "--no-session-persistence"]
    if rethink:
        cmd += ["--settings", json.dumps({"alwaysThinkingEnabled": True})]
    timeout = RETHINK_TIMEOUT_SECS if rethink else TIMEOUT_SECS
    try:
        proc = subprocess.run(cmd, input=json.dumps(message) + "\n", capture_output=True,
                              text=True, encoding="utf-8", cwd=WORK_DIR, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        # The default message repeats the whole command line, system prompt included
        raise RuntimeError(f"No reply after {timeout}s, so it was stopped. It was either thinking "
                           "too long or lost its connection (after sleep, say). Try again.") from None
    result = None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            result = event
    if result is None:
        raise RuntimeError(f"Claude returned no result (exit {proc.returncode}).\n{proc.stderr[-800:]}")
    if result.get("is_error"):
        raise RuntimeError(str(result.get("result") or result))
    return (result.get("result") or "").strip()


def split_reply(reply):
    # The answers come after the work, so they are written once the model has finished working and
    # can't be contradicted by it. Drop "Part 1:" style headings; if the marker shows up more than
    # once, the last non-empty section is the model's final word and the rest is the solution.
    sections = []
    for section in reply.split(ANSWER_MARKER):
        # Lines inside ``` code blocks are never headings, even "</div>" or "Solution"
        lines, in_code = [], False
        for line in section.strip().splitlines():
            if line.lstrip().startswith("```"):
                in_code = not in_code
            elif not in_code and HEADING_RE.match(line):
                continue
            lines.append(line)
        text = "\n".join(lines).strip()
        if text:
            sections.append(text)
    if not sections:
        return reply.strip(), ""
    return sections[-1], "\n\n".join(sections[:-1])


def split_figure(reply):
    # The figure should come last, but if the model puts it before the answers, keep the answers
    body, marker, figure = reply.partition(FIGURE_MARKER)
    if not marker:
        return reply, None
    figure, marker, answers = figure.partition(ANSWER_MARKER)
    return body + marker + answers, figure


def parse_figure(text):
    # The outermost {...}, which also skips a ```json fence if the model adds one anyway
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object after the figure marker")
    raw = text[start:end + 1]
    try:
        spec = json.loads(raw)
    except ValueError:
        # A stray LaTeX command like "\sqrt" is an invalid JSON escape; keep its backslash literally
        spec = json.loads(re.sub(r'\\(?![\\"/bfnrtu])', r"\\\\", raw))
    if not isinstance(spec, dict) or not isinstance(spec.get("items"), list) or not spec["items"]:
        raise ValueError("figure has no items")
    return spec


def math_names(np):
    return {"pi": np.pi, "e": np.e, "sqrt": np.sqrt, "cbrt": np.cbrt, "abs": np.abs, "exp": np.exp,
            "ln": np.log, "log": np.log, "log10": np.log10, "log2": np.log2,
            "sin": np.sin, "cos": np.cos, "tan": np.tan, "sec": lambda t: 1 / np.cos(t),
            "csc": lambda t: 1 / np.sin(t), "cot": lambda t: 1 / np.tan(t),
            "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan,
            "arcsin": np.arcsin, "arccos": np.arccos, "arctan": np.arctan,
            "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
            "floor": np.floor, "ceil": np.ceil, "sign": np.sign}


def compile_expr(expr, names):
    # The expression comes from the model, so only arithmetic on x and the functions in names get
    # through to eval. Numbers become floats so something like 10^10^10 overflows instead of hanging.
    tree = ast.parse(str(expr).replace("^", "**"), mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, EXPR_NODES):
            raise ValueError(f"not allowed in a figure expression: {expr!r}")
        if isinstance(node, ast.Name) and node.id != "x" and node.id not in names:
            raise ValueError(f"unknown name {node.id!r} in {expr!r}")
        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, float):
                raise ValueError(f"not a number: {node.value!r} in {expr!r}")
            node.value = float(node.value)
    code = compile(tree, "<figure>", "eval")
    return lambda x: eval(code, {"__builtins__": {}, **names, "x": x})


def _sample(fn, xs, np):
    with np.errstate(all="ignore"):
        ys = np.broadcast_to(np.asarray(fn(xs), dtype=float), xs.shape).copy()
    ys[~np.isfinite(ys)] = np.nan
    return ys


def _break_jumps(ys, ylim, np):
    # Don't let the plot join the two sides of a vertical asymptote with a vertical line
    span = ylim[1] - ylim[0]
    ys = ys.copy()
    ys[(ys < ylim[0] - span) | (ys > ylim[1] + span)] = np.nan
    ys[1:][np.abs(np.diff(ys)) > span] = np.nan
    return ys


def _range(value):
    if isinstance(value, list) and len(value) == 2:
        lo, hi = float(value[0]), float(value[1])
        if math.isfinite(lo) and math.isfinite(hi) and lo < hi:
            return lo, hi
    return None


def _item_values(item, axis):
    # Every coordinate an item mentions, to size the view when the model gave no range
    points = [item["at"]] if "at" in item else list(item.get("points", []))
    values = [p[axis] for p in points]
    values += [item[k] for k in (("from", "to", "x") if axis == 0 else ("y",)) if k in item]
    return [float(v) for v in values]


def _pad(values, default):
    values = [v for v in values if math.isfinite(v)]
    if not values:
        return default
    lo, hi = min(values), max(values)
    margin = (hi - lo) * 0.1 or 1.0
    return lo - margin, hi + margin


def _draw_figure(spec, width_px, dpi):
    import numpy as np
    from matplotlib.figure import Figure

    names = math_names(np)
    items = [item for item in spec["items"][:MAX_FIGURE_ITEMS] if isinstance(item, dict)]
    xlim = _range(spec.get("x")) or _pad([v for it in items for v in _item_values(it, 0)], (-10, 10))

    # Sample every curve once; the samples also give a y range when the model left it out
    curves = {}
    for i, item in enumerate(items):
        if item.get("type") in ("function", "shade"):
            xs = np.linspace(max(float(item.get("from", xlim[0])), xlim[0]),
                             min(float(item.get("to", xlim[1])), xlim[1]), FIGURE_SAMPLES)
            exprs = [item["expr"]] + ([item.get("expr2", "0")] if item["type"] == "shade" else [])
            curves[i] = xs, [_sample(compile_expr(e, names), xs, np) for e in exprs]
    ylim = _range(spec.get("y"))
    if ylim is None:
        values = [v for it in items for v in _item_values(it, 1)]
        for _, ys_list in curves.values():
            for ys in ys_list:
                finite = ys[np.isfinite(ys)]
                if finite.size:
                    values += list(np.percentile(finite, [2, 98]))
        ylim = _pad(values, (-10, 10))

    width_in = width_px / dpi
    fig = Figure(figsize=(width_in, width_in * FIGURE_ASPECT), dpi=dpi, layout="constrained")
    ax = fig.add_subplot()
    colors = itertools.cycle(FIGURE_COLORS)
    for i, item in enumerate(items):
        kind = item.get("type")
        label = str(item["label"]) if item.get("label") else None
        dash = "--" if item.get("style") == "dashed" else "-"
        if kind == "function":
            xs, (ys,) = curves[i]
            ax.plot(xs, _break_jumps(ys, ylim, np), color=item.get("color") or next(colors),
                    lw=2.5, ls=dash, label=label)
        elif kind == "shade":
            xs, (ys, ys2) = curves[i]
            ax.fill_between(xs, ys, ys2, color=item.get("color") or next(colors), alpha=0.25,
                            lw=0, label=label)
        elif kind == "point":
            x, y = map(float, item["at"])
            color = item.get("color") or "black"
            ax.plot([x], [y], "o", ms=6, mew=1.5, color=color,
                    mfc="white" if item.get("open") else color, zorder=5)
            if label:
                ax.annotate(label, (x, y), xytext=(5, 5), textcoords="offset points", fontsize=8)
        elif kind in ("vline", "hline"):
            draw, key = (ax.axvline, "x") if kind == "vline" else (ax.axhline, "y")
            draw(float(item[key]), color=item.get("color") or "gray", lw=1.2,
                 ls="-" if item.get("style") == "solid" else "--", label=label)
        elif kind == "line":
            points = [tuple(map(float, p)) for p in item["points"]]
            if item.get("closed"):
                points.append(points[0])
            xs, ys = zip(*points, strict=False)
            color = item.get("color") or next(colors)
            ax.plot(xs, ys, color=color, lw=2.5, ls=dash, label=label)
            if item.get("fill"):
                ax.fill(xs, ys, color=color, alpha=0.2)
        elif kind == "text":
            x, y = map(float, item["at"])
            ax.text(x, y, str(item["text"]), ha="center", va="center", fontsize=9,
                    color=item.get("color") or "black", zorder=6,
                    bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.8, "pad": 1})
        else:
            log(f"Figure: skipped unknown item {item!r}")

    if spec.get("axes", True) is False:
        ax.set_axis_off()
        ax.set_aspect("equal", adjustable="datalim")
    else:
        ax.grid(True, alpha=0.3)
        ax.axhline(0, color="black", lw=0.8, zorder=1)
        ax.axvline(0, color="black", lw=0.8, zorder=1)
        ax.tick_params(labelsize=8)
        for spine in ax.spines.values():  # an inked panel edge, like the window's
            spine.set_linewidth(1.6)
            spine.set_color(INK)
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    if spec.get("title"):
        ax.set_title(str(spec["title"]), fontsize=10)
    if ax.get_legend_handles_labels()[0]:
        ax.legend(fontsize=8, framealpha=0.85)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    return buf.getvalue()


def render_figure(spec, width_px, dpi):
    from matplotlib import rc_context
    try:
        return _draw_figure(spec, width_px, dpi)
    except ValueError:
        # Usually a label with $...$ that matplotlib can't parse: draw labels as plain text instead
        with rc_context({"text.parse_math": False}):
            return _draw_figure(spec, width_px, dpi)


def render_math(latex, size_pt, dpi, big_fractions=False):
    from matplotlib import mathtext, rc_context
    from matplotlib.font_manager import FontProperties
    # mathtext knows \leq and \geq but not the \le and \ge shorthands, and has no \displaystyle
    latex = re.sub(r"\\(le|ge)(?![a-zA-Z])", r"\\\1q", latex).replace(r"\displaystyle", "")
    if big_fractions:
        latex = re.sub(r"\\frac(?![a-zA-Z])", r"\\dfrac", latex)
    buf = io.BytesIO()
    with rc_context({"mathtext.fontset": "cm", "savefig.transparent": True}):
        mathtext.math_to_image(f"${latex}$", buf, prop=FontProperties(size=size_pt * MATH_SCALE),
                               dpi=dpi, format="png")
    return buf.getvalue()


def _math_parts(text, size_pt, dpi, big_fractions):
    # Plain runs and $...$ formulas. A formula mathtext can't draw is shown as written, dollar
    # signs included, so nothing is lost.
    parts, pos = [], 0
    for m in MATH_RE.finditer(text):
        parts.append(("text", text[pos:m.start()]))
        try:
            parts.append(("math", render_math(m[1], size_pt, dpi, big_fractions)))
        except Exception as e:
            log(f"Formula failed: {m[1]!r}: {e!r}")
            parts.append(("text", m[0]))
        pos = m.end()
    parts.append(("text", text[pos:]))
    return [(kind, value.replace("\\$", "$") if kind == "text" else value) for kind, value in parts]


def render_rich(text, size_pt, dpi, big_fractions=False):
    # [(kind, value)] with kind "text", "math" (a PNG), "code" (a ``` block, one line per line of
    # code) or "icode" (`inline code`). Code is cut out first, so a $ in code never becomes a
    # formula; text without backticks gets exactly the same math handling as before.
    parts = []
    for i, chunk in enumerate(CODE_RE.split(text)):
        if i % 2:
            if parts and not (parts[-1][0] == "text" and parts[-1][1].endswith("\n")):
                parts.append(("text", "\n"))  # a block starts on its own line
            # The block's own newline lets its background run to the right edge
            parts.append(("code", chunk + "\n"))
            continue
        if i and chunk.startswith("\n"):
            chunk = chunk[1:]  # that newline ended the code block already
        for j, piece in enumerate(INLINE_CODE_RE.split(chunk)):
            if piece:
                parts += ([("icode", piece)] if j % 2
                          else _math_parts(piece, size_pt, dpi, big_fractions))
    return [(kind, value) for kind, value in parts if value]


def hotkey_loop(events):
    # RegisterHotKey and the message loop must run on the same thread
    for hotkey_id, (_, modifiers, key, label) in HOTKEYS.items():
        if not user32.RegisterHotKey(None, hotkey_id, modifiers, key):
            events.put(("fatal", f"Couldn't register {label}. The program may already be "
                                 "running, or another app is using that shortcut."))
            return
    events.put(("ready", None))
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam in HOTKEYS:
            events.put((HOTKEYS[msg.wParam][0], None))


def load_comic_font():
    # Loaded privately for this process only, so nothing gets installed; Impact if it's missing
    global COMIC_FAMILY
    path = os.path.join(FONTS_DIR, "Bangers-Regular.ttf")
    if os.path.exists(path) and ctypes.windll.gdi32.AddFontResourceExW(path, 0x10, 0):  # FR_PRIVATE
        COMIC_FAMILY = "Bangers"


def color_title_bar(window, caption, text, border):
    # Windows 11 only; older versions ignore these attributes and keep the normal title bar
    hwnd = user32.GetParent(window.winfo_id())
    for attr, rgb in ((35, caption), (36, text), (34, border)):  # DWMWA_CAPTION/TEXT/BORDER_COLOR
        colorref = ctypes.c_int(int(rgb[5:7], 16) << 16 | int(rgb[3:5], 16) << 8 | int(rgb[1:3], 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(colorref), 4)


def lighten(rgb, amount=0.35):
    r, g, b = (int(rgb[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{int(c + (255 - c) * amount):02x}" for c in (r, g, b))


def outlined_text(canvas, x, y, text, font, fill, outline, width, **kw):
    # Canvas text can't be stroked, so the ink outline is the text drawn around it 8 times
    for dx in (-width, 0, width):
        for dy in (-width, 0, width):
            if dx or dy:
                canvas.create_text(x + dx, y + dy, text=text, font=font, fill=outline, **kw)
    canvas.create_text(x, y, text=text, font=font, fill=fill, **kw)


def rotated_rect(cx, cy, w, h, angle):
    # Corners of a w x h rectangle turned counterclockwise on screen, like canvas text's angle
    a = math.radians(angle)
    return [v for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))
            for v in (cx + dx * math.cos(a) + dy * math.sin(a), cy - dx * math.sin(a) + dy * math.cos(a))]


class ComicButton(tk.Canvas):
    # A caption-box button: ink outline and a hard shadow, and it drops onto the shadow when
    # pressed. config(text=..., state=...) and cget work like a tk.Button's.
    def __init__(self, parent, text, command, color, scale, fg=INK):
        super().__init__(parent, bg=PAPER, highlightthickness=0, bd=0, cursor="hand2")
        self.command, self.color, self.fg = command, color, fg
        self.text, self.state, self.hover, self.pressed = text, "normal", False, False
        self.font = tkfont.Font(root=self, family=COMIC_FAMILY, size=12)
        self.shadow, self.line = max(2, int(3 * scale)), max(2, int(1.5 * scale))
        self.padx, self.pady = int(7 * scale), int(2 * scale)
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def configure(self, cnf=None, **kw):
        self.text = kw.pop("text", self.text)
        self.state = kw.pop("state", self.state)
        if cnf or kw:
            super().configure(cnf, **kw)
        self._draw()

    config = configure

    def cget(self, key):
        return {"text": self.text, "state": self.state}[key] if key in ("text", "state") \
            else super().cget(key)

    def _set(self, **flags):
        if self.state != "disabled":
            self.__dict__.update(flags)
            self._draw()

    def _release(self, event):
        clicked = self.pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._set(pressed=False)
        if clicked and self.state != "disabled":
            self.command()

    def _draw(self):
        self.delete("all")
        w = self.font.measure(self.text) + 2 * self.padx
        h = self.font.metrics("linespace") + 2 * self.pady
        s, edge = self.shadow, self.line
        super().configure(width=w + s + edge, height=h + s + edge)
        off = s if self.pressed else 0
        disabled = self.state == "disabled"
        if not self.pressed:
            self.create_rectangle(edge + s, edge + s, w + s, h + s, fill=INK, outline="")
        fill = "#E4DCC6" if disabled else lighten(self.color) if self.hover else self.color
        self.create_rectangle(edge + off, edge + off, w + off, h + off, fill=fill, outline=INK,
                              width=edge)
        self.create_text(edge / 2 + off + w / 2, edge / 2 + off + h / 2, text=self.text,
                         font=self.font, fill="#8A8170" if disabled else self.fg)


class Panel(tk.Canvas):
    # A comic panel: its content framed in thick ink with a hard, unblurred shadow. The content
    # goes in .inner; sync() sizes the panel to it before the window is measured.
    def __init__(self, parent, scale, fill):
        super().__init__(parent, bg=PAPER, highlightthickness=0, bd=0, width=1, height=1)
        self.border, self.shadow = max(2, int(3 * scale)), int(6 * scale)
        self.inner = tk.Frame(self, bg=fill)
        self.item = self.create_window(self.border, self.border, window=self.inner, anchor="nw")
        self.bind("<Configure>", lambda e: self._draw(e.width, e.height))

    def sync(self):
        b, s = self.border, self.shadow
        w = self.inner.winfo_reqwidth() + 2 * b + s
        h = self.inner.winfo_reqheight() + 2 * b + s
        self.configure(width=w, height=h)
        self._draw(max(w, self.winfo_width()), h)

    def _draw(self, w, h):
        b, s = self.border, self.shadow
        self.delete("frame")
        self.create_rectangle(s, s, w, h, fill=INK, outline="", tags="frame")
        self.create_rectangle(0, 0, w - s, h - s, fill=INK, outline="", tags="frame")
        # The content stretches with the panel when the window is wider than it needs
        self.itemconfigure(self.item, width=max(1, w - s - 2 * b), height=max(1, h - s - 2 * b))


class Masthead(tk.Canvas):
    # The title as ransom-note letters cut from different papers.
    # shuffle() picks new papers and fonts (punk doesn't do consistent); while busy, twitch()
    # re-tilts them at a low frame rate, like Spider-Punk's cut-out edges.
    TITLE = "SCREEN ANSWER"

    def __init__(self, parent, scale):
        super().__init__(parent, bg=PAPER, highlightthickness=0, bd=0, width=1,
                         height=int(50 * scale))
        self.scale = scale
        self.rng = random.Random()
        self.shuffle()
        self.bind("<Configure>", lambda e: self.draw())

    def shuffle(self):
        self.letters = [[self.rng.choice(RANSOM_FONTS), self.rng.choice(RANSOM_PAPERS),
                         self.rng.uniform(-9, 9), self.rng.uniform(-3, 3)] for _ in self.TITLE]
        self.draw()

    def twitch(self):
        for letter in self.letters:
            letter[2], letter[3] = self.rng.uniform(-12, 12), self.rng.uniform(-4, 4)
        self.draw(dots=False)

    def draw(self, dots=True):
        s, w, h = self.scale, self.winfo_width(), int(self.cget("height"))
        if dots:
            self.delete("all")
            badge_w = 46 * s
            self.create_rectangle(w - badge_w - 6 * s, h / 2 - 12 * s, w - 6 * s, h / 2 + 12 * s,
                                  fill=INK, outline="")
            self.create_text(w - badge_w / 2 - 6 * s, h / 2, text="#001", fill=YELLOW,
                             font=(COMIC_FAMILY, 14))
        self.delete("letter")
        x, cw, ch = 8 * s, 21 * s, 30 * s
        for char, (font, (paper, ink), angle, dy) in zip(self.TITLE, self.letters, strict=True):
            if char == " ":
                x += 10 * s
                continue
            cx, cy = x + cw / 2, h / 2 + dy * s
            self.create_polygon(rotated_rect(cx, cy, cw, ch, angle), fill=paper, outline=INK,
                                width=max(1, int(s)), tags="letter")
            self.create_text(cx, cy, text=char, angle=angle, fill=ink, font=(font, 15, "bold"),
                             tags="letter")
            x += cw + 2 * s


class StatusBurst(tk.Canvas):
    # The status as a comic caption box, with a sound-effect burst (BAM!) that pops in when
    # something lands
    def __init__(self, parent, scale):
        super().__init__(parent, bg=PAPER, highlightthickness=0, bd=0, width=1,
                         height=int(50 * scale))
        self.scale = scale
        self.word_font = tkfont.Font(root=self, family=COMIC_FAMILY, size=16)
        self.caption_font = tkfont.Font(root=self, family=COMIC_FAMILY, size=13)
        self.text, self.word, self.color = "", None, RED
        self.pop_jobs = []

    def cget(self, key):
        return self.text if key == "text" else super().cget(key)

    def set(self, text, word=None, color=RED, pop=False):
        for job in self.pop_jobs:
            self.after_cancel(job)
        self.text, self.word, self.color = text, word, color
        if pop and word:
            self._draw(0.55)
            self.pop_jobs = [self.after(45, self._draw, 1.18), self.after(110, self._draw, 1.0)]
        else:
            self.pop_jobs = []
            self._draw(1.0)

    def _draw(self, zoom):
        s, h = self.scale, int(self.cget("height"))
        self.delete("all")
        x, edge = 3 * s, max(2, int(1.5 * s))
        if self.word:
            # Sized at full zoom, so the pop doesn't shove the buttons around
            rx, ry = self.word_font.measure(self.word) / 2 / 0.62 + 4 * s, h / 2 - edge
            cx, cy = x + rx, h / 2
            spikes, rng = [], random.Random(self.word)
            for i in range(24):
                k = 1.0 if i % 2 == 0 else 0.66 + 0.08 * rng.random()
                a = math.pi * i / 12
                spikes += [cx + rx * k * zoom * math.cos(a), cy + ry * k * zoom * math.sin(a)]
            self.create_polygon(spikes, fill=self.color, outline=INK, width=edge)
            if zoom >= 1:
                outlined_text(self, cx, cy, self.word, self.word_font, BURST_TEXT[self.color], INK,
                              max(1, int(1.2 * s)))
            x = cx + rx + 4 * s
        if self.text:
            pad_x, pad_y = 7 * s, 2 * s
            cw = self.caption_font.measure(self.text) + 2 * pad_x
            ch = self.caption_font.metrics("linespace") + 2 * pad_y
            self.create_rectangle(x, (h - ch) / 2, x + cw, (h + ch) / 2, fill=YELLOW, outline=INK,
                                  width=edge)
            self.create_text(x + cw / 2, h / 2, text=self.text, font=self.caption_font, fill=INK)
            x += cw + edge
        self.configure(width=int(x + 2 * s))


class App:
    def __init__(self):
        load_comic_font()  # before Tk exists, so its fonts can find Bangers
        self.root = tk.Tk()
        self.root.withdraw()
        # pythonw has no stderr, so exceptions in Tk callbacks go to the log
        self.root.report_callback_exception = lambda *exc: log("".join(traceback.format_exception(*exc)))
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.events = queue.Queue()
        self.busy = False
        self.started = 0.0
        self.hide_job = None
        self.expanded = False
        self.shots = []  # screenshots queued with Ctrl+Alt+A, in memory only
        self.last_add = 0.0
        self.busy_label = ""
        self.last_pngs = []  # the last question's screenshots, kept in memory for Rethink
        self.last_answer = ""
        self.last_note = ""  # what the user typed for the last rethink of this question
        self.rethinking = False
        self.math_images = {}  # widget name -> the formula images it shows
        self.answer_font = tkfont.Font(root=self.root, font=FONT_ANSWER)
        self.solution_font = tkfont.Font(root=self.root, font=FONT_SOLUTION)
        self.code_font = tkfont.Font(root=self.root, font=FONT_CODE)
        self.work = None  # work area of the monitor the window is on, so it never outgrows it
        self.answer_lines = 1  # how many lines the answer needs, before any cap or screen fit
        self.solution_lines = SOLUTION_LINES
        self._build_popup()
        threading.Thread(target=hotkey_loop, args=(self.events,), daemon=True).start()
        self.root.after(50, self._poll)

    def _build_popup(self):
        s = self.scale
        pad = int(8 * s)
        self.popup = tk.Toplevel(self.root, bg=PAPER)
        self.popup.title("Screen Answer")
        self.popup.attributes("-topmost", True)
        self.popup.protocol("WM_DELETE_WINDOW", self.hide)
        self.popup.bind("<Escape>", lambda e: self.hide())

        self.masthead = Masthead(self.popup, s)
        self.masthead.pack(fill="x", padx=pad, pady=(pad, 0))
        bar = tk.Frame(self.popup, bg=PAPER)
        bar.pack(fill="x", padx=pad)
        self.status = StatusBurst(bar, s)
        self.status.pack(side="left")
        button_gap = (int(6 * s), 0)
        self.toggle = ComicButton(bar, "Show solution ▾", self.toggle_solution, CYAN, s)
        self.toggle.pack(side="right", padx=button_gap)
        self.clear_btn = ComicButton(bar, "Clear queue", self.clear_queue, WHITE, s)
        self.rethink_btn = ComicButton(bar, "Rethink ↻", self.rethink, RED, s, fg=WHITE)
        self.copy_btn = ComicButton(bar, "Copy", self.copy_answer, WHITE, s)
        self.button_gap = button_gap
        self.margin = int(16 * s)

        # Space above and below every line, so stacked fractions on neighboring lines don't touch
        self.line_gap = int(3 * s)
        gaps = {"spacing1": self.line_gap, "spacing2": 2 * self.line_gap, "spacing3": self.line_gap}
        boxes = {"wrap": "word", "relief": "flat", "bd": 0, "highlightthickness": 0,
                 "padx": int(5 * s), "pady": int(4 * s), "width": TEXT_WIDTH_CHARS}
        # The answer's scrollbars appear only when needed: vertical when the answer is cut short,
        # horizontal when a line of code is wider than the box
        self.answer_panel = Panel(self.popup, s, WHITE)
        self.answer_panel.pack(fill="x", padx=pad, pady=(int(4 * s), pad))
        self.answer_frame = self.answer_panel.inner
        self.answer = tk.Text(self.answer_frame, height=3, font=FONT_ANSWER, bg=WHITE,
                              **boxes, **gaps)
        self.answer.pack(side="left", fill="both", expand=True)
        self.answer_scroll = tk.Scrollbar(self.answer_frame, command=self.answer.yview)
        self.answer_xscroll = tk.Scrollbar(self.answer_frame, orient="horizontal",
                                           command=self.answer.xview)
        self.answer.config(yscrollcommand=self.answer_scroll.set,
                           xscrollcommand=self.answer_xscroll.set)

        # Rethink's note box, shown under the answer when Rethink is clicked
        self.note_frame = tk.Frame(self.popup, bg=PAPER)
        tk.Label(self.note_frame, text="Tell Opus what to check or focus on (optional)",
                 anchor="w", bg=PAPER, fg=INK, font=(COMIC_FAMILY, 13)).pack(fill="x")
        note_panel = Panel(self.note_frame, s, WHITE)
        note_panel.pack(fill="x")
        self.note = tk.Text(note_panel.inner, height=3, font=FONT_SOLUTION, bg=WHITE,
                            insertwidth=max(2, int(s)), **boxes)
        self.note.pack(fill="both", expand=True)
        note_bar = tk.Frame(self.note_frame, bg=PAPER)
        note_bar.pack(fill="x", pady=(int(4 * s), 0))
        tk.Label(note_bar, text="Enter: go   Shift+Enter: new line   Esc: cancel",
                 bg=PAPER, fg="#8A8170", font=FONT_UI).pack(side="left")
        ComicButton(note_bar, "Rethink ↻", self._start_rethink, RED, s, fg=WHITE).pack(
            side="right", padx=button_gap)
        ComicButton(note_bar, "Cancel", self._close_note, WHITE, s).pack(side="right",
                                                                           padx=button_gap)
        # Shift+Enter falls through to Text's own newline; "break" keeps Esc from hiding the window
        self.note.bind("<Return>", lambda e: self._start_rethink() or "break")
        self.note.bind("<Shift-Return>", lambda e: None)
        self.note.bind("<Escape>", lambda e: self._close_note() or "break")

        # "Show solution" reveals the figure (if any) above the solution text
        self.details = tk.Frame(self.popup, bg=PAPER)
        self.figure_panel = Panel(self.details, s, WHITE)
        self.figure = tk.Label(self.figure_panel.inner, bg=WHITE, bd=0)
        self.figure.pack()
        self.figure_img = None
        self.solution_panel = Panel(self.details, s, SOLUTION_BG)
        self.solution_panel.pack(fill="both", expand=True)
        self.solution_frame = self.solution_panel.inner
        scroll = tk.Scrollbar(self.solution_frame)
        scroll.pack(side="right", fill="y")
        self.solution = tk.Text(self.solution_frame, height=SOLUTION_LINES, font=FONT_SOLUTION,
                                bg=SOLUTION_BG, yscrollcommand=scroll.set, **boxes, **gaps)
        self.solution.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.solution.yview)
        self.solution_xscroll = tk.Scrollbar(self.solution_frame, orient="horizontal",
                                             command=self.solution.xview)
        self.solution.config(xscrollcommand=self.solution_xscroll.set)
        for box in (self.answer, self.solution):
            # Code: monospace, never wrapped (wrapping would break its indentation), tight lines
            box.tag_configure("code", font=FONT_CODE, background=CODE_BG, wrap="none",
                              spacing1=0, spacing3=0, lmargin1=pad, lmargin2=pad)
            box.tag_configure("icode", font=FONT_CODE, background=CODE_BG)
            # What a rethink changed, in Spider-Punk highlighter pink
            box.tag_configure("changed", background=PINK, foreground=WHITE,
                              font=("Segoe UI Black", FONT_ANSWER[1] - 2))
        self.panels = [self.answer_panel, note_panel, self.figure_panel, self.solution_panel]
        self.popup.withdraw()
        self.popup.update_idletasks()
        try:
            color_title_bar(self.popup, YELLOW, INK, INK)
        except (AttributeError, OSError):
            pass  # no dwmapi: keep the normal title bar
        self.figure_width = max(self.answer_frame.winfo_reqwidth(),
                                self.solution_frame.winfo_reqwidth())

    def _set_text(self, widget, content):
        # content: plain text, or parts from render_rich whose formulas go in as inline images.
        # Returns the parts with each formula as its PhotoImage, for _fit_answer.
        parts = [("text", content)] if isinstance(content, str) else content
        widget.config(state="normal")
        widget.delete("1.0", "end")
        shown = []
        for kind, value in parts:
            if kind == "math":
                value = tk.PhotoImage(data=base64.b64encode(value).decode())
                widget.image_create("end", image=value, align="center")
            elif kind in ("code", "icode"):
                widget.insert("end", value, kind)
            else:
                for i, piece in enumerate(value.split("(changed)")):
                    if i:
                        widget.insert("end", " CHANGED ", "changed")
                    widget.insert("end", piece)
            shown.append((kind, value))
        widget.config(state="disabled")
        # Tk blanks an image once Python drops the last reference to it
        self.math_images[str(widget)] = [value for kind, value in shown if kind == "math"]
        return shown

    def _set_figure(self, png):
        self.figure_img = tk.PhotoImage(data=base64.b64encode(png).decode()) if png else None
        if png:
            self.figure.config(image=self.figure_img)
            self.figure_panel.pack(before=self.solution_panel, fill="x",
                                   pady=(0, int(8 * self.scale)))
        else:
            self.figure_panel.pack_forget()
        self.solution_lines = SOLUTION_LINES_WITH_FIGURE if png else SOLUTION_LINES

    def _toggle_text(self):
        what = "solution + figure" if self.figure_img else "solution"
        return f"Hide {what} ▴" if self.expanded else f"Show {what} ▾"

    def _fit_answer(self, parts):
        # Rough wrapped-height estimate of the answer in box lines, so the window is only as tall as
        # the answer. A formula counts by its pixel size and a tall one (a fraction) makes its whole
        # line taller; a line of code never wraps.
        wrap_px = TEXT_WIDTH_CHARS * self.answer_font.measure("0")
        line_h = self.answer_font.metrics("linespace")
        total, width, tallest, is_code = 0, 0, line_h, False
        for kind, value in parts + [("text", "\n")]:
            if kind == "math":
                width += value.width()
                tallest = max(tallest, value.height())
                continue
            font = self.code_font if kind in ("code", "icode") else self.answer_font
            for i, run in enumerate(value.split("\n")):
                if i:  # a line break ends the line before it
                    if is_code:
                        total += self.code_font.metrics("linespace")
                    else:
                        total += max(1, math.ceil(width / wrap_px)) * (tallest + 2 * self.line_gap)
                    width, tallest, is_code = 0, line_h, False
                width += font.measure(run)
                is_code = is_code or (kind == "code" and bool(run))
        # Tk sizes the box in lines of linespace plus the spacing above and below each line
        self.answer_lines = max(1, math.ceil(total / (line_h + 2 * self.line_gap)))

    def _code_too_wide(self, parts, box_font):
        # Whether a line of code is wider than the box, so the box needs a horizontal scrollbar
        room = TEXT_WIDTH_CHARS * box_font.measure("0")
        return any(self.code_font.measure(line) + int(8 * self.scale) > room
                   for kind, value in parts if kind == "code" for line in value.split("\n"))

    @staticmethod
    def _show_bar(bar, needed, **pack):
        if needed and not bar.winfo_manager():
            bar.pack(**pack)
        elif not needed and bar.winfo_manager():
            bar.pack_forget()

    def _fill(self, answer, solution):
        # answer, solution: plain text or render_rich parts
        answer_parts = self._set_text(self.answer, answer)
        solution_parts = self._set_text(self.solution, solution)
        self._fit_answer(answer_parts)
        self._show_bar(self.answer_xscroll, self._code_too_wide(answer_parts, self.answer_font),
                       side="bottom", fill="x", before=self.answer)
        self._show_bar(self.solution_xscroll, self._code_too_wide(solution_parts, self.solution_font),
                       side="bottom", fill="x", before=self.solution)

    def _place(self, right, bottom):
        # Size the window to its content, keeping its bottom-right corner fixed. It never grows taller
        # than the screen, or the top bar and its buttons would end up above it: the solution box
        # gives up lines first, then the answer box, which then shows its scrollbar.
        self.solution.config(height=self.solution_lines)
        self.answer.config(height=min(self.answer_lines, MAX_ANSWER_LINES))
        limit = self.work.bottom - self.work.top - 2 * self.margin
        for box, floor, font in ((self.solution, MIN_SOLUTION_LINES, self.solution_font),
                                 (self.answer, MIN_ANSWER_LINES, self.answer_font)):
            excess = self._measure() - limit
            if excess <= 0:
                break
            step = font.metrics("linespace") + 2 * self.line_gap
            box.config(height=max(floor, int(box.cget("height")) - math.ceil(excess / step)))
        self._show_bar(self.answer_scroll, int(self.answer.cget("height")) < self.answer_lines,
                       side="right", fill="y", before=self.answer)
        self.popup.geometry("")
        h, w = self._measure(), self.popup.winfo_reqwidth()
        self.popup.geometry(f"+{right - w}+{max(self.work.top, bottom - h)}")

    def _measure(self):
        # The window's requested height, once every panel has taken its content's size
        self.popup.update_idletasks()
        for panel in self.panels:
            panel.sync()
        self.popup.update_idletasks()
        return self.popup.winfo_reqheight()

    def _current_corner(self):
        size, x, y = self.popup.geometry().split("+")
        w, h = map(int, size.split("x"))
        return int(x) + w, int(y) + h

    def show(self, work, status, answer="", solution="", queued=False, word=None, color=RED):
        # word: a sound effect (BAM!) to pop in a burst next to the status
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None
        self.work = work
        self.rethink_btn.pack_forget()
        self.copy_btn.pack_forget()
        self.note_frame.pack_forget()
        if queued:
            self.toggle.pack_forget()
            self.clear_btn.pack(side="right", padx=self.button_gap)
        else:
            self.clear_btn.pack_forget()
            self.toggle.pack(side="right", padx=self.button_gap)
        self.status.set(status, word, color, pop=True)
        self._fill(answer, solution)
        self._set_figure(None)
        self.details.pack_forget()
        self.expanded = False
        self.toggle.config(state="normal" if solution else "disabled", text=self._toggle_text())
        self._place(work.right - self.margin, work.bottom - self.margin)
        self.popup.deiconify()
        self.popup.lift()

    def update_result(self, status, answer, solution="", figure=None, word=None, color=RED):
        # answer, solution: plain text or render_rich parts. Keep the window where it is (the user
        # may have moved it) and resize around it
        corner = self._current_corner()
        self.status.set(status, word, color, pop=True)
        self._fill(answer, solution)
        self._set_figure(figure)
        self.toggle.config(state="normal" if solution or figure else "disabled",
                           text=self._toggle_text())
        self._place(*corner)

    def toggle_solution(self):
        corner = self._current_corner()
        self.expanded = not self.expanded
        if self.expanded:
            self.details.pack(fill="both", expand=True, padx=int(8 * self.scale),
                              pady=(0, int(8 * self.scale)))
        else:
            self.details.pack_forget()
        self.toggle.config(text=self._toggle_text())
        self._place(*corner)

    def hide(self):
        self.popup.withdraw()

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                getattr(self, f"on_{kind}")(payload)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def on_ready(self, _):
        _, work = monitor_under_cursor()
        self.show(work, "Running", "Ctrl+Alt+Q   solve the screen under the mouse\n"
                                   "Ctrl+Alt+A   queue this screen (long questions)\n"
                                   "Ctrl+Alt+Shift+Q   quit\n"
                                   "Esc   close this window", word="READY!", color=YELLOW)
        self.hide_job = self.root.after(4000, self.hide)

    def on_fatal(self, text):
        messagebox.showerror("Screen Answer", text)
        self.root.destroy()

    def on_quit(self, _):
        self.root.destroy()

    def on_add(self, _):
        if self.busy:
            self.popup.deiconify()
            return
        _, work = monitor_under_cursor()
        if len(self.shots) >= MAX_SHOTS - 1:  # leave room for the screen Ctrl+Alt+Q takes
            self.show(work, f"Queue full ({len(self.shots)})",
                      "Press Ctrl+Alt+Q to solve.", queued=True)
            return
        self._grab_then(self._queued)

    def on_answer(self, _):
        if self.busy:
            self.popup.deiconify()
            return
        self._grab_then(self._solve)

    def clear_queue(self):
        self.shots = []
        self.hide()

    def _grab_then(self, callback):
        self.busy = True
        monitor, work = monitor_under_cursor()
        self.hide()
        self.root.update()
        # Wait for the window to disappear before the screenshot, or it captures itself
        self.root.after(150, lambda: self._grab(monitor, work, callback))

    def _grab(self, monitor, work, callback):
        try:
            png = grab_png(monitor)
        except Exception as e:
            log(f"Screenshot failed: {e!r}")
            self.busy = False
            self.show(work, "Screenshot failed", str(e), word="WHOOPS!", color=CYAN)
            return
        callback(work, png)

    def _queued(self, work, png):
        self.busy = False
        self.shots.append(png)
        self.last_add = time.time()
        n = len(self.shots)
        self.show(work, f"{n} screenshot{'s' if n > 1 else ''} queued",
                  "Scroll, then Ctrl+Alt+A to add more.\n"
                  "Ctrl+Alt+Q solves with these plus the current screen.", queued=True,
                  word="SNAP!", color=CYAN)
        self.hide_job = self.root.after(2500, self.hide)

    def _solve(self, work, png):
        # A forgotten queue from an earlier question shouldn't leak into this one
        if time.time() - self.last_add > QUEUE_TTL_SECS:
            self.shots = []
        pngs, self.shots = self.shots + [png], []
        self.last_pngs, self.last_answer, self.last_note, self.rethinking = pngs, "", "", False
        self.busy_label = "Thinking" + (f" ({len(pngs)} screenshots)" if len(pngs) > 1 else "")
        self.started = time.time()
        self.masthead.shuffle()  # new cut-out letters for every question
        self.show(work, f"{self.busy_label}… 0s")
        self._tick()
        self._twitch()
        threading.Thread(target=self._worker, args=(pngs,), daemon=True).start()

    def rethink(self):
        # Open the note box first; the rethink itself starts from there
        if self.busy or not self.last_pngs:
            return
        corner = self._current_corner()
        self.rethink_btn.pack_forget()
        self.note.delete("1.0", "end")
        self.note.insert("1.0", self.last_note)  # a second rethink starts from the last note
        self.note_frame.pack(after=self.answer_panel, fill="x", padx=int(8 * self.scale),
                             pady=(0, int(8 * self.scale)))
        self._place(*corner)
        self.note.focus_force()

    def _close_note(self):
        corner = self._current_corner()
        self.note_frame.pack_forget()
        self.rethink_btn.pack(side="right", after=self.toggle, padx=self.button_gap)
        self._place(*corner)

    def _start_rethink(self):
        # Same screenshots again, with the slower model checking the answers now on screen
        if self.busy or not self.last_pngs:
            return
        self.last_note = self.note.get("1.0", "end").strip()
        corner = self._current_corner()
        self.note_frame.pack_forget()
        self._place(*corner)
        self.busy, self.rethinking = True, True
        self.busy_label = f"Rethinking with {RETHINK_MODEL.capitalize()}"
        self.started = time.time()
        self._tick()
        self._twitch()
        previous = self.last_answer or "(none, the first attempt failed)"
        threading.Thread(target=self._worker, args=(self.last_pngs, previous, self.last_note),
                         daemon=True).start()

    def _worker(self, pngs, previous=None, note=""):
        try:
            reply, figure = split_figure(ask_claude(pngs, previous, note))
        except Exception as e:
            log(f"Claude failed: {e!r}")
            self.events.put(("error", str(e)))
            return
        png = None
        if figure is not None:
            # Drawn here, off the Tk thread: the first matplotlib import alone takes a second or two
            try:
                png = render_figure(parse_figure(figure), self.figure_width, 96 * self.scale)
            except Exception as e:
                log(f"Figure failed: {e!r}\n{figure.strip()[:3000]}")
        answer, solution = split_reply(reply)
        # Formulas are drawn here too; only turning the PNGs into Tk images happens on the Tk thread.
        # Full-size fractions in the answers, which get read to be typed in; compact in the solution.
        dpi = 96 * self.scale
        answer_parts = render_rich(answer, FONT_ANSWER[1], dpi, big_fractions=True)
        solution_parts = render_rich(solution, FONT_SOLUTION[1], dpi)
        self.events.put(("done", (answer, answer_parts, solution_parts, png, figure is not None)))

    def _tick(self):
        if self.busy:
            self.status.set(f"{self.busy_label}… {time.time() - self.started:.0f}s")
            self.root.after(500, self._tick)

    def _twitch(self):
        # The cut-out title twitches at about 6 frames a second while Claude works
        if self.busy:
            self.masthead.twitch()
            self.root.after(160, self._twitch)
        else:
            self.masthead.draw(dots=False)

    def _offer_rethink(self, copy=True):
        # Packed before update_result sizes the window, so the buttons are counted in the width.
        # Right to left: Show solution, Rethink, Copy
        self.rethink_btn.pack(side="right", after=self.toggle, padx=self.button_gap)
        if copy:
            self.copy_btn.pack(side="right", after=self.rethink_btn, padx=self.button_gap)
        else:
            self.copy_btn.pack_forget()
        if self.rethinking:
            # A rethink takes a while; bring the window back if it was closed in the meantime
            self.popup.deiconify()
            self.popup.lift()

    def on_done(self, result):
        self.busy = False
        answer, answer_parts, solution_parts, figure, wanted_figure = result
        self.last_answer = answer  # the LaTeX source, for a rethink to compare against
        status = f"{time.time() - self.started:.1f}s"  # the BAM! already says it's done
        if wanted_figure and not figure:
            status += ", figure failed (see log)"
        self._offer_rethink()
        self.update_result(status, answer_parts, solution_parts, figure,
                           word="KA-POW!" if self.rethinking else "BAM!")

    def on_error(self, text):
        self.busy = False
        self._offer_rethink(copy=False)  # the box shows the error now, not an answer to copy
        # The box shows the error itself; the log is %TEMP%\screen-answer\log.txt
        self.update_result("Error, see log.txt", text, word="WHOOPS!", color=CYAN)

    def copy_answer(self):
        # Just the code when the answer has some, ready to paste into an editor; otherwise the
        # answer as text, with formulas as their LaTeX source
        code = [m[1] for m in CODE_RE.finditer(self.last_answer)]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n\n".join(code) if code else self.last_answer)
        self.copy_btn.config(text="Copied ✓")
        self.root.after(1500, lambda: self.copy_btn.config(text="Copy"))


if __name__ == "__main__":
    os.makedirs(WORK_DIR, exist_ok=True)
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
    enable_dpi_awareness()
    App().root.mainloop()
