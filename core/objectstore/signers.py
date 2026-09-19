# -*- coding: utf-8 -*-
"""对象存储请求签名（零第三方依赖，仅用 hashlib/hmac/urllib.parse）。

两套签名并存的原因：

* :class:`SigV4` —— AWS Signature Version 4（S3 协议的事实标准）
  适用：MinIO / AWS S3 / Ceph RGW / **腾讯云 COS**（官方提供 S3 兼容 API，
  见 docs: 使用 AWS S3 SDK 访问 COS）/ 华为云 OBS（S3 兼容模式）/
  任何自称 S3 兼容的存储。
* :class:`OssV4` —— 阿里云 OSS 自有 V4 签名（``OSS4-HMAC-SHA256``）
  阿里云虽然也叫"V4"，但派生链与请求规范与 AWS 不同（官方《V1 签名升级为
  V4 签名指引》），必须单独实现。OSS 原生接口走它。

为什么自己算而不是引入 boto3 / oss2 / cos-sdk：
平台最终交付形态是**完全离线（air-gapped）环境**，运行时不允许 pip 安装
任何东西，且要能在信创环境较老的 Python 上跑。签名算法各约 60 行，
自研即可零依赖，也避免把几百 MB 的 SDK 打进离线交付包。

验证状态（如实标注）：
* SigV4 —— 已在**本机真实 MinIO 容器**上完成端到端验证（见
  ``docs/object_storage_e2e_report_20260919.md``）。
* OssV4 —— 按阿里云公开文档实现，**本机无 OSS 资源故未经真实环境验证**，
  使用时请先在对象存储页点"连接测试"确认。
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone

__all__ = ["SigV4", "OssV4", "EMPTY_SHA256", "payload_hash"]


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_UNSIGNED = "UNSIGNED-PAYLOAD"


def payload_hash(data: bytes | None) -> str:
    """计算请求体 SHA256；``None`` 表示无请求体（GET/HEAD/DELETE）。"""
    if data is None:
        return EMPTY_SHA256
    return hashlib.sha256(data).hexdigest()


def _hmac_hex(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _hmac_b64(key: bytes, msg: str) -> str:
    import base64
    return base64.b64encode(hmac.new(key, msg.encode("utf-8"),
                                     hashlib.sha1).digest()).decode()


def _iso_basic(dt: datetime) -> str:
    """AWS SigV4 时间戳：YYYYMMDDTHHMMSSZ。"""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def canonical_query(query: dict | None) -> str:
    """按 S3 要求序列化查询串：key/value 均 URI 编码后按 key 字典序排序。"""
    if not query:
        return ""
    parts = []
    for k, v in sorted(query.items()):
        if v is None:
            continue
        parts.append("%s=%s" % (urllib.parse.quote(str(k), safe="-_.~"),
                                urllib.parse.quote(str(v), safe="-_.~")))
    return "&".join(parts)


def canonical_path(path: str) -> str:
    """规范化 URI：路径 URI 编码（保留 `/`），空路径为 `/`。"""
    if not path:
        return "/"
    return urllib.parse.quote(path, safe="/%~")


class SigV4:
    """AWS Signature Version 4（S3 兼容存储的通用签名）。"""

    service = "s3"
    _algo = "AWS4-HMAC-SHA256"
    _terminator = "aws4_request"
    _date_header = "x-amz-date"
    _hash_header = "x-amz-content-sha256"

    def __init__(self, access_key: str, secret_key: str, region: str = "us-east-1",
                 service: str = "s3"):
        self.access_key = access_key or ""
        self.secret_key = secret_key or ""
        self.region = region or "us-east-1"
        self.service = service or "s3"

    # ---------------- 给 client 用的网络无关工具 ----------------
    @property
    def date_header(self) -> str:
        return self._date_header

    @property
    def hash_header(self) -> str:
        return self._hash_header

    def timestamps(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        return _iso_basic(now), now.strftime("%Y%m%d")

    def _signing_key(self, date_stamp: str) -> bytes:
        k = ("AWS4" + self.secret_key).encode("utf-8")
        k = _hmac_hex(k, date_stamp)
        k = _hmac_hex(k, self.region)
        k = _hmac_hex(k, self.service)
        return _hmac_hex(k, self._terminator)

    def canonical_request(self, method: str, path: str, query: dict | None,
                          headers: dict, signed_headers: list, body_hash: str) -> str:
        canon_headers = "".join(
            "%s:%s\n" % (h, str(headers.get(h, "")).strip())
            for h in sorted(signed_headers))
        return "\n".join([
            method.upper(),
            canonical_path(path),
            canonical_query(query),
            canon_headers,
            ";".join(sorted(signed_headers)),
            body_hash,
        ])

    def sign(self, method: str, path: str, query: dict | None, headers: dict,
             body_hash: str, host: str) -> dict:
        """返回补全了 Authorization / 日期 / 载荷哈希后的请求头。"""
        amzdate, date_stamp = self.timestamps()
        hdrs = {k.lower(): str(v) for k, v in (headers or {}).items()}
        hdrs["host"] = host
        hdrs[self._date_header] = amzdate
        hdrs[self._hash_header] = body_hash
        # 除 host/时间戳/载荷哈希外，凡是 x-amz-* 的头都参与签名，否则服务端以
        # "签名不匹配"拒绝（例如 x-amz-acl、storage 相关的私有头）。
        signed = ["host", self._date_header, self._hash_header]
        signed += sorted(h for h in hdrs if h.startswith("x-amz-")
                         and h not in signed)

        scope = "%s/%s/%s/%s" % (date_stamp, self.region, self.service,
                                 self._terminator)
        canon = self.canonical_request(method, path, query, hdrs, signed, body_hash)
        sts = "\n".join([
            self._algo, amzdate, scope,
            hashlib.sha256(canon.encode("utf-8")).hexdigest(),
        ])
        sig = hmac.new(self._signing_key(date_stamp), sts.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        hdrs["authorization"] = (
            "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (
                self._algo, self.access_key, scope, ";".join(sorted(signed)), sig))
        return hdrs


class OssV4(SigV4):
    """阿里云 OSS 自有 V4 签名（OSS4-HMAC-SHA256）。

    与 AWS SigV4 的差异：
    * 派生链密钥种子是 ``aliyun_v4`` + SecretKey，最后一步是 ``aliyun_v4_request``；
    * StringToSign 的算法标识是 ``OSS4-HMAC-SHA256``；
    * 请求头是 ``x-oss-date`` / ``x-oss-content-sha256``；
    * CanonicalRequest 末尾多一段 AdditionalHeaders（显式声明参与签名的头列表）。
    """

    _algo = "OSS4-HMAC-SHA256"
    _date_header = "x-oss-date"
    _hash_header = "x-oss-content-sha256"

    def __init__(self, access_key: str, secret_key: str, region: str):
        super().__init__(access_key, secret_key, region or "cn-hangzhou", "oss")

    def _signing_key(self, date_stamp: str) -> bytes:
        k = ("aliyun_v4" + self.secret_key).encode("utf-8")
        k = _hmac_hex(k, date_stamp)
        k = _hmac_hex(k, self.region)
        k = _hmac_hex(k, "oss")
        return _hmac_hex(k, "aliyun_v4_request")

    def timestamps(self) -> tuple[str, str]:
        # OSS V4 的时间戳格式与 SigV4 相同（yyyyMMddTHHmmssZ）
        return super().timestamps()

    def sign(self, method: str, path: str, query: dict | None, headers: dict,
             body_hash: str, host: str) -> dict:
        ts, date_stamp = self.timestamps()
        hdrs = {k.lower(): str(v) for k, v in (headers or {}).items()}
        hdrs["host"] = host
        hdrs[self._date_header] = ts
        hdrs[self._hash_header] = body_hash
        signed = ["host", self._date_header, self._hash_header]
        signed += sorted(h for h in hdrs if h.startswith("x-oss-")
                         and h not in signed)
        signed = sorted(signed)

        canon = "\n".join([
            method.upper(),
            canonical_path(path),
            canonical_query(query),
            "".join("%s:%s\n" % (h, str(hdrs.get(h, "")).strip()) for h in signed),
            ";".join(signed),
            body_hash,
        ])
        scope = "%s/%s/oss/aliyun_v4_request" % (date_stamp, self.region)
        sts = "\n".join([
            self._algo, ts, scope,
            hashlib.sha256(canon.encode("utf-8")).hexdigest(),
        ])
        sig = hmac.new(self._signing_key(date_stamp), sts.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        hdrs["authorization"] = (
            "%s Credential=%s/%s,AdditionalHeaders=%s,Signature=%s" % (
                self._algo, self.access_key, scope, ";".join(signed), sig))
        return hdrs


def oss_v1_authorization(method: str, resource: str, headers: dict,
                         access_key: str, secret_key: str, content_md5: str = "",
                         content_type: str = "", date_str: str = "") -> str:
    """阿里云 OSS V1 签名（HMAC-SHA1，仅用于兼容只接受 V1 的旧区域/网关）。

    平台默认使用 :class:`OssV4`；若用户环境被明确要求 V1，可在 extra_options
    里设 ``sign_version: "v1"``。
    """
    canon_headers = "".join(
        "%s:%s\n" % (k.lower(), str(v).strip())
        for k, v in sorted((headers or {}).items())
        if k.lower().startswith("x-oss-"))
    sign_str = "\n".join([method.upper(), content_md5, content_type, date_str,
                          canon_headers, resource])
    return "OSS %s:%s" % (access_key, _hmac_b64(secret_key.encode("utf-8"), sign_str))
