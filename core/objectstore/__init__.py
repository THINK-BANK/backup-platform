# -*- coding: utf-8 -*-
"""对象存储访问层（AIDBM 自研，零第三方依赖）。

层级关系::

    providers.py  各家厂商预设（端点/区域/寻址风格/签名方言）
        └── client.py     S3 协议操作（列举/下载/上传/删除/桶管理）
                └── signers.py  请求签名（SigV4 / OSS V4）

设计遵循平台的"客户端零安装 + 完全离线"约束：只用标准库 http.client +
xml.etree，不引入 boto3 / oss2 / minio-python，也不需要目标端安装任何东西
——对象存储备份是**纯服务端到服务端**的协议会话，被保护对象（桶）侧
零部署、零改造。
"""
from __future__ import annotations

__all__ = ["client", "providers", "signers"]
