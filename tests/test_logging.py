import json
import logging

import structlog

from src.config import Settings
from src.main import _setup_logging


def test_entry_rejections_are_persisted_as_json(tmp_path):
    previous = structlog.get_config().copy()
    logger = logging.getLogger("arbitrage")
    original_handlers = logger.handlers[:]
    logger.handlers = []
    original_level, original_propagate = logger.level, logger.propagate
    try:
        path = tmp_path / "nested" / "arb.log"
        cfg = Settings(_env_file=None, log_file=path)
        _setup_logging(cfg)
        # Reconfiguration must not duplicate output or retain file handles.
        _setup_logging(cfg)
        structlog.get_logger("test").info(
            "executor.skip_risk_check", symbol="BTC", reason="costs"
        )
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["event"] == "executor.skip_risk_check"
        assert event["reason"] == "costs"
        assert event["symbol"] == "BTC"
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        structlog.configure(**previous)
