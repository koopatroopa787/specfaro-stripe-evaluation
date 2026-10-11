"""Run untrusted repair commands in a credential-free Docker container.

Only a copied project directory is mounted. The controller keeps the model key,
official-doc tool, patch policy, and Docker socket outside the container.
"""

from __future__ import annotations

from pathlib import Path

from openhands.sdk.tool import ToolExecutor, register_tool
from openhands.tools.file_editor.definition import FileEditorAction, FileEditorObservation, FileEditorTool
from openhands.tools.file_editor.impl import FileEditorExecutor
from openhands.tools.terminal.definition import TerminalAction, TerminalObservation, TerminalTool
from openhands.tools.terminal.metadata import CmdOutputMetadata

from sandbox_runtime import DEFAULT_IMAGE, MAX_OUTPUT_CHARS, DockerCommandRunner, SandboxUnavailable

MAX_TERMINAL_SECONDS = 120


class SandboxTerminalExecutor(ToolExecutor):
    def __init__(self, root: Path, runner: DockerCommandRunner):
        self.root = root
        self.runner = runner

    def __call__(self, action: TerminalAction, conversation=None) -> TerminalObservation:
        if action.is_input or action.reset or not action.command:
            return TerminalObservation.from_text(
                text="This sandbox runs one complete command per call; interactive input is unavailable.",
                command=action.command, exit_code=2, is_error=True)
        timeout = min(MAX_TERMINAL_SECONDS, max(1, int(action.timeout or 30)))
        result = self.runner.run_shell(action.command, self.root, timeout)
        output = (result.get("stdout", "") + result.get("stderr", ""))[-MAX_OUTPUT_CHARS:]
        return TerminalObservation.from_text(
            text=output, command=action.command,
            exit_code=result.get("exit_code", 124),
            timeout="Timed out" in result.get("reason", ""),
            is_error=result["status"] != "passed",
            metadata=CmdOutputMetadata(exit_code=result.get("exit_code", 124),
                                       working_dir="/workspace"))


class SandboxedTerminalTool(TerminalTool):
    name = "sandboxed_terminal"

    @classmethod
    def create(cls, conv_state, *, image: str = DEFAULT_IMAGE):
        root = Path(conv_state.workspace.working_dir)
        return [cls(action_type=TerminalAction, observation_type=TerminalObservation,
                    description=("Run one non-interactive shell command in a network-disabled "
                                 "container. The project is at /workspace. Each call starts "
                                 "in /workspace; shell state does not persist. No secrets or "
                                 "host files are available."),
                    executor=SandboxTerminalExecutor(root, DockerCommandRunner(image)))]


class GuardedFileEditorExecutor(FileEditorExecutor):
    def __init__(self, root: Path, focus: list[str]):
        self.root = root.resolve(strict=True)
        allowed = [str((self.root / name).resolve()) for name in focus]
        super().__init__(workspace_root=str(self.root), allowed_edits_files=allowed)

    def __call__(self, action: FileEditorAction, conversation=None) -> FileEditorObservation:
        target = Path(action.path)
        if action.path == "/workspace":
            target = self.root
        elif action.path.startswith("/workspace/"):
            target = self.root / action.path.removeprefix("/workspace/")
        if not target.is_absolute():
            target = self.root / target
        resolved = target.resolve()
        if not resolved.is_relative_to(self.root):
            return FileEditorObservation.from_text(
                text="File path is outside the isolated project copy",
                command=action.command, is_error=True)
        return super().__call__(action.model_copy(update={"path": str(resolved)}), conversation)


class SandboxedFileEditorTool(FileEditorTool):
    name = "sandboxed_file_editor"

    @classmethod
    def create(cls, conv_state, *, focus: list[str]):
        root = Path(conv_state.workspace.working_dir)
        return [cls(action_type=FileEditorAction, observation_type=FileEditorObservation,
                    description=("View files inside the project copy and edit only these "
                                 f"production files: {', '.join(focus)}. "
                                 "Use project-relative paths or paths under the workspace."),
                    executor=GuardedFileEditorExecutor(root, focus))]


register_tool(SandboxedTerminalTool.name, SandboxedTerminalTool)
register_tool(SandboxedFileEditorTool.name, SandboxedFileEditorTool)
