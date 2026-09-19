"""不可变发布版本登记与医院租户。

发布版本（release）的身份不是模型名称，而是代码提交、权重摘要、运行环境、
运行阈值、适用病种与禁用场景共同决定的指纹。任何一项变化都会得到新的
release_id，旧版本保持不动，供回滚与审计引用。

租户令牌只以 SHA-256 摘要保存，鉴权使用恒定时间比较。
"""

import hashlib
import hmac
import json
import re

from . import catalog
from .errors import ConflictError, NotFoundError, ValidationError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REV_RE = re.compile(r"^[0-9a-f]{7,40}$")
_THRESHOLD_FLOOR = 0.0
_THRESHOLD_CEIL = 1.0


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_contraindications(items):
    """禁用场景形如 condition:<code> / protocol:<协议> / scenario:<说明>。"""
    normalized = []
    for item in items:
        if not isinstance(item, str) or ":" not in item:
            raise ValidationError(f"禁用场景格式无效: {item!r}，应为 condition:/protocol:/scenario: 前缀")
        kind, ref = item.split(":", 1)
        kind = kind.strip()
        ref = ref.strip()
        if not ref:
            raise ValidationError(f"禁用场景缺少引用对象: {item!r}")
        if kind == "condition":
            if ref not in catalog.CONDITIONS:
                raise ValidationError(f"禁用场景引用了未知病种: {ref}")
        elif kind == "protocol":
            if ref not in catalog.PROTOCOLS:
                raise ValidationError(f"禁用场景引用了未知协议: {ref}")
        elif kind != "scenario":
            raise ValidationError(f"禁用场景前缀不支持: {kind}")
        normalized.append(f"{kind}:{ref}")
    if len(normalized) != len(set(normalized)):
        raise ValidationError("禁用场景存在重复条目")
    return normalized


def build_fingerprint(spec):
    """由代码/权重/运行环境/阈值/适用范围计算发布指纹。"""
    material = {
        "code": spec["code"],
        "weights": spec["weights"],
        "runtime": spec["runtime"],
        "threshold": round(float(spec["threshold"]), 6),
        "conditions": sorted(spec["conditions"]),
        "contraindications": sorted(spec["contraindications"]),
    }
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def normalize_release_payload(payload):
    """校验登记输入并返回规范化后的发布规格。"""
    if not isinstance(payload, dict):
        raise ValidationError("发布登记内容必须是对象")

    model_name = payload.get("model_name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValidationError("model_name 不能为空")

    code = payload.get("code")
    if not isinstance(code, dict):
        raise ValidationError("code 必须为对象")
    repo = code.get("repo")
    commit = code.get("commit")
    if not isinstance(repo, str) or not repo.strip():
        raise ValidationError("code.repo 不能为空")
    if not isinstance(commit, str) or not _GIT_REV_RE.match(commit):
        raise ValidationError("code.commit 必须是 7-40 位十六进制 git 修订号")
    code_digest = code.get("sha256")
    if code_digest is not None and not _SHA256_RE.match(str(code_digest)):
        raise ValidationError("code.sha256 必须是 64 位十六进制摘要")

    weights = payload.get("weights")
    if not isinstance(weights, dict):
        raise ValidationError("weights 必须为对象")
    weights_uri = weights.get("uri")
    weights_sha = weights.get("sha256")
    if not isinstance(weights_uri, str) or not weights_uri.strip():
        raise ValidationError("weights.uri 不能为空")
    if not isinstance(weights_sha, str) or not _SHA256_RE.match(weights_sha):
        raise ValidationError("weights.sha256 必须是 64 位十六进制摘要（权重内容不可混淆）")

    runtime = payload.get("runtime")
    if not isinstance(runtime, dict) or not runtime:
        raise ValidationError("runtime 必须是非空对象（镜像/解释器/CUDA/依赖锁摘要等）")
    for key in ("image", "image_sha256"):
        if key in runtime and runtime[key] is not None and not str(runtime[key]).strip():
            raise ValidationError(f"runtime.{key} 不能为空字符串")

    try:
        threshold = float(payload.get("threshold"))
    except (TypeError, ValueError):
        raise ValidationError("threshold 必须是 0~1 之间的数值")
    if not (_THRESHOLD_FLOOR < threshold < _THRESHOLD_CEIL):
        raise ValidationError("threshold 必须严格位于 (0, 1) 内")

    conditions = payload.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise ValidationError("conditions 至少包含一个目录病种")
    if len(conditions) != len(set(conditions)):
        raise ValidationError("conditions 存在重复病种")
    organs = set()
    for code_id in conditions:
        entry = catalog.get(code_id)
        if entry is None:
            raise ValidationError(f"conditions 包含目录外病种: {code_id}")
        organs.add(entry["organ"])

    contraindications = _validate_contraindications(payload.get("contraindications", []))

    return {
        "model_name": model_name.strip(),
        "code": {"repo": repo.strip(), "commit": commit, **({"sha256": code_digest} if code_digest else {})},
        "weights": {"uri": weights_uri.strip(), "sha256": weights_sha},
        "runtime": runtime,
        "threshold": threshold,
        "conditions": list(conditions),
        "contraindications": contraindications,
        "organs": sorted(organs),
    }


class Registry:
    """发布版本与医院租户的登记/查询。"""

    def __init__(self, store, clock):
        self.store = store
        self.clock = clock

    # ── 发布版本 ───────────────────────────────────────────
    def register_release(self, payload, registered_by="system"):
        spec = normalize_release_payload(payload)
        fingerprint = build_fingerprint(spec)

        for existing in self.store.data["releases"].values():
            if existing["fingerprint"] == fingerprint:
                raise ConflictError(
                    f"等价发布版本已登记为 {existing['id']}（代码/权重/环境/阈值/范围完全一致）"
                )

        self.store.data["release_seq"] += 1
        release_id = f"rel-{self.store.data['release_seq']:04d}"
        record = {
            "id": release_id,
            "model_name": spec["model_name"],
            "code": spec["code"],
            "weights": spec["weights"],
            "runtime": spec["runtime"],
            "threshold": spec["threshold"],
            "conditions": spec["conditions"],
            "contraindications": spec["contraindications"],
            "organs": spec["organs"],
            "fingerprint": fingerprint,
            "registered_by": registered_by,
            "created_at": self.clock(),
        }
        self.store.data["releases"][release_id] = record
        self.store.save()
        return record

    def get_release(self, release_id):
        record = self.store.data["releases"].get(release_id)
        if record is None:
            raise NotFoundError(f"发布版本不存在: {release_id}")
        return record

    def list_releases(self, model_name=None):
        records = list(self.store.data["releases"].values())
        if model_name:
            records = [r for r in records if r["model_name"] == model_name]
        return records

    # ── 医院租户 ───────────────────────────────────────────
    def register_tenant(self, hospital_id, name, token):
        if not hospital_id or not isinstance(hospital_id, str):
            raise ValidationError("hospital_id 不能为空")
        if hospital_id in self.store.data["tenants"]:
            raise ConflictError(f"机构已登记: {hospital_id}")
        if not isinstance(token, str) or len(token) < 16:
            raise ValidationError("令牌长度至少 16 位")
        record = {
            "hospital_id": hospital_id,
            "name": name,
            "token_hash": _hash_token(token),
            "created_at": self.clock(),
        }
        self.store.data["tenants"][hospital_id] = record
        self.store.save()
        return record

    def authenticate(self, token):
        """由令牌解析 hospital_id，失败抛 AuthError。"""
        from .errors import AuthError

        if not isinstance(token, str) or not token:
            raise AuthError("缺少机构令牌")
        presented = _hash_token(token)
        for record in self.store.data["tenants"].values():
            if hmac.compare_digest(record["token_hash"], presented):
                return record["hospital_id"]
        raise AuthError("机构令牌无效")

    def get_tenant(self, hospital_id):
        record = self.store.data["tenants"].get(hospital_id)
        if record is None:
            raise NotFoundError(f"机构未登记: {hospital_id}")
        return record
