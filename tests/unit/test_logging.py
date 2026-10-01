import json
import logging

import structlog

from reviewer.logging import configure_logging


def test_structlog_and_stdlib_logs_are_json(capsys):
    configure_logging("INFO")

    structlog.get_logger("app").info("review_started", run_id="abc")
    logging.getLogger("uvicorn.error").warning("from stdlib")

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["event"] == "review_started"
    assert lines[0]["run_id"] == "abc"
    assert lines[0]["level"] == "info"
    assert "timestamp" in lines[0]
    assert lines[1]["event"] == "from stdlib"
    assert lines[1]["level"] == "warning"
