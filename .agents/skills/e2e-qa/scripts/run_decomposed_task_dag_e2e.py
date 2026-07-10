"""Live E2E smoke for M24 sequential decomposed task execution."""

import asyncio
import importlib
import os

os.environ.setdefault(
    "CODE_AGENT_QA_TASK_TEXT",
    (
        "Implement a multi-file change: inspect the dummy repository, create qa-hello.txt "
        "containing the text 'Hello QA', update README.md to reference qa-hello.txt, run the "
        "verification command, and commit the changes."
    ),
)
os.environ.setdefault("CODE_AGENT_QA_EXPECT_DECOMPOSED_DAG", "1")
os.environ.setdefault("CODE_AGENT_WORKER_OVERRIDE", "")

run_e2e_qa = importlib.import_module("run_e2e_qa")


if __name__ == "__main__":
    asyncio.run(run_e2e_qa.main())
