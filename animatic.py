"""The Fathom animatic node (FTH-562.8.4): a Story's animatic encoded as an H.264 MP4 with PyAV, with a song's audio as AAC.

Fathom uploads each frame's picture to the input folder under `fathom-animatic/` and submits this one node with
a spec: the video's size and rate, the camera's numbers, each frame's picture or card, seconds, way in,
camera move and how far it moves, and for a song with audio the audio file it uploaded beside the pictures.
The node draws every video frame as the Animatic's player shows that time
(`app/client/src/components/storytime/camera.ts`, whose numbers `docs/architecture.md` states), encodes it, and
writes `fathom-animatic/<name>.mp4` in the output folder, which Fathom reads over `/view`.

The camera's numbers come from the spec alone, so the node holds none of its own: a picture fills the frame at
scale 1, fitted whole on paper; a move scales it about its centre and then shifts it by a fraction of the frame's
width and height, eased with a sine in and out across the frame's seconds. At How far 100 a push or pull runs
between 1 and `biggest_scale`, and a pan or tilt holds the picture at that scale and crosses all its spare width
or height. A dissolve or a fade from black lasts `transition` seconds, or half the incoming frame if shorter, from
the incoming frame's start; the first frame's dissolve plays as a fade from black.

The audio is opened with the one demuxer its name's extension gives (wav, mp3, mp4 for M4A, ogg), reading that
file alone, never a format FFmpeg guesses, so a playlist or concat list named as audio is refused unread. Its
codec must be PCM, MP3, AAC, Vorbis, Opus or FLAC, at `MIN_AUDIO_RATE` to `MAX_AUDIO_RATE` on 1 to
`MAX_AUDIO_CHANNELS` channels. It is decoded with PyAV from its start and muxed as one AAC track, mono for a mono source and else
stereo, at the source's own rate when AAC takes it and else 48 kHz, at `AUDIO_BITRATE` a channel. Its first
sample plays with the first video frame, and it is cut where the video ends; a Story with no audio has no audio
track.

The work is bounded here as Fathom bounds it: at most `MAX_FRAMES` frames, `MAX_SECONDS` seconds and a long
side of `MAX_SIDE`, each picture at most `MAX_PICTURE_PIXELS`, the audio file at most `MAX_AUDIO_BYTES` and
decoded no further than the video runs, and an average of at most `MAX_BITRATE`. The node removes the pictures
and the audio it was given once it has read them, and any animatic it wrote more than `KEEP_SECONDS` ago.

PIL, numpy and PyAV come with ComfyUI; `folder_paths` and `comfy` are imported only when the node runs.
"""

import json
from fractions import Fraction
import math
import os
import re
import textwrap
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .progress import ProgressBar

FOLDER = "fathom-animatic"
NAME = re.compile(r"[A-Za-z0-9_-]{1,80}")
PICTURE = re.compile(r"[A-Za-z0-9_-]{1,80}\.png")
AUDIO = re.compile(r"[A-Za-z0-9_-]{1,80}\.(mp3|wav|m4a|ogg)")
MAX_AUDIO_BYTES = 50 * 1024 * 1024
# Each extension's one demuxer; FFmpeg is never left to guess.
AUDIO_DEMUXERS = {"wav": "wav", "mp3": "mp3", "m4a": "mp4", "ogg": "ogg"}
# The decoders a song's audio may need, by FFmpeg's names, besides any PCM.
AUDIO_CODECS = ("mp3", "mp3float", "aac", "aac_fixed", "vorbis", "libvorbis", "opus", "libopus", "flac")
MIN_AUDIO_RATE = 8000
MAX_AUDIO_RATE = 192000
MAX_AUDIO_CHANNELS = 8
# The rates AAC takes that the soundtrack keeps; any other is resampled to 48 kHz.
AAC_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)
AUDIO_RATE = 48000
AUDIO_BITRATE = 96_000
MAX_FRAMES = 300
MAX_SECONDS = 600
MAX_SIDE = 1920
MAX_PICTURE_PIXELS = 40_000_000
MAX_BITRATE = 8_000_000
KEEP_SECONDS = 3600
WAYS_IN = ("cut", "dissolve", "fade_from_black")
MOVES = ("static", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down")
# The player's card and the paper a picture sits on: Tailwind's #f4f0e6 under #262626 text.
PAPER = (0xF4, 0xF0, 0xE6)
INK = (0x26, 0x26, 0x26)


def input_dir():
    import folder_paths

    return folder_paths.get_input_directory()


def output_dir():
    import folder_paths

    return folder_paths.get_output_directory()


def interrupted():
    """Raises ComfyUI's own interruption when the prompt was cancelled; outside ComfyUI, never."""
    try:
        import comfy.model_management
    except Exception:  # noqa: BLE001
        return
    comfy.model_management.throw_exception_if_processing_interrupted()


class SpecRefused(ValueError):
    """The spec is not one Fathom sends."""


def _number(value, low, high, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < low or value > high:
        raise SpecRefused(f"{what} must be a number from {low} to {high}.")
    return value


def parse_spec(text):
    """The spec as Fathom sends it, every field checked against the node's bounds."""
    try:
        spec = json.loads(text)
    except (TypeError, ValueError) as error:
        raise SpecRefused("The spec is not JSON.") from error
    if not isinstance(spec, dict):
        raise SpecRefused("The spec is not a JSON object.")
    width = int(_number(spec.get("width"), 16, MAX_SIDE, "width"))
    height = int(_number(spec.get("height"), 16, MAX_SIDE, "height"))
    if width % 2 or height % 2:
        raise SpecRefused("width and height must be even for H.264.")
    fps = int(_number(spec.get("fps"), 1, 60, "fps"))
    camera = spec.get("camera")
    if not isinstance(camera, dict):
        raise SpecRefused("camera is missing.")
    biggest = float(_number(camera.get("biggest_scale"), 1, 4, "camera.biggest_scale"))
    transition = float(_number(camera.get("transition"), 0, 10, "camera.transition"))
    frames = spec.get("frames")
    if not isinstance(frames, list) or not frames or len(frames) > MAX_FRAMES:
        raise SpecRefused(f"frames must be a list of 1 to {MAX_FRAMES} frames.")
    parsed = []
    start = 0.0
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise SpecRefused(f"frame {index + 1} is not a JSON object.")
        seconds = float(_number(frame.get("seconds"), 0.1, MAX_SECONDS, f"frame {index + 1}'s seconds"))
        picture = frame.get("picture")
        if picture is not None and (not isinstance(picture, str) or not PICTURE.fullmatch(picture)):
            raise SpecRefused(f"frame {index + 1}'s picture is not a name Fathom gives.")
        comes_in = frame.get("comes_in")
        move = frame.get("move")
        if comes_in not in WAYS_IN:
            raise SpecRefused(f"frame {index + 1}'s comes_in is not one of {', '.join(WAYS_IN)}.")
        if move not in MOVES:
            raise SpecRefused(f"frame {index + 1}'s move is not one of {', '.join(MOVES)}.")
        number = frame.get("number")
        description = frame.get("description", "")
        if not isinstance(number, int) or isinstance(number, bool) or not isinstance(description, str):
            raise SpecRefused(f"frame {index + 1}'s card needs a number and a description.")
        parsed.append({
            "picture": picture,
            "number": number,
            "description": description[:2000],
            "start": start,
            "seconds": seconds,
            "comes_in": comes_in,
            "move": move,
            "amount": float(_number(frame.get("amount"), 0, 100, f"frame {index + 1}'s amount")),
        })
        start += seconds
    if start > MAX_SECONDS + 1e-9:
        raise SpecRefused(f"The animatic is longer than {MAX_SECONDS} seconds.")
    audio = spec.get("audio")
    if audio is not None and (not isinstance(audio, str) or not AUDIO.fullmatch(audio)):
        raise SpecRefused("audio is not a name Fathom gives.")
    return {"width": width, "height": height, "fps": fps, "biggest_scale": biggest, "transition": transition, "frames": parsed, "length": start, "audio": audio}


# The camera function, as camera.ts has it.


def eased(p):
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    return (1 - math.cos(math.pi * p)) / 2


def move_at(move, amount, p, biggest):
    """(scale, x, y): the picture's scale about its centre, then its shift as a fraction of the frame's width and height."""
    most = 1 + (biggest - 1) * (min(max(amount, 0), 100) / 100)
    e = eased(p)
    edge = (most - 1) / 2
    across = -edge + 2 * edge * e
    if move == "push_in":
        return (1 + (most - 1) * e, 0.0, 0.0)
    if move == "pull_out":
        return (most - (most - 1) * e, 0.0, 0.0)
    if move == "pan_left":
        return (most, across, 0.0)
    if move == "pan_right":
        return (most, -across, 0.0)
    if move == "tilt_up":
        return (most, 0.0, across)
    if move == "tilt_down":
        return (most, 0.0, -across)
    return (1.0, 0.0, 0.0)


def transition_of(frame, transition):
    return 0.0 if frame["comes_in"] == "cut" else min(transition, frame["seconds"] / 2)


def _placed(frame, at, biggest):
    p = (at - frame["start"]) / frame["seconds"] if frame["seconds"] > 0 else 1
    return move_at(frame["move"], frame["amount"], p, biggest)


def shot_at(frames, time_, biggest, transition):
    """What playback shows at `time_`: (over, under), over as (index, opacity, placement), under as "black", one such layer, or None."""
    index = next((i for i, frame in enumerate(frames) if time_ < frame["start"] + frame["seconds"]), len(frames) - 1)
    frame = frames[index]
    at = max(time_, frame["start"])
    over = (index, 1.0, _placed(frame, min(at, frame["start"] + frame["seconds"]), biggest))
    fade = transition_of(frame, transition)
    into = at - frame["start"]
    if fade <= 0 or into >= fade - 1e-9:
        return over, None
    over = (index, into / fade, over[2])
    if frame["comes_in"] == "fade_from_black" or index == 0:
        return over, "black"
    before = frames[index - 1]
    return over, (index - 1, 1.0, _placed(before, before["start"] + before["seconds"], biggest))


# Drawing.


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # A Pillow before 10.1 has only the small bitmap font.
        return ImageFont.load_default()


def card(width, height, number, description):
    """A frame with no picture, as the player draws it: its number, then what we see, centred on paper."""
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)
    small = _font(max(12, round(min(width, height) * 0.03)))
    large = _font(max(16, round(min(width, height) * 0.05)))
    lines = textwrap.wrap(description, width=32)[:4]
    if len(textwrap.wrap(description, width=32)) > 4:
        lines[-1] = lines[-1][:31].rstrip() + "…"
    gap = round(large.size * 0.4)
    heights = [small.size] + [large.size] * len(lines)
    top = (height - (sum(heights) + gap * len(lines))) / 2
    draw.text((width / 2, top), f"Frame {number}", fill=INK, font=small, anchor="mt")
    y = top + small.size + gap
    for line in lines:
        draw.text((width / 2, y), line, fill=INK, font=large, anchor="mt")
        y += large.size + gap
    return image


def fitted(path, width, height):
    """A picture fitted whole into the frame on paper, as the player's object-contain shows it."""
    with Image.open(path) as picture:
        if picture.width * picture.height > MAX_PICTURE_PIXELS:
            raise ValueError("picture too large")
        picture = picture.convert("RGB")
        scale = min(width / picture.width, height / picture.height)
        size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
        picture = picture.resize(size, Image.Resampling.LANCZOS)
        image = Image.new("RGB", (width, height), PAPER)
        image.paste(picture, ((width - size[0]) // 2, (height - size[1]) // 2))
        return image


def placed(image, placement):
    """The picture as CSS's `translate(x%, y%) scale(s)` about its centre draws it, black where it leaves the frame."""
    scale, x, y = placement
    if scale == 1 and x == 0 and y == 0:
        return image
    width, height = image.size
    cx, cy = width / 2, height / 2
    # Output point q comes from picture point c + (q - c - shift) / scale.
    a = 1 / scale
    c = cx - (cx + x * width) / scale
    f = cy - (cy + y * height) / scale
    return image.transform((width, height), Image.Transform.AFFINE, (a, 0, c, 0, a, f), resample=Image.Resampling.BILINEAR, fillcolor=(0, 0, 0))


def composed(picture, over, under):
    """The video frame: `picture(index)` is a frame's picture or card, drawn in its place over what it comes in from."""
    index, opacity, placement = over
    top = placed(picture(index), placement)
    if under is None or opacity >= 1:
        return top
    bottom = Image.new("RGB", top.size, (0, 0, 0)) if under == "black" else placed(picture(under[0]), under[2])
    return Image.blend(bottom, top, opacity)


class Pictures:
    """Each frame's picture or card, drawn when first shown; only the two a way in shows at once are held, so 300 pictures never sit in memory together."""

    def __init__(self, frames, folder, width, height):
        self.frames = frames
        self.folder = folder
        self.size = (width, height)
        self.held = {}

    def path(self, index):
        name = self.frames[index]["picture"]
        return None if name is None else os.path.join(self.folder, name)

    def __call__(self, index):
        if index not in self.held:
            for old in [held for held in self.held if held not in (index, index - 1)]:
                del self.held[old]
            frame = self.frames[index]
            picture = None
            path = self.path(index)
            if path is not None:
                try:
                    picture = fitted(path, *self.size)
                except (OSError, ValueError, Image.DecompressionBombError):
                    picture = None
            self.held[index] = picture if picture is not None else card(*self.size, frame["number"], frame["description"])
        return self.held[index]

    def remove(self):
        """Removes every picture Fathom uploaded for this animatic."""
        for index in range(len(self.frames)):
            path = self.path(index)
            if path is not None:
                try:
                    os.remove(path)
                except OSError:
                    pass


class Soundtrack:
    """A song's audio as the MP4's AAC track: decoded from its start as the video is written, so its first sample plays with the first
    video frame, and cut at `seconds`, where the video ends. Only what the video needs is decoded, a few frames at a time."""

    def __init__(self, av, path, container, seconds):
        try:
            size = os.path.getsize(path)
        except OSError as error:
            raise SpecRefused("The song's audio is not in the input folder.") from error
        if size > MAX_AUDIO_BYTES:
            raise SpecRefused(f"The song's audio is over {MAX_AUDIO_BYTES // (1024 * 1024)} MB.")
        self.av = av
        self.container = container
        # The demuxer is fixed by the name's extension and reads only this file, so FFmpeg never probes the file for a format of its
        # own: a playlist or a concat list named as audio would otherwise have it read other files or addresses.
        demuxer = AUDIO_DEMUXERS[os.path.splitext(path)[1][1:]]
        try:
            self.source = av.open(path, format=demuxer, options={"protocol_whitelist": "file"})
        except av.error.FFmpegError as error:
            raise SpecRefused(f"The song's audio is not a readable {demuxer} file.") from error
        try:
            if not self.source.streams.audio:
                raise SpecRefused("The song's audio holds no sound.")
            source = self.source.streams.audio[0]
            codec = source.codec_context.name
            if not (codec.startswith("pcm_") or codec in AUDIO_CODECS):
                raise SpecRefused(f"The song's audio is {codec}, not a codec Fathom takes.")
            rate, channels = source.codec_context.sample_rate, source.codec_context.layout.nb_channels
            if not (MIN_AUDIO_RATE <= rate <= MAX_AUDIO_RATE and 1 <= channels <= MAX_AUDIO_CHANNELS):
                raise SpecRefused(f"The song's audio runs at {rate} Hz on {channels} channels; Fathom takes {MIN_AUDIO_RATE} to {MAX_AUDIO_RATE} Hz on 1 to {MAX_AUDIO_CHANNELS}.")
            mono = channels == 1
            self.layout = "mono" if mono else "stereo"
            self.rate = source.codec_context.sample_rate if source.codec_context.sample_rate in AAC_RATES else AUDIO_RATE
            self.stream = container.add_stream("aac", rate=self.rate)
            self.stream.codec_context.layout = self.layout
            self.stream.codec_context.bit_rate = AUDIO_BITRATE * (1 if mono else 2)
            self.resampler = av.AudioResampler(format="fltp", layout=self.layout, rate=self.rate)
            self.decoded = self.source.decode(source)
            self.total = round(seconds * self.rate)
            self.written = 0
        except BaseException:
            self.source.close()
            raise

    def _write(self, frames):
        for decoded in frames:
            keep = min(decoded.samples, self.total - self.written)
            if keep <= 0:
                return
            samples = np.ascontiguousarray(decoded.to_ndarray()[:, :keep])
            frame = self.av.AudioFrame.from_ndarray(samples, format="fltp", layout=self.layout)
            frame.sample_rate = self.rate
            frame.pts = self.written
            frame.time_base = Fraction(1, self.rate)
            for packet in self.stream.encode(frame):
                self.container.mux(packet)
            self.written += keep

    def to(self, seconds):
        """Encodes the audio up to `seconds` into the video, or its end if that comes first."""
        target = min(self.total, round(seconds * self.rate))
        while self.decoded is not None and self.written < target:
            decoded = next(self.decoded, None)
            if decoded is None:
                self._write(self.resampler.resample(None))
                self.decoded = None
            else:
                self._write(self.resampler.resample(decoded))

    def finish(self):
        self.to(self.total / self.rate)
        for packet in self.stream.encode(None):
            self.container.mux(packet)

    def close(self):
        self.source.close()


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# The node.


def _sweep(folder, older_than):
    """Removes the animatics this node wrote before `older_than`, and any partial one."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if not name.endswith(".mp4"):
            continue
        path = os.path.join(folder, name)
        try:
            if os.path.getmtime(path) < older_than:
                os.remove(path)
        except OSError:
            pass


def encode(spec, name, progress=None):
    """Writes `fathom-animatic/<name>.mp4` in the output folder from a parsed spec; answers the file's ComfyUI address."""
    import av

    width, height, fps = spec["width"], spec["height"], spec["fps"]
    frames = spec["frames"]
    pictures = Pictures(frames, os.path.join(input_dir(), FOLDER), width, height)
    audio = None if spec.get("audio") is None else os.path.join(input_dir(), FOLDER, spec["audio"])
    folder = os.path.join(output_dir(), FOLDER)
    final = os.path.join(folder, f"{name}.mp4")
    partial = os.path.join(folder, f"{name}.part.mp4")
    total = max(1, round(spec["length"] * fps))
    try:
        os.makedirs(folder, exist_ok=True)
        _sweep(folder, time.time() - KEEP_SECONDS)
        with av.open(partial, mode="w", format="mp4", options={"movflags": "+faststart"}) as container:
            stream = container.add_stream("libx264", rate=fps)
            stream.width = width
            stream.height = height
            stream.pix_fmt = "yuv420p"
            stream.options = {"crf": "20", "preset": "veryfast", "maxrate": str(MAX_BITRATE), "bufsize": str(MAX_BITRATE * 2)}
            soundtrack = None if audio is None else Soundtrack(av, audio, container, total / fps)
            try:
                last_key = None
                last = None
                for k in range(total):
                    interrupted()
                    over, under = shot_at(frames, k / fps, spec["biggest_scale"], spec["transition"])
                    # A still frame draws once: the same layers in the same places give the same picture.
                    key = (over, under)
                    if key != last_key:
                        last = np.asarray(composed(pictures, over, under))
                        last_key = key
                    frame = av.VideoFrame.from_ndarray(last, format="rgb24")
                    frame.pts = k
                    frame.time_base = Fraction(1, fps)
                    for packet in stream.encode(frame):
                        container.mux(packet)
                    # The audio goes in beside its video frames, so the two are interleaved through the file.
                    if soundtrack is not None:
                        soundtrack.to((k + 1) / fps)
                    if progress is not None and (k % fps == 0 or k == total - 1):
                        progress(k + 1, total)
                for packet in stream.encode():
                    container.mux(packet)
                if soundtrack is not None:
                    soundtrack.finish()
            finally:
                if soundtrack is not None:
                    soundtrack.close()
        os.replace(partial, final)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    finally:
        pictures.remove()
        if audio is not None:
            _remove(audio)
    return {"filename": f"{name}.mp4", "subfolder": FOLDER, "type": "output"}


class FathomAnimatic:
    CATEGORY = "Fathom"
    RETURN_TYPES = ()
    FUNCTION = "make"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"spec": ("STRING", {"default": "{}", "multiline": True}), "name": ("STRING", {"default": "animatic"})}}

    def make(self, spec, name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise SpecRefused("name must be letters, digits, - and _, at most 80.")
        parsed = parse_spec(spec)
        bar = None
        if ProgressBar is not None:
            try:
                bar = ProgressBar(1000)
            except Exception:  # noqa: BLE001
                bar = None

        def progress(done, total):
            if bar is not None:
                try:
                    bar.update_absolute(round(1000 * done / total), 1000)
                except Exception:  # noqa: BLE001
                    pass

        return {"ui": {"animatic": [encode(parsed, name, progress)]}}
