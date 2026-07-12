"""Dedicated subprocess entrypoint for one strict BestPlan slice."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        runtime = dict(payload["runtime"])
        workspace = Path(payload["workspace"]).resolve()
        runtime_home = Path(payload["runtime_home"]).resolve()
        os.environ["HERMES_HOME"] = str(runtime_home)
        os.environ["TERMINAL_CWD"] = str(workspace)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        from hermes_constants import set_hermes_home_override
        from run_agent import AIAgent

        set_hermes_home_override(runtime_home)
        toolsets = list(runtime["bestplan_toolsets"])
        started = time.monotonic()
        agent = AIAgent(
            base_url=runtime.get("base_url"),
            api_key=runtime.get("api_key"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            command=runtime.get("command"),
            args=runtime.get("args"),
            acp_command=runtime.get("acp_command"),
            acp_args=runtime.get("acp_args"),
            model=str(runtime.get("model") or ""),
            max_iterations=int(payload.get("max_iterations") or 50),
            max_tokens=runtime.get("max_output_tokens"),
            request_overrides=runtime.get("request_overrides"),
            enabled_toolsets=toolsets,
            quiet_mode=True,
            save_trajectories=False,
            platform="bestplan-worker",
            skip_context_files=True,
            skip_memory=True,
            checkpoints_enabled=False,
        )
        agent.terminal_cwd = str(workspace)
        try:
            result = agent.run_conversation(
                user_message=str(payload.get("goal") or ""),
                system_message=str(payload.get("system_prompt") or ""),
                conversation_history=[],
                task_id=str(payload.get("task_id") or "bestplan"),
            )
            output = {
                "status": "completed" if result.get("completed", True) else "error",
                "summary": str(result.get("final_response") or ""),
                "error": result.get("error"),
                "api_calls": int(result.get("api_calls") or 0),
                "duration_seconds": round(time.monotonic() - started, 2),
                "model": str(runtime.get("model") or ""),
            }
        finally:
            agent.close()
        sys.stdout.write("HERMES_BESTPLAN_RESULT=" + json.dumps(output, ensure_ascii=False))
        sys.stdout.flush()
        return 0
    except BaseException as exc:  # process protocol must always produce a result
        sys.stdout.write("HERMES_BESTPLAN_RESULT=" + json.dumps({
            "status": "error",
            "summary": "",
            "error": f"{type(exc).__name__}: {exc}",
            "api_calls": 0,
        }))
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
