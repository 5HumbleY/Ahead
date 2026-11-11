import copy
import hashlib
import json
from pathlib import Path
import time
import urllib.request
import uuid
from .catalog import PreparedMetadata
from .observed_sources import SourceData
from .embedded_data import structure


def sha(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def table_value(raw, ref, url):
    labels = [h["text"] for h in raw["headers"]]
    width = max([len(labels)] + [len(r) for r in raw["cells"]])
    labels += [""] * (width - len(labels))
    used = set()
    keys = []
    for i, label in enumerate(labels):
        base = label or "column_" + str(i)
        key = base
        n = 2
        while key in used:
            key = base + "#" + str(n)
            n += 1
        used.add(key)
        keys.append(key)
    rows = []
    primary = []
    details = []
    for raw_row in raw["cells"]:
        row = [
            {
                **c,
                "primary_text": " ".join(c["text_nodes"])
                if c["text_nodes"]
                else c["text"],
            }
            for c in raw_row
        ]
        details.append(row)
        rows.append(
            {
                key: row[i]["text"] if i < len(row) else None
                for (i, key) in enumerate(keys)
            }
        )
        primary.append(
            {
                key: row[i]["primary_text"] if i < len(row) else None
                for (i, key) in enumerate(keys)
            }
        )
    columns = [
        {
            "key": key,
            "label": labels[i],
            "position": i,
            "title": raw["headers"][i].get("title", "")
            if i < len(raw["headers"])
            else "",
            "samples": [
                {k: row[i][k] for k in ("text", "primary_text", "annotations")}
                for row in details[:2]
                if i < len(row)
            ],
        }
        for (i, key) in enumerate(keys)
    ]
    profile = {
        "ref": ref,
        "url": url,
        "columns": columns,
        "row_count": len(rows),
        "scope": "Currently rendered table page; inspect controls for pagination/filtering.",
    }
    data = {
        "ref": ref,
        "url": url,
        "headers": labels,
        "column_keys": keys,
        "records": rows,
        "primary_records": primary,
        "rows": [[c["text"] for c in row] for row in details],
        "cell_details": details,
        "row_count": len(rows),
    }
    return (profile, data)


class WebTools:
    def __init__(self, endpoint, directory, session=None):
        self.endpoint = endpoint
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.session = session or uuid.uuid4().hex
        self.count = 0
        self.snapshots = {}
        self.current = None
        self.last_resource = None
        self.capture_dir = self.directory / "snapshots"
        self.capture_dir.mkdir(exist_ok=True)
        self.last_available = None

    def capture(self, frame):
        state = frame["state"]
        version = frame["version"]
        profiles = []
        for i, raw in enumerate(frame["tables"]):
            ref = "table:" + version + ":" + str(i)
            (profile, data) = table_value(raw, ref, state["url"])
            path = self.capture_dir / (sha(ref) + ".json")
            if not path.exists():
                path.write_text(
                    json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n"
                )
            self.snapshots[ref] = {
                "path": path,
                "version": hashlib.sha256(path.read_bytes()).hexdigest(),
                "profile": profile,
            }
            profiles.append(profile)
        datasets = []
        for index, item in enumerate(frame.get("datasets", [])):
            ref = "dataset:" + version + ":" + str(index)
            value = {
                "data": item["data"],
                "name": item["name"],
                "url": state["url"],
                "ref": ref,
            }
            path = self.capture_dir / (sha(ref) + ".json")
            if not path.exists():
                path.write_text(
                    json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
                )
            observed = {
                "ref": ref,
                "name": item["name"],
                "carrier": item["carrier"],
                "structure": structure(item["data"]),
            }
            self.snapshots[ref] = {
                "path": path,
                "version": hashlib.sha256(path.read_bytes()).hexdigest(),
                "profile": observed,
                "kind": "dataset",
            }
            datasets.append(observed)
        status = state.get("http_status")
        profile = {
            "ref": "page:" + version,
            "url": state["url"],
            "title": state["title"],
            "version": version,
            "http_status": status,
            "available": status is None or 200 <= status < 400,
            "links": state["links"],
            "controls": state["controls"],
            "tables": profiles,
            "datasets": datasets,
            "text_preview": state["text"][:1000],
        }
        self.current = profile
        self.last_resource = ("current", version, profile)
        if profile["available"]:
            self.last_available = copy.deepcopy(profile)
        return PreparedMetadata(copy.deepcopy(profile))

    def call(self, op, **args):
        started = time.time()
        payload = {"session": self.session, "op": op, "args": args}
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=40) as response:
            body = json.load(response)
        if op != "close":
            self.count += 1
            (self.directory / (str(self.count).zfill(3) + ".json")).write_text(
                json.dumps(
                    {
                        "at": started,
                        "seconds": time.time() - started,
                        **payload,
                        **body,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        if not body.get("ok"):
            raise RuntimeError(body.get("error", "Browser request failed"))
        if op == "close":
            return body["result"]
        profile = self.capture(body["result"])
        return body["result"]["payload"] if op == "html" else profile

    def web_open(self, url):
        profile = self.call("open", url=url)
        if not profile["available"]:
            error = RuntimeError(
                "Source acquisition failed: HTTP "
                + str(profile["http_status"])
                + " at "
                + profile["url"]
                + ". Use an actually available observed source before consuming its data."
            )
            previous = self.last_available
            error.source_failure = {
                "requested": {
                    key: profile[key] for key in ("url", "http_status", "title")
                },
                "last_available": None
                if previous is None
                else {
                    key: previous[key]
                    for key in ("url", "ref", "title", "tables", "datasets")
                },
                "observed_links": [] if previous is None else previous["links"][:24],
                "remaining_observations": "read_evidence / evidence_index retain complete previous source profiles",
            }
            raise error
        return profile

    def web_read(self):
        return self.call("read")

    def web_table(self, ref=0):
        if isinstance(ref, int):
            self.web_read()
            if not 0 <= ref < len(self.current["tables"]):
                raise ValueError(
                    "The observed page has no table at that index; inspect its HTTP status, links and datasets before selecting a source"
                )
            ref = self.current["tables"][ref]["ref"]
        item = self.snapshots.get(ref)
        if item is None:
            raise KeyError(
                "Use a table ref returned by a page profile, or an index for the current page"
            )
        data = json.loads(item["path"].read_text())
        version = hashlib.sha256(item["path"].read_bytes()).hexdigest()
        self.last_resource = (ref, version, item["profile"])
        return SourceData(data)

    def web_dataset(self, ref):
        item = self.snapshots.get(ref)
        if not item or item.get("kind") != "dataset":
            raise KeyError("Use an immutable dataset ref from a page profile")
        value = json.loads(item["path"].read_text())
        self.last_resource = (
            ref,
            hashlib.sha256(item["path"].read_bytes()).hexdigest(),
            item["profile"],
        )
        return SourceData(value)

    def web_html(self, selector="body", start=0, length=12000):
        return self.call("html", selector=selector, start=start, length=length)

    def web_click(self, selector):
        return self.call("click", selector=selector)

    def web_fill(self, selector, value):
        return self.call("fill", selector=selector, value=value)

    def web_select(self, selector, value):
        return self.call("select", selector=selector, value=value)
