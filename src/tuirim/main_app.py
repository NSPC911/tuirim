import asyncio
import json
import subprocess
from bisect import bisect_right, insort
from collections.abc import Callable
from fractions import Fraction
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from typing import cast

from PIL import Image, UnidentifiedImageError
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.renderables.bar import Bar as BarRenderable
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, ProgressBar, Rule, Static
from textual_drivers import DrivenApp
from textual_image.widget import Image as TerminalImage

from tuirim import iterm2_image

TImage = iterm2_image.ITerm2Image if iterm2_image.is_supported() else TerminalImage


class ThickBar(BarRenderable):
    HALF_BAR_LEFT = "▐"
    BAR = "█"
    HALF_BAR_RIGHT = "▌"


async def run_media(*args: str, on_frame: Callable[[int], None] | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if on_frame is None:
            data, error = await proc.communicate()
        else:

            async def read_progress() -> None:
                assert proc.stdout is not None
                async for line in proc.stdout:
                    key, _, value = line.partition(b"=")
                    if key == b"frame" and value.strip().isdigit():
                        on_frame(int(value))

            assert proc.stderr is not None
            _, error = await asyncio.gather(read_progress(), proc.stderr.read())
            await proc.wait()
            data = b""
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
    if proc.returncode:
        raise ValueError(error.decode(errors="replace").strip())
    return data


class ModalInput(ModalScreen[str]):
    DEFAULT_CSS = """
    #modalInput_group {
        border: round $primary;
        width: 50vw;
        max-height: 50vh;
        background: transparent !important;
        border-title-align: left;
        border-subtitle-align: right;
    }
    Input {
        scrollbar-size: 0 0;
        background: transparent !important;
        width: 1fr;
        border: none !important;
        border-top: round $primary !important;
        height: 2 !important;

        .input--placeholder { color: $foreground-darken-1 }
        &:ansi {
            .input--cursor { color: $primary }
            .input--selection {
                background: $secondary-darken-3;
                text-style: bold;
            }
        }
    }
    ModalInput { align: center middle }
    """

    def __init__(
        self, placeholder: str = "", title: str = "", subtitle: str = ""
    ) -> None:
        super().__init__()
        self.placeholder = placeholder
        self.title = title
        self.subtitle = subtitle

    def compose(self) -> ComposeResult:
        with Container(id="modalInput_group"):
            yield Input(placeholder=self.placeholder, value=self.placeholder)

    def on_mount(self) -> None:
        cont = self.query_one("#modalInput_group", Container).focus()
        cont.border_title = self.title
        cont.border_subtitle = self.subtitle
        self.query_one(Input).cursor_blink = False
        image = self.app.default_screen.query_one(TImage).image
        if not isinstance(image, Image.Image):
            return
        cont.mount(
            Container(
                TImage(image.copy(), id="preview"),
                id="viewport",
            ),
            before=0,
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss("")


class ExportProgress(ModalScreen):
    DEFAULT_CSS = """
    ExportProgress { align: center middle; }
    #holder {
        width: 50%; height: 50%; padding: 0;
        background: $surface; border: round $accent;
    }
    ProgressBar { margin: 0 2 }
    """

    def __init__(self, filename: str, frames: int) -> None:
        super().__init__()
        self.filename = filename
        self.frames = frames
        self.bar = ProgressBar(show_eta=False)
        self.bar.BAR_RENDERABLE = ThickBar

    def update_progress(self, frames: int) -> None:
        if frames > 0:
            self.bar.update(total=self.frames, progress=min(frames, self.frames))

    def compose(self) -> ComposeResult:
        with Container(id="holder"):
            yield Rule()
            yield self.bar

    def on_mount(self) -> None:
        image = self.app.default_screen.query_one(TImage).image
        if not isinstance(image, Image.Image):
            return
        self.query_one(Container).mount(
            Container(TImage(image.copy(), id="preview"), id="viewport"),
            before=0
        )
        self.query_one(Rule).styles.color = self.app.theme_variables["accent"]


class Timeline(Static):
    def on_click(self, event: events.Click) -> None:
        app = cast("App", self.app)
        app.seek_time(app.duration * event.x / max(1, self.size.width - 1))

    def render(self) -> Text:
        app = cast("App", self.app)
        width = self.size.width
        scale = max(1, width - 1) / (app.duration or 1)
        cursor = round(app.frame_time(app.frame) * scale)
        boundaries = {round(app.frame_time(i) * scale) for i in app.cuts[1:]}
        bar = Text(no_wrap=True)
        for x in range(width):
            frame = app.frame_at(x / scale)
            ignored = app.section(frame) in app.ignored
            glyph = "╌" if ignored else "━"
            bar.append(
                "◆" if x == cursor else "│" if x in boundaries else glyph,
                "bold white" if x == cursor else "dim red" if ignored else "green",
            )
        return bar


class App(DrivenApp):
    ALLOW_SELECT = False

    CSS = """
    #viewport { height: 1fr; align: center middle; }
    #preview { width: auto; height: auto; }
    Timeline { height: 1; }
    #status { border: round $accent; border-top: none }
    FooterKey, FooterKey .footer-key--key { color: $primary !important }
    ProgressBar { width: 100%; }
    ProgressBar > Bar { width: 1fr; }
    Rule { margin-bottom: 0 }
    """
    BINDINGS = [
        Binding("space", "play", "Play/pause"),
        Binding("s", "split", "Split"),
        Binding("c", "clear_splits", "Clear splits"),
        Binding("i", "ignore", "Ignore/keep"),
        Binding("r", "invert", "Invert"),
        Binding("comma", "step(-1)", "-1 frame"),
        Binding("full_stop", "step(1)", "+1 frame"),
        Binding("less_than_sign", "jump(-1)", "−1s"),
        Binding("greater_than_sign", "jump(1)", "+1s"),
        Binding("h", "jump(-10)", "−10s"),
        Binding("l", "jump(10)", "+10s"),
        Binding("H", "jump(-60)", "−1m"),
        Binding("L", "jump(60)", "+1m"),
        Binding("e", "export", "Export"),
        Binding("q", "quit", "Quit"),
    ]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, source: Path, output: Path) -> None:
        super().__init__()
        self.theme = "ansi-dark"
        self.source, self.output = source.resolve(), output.resolve()
        probe = subprocess.check_output([
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(self.source),
        ])
        info = json.loads(probe)
        streams = info["streams"]
        video = next((s for s in streams if s["codec_type"] == "video"), None)
        if video is None:
            raise ValueError("No video stream")
        self.audio = any(s["codec_type"] == "audio" for s in streams)
        for key in ("avg_frame_rate", "r_frame_rate"):
            try:
                self.fps = float(Fraction(video.get(key, "0")))
            except ValueError, ZeroDivisionError:
                continue
            if self.fps > 0:
                break
        else:
            raise ValueError("No usable video frame rate")
        origin = float(info["format"].get("start_time", 0))
        self.start = float(video.get("start_time", origin)) - origin
        self.regular_rate = video.get("avg_frame_rate") == video.get("r_frame_rate")
        self.frame_count = int(video.get("nb_frames", 0))
        if self.frame_count <= 0:
            length = (
                float(video["duration"])
                if "duration" in video
                else float(info["format"].get("duration", 0)) - self.start
            )
            if length <= 0:
                raise ValueError("No usable video frame count or duration")
            self.frame_count = max(1, round(length * self.fps))
        self.duration = self.frame_time(self.frame_count)
        self.cuts = [0]
        self.ignored: set[int] = set()
        self.frame = 0
        self.playing = self.exporting = False
        self.preview = TImage(id="preview")

    def compose(self) -> ComposeResult:
        yield Container(self.preview, id="viewport")
        yield Timeline()
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.seek(0)

    def frame_time(self, frame: int) -> float:
        return self.start + frame / self.fps

    def frame_at(self, seconds: float) -> int:
        frame = round((seconds - self.start) * self.fps)
        return min(
            self.frame_count - 1, max(0, frame - (self.frame_time(frame) > seconds))
        )

    def section(self, frame: int) -> int:
        return self.cuts[bisect_right(self.cuts, frame) - 1]

    def update_status(self) -> None:
        state = "IGNORE" if self.section(self.frame) in self.ignored else "KEEP"
        self.query_one(Timeline).refresh()
        self.query_one("#status", Static).update(
            f"{'Playing' if self.playing else 'Paused'}  {self.frame_time(self.frame):.3f}s / "
            f"{self.duration:.3f}s   Frame {self.frame + 1}/{self.frame_count}   {state}     {self.source.name} → {self.output.name}"
        )

    def seek(self, frame: int) -> None:
        self.playing = False
        self.frame = min(max(frame, 0), self.frame_count - 1)
        self.update_status()
        self.render_preview()

    def seek_time(self, seconds: float) -> None:
        self.seek(self.frame_at(seconds))

    def action_step(self, delta: int) -> None:
        self.seek(self.frame + delta)

    def action_jump(self, seconds: int) -> None:
        self.seek_time(self.frame_time(self.frame) + seconds)

    def action_split(self) -> None:
        self.seek(self.frame)
        if self.frame not in self.cuts:
            if self.section(self.frame) in self.ignored:
                self.ignored.add(self.frame)
            insort(self.cuts, self.frame)
            self.update_status()

    def action_clear_splits(self) -> None:
        self.cuts = [0]
        self.ignored.clear()
        self.seek(self.frame)

    def action_ignore(self) -> None:
        self.ignored.symmetric_difference_update({self.section(self.frame)})
        self.seek(self.frame)

    def action_invert(self) -> None:
        self.ignored = set(self.cuts) - self.ignored
        self.seek(self.frame)

    def action_play(self) -> None:
        if self.playing:
            self.seek(self.frame)
            return
        if len(self.cuts) == len(self.ignored):
            self.notify("No kept sections to play")
            return
        if self.frame == self.frame_count - 1:
            self.frame = 0
        self.playing = True
        self.render_preview()

    @work(exclusive=True, group="preview")
    async def render_preview(self) -> None:
        anchor = monotonic() - self.frame_time(self.frame)
        try:
            while True:
                if self.playing and self.section(self.frame) in self.ignored:
                    following = next(
                        (
                            i
                            for i in self.cuts
                            if i > self.frame and i not in self.ignored
                        ),
                        None,
                    )
                    if following is None:
                        break
                    self.frame = following
                    anchor = monotonic() - self.frame_time(following)
                self.update_status()
                data = await run_media(
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    f"{max(0, self.frame_time(self.frame) - 0.000001):.6f}",
                    "-i",
                    str(self.source),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:360:force_original_aspect_ratio=decrease",
                    "-f",
                    "image2pipe",
                    "-c:v",
                    "png",
                    "-",
                )
                self.preview.image = Image.open(BytesIO(data)).copy()
                if not self.playing:
                    return
                await asyncio.sleep(
                    max(
                        0.01,
                        self.frame_time(self.frame + 1) + anchor - monotonic(),
                    )
                )
                elapsed = monotonic() - anchor
                if elapsed >= self.duration:
                    break
                self.frame = self.frame_at(elapsed)
        except UnidentifiedImageError as exc:
            # likely not last frame
            if "cannot identify image file" not in str(exc):
                self.notify(str(exc), title="Preview failed", severity="error")
        except (OSError, ValueError) as exc:
            raise
            self.notify(str(exc), title="Preview failed", severity="error")
        self.playing = False
        self.update_status()

    @work(group="export")
    async def action_export(self) -> None:
        if self.exporting:
            return
        overwrite = False
        while self.output.exists():
            modal = ModalInput(
                placeholder=str(self.output),
                title="Output file exists",
                subtitle="Enter a new filename or press Enter to overwrite",
            )
            new_name = await self.push_screen_wait(modal)
            if not new_name:
                self.notify("Export cancelled")
                return
            selected = Path(new_name).resolve()
            if selected == self.output:
                overwrite = True
                break
            self.output = selected
        if self.output == self.source:
            self.notify("Choose an output other than the source video", severity="error")
            return
        filters, labels = [], []
        kept_frames = 0
        seek = 0.0
        first = next((i for i in self.cuts if i not in self.ignored), 0)
        # With matching frame rates, seek between frames so rounding doesn't skip the cut.
        if first and self.regular_rate:
            seek = max(0.0, self.frame_time(first) - 0.5 / self.fps)
        frame_offset = first if seek else 0
        for j, (start, stop) in enumerate(pairwise([*self.cuts, self.frame_count])):
            if start in self.ignored:
                continue
            kept_frames += stop - start
            end = self.frame_time(stop) - seek
            interval = f"start={self.frame_time(start) - seek:.6f}:end={end:.6f}"
            filters.append(
                f"[0:v:0]trim=start_frame={start - frame_offset}:end_frame={stop - frame_offset},setpts=PTS-STARTPTS[v{j}]"
            )
            labels.append(f"[v{j}]")
            if self.audio:
                filters.append(f"[0:a:0]atrim={interval},asetpts=PTS-STARTPTS[a{j}]")
                labels.append(f"[a{j}]")
        if not labels:
            self.notify("No kept sections to export")
            return
        filters.append(
            "".join(labels)
            + f"concat=n={len(self.cuts) - len(self.ignored)}:v=1:a={int(self.audio)}[v]"
            + ("[a]" if self.audio else "")
        )
        self.exporting = True
        self.playing = False
        self.workers.cancel_group(self, "preview")
        self.update_status()
        modal = ExportProgress(self.output.name, kept_frames)
        temporary = None
        try:
            await self.push_screen(modal)
            if overwrite:
                with NamedTemporaryFile(
                    dir=self.output.parent, suffix=self.output.suffix, delete=False
                ) as tmp:
                    temporary = Path(tmp.name)
            await run_media(
                "ffmpeg",
                "-progress",
                "pipe:1",
                "-nostdin",
                "-v",
                "error",
                "-y" if overwrite else "-n",
                *(["-ss", f"{seek:.6f}"] if seek else []),
                "-i",
                str(self.source),
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[v]",
                *(["-map", "[a]"] if self.audio else []),
                "-fps_mode:v",
                "vfr",
                str(temporary or self.output),
                on_frame=modal.update_progress,
            )
            if temporary is not None:
                temporary.replace(self.output)
        except (ValueError, OSError) as exc:
            self.notify(str(exc), title="Export failed", severity="error")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            self.exporting = False
            if self.screen is modal:
                await modal.dismiss()
            self.refresh()
