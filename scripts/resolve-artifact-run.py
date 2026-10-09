#!/usr/bin/env python3
"""解析具有全部所需 artifact 的成功 Actions run；过期或失败的 run 不可用。"""

import argparse
import json
import os
import subprocess


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True))


def resolve(repository, workflow, names, run_id=None):
    if run_id:
        candidates = [api(f"repos/{repository}/actions/runs/{int(run_id)}")]
    else:
        candidates = api(f"repos/{repository}/actions/workflows/{workflow}/runs?status=success&per_page=50")["workflow_runs"]
    for run in candidates:
        if run["status"] != "completed" or run["conclusion"] != "success":
            continue
        artifacts = api(f"repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
        available = {a["name"] for a in artifacts if not a["expired"]}
        if set(names) <= available:
            return str(run["id"])
    raise ValueError(f"no successful {workflow} run has nonexpired artifacts {names}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--artifact", action="append", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--output-key", default="run_id")
    args = ap.parse_args()
    if not args.repository:
        raise ValueError("repository is required")
    value = resolve(args.repository, args.workflow, args.artifact, args.run_id)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(args.output_key + "=" + value + "\n")
    print(value)


if __name__ == "__main__":
    main()
