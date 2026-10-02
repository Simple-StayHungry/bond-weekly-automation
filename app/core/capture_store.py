"""Persist exchange responses beside each run, including failed attempts."""

import copy
import hashlib
import json
import os


def persist_evidence(evidence, directory):
    if not isinstance(evidence, dict):
        raise RuntimeError("抓取结果没有完整性凭据，不能保存为成功快照")
    os.makedirs(directory, exist_ok=True)
    saved = copy.deepcopy(evidence)
    for number, response in enumerate(saved.get("responses", []), 1):
        body = response.get("response_text")
        if not isinstance(body, str):
            raise RuntimeError(f"第 {number} 个官方响应没有原文")
        body_bytes = body.encode("utf-8")
        digest = hashlib.sha256(body_bytes).hexdigest()
        if response.get("response_sha256") != digest:
            raise RuntimeError(f"第 {number} 个官方响应校验值不一致")
        name = f"response_{number:04d}.txt"
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(body_bytes)
        response["response_file"] = name
    manifest_path = os.path.join(directory, "capture.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(saved, handle, ensure_ascii=False, indent=2)
    return manifest_path


def persist_frame(frame, source, directory):
    evidence = frame.attrs.get("capture_evidence")
    if not evidence or evidence.get("source") != source:
        raise RuntimeError(f"{source} 抓取缺少对应的完整性凭据")
    return persist_evidence(evidence, directory)


def persist_failure(error, directory):
    evidence = getattr(error, "capture_evidence", None)
    if evidence:
        persist_evidence(evidence, directory)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "failure.json"), "w", encoding="utf-8") as handle:
        json.dump({"status": "failed", "error_type": type(error).__name__,
                   "error": str(error)}, handle, ensure_ascii=False, indent=2)
