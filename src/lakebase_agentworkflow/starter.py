from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid

from lakebase_agentworkflow.helpers.env import load_env_file
from lakebase_agentworkflow.helpers.models import AgentRequest
from lakebase_agentworkflow.helpers.temporal_client import connect_temporal
from lakebase_agentworkflow.workflows import LakebaseAgentWorkflow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start a Lakebase AgentWorkflow run.")
    parser.add_argument("request", help="User request to send to the agent.")
    parser.add_argument("--user-id", default="local-user@example.com")
    parser.add_argument("--applicant-id", required=True)
    parser.add_argument("--loan-amount", required=True, type=float)
    parser.add_argument("--loan-purpose", required=True)
    parser.add_argument("--max-turns", default=8, type=int)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--model", default=os.getenv("AGENT_MODEL", "gpt-6-luna"))
    return parser.parse_args()


async def main() -> None:
    load_env_file()
    args = parse_args()
    run_id = args.run_id or str(uuid.uuid4())
    client = await connect_temporal()

    result = await client.execute_workflow(
        LakebaseAgentWorkflow.run,
        AgentRequest(
            run_id=run_id,
            user_id=args.user_id,
            user_request=args.request,
            applicant_id=args.applicant_id,
            loan_amount=args.loan_amount,
            loan_purpose=args.loan_purpose,
            model=args.model,
            max_turns=args.max_turns,
        ),
        id=f"lakebase-agent-{run_id}",
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "lakebase-agent-task-queue"),
    )
    print(json.dumps(result.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
