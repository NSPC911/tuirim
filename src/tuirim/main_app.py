import asyncio
import json
import re
import subprocess
from bisect import bisect_right, insort
from collections.abc import Callable
from fractions import Fraction
from importlib import resources
from importlib.util import find_spec
from itertools import pairwise
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from typing import cast

from PIL import Image
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Container
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


def format_ytes(size: float) -> str:
    for unit in ["", "K", "M", "G", "T", "P"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} E"


def ft(seconds: float) -> str:
    total_seconds = int(seconds)
    milliseconds = round((seconds - total_seconds) * 100)

    if milliseconds == 100:
        total_seconds += 1
        milliseconds = 0

    minutes, sec = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}.{milliseconds:02d}"
    return f"{minutes:02d}:{sec:02d}.{milliseconds:02d}"


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


class PreviewClock:
    def __init__(self, start: float) -> None:
        self.anchor = monotonic() - start
        self.ready = asyncio.Event()

    async def follow(self, stream: asyncio.StreamReader, origin: float) -> None:
        # FFplay emits a carriage-return-delimited status with the master clock.
        try:
            while True:
                record = await stream.readuntil(b"\r")
                match = re.search(rb"(?:^|\s)(-?\d+\.\d+)\s+M-A:", record)
                if match:
                    self.anchor = monotonic() - (float(match[1]) - origin)
                    self.ready.set()
        except asyncio.IncompleteReadError:
            pass
        finally:
            self.ready.set()


class ModalInput(ModalScreen[str]):
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
            Container(TImage(image.copy(), id="preview"), id="viewport"), before=0
        )
        self.query_one(Rule).styles.color = self.app.theme_variables["accent"]


class Timeline(Static):
    # def on_click(self, event: events.Click) -> None:
    #     app = cast("App", self.app)
    #     app.seek_time(app.start + app.duration * event.x / max(1, self.size.width - 1))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.capture_mouse()
        app = cast("App", self.app)
        app.seek_time(app.start + app.duration * event.x / max(1, self.size.width - 1))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        # app = cast("App", self.app)
        # app.seek_time(app.start + app.duration * event.x / max(1, self.size.width - 1))
        self.release_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.app.mouse_captured == self:
            app = cast("App", self.app)
            app.seek_time(
                app.start + app.duration * event.x / max(1, self.size.width - 1)
            )

    def render(self) -> Text:
        app = cast("App", self.app)
        width = self.size.width
        scale = max(1, width - 1) / (app.duration or 1)
        cursor = round(app.elapsed_time(app.frame) * scale)
        boundaries = {round(app.elapsed_time(i) * scale) for i in app.cuts[1:]}
        bar = Text(no_wrap=True)
        for x in range(width):
            frame = app.frame_at(app.start + x / scale)
            ignored = app.section(frame) in app.ignored
            glyph = "╌" if ignored else "━"
            item: tuple[str, str] | None
            if x == cursor:
                item = ("◈", "bold white")
            elif x in boundaries:
                section = lambda sect: app.section(
                    app.frame_at(app.start + (sect) / scale)
                )
                left = section(x - 1) in app.ignored
                right = section(x + 1) in app.ignored
                if left and right:
                    item = ("┇", app.theme_variables["error"][5:])
                else:
                    item = ("┃", app.theme_variables["secondary"][5:])
            else:
                item = (
                    glyph,
                    "dim " + app.theme_variables["error"][5:]
                    if ignored
                    else app.theme_variables["secondary"][5:],
                )
            bar.append(*item)
        return bar


class App(DrivenApp):
    ALLOW_SELECT = False

    CSS_PATH = (
        resources.files("_tuirim" if find_spec("_tuirim") else "tuirim") / "style.tcss"
    )

    BINDINGS = [
        Binding("space", "play", "Play"),
        Binding("space", "pause", "Pause"),
        Binding("s", "split", "Split"),
        Binding("c", "clear_splits", "Clear splits"),
        Binding("i", "hide", "hide"),
        Binding("i", "keep", "keep"),
        Binding("r", "invert", "Invert"),
        Binding("comma", "step(-1)", group=Binding.Group("± 1f")),
        Binding("full_stop", "step(1)", group=Binding.Group("± 1f")),
        Binding("less_than_sign", "jump(-1)", group=Binding.Group("±1s")),
        Binding("greater_than_sign", "jump(1)", group=Binding.Group("±1s")),
        Binding("h", "jump(-10)", "−10s", group=Binding.Group("±10s")),
        Binding("l", "jump(10)", "+10s", group=Binding.Group("±10s")),
        Binding("H", "jump(-60)", "−1m", group=Binding.Group("±60s")),
        Binding("L", "jump(60)", "+1m", group=Binding.Group("±60s")),
        Binding("e", "export", "Export"),
        Binding("q", "quit", "Quit"),
    ]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, source: Path, output: Path) -> None:
        super().__init__(watch_css=True)
        self.theme = "ansi-dark"
        self.source, self.output = source.resolve(), output.resolve()
        probe = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams"]
            + ["-show_format", "-of", "json", str(self.source)]
        )
        info = json.loads(probe)
        streams = info["streams"]
        video = next((s for s in streams if s["codec_type"] == "video"), None)
        if video is None:
            raise ValueError("No video stream")
        self.video = video
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
        self.origin = origin
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
        self.duration = self.elapsed_time(self.frame_count)
        self.cuts = [0]
        self.ignored: set[int] = set()
        self.frame = 0
        self.playing = self.exporting = False
        self.preview = TImage(id="preview")

    def compose(self) -> ComposeResult:
        yield Container(self.preview, id="viewport")
        with Center(), (s := Container(id="footerKinda")):
            yield Static(
                f" [dim]Codec[/] {self.video['codec_name']}  -  [dim]Avg FPS[/] "
                + str(round(Fraction(self.video["avg_frame_rate"])))
                + f"  -  [dim]Resolution[/] {self.video['width']}x{self.video['height']}"
                + f"  -  [dim]Size[/] {format_ytes(self.source.stat().st_size)}B"
                + f"  -  [dim]Bitrate[/] {format_ytes(float(self.video.get('bit_rate', 0)))}bps",
                id="stats",
            )
            s.border_title = f"{self.source.name} → {self.output.name}"
            yield Timeline()
            yield Static(id="progress")
        yield Footer()

    def on_mount(self) -> None:
        self.seek(0)
        self.screen.BINDINGS = []
        self.call_later(self.query_one(Footer).recompose)

    def elapsed_time(self, frame: int) -> float:
        return frame / self.fps

    def frame_time(self, frame: int) -> float:
        return self.start + self.elapsed_time(frame)

    def frame_at(self, seconds: float) -> int:
        frame = round((seconds - self.start) * self.fps)
        return min(
            self.frame_count - 1, max(0, frame - (self.frame_time(frame) > seconds))
        )

    def section(self, frame: int) -> int:
        return self.cuts[bisect_right(self.cuts, frame) - 1]

    def update_progress(self) -> None:
        state = "HIDE" if self.section(self.frame) in self.ignored else "KEEP"
        self.query_one(Timeline).refresh()
        self.query_one("#progress", Static).update(
            f"{'\u25b6' if self.playing else '\u23f8'}  {ft(self.elapsed_time(self.frame))} / "
            f"{ft(self.duration)}   Frame {self.frame + 1}/{self.frame_count}   {state}"
        )

    def seek(self, frame: int) -> None:
        self.playing = False
        self.frame = min(max(frame, 0), self.frame_count - 1)
        self.update_progress()
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
            self.update_progress()

    def action_clear_splits(self) -> None:
        self.cuts = [0]
        self.ignored.clear()
        self.seek(self.frame)

    # in essence, they do the same thing, im lazy to change
    def action_hide(self) -> None:
        self.ignored.symmetric_difference_update({self.section(self.frame)})
        self.seek(self.frame)

    def action_keep(self) -> None:
        self.ignored.symmetric_difference_update({self.section(self.frame)})
        self.seek(self.frame)

    def action_invert(self) -> None:
        self.ignored = set(self.cuts) - self.ignored
        self.seek(self.frame)

    def action_play(self) -> None:
        if len(self.cuts) == len(self.ignored):
            self.notify("No kept sections to play")
            return
        if self.frame == self.frame_count - 1:
            self.frame = 0
        self.playing = True
        self.render_preview()

    def action_pause(self) -> None:
        self.seek(self.frame)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "play":
            return not self.playing
        if action == "pause":
            return self.playing
        if action == "clear_splits":
            return len(self.cuts) > 1
        if action == "ignore":
            return self.section(self.frame) not in self.ignored
        if action == "keep":
            return self.section(self.frame) in self.ignored
        return super().check_action(action, parameters)

    @work(exclusive=True, group="preview")
    async def render_preview(self) -> None:
        play_audio = self.playing and self.audio
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
                first = self.frame
                end = (
                    next(
                        (i for i in self.cuts if i > first and i in self.ignored),
                        self.frame_count,
                    )
                    if self.playing
                    else first + 1
                )
                proc = await asyncio.create_subprocess_exec(
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
                    str(end - first),
                    "-vf",
                    (
                        f"fps={self.fps}:start_time=0,"
                        if self.playing and not self.regular_rate
                        else ""
                    )
                    + "scale=640:360:force_original_aspect_ratio=decrease",
                    "-f",
                    "image2pipe",
                    "-c:v",
                    "ppm",
                    "-fps_mode",
                    "passthrough",
                    "-",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                player = None
                clock_task = None
                start = self.frame_time(first)
                clock = PreviewClock(start)
                try:
                    stream = proc.stdout
                    assert stream is not None
                    for frame in range(first, end):
                        header = await stream.readline()
                        if not header and await proc.wait() == 0:
                            break
                        if header != b"P6\n":
                            raise ValueError("FFmpeg did not produce a preview frame")
                        width, height = map(int, (await stream.readline()).split())
                        if await stream.readline() != b"255\n":
                            raise ValueError("Unexpected preview pixel format")
                        data = await stream.readexactly(width * height * 3)
                        if frame == first:
                            clock.anchor = monotonic() - start
                            if play_audio:
                                try:
                                    player = await asyncio.create_subprocess_exec(
                                        "ffplay",
                                        "-nodisp",
                                        "-vn",
                                        "-autoexit",
                                        "-stats",
                                        "-sync",
                                        "audio",
                                        "-hide_banner",
                                        "-ss",
                                        str(max(0, self.frame_time(first))),
                                        "-af",
                                        f"atrim=start={self.origin + start}:end={self.origin + self.frame_time(end)}",
                                        "-t",
                                        str((end - first) / self.fps),
                                        str(self.source),
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL,
                                        stderr=asyncio.subprocess.PIPE,
                                    )
                                    assert player.stderr is not None
                                    clock_task = asyncio.create_task(
                                        clock.follow(player.stderr, self.origin)
                                    )
                                    try:
                                        await asyncio.wait_for(clock.ready.wait(), 2)
                                    except TimeoutError:
                                        clock.anchor = monotonic() - start
                                except OSError as exc:
                                    play_audio = False
                                    self.notify(
                                        str(exc),
                                        title="Preview audio unavailable",
                                        severity="warning",
                                    )
                        delay = clock.anchor + self.frame_time(frame) - monotonic()
                        if frame != end - 1 and delay < -1 / self.fps:
                            continue
                        await asyncio.sleep(max(0, delay))
                        self.frame = frame
                        self.preview.image = Image.frombytes("RGB", (width, height), data)
                        self.update_progress()
                    if player is not None and self.frame == end - 1:
                        await asyncio.sleep(
                            max(0, clock.anchor + self.frame_time(end) - monotonic())
                        )
                finally:
                    if player is not None:
                        if player.returncode is None:
                            player.kill()
                        await player.wait()
                    if clock_task is not None:
                        await clock_task
                    if proc.returncode is None:
                        proc.kill()
                    await proc.communicate()
                if not self.playing or end == self.frame_count or self.frame != end - 1:
                    break
                self.frame = end
        except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
            self.notify(str(exc), title="Preview failed", severity="error")
        self.playing = False
        self.update_progress()

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
        self.update_progress()
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

    async def on_key(self) -> None:
        self.call_later(self.refresh_bindings)
