"""V-FFMPEG-ANTIHANG 回归测试:验证统一执行器的防卡死封装与极简保底拼接。

通过注入假的 ffmpeg_utils 避免模块加载时联网下载 ffmpeg;
用 monkeypatch 捕获 subprocess.run 的实际入参,断言:
  1. 缺 -y 时自动注入 -y(防覆盖确认卡死)
  2. 始终传入 stdin=subprocess.DEVNULL(防 stdin 交互卡死)
  3. 始终传入 timeout(防无界等待)
  4. 极简保底 _compose_ultra_minimal 生成 concat demuxer -c copy 命令
  5. _assert_inputs_nonempty 对缺失/0字节文件抛异常
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import types


def _install_fake_ffmpeg_utils() -> None:
    """注入假的 ffmpeg_utils,避免模块加载触发联网下载。"""
    app_utils = types.ModuleType("app.utils")
    sys.modules.setdefault("app.utils", app_utils)
    fake = types.ModuleType("app.utils.ffmpeg_utils")
    fake.ensure_ffmpeg_exe = lambda: "ffmpeg"
    sys.modules["app.utils.ffmpeg_utils"] = fake


def _install_fake_ffmpeg_module() -> None:
    """venv_verify 未装 ffmpeg-python,注入桩模块满足导入(copy 路径用不到图构建)。"""
    fake = types.ModuleType("ffmpeg")

    class _Node:
        def __getattr__(self, name):
            def _meth(*a, **k):
                return _Node()
            return _meth

    def _node(*a, **k):
        return _Node()

    fake.input = _node
    fake.output = _node
    fake.filter = _node
    fake.concat = _node
    fake.compile = lambda *a, **k: ["ffmpeg", "-y", "stub"]
    sys.modules["ffmpeg"] = fake


_install_fake_ffmpeg_utils()
_install_fake_ffmpeg_module()

import importlib  # noqa: E402

import app.services.compositor as C  # noqa: E402


# ---------------------------------------------------------------------------
# 捕获 subprocess.run 调用
# ---------------------------------------------------------------------------

_CAPTURED = []


class _FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


def _fake_run(args, *a, **kw):
    _CAPTURED.append((args, kw))
    # 模拟 ffmpeg 真实产出:创建输出文件(最后一个非选项参数),使 _verify_product 通过
    if args:
        out = args[-1]
        if isinstance(out, str) and not out.startswith("-"):
            try:
                with open(out, "wb") as f:
                    f.write(b"x" * 2048)
            except Exception:
                pass
    return _FakeProc(returncode=0, stderr=b"")


def _install_fake_run():
    _CAPTURED.clear()
    C.subprocess.run = _fake_run


# ---------------------------------------------------------------------------
# 测试 1:-y 注入 + stdin=DEVNULL + timeout
# ---------------------------------------------------------------------------

def test_run_ffmpeg_cmd_injects_y_and_devnull():
    _install_fake_run()
    # 命令未含 -y,应被自动注入
    C._run_ffmpeg_cmd(["ffmpeg", "-i", "in.mp4", "out.mp4"], "TEST", timeout=300)
    assert _CAPTURED, "subprocess.run 未被调用"
    args, kw = _CAPTURED[0]
    assert "-y" in args, f"命令必须含 -y, 实际: {args}"
    assert kw.get("stdin") is subprocess.DEVNULL, "必须 stdin=DEVNULL"
    assert kw.get("timeout") == 300, "必须传入 timeout=300"
    print(f"✅ [1] -y 注入 + stdin=DEVNULL + timeout=300 -> {args}")


def test_run_ffmpeg_cmd_keeps_existing_y():
    _install_fake_run()
    # 命令已含 -y,不应再插入第二个(避免歧义)
    C._run_ffmpeg_cmd(
        ["ffmpeg", "-y", "-f", "concat", "-i", "l.txt", "-c", "copy", "o.mp4"],
        "VO", timeout=120,
    )
    args, kw = _CAPTURED[0]
    assert args.count("-y") == 1, f"-y 不应重复, 实际: {args}"
    assert kw.get("stdin") is subprocess.DEVNULL
    assert kw.get("timeout") == 120
    print(f"✅ [2] 已有 -y 不重复注入, 仍含 stdin=DEVNULL -> {args}")


def test_run_ffmpeg_cmd_raises_on_nonzero():
    _install_fake_run()
    # 让 subprocess.run 返回非零退出码
    def _fake_fail(args, *a, **kw):
        _CAPTURED.append((args, kw))
        return _FakeProc(returncode=1, stderr=b"ERROR: bad filter")
    C.subprocess.run = _fake_fail
    try:
        C._run_ffmpeg_cmd(["ffmpeg", "-i", "in.mp4", "out.mp4"], "FAIL", timeout=300)
        assert False, "应抛出 RuntimeError"
    except RuntimeError as e:
        assert "bad filter" in str(e), f"异常应携带 stderr, 实际: {e}"
        print(f"✅ [3] 非零退出码抛 RuntimeError 并携带 stderr -> {e}")


# ---------------------------------------------------------------------------
# 测试 4:极简保底拼接命令(concat demuxer -c copy)
# ---------------------------------------------------------------------------

def test_ultra_minimal_copy_command():
    _install_fake_run()
    with tempfile.TemporaryDirectory() as d:
        inputs = [os.path.join(d, f"v{i}.mp4") for i in range(3)]
        # 创建非空占位文件,模拟真实视频
        for p in inputs:
            with open(p, "wb") as f:
                f.write(b"x" * 2048)
        out = os.path.join(d, "final.mp4")
        C._compose_ultra_minimal(inputs, out, "ffmpeg", aspect_ratio="9:16")
        assert _CAPTURED, "subprocess.run 未被调用"
        args, _ = _CAPTURED[0]
        # 多分镜应走 concat demuxer -c copy
        assert "-f" in args and "concat" in args, f"应走 concat demuxer, 实际: {args}"
        assert "-c" in args and "copy" in args, f"应 -c copy, 实际: {args}"
        assert "-y" in args
        print(f"✅ [4] 极简保底生成 concat -c copy 命令 -> {args}")


# ---------------------------------------------------------------------------
# 测试 5:输入生死校验
# ---------------------------------------------------------------------------

def test_assert_inputs_nonempty():
    with tempfile.TemporaryDirectory() as d:
        good = os.path.join(d, "good.mp4")
        with open(good, "wb") as f:
            f.write(b"x" * 2048)
        zero = os.path.join(d, "zero.mp4")
        with open(zero, "wb") as f:
            f.write(b"")
        missing = os.path.join(d, "missing.mp4")

        # 正常文件放行
        C._assert_inputs_nonempty("p1", [("视频[1]", good)])

        # 0 字节文件抛异常
        try:
            C._assert_inputs_nonempty("p1", [("视频[2]", zero)])
            assert False, "0 字节应抛异常"
        except RuntimeError as e:
            assert "大小为 0" in str(e), f"应报大小为0, 实际: {e}"

        # 缺失文件抛异常
        try:
            C._assert_inputs_nonempty("p1", [("视频[3]", missing)])
            assert False, "缺失应抛异常"
        except RuntimeError as e:
            assert "不存在" in str(e), f"应报不存在, 实际: {e}"
        print("✅ [5] 输入生死校验: 有效放行 / 0字节抛'大小为0' / 缺失抛'不存在'")


if __name__ == "__main__":
    test_run_ffmpeg_cmd_injects_y_and_devnull()
    test_run_ffmpeg_cmd_keeps_existing_y()
    test_run_ffmpeg_cmd_raises_on_nonzero()
    test_ultra_minimal_copy_command()
    test_assert_inputs_nonempty()
    print("\n🎉 全部 5 项回归测试通过")
