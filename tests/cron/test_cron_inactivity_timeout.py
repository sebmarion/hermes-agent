"""Tests for the cron inactivity watchdog loop (runs on its own daemon thread).

Tests cover:
- Active agent runs indefinitely (no inactivity timeout)
- Idle agent triggers inactivity timeout with diagnostic info
- Unlimited timeout (HERMES_CRON_TIMEOUT=0)
- Backward compat: HERMES_CRON_TIMEOUT env var still works
- Error message includes activity summary
"""

import concurrent.futures
import os
import sys
import time
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

