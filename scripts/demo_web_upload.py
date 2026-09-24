# -*- coding: utf-8 -*-
"""
demo_web_upload.py — 演示「网站拖拽上传 / 管理后台」如何接入 llm-wiki

本脚本**不落盘**（dry_run=True），只演示接口形状，可直接作为后端集成模板。

运行:
    python scripts/demo_web_upload.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_raw import ingest_file          # noqa: E402
from hooks import CallbackHook              # noqa: E402
from hooks import ensure_utf8_stdout        # noqa: E402

ensure_utf8_stdout()


def main() -> int:
    # ---- 模拟：前端把用户拖拽的文件读成 bytes 传进来 ----
    uploaded_bytes = "# 测试上传\n\n金银花 7 月 141.0 元/kg\n".encode("utf-8")
    uploaded_name = "upload_test.md"

    received = []

    def on_upload(result):
        """后端在这一步做三件事：回显、入队编译、推前端。"""
        received.append(result)
        if result.status == "collected":
            pass  # 真实场景：queue_compile(result.to_dict()) / ws.send(...)

    print("=" * 68)
    print("演示：网站上传 → ingest_file() → 钩子")
    print("=" * 68)

    # ⭐ 这就是集成点：一个函数，bytes + 文件名 + 钩子
    r = ingest_file(
        uploaded_bytes,
        filename=uploaded_name,
        hooks=[CallbackHook(on_upload)],
        dry_run=True,          # 演示用；实际传 False 即会落盘
    )

    print(f"\n  返回 status        : {r.status}")
    print(f"  自动推断主题       : {r.topic}  (置信度 {r.topic_confidence})")
    print(f"  dry_run 未落盘     : {r.raw_path is None}")
    print(f"  钩子收到通知       : {len(received)} 次")
    print(f"  级联候选           : {len(r.cascade_candidates)} 篇")

    print("\n" + "-" * 68)
    print("后端集成模板（照抄即可）")
    print("-" * 68)
    print('''
    from ingest_raw import ingest_file
    from hooks import CallbackHook

    @app.post("/api/wiki/upload")            # FastAPI / Flask 均可
    async def upload(file: UploadFile):
        data = await file.read()
        r = ingest_file(data, filename=file.filename,
                        hooks=[CallbackHook(on_upload)])
        if r.status == "collected":
            queue_compile(r.to_dict())       # 投递编译任务（见 INGEST_AGENT.md）
            return {"ok": True, "raw": r.raw_path,
                    "cascade": r.cascade_candidates}
        if r.status == "skipped":
            return {"ok": True, "duplicate": True, "at": r.raw_path}
        return {"ok": False, "error": r.error}, 400
''')
    print("-" * 68)
    print("要点：入口变了，规范不变。拖拽、inbox、CLI 走的是同一个 ingest_file()。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
