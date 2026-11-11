import hashlib, json, subprocess
from pathlib import Path

OFFICIAL_BASE = "ghcr.io/snowflake-labs/data-eng-bench-base:1.0.0"


def build_task_environment(taskroot, base_image, record_dir):
    environment = Path(taskroot) / "environment"
    record_dir = Path(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)
    original = (environment / "Dockerfile").read_text()
    source_line = "FROM " + OFFICIAL_BASE
    if source_line not in original:
        raise ValueError(
            "Unsupported task base image; inspect its official Dockerfile before running"
        )
    adapted = original.replace(source_line, "FROM " + base_image, 1)
    (record_dir / "Dockerfile.original").write_text(original)
    buildfile = record_dir / "Dockerfile.agent"
    buildfile.write_text(adapted)
    hashes = {
        str(p.relative_to(environment)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(environment.rglob("*"))
        if p.is_file()
    }
    base_id = subprocess.check_output(
        ["docker", "image", "inspect", base_image, "--format", "{{.Id}}"], text=True
    ).strip()
    digest = hashlib.sha256(
        json.dumps([base_id, hashes], sort_keys=True).encode()
    ).hexdigest()[:16]
    image = "ahead-data-eng-task:" + digest
    with (record_dir / "build.log").open("w") as log:
        subprocess.run(
            [
                "docker",
                "build",
                "--network=none",
                "-f",
                str(buildfile.resolve()),
                "-t",
                image,
                str(environment.resolve()),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    record = {
        "base_image": base_id,
        "task_image": image_id,
        "task_image_tag": image,
        "environment_source_hashes": hashes,
        "adaptation": "Official task Dockerfile applied in full; FROM replaced with provisioned offline agent base only.",
    }
    (record_dir / "image.json").write_text(json.dumps(record, indent=2) + "\n")
    return (image, record)
