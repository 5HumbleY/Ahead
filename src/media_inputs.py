import base64
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}


class MediaInputs:
    def __init__(self, root, workspace, config, event=lambda *a, **k: None):
        self.root = Path(root).resolve()
        self.workspace = Path(workspace).resolve()
        self.config = config
        self.event = event
        self.pending = []
        self.observed = []
        self.directory = self.workspace / "media_observations"
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, value):
        path = Path(value).resolve()
        if not path.is_relative_to(self.root) and (
            not path.is_relative_to(self.workspace)
        ):
            raise ValueError("Media must be in the task input or work directory")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def inspect_video(self, path):
        path = self.path(path)
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        duration = re.search(
            "Duration:\\s*(\\d+):(\\d+):(\\d+(?:\\.\\d+)?)", proc.stderr
        )
        if not duration:
            raise ValueError("Could not read video duration: " + proc.stderr[-1500:])
        seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
        streams = [
            line.strip()
            for line in proc.stderr.splitlines()
            if re.search("Stream #\\d+:\\d+", line)
        ]
        return {
            "path": str(path),
            "duration_seconds": seconds,
            "streams": streams,
            "bytes": path.stat().st_size,
            "visual_observation": "video_frames(path, timestamps=[...]) decodes exact addressed frames and queues them for your next model response. Up to 4 images per turn. image observations preserve timestamps; sampling is not proof of complete temporal coverage.",
            "audio": "Audio is not transcribed by this visual adapter. Do not infer speech from images.",
        }

    def video_frames(self, path, timestamps):
        path = self.path(path)
        meta = self.inspect_video(path)
        if not isinstance(timestamps, (list, tuple)) or not 1 <= len(timestamps) <= 4:
            raise ValueError("Choose 1 to 4 timestamps in seconds")
        if len(self.pending) + len(timestamps) > 4:
            raise ValueError("At most four pending image observations per model call")
        if any(
            (
                not isinstance(t, (int, float))
                or not math.isfinite(t)
                or (not 0 <= t < meta["duration_seconds"])
                for t in timestamps
            )
        ):
            raise ValueError("Timestamp outside observed duration")
        rows = []
        for t in timestamps:
            key = hashlib.sha256(
                (str(path) + str(path.stat().st_mtime_ns) + repr(t)).encode()
            ).hexdigest()[:24]
            target = self.directory / (key + ".png")
            if not target.exists():
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        "-ss",
                        str(t),
                        "-i",
                        str(path),
                        "-frames:v",
                        "1",
                        "-threads",
                        "1",
                        str(target),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=45,
                )
            rows.append(
                self.observe_image(target, label=f"{path.name} at {t:g} seconds")
            )
        self.event(
            "video_frames_observed", source=str(path), timestamps=list(timestamps)
        )
        return {
            "frames": rows,
            "coverage": "Only these timestamps observed; inspect intervening changes or unreadable regions as needed. Retain extracted records and deduplicate identities across overlapping frames.",
        }

    def observe_image(self, path, label="", crop=None):
        from PIL import Image

        path = self.path(path)
        if len(self.pending) >= 4:
            raise ValueError("At most four pending images per turn")
        with Image.open(path) as im:
            im = im.convert("RGB")
            original = im.size
            if crop is not None:
                if (
                    len(crop) != 4
                    or not 0 <= crop[0] < crop[2] <= im.width
                    or (not 0 <= crop[1] < crop[3] <= im.height)
                ):
                    raise ValueError(
                        "Crop is [left,top,right,bottom] within original pixels"
                    )
                im = im.crop(tuple(crop))
            im.thumbnail((self.config.get("media_max_side", 1280),) * 2)
            output = io.BytesIO()
            im.save(output, format="PNG")
            data = output.getvalue()
        row = {
            "path": str(path),
            "label": label,
            "original_size": original,
            "sent_size": im.size,
            "crop": crop,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        self.pending.append(
            (row, "data:image/png;base64," + base64.b64encode(data).decode())
        )
        self.observed.append(row)
        (self.directory / "observations.json").write_text(
            json.dumps(self.observed, ensure_ascii=False, indent=2)
        )
        return row

    def messages(self):
        if not self.pending:
            return []
        content = [
            {
                "type": "text",
                "text": "Actual task-local visual observations. Read the images themselves; retain extracted facts in your program with their timestamp/source. These sampled images do not certify full video coverage.",
            }
        ]
        pending = self.pending
        if len(pending) > self.config.get("media_max_images", 4):
            from PIL import Image, ImageDraw, ImageFont

            columns = 2
            size = 640
            rows = math.ceil(len(pending) / columns)
            sheet = Image.new("RGB", (columns * size, rows * (size + 40)), "white")
            draw = ImageDraw.Draw(sheet)
            positions = []
            for index, (row, data) in enumerate(pending):
                with Image.open(
                    io.BytesIO(base64.b64decode(data.split(",", 1)[1]))
                ) as im:
                    im = im.convert("RGB")
                    im.thumbnail((size, size))
                    x = index % columns * size
                    y = index // columns * (size + 40)
                    sheet.paste(im, (x, y + 40))
                    draw.text(
                        (x + 5, y + 5),
                        str(index + 1),
                        fill="black",
                        font=ImageFont.load_default(size=24),
                    )
                    positions.append({"panel": index + 1, **row})
            encoded = io.BytesIO()
            sheet.save(encoded, format="PNG")
            raw = encoded.getvalue()
            identity = hashlib.sha256(raw).hexdigest()
            pending = [
                (
                    {
                        "label": "Contact sheet overview; panel metadata follows. Use observe_image(original_path,crop=...) to read small text.",
                        "panels": positions,
                        "sha256": identity,
                    },
                    "data:image/png;base64," + base64.b64encode(raw).decode(),
                )
            ]
        for row, data in pending:
            content.extend(
                [
                    {"type": "text", "text": json.dumps(row, ensure_ascii=False)},
                    {
                        "type": "image_url",
                        "image_url": {"url": data},
                        "uuid": "vision-" + row["sha256"],
                    },
                ]
            )
        return [{"role": "user", "content": content}]

    def acknowledge(self):
        self.pending.clear()
