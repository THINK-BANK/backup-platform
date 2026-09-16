# -*- coding: utf-8 -*-
"""VM Provider 共用的 SSH 通道工具（复用平台既有的连接池与断点续传）。

被管侧零安装：控制面执行宿主机自带命令（virsh / qemu-img / vim-cmd / PowerShell），
数据面走 SSH/SFTP 流式拉取，不在虚拟化平台上留下任何程序。
"""
import os
import shlex


def ssh_host_from_hv(hv: dict) -> dict:
    """从 vm_hypervisors 记录构造 SSH 主机 dict（给 remote_dump._connect 用）。

    优先取 extra_config.ssh（显式指定跳板/管理地址），否则用 endpoint 的主机名。
    password 已由调用方解密。
    """
    import json
    extra = hv.get("extra_config")
    if isinstance(extra, str) and extra.strip():
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    extra = extra or {}
    ssh = extra.get("ssh") or {}

    endpoint = (hv.get("endpoint") or "").strip()
    host = (ssh.get("host") or "").strip()
    if not host:
        # endpoint 形如 https://pve01:8006 / root@kvm01 / 10.0.0.5
        e = endpoint
        for prefix in ("https://", "http://", "ssh://"):
            if e.startswith(prefix):
                e = e[len(prefix):]
        host = e.split("/")[0].split(":")[0]
        if "@" in host:
            host = host.rsplit("@", 1)[-1]
    user = (ssh.get("username") or hv.get("username") or "root").strip()
    port = int(ssh.get("port") or 22)
    pw = ssh.get("password") or hv.get("password") or ""
    return {
        "name": hv.get("name") or host,
        "host_key": "%s@%s:%d" % (user, host, port),
        "hostname": host,
        "port": port,
        "username": user,
        "password": pw,
        "auth_type": "password" if pw else "key",
        "has_password": bool(pw),
        "os_type": ssh.get("os_type") or ("windows" if port == 22 and
                                          str(ssh.get("windows")).lower() == "true"
                                          else "linux"),
        "key_path": ssh.get("key_path") or "",
    }


def connect(hv: dict):
    """建立 SSH 连接（复用平台连接池，带活性探测）。"""
    from core.engines.file import _get_ssh_client
    h = ssh_host_from_hv(hv)
    return _get_ssh_client(h["host_key"], password=h.get("password") or None)


def exec_capture(client, cmd: str, timeout: int = 600, wrap: bool = True) -> dict:
    """执行一条 shell（默认 bash -lc 包裹），返回 {"rc","out","err"}。

    wrap=False 用于 Windows OpenSSH（PowerShell 命令不能再套 bash -lc）。
    """
    from core.engines.file import _ssh_exec_pipe
    from core.remote_dump import _wrap_login
    out, err, rc = _ssh_exec_pipe(client, _wrap_login(cmd) if wrap else cmd,
                                  timeout=timeout)
    if isinstance(out, bytes):
        out = out.decode("utf-8", "ignore")
    return {"rc": rc, "out": out or "", "err": err or ""}


def stream_to_file(client, cmd: str, out_path: str, *, timeout: int = 0,
                   idle_timeout: int = 0, rate_kbps: int = 0,
                   label: str = "") -> dict:
    """把远端命令的 stdout 流式落盘（tar 流 / 镜像流，内存恒定）。"""
    from core.engines.file import _ssh_exec_pipe_to_file
    from core.remote_dump import _wrap_login
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    return _ssh_exec_pipe_to_file(
        client, _wrap_login(cmd), out_path, timeout=timeout,
        idle_timeout=idle_timeout, rate_kbps=rate_kbps, label=label,
        resume=True)


def pull_file(client, remote_path: str, local_path: str, task: dict = None,
              label: str = "", stable_secs: float = 3) -> dict:
    """断点续传拉回远端文件（大镜像友好）。"""
    from core import remote_dump
    host_key = ""
    try:
        host_key = client.get_transport().getpeername()[0] if client.get_transport() else ""
    except Exception:
        host_key = ""
    return remote_dump.sftp_pull_resumable(
        client, remote_path, local_path,
        task=task or {}, key=os.path.basename(remote_path)[:60],
        db_type="vm_fixed", host_key=str(host_key),
        has_rc=False, stable_secs=stable_secs,
        label=label or os.path.basename(remote_path), min_size=1)


def push_file(client, local_path: str, remote_path: str) -> None:
    """上传文件到远端（恢复/克隆时把产物推回宿主机）。"""
    exec_capture(client, "mkdir -p %s" % shlex.quote(os.path.dirname(remote_path)),
                 timeout=60)
    sftp = client.open_sftp()
    try:
        sftp.put(local_path, remote_path)
    finally:
        try:
            sftp.close()
        except Exception:
            pass


def remote_size(client, remote_path: str) -> int:
    r = exec_capture(client, "stat -c %%s %s 2>/dev/null || echo 0"
                     % shlex.quote(remote_path), timeout=60)
    try:
        return int((r["out"] or "0").strip().splitlines()[-1])
    except Exception:
        return 0
