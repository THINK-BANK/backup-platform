# -*- coding: utf-8 -*-
"""AIDBM 无 Agent（Agentless）能力体系。

- ``channels``：通道声明与侵入等级（A0–A3 / 禁止项 X）
- ``audit``：目标端免装取证与判定

设计文档：``docs/agentless_architecture_20260919.md``
"""
from __future__ import annotations

from . import audit, channels  # noqa: F401

__all__ = ["audit", "channels"]
