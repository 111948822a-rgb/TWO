"""V-FFMPEG-GUARD 回归测试(自包含):验证输入自检算法 + subprocess 超时机制。

注:compositor 模块加载时会触发 ensure_ffmpeg_exe()(可能下载 ffmpeg),
不适合在沙箱直接导入。此处内联复刻 _assert_inputs_nonempty 的等价逻辑
(与源文件字节级一致)进行行为验证,并单独验证 subprocess.run(timeout=) 能
真正 kill 卡死进程——这正是本次修复替换 ffmpeg.run() 的核心保障。
"""
import os
import subprocess
import sys
import tempfile


# —— 与 compositor._assert_inputs_nonempty 等价的逻辑 ——
def _assert_inputs_nonempty(project_id, labeled_paths):
    for label, path in labeled_paths:
        if not path or not os.path.exists(path):
            raise RuntimeError(f"[Compositor] ❌ 输入文件无效: {label} 文件不存在: {path}")
        size = os.path.getsize(path)
        if size == 0:
            raise RuntimeError(f"[Compositor] ❌ 输入文件无效: {label} 大小为 0: {path}")


def test_assert_inputs():
    with tempfile.TemporaryDirectory() as d:
        good = os.path.join(d, "good.mp4")
        with open(good, "wb") as f:
            f.write(b"x" * 2048)
        empty = os.path.join(d, "empty.mp4")
        open(empty, "wb").close()
        missing = os.path.join(d, "missing.mp4")

        _assert_inputs_nonempty("p", [("视频[s1]", good)])
        print("[1] OK 有效文件不抛异常")

        try:
            _assert_inputs_nonempty("p", [("视频[s2]", empty)])
            print("[2] FAIL 0字节未抛"); return False
        except RuntimeError as e:
            assert "大小为 0" in str(e)
            print(f"[2] OK 0字节抛异常: {e}")

        try:
            _assert_inputs_nonempty("p", [("音频[s3]", missing)])
            print("[3] FAIL 缺失未抛"); return False
        except RuntimeError as e:
            assert "不存在" in str(e)
            print(f"[3] OK 缺失文件抛异常: {e}")
    return True


def test_subprocess_timeout():
    py = sys.executable
    try:
        subprocess.run(
            [py, "-c", "import time; time.sleep(30)"],
            capture_output=True, timeout=1.5,
        )
        print("[4] FAIL 卡死进程未超时"); return False
    except subprocess.TimeoutExpired:
        print("[4] OK subprocess.run(timeout=1.5) 成功 kill 卡死进程并抛 TimeoutExpired")
        return True


if __name__ == "__main__":
    ok = test_assert_inputs() and test_subprocess_timeout()
    print("=== ALL PASS ===" if ok else "=== FAILED ===")
    sys.exit(0 if ok else 1)
