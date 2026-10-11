"""Credential-free hosted smoke check for the disposable Docker repair boundary."""

import os
from pathlib import Path
import tempfile

from sandbox_runtime import DockerCommandRunner


def main() -> None:
    # This is a synthetic marker, never the actual model key.
    os.environ["SPECFARO_DEEPSEEK_API_KEY"] = "synthetic-smoke-marker"
    os.environ["GH_TOKEN"] = "synthetic-github-marker"
    with tempfile.TemporaryDirectory(prefix="specfaro-sandbox-smoke-") as temp:
        project = Path(temp) / "project"
        project.mkdir()
        runner = DockerCommandRunner()
        script = (
            "const fs=require('node:fs');"
            "if(process.env.SPECFARO_DEEPSEEK_API_KEY||process.env.GH_TOKEN)process.exit(11);"
            "if(fs.existsSync('/var/run/docker.sock'))process.exit(12);"
            "if(fs.existsSync('/etc/relay/deepseek_api_key'))process.exit(13);"
            "fs.writeFileSync('/workspace/sandbox-created.txt','isolated');"
            "console.log('credential-and-mount-boundary-passed');"
        )
        result = runner.run_argv(["node", "-e", script], project, 60)
        if result["status"] != "passed" or "boundary-passed" not in result["stdout"]:
            raise RuntimeError(f"Sandbox smoke failed: {result.get('reason', result.get('stderr', 'unknown'))[:500]}")
        if (project / "sandbox-created.txt").read_text() != "isolated":
            raise RuntimeError("Sandbox did not write to the mounted project copy")
        network = runner.run_argv(["node", "-e",
                                   "fetch('https://example.com',{signal:AbortSignal.timeout(3000)})"
                                   ".then(()=>process.exit(21),()=>console.log('network-blocked'))"],
                                  project, 15)
        if network["status"] != "passed" or "network-blocked" not in network["stdout"]:
            raise RuntimeError("Sandbox unexpectedly reached the public network")
    print("Sandbox smoke passed: no credential env, Docker socket, Oracle key path, or network; project copy writable")


if __name__ == "__main__":
    main()
